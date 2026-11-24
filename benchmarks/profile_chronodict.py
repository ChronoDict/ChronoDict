"""Profile ChronoDict CPU, memory and historical lookup latency in isolated processes."""
import argparse
import cProfile
import gc
import io
import json
import platform
import pstats
import random
import resource
import statistics
import subprocess
import sys
import tracemalloc
from time import perf_counter_ns

from chronodict import ChronoDict


def rss_mib():
    """Return peak process RSS on macOS or Linux."""
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (1048576 if sys.platform == 'darwin' else 1024)


def worker(args):
    n = args.size
    trace = args.mode == 'memory'
    if trace:
        tracemalloc.start()
    d = ChronoDict(indexes=['score'])
    profiles = {}
    def run(name, fn):
        if args.mode == 'cpu':
            profiler = cProfile.Profile()
            result = profiler.runcall(fn)
            buf = io.StringIO()
            pstats.Stats(profiler, stream=buf).strip_dirs().sort_stats('cumulative').print_stats(args.top)
            profiles[name] = buf.getvalue()
            return result
        return fn()
    def insert():
        for k in range(n):
            d[k] = {'score': k % 1000}
    def update():
        for k in range(n):
            d.patch(k, score=(k + 1) % 1000)
    def delete():
        for k in range(0, n, 10):
            del d[k]
    run('insert', insert)
    run('update', update)
    run('delete', delete)
    rng = random.Random(4102)
    keys = [rng.randrange(n) for _ in range(args.queries)]
    scores = [rng.randrange(1000) for _ in keys]
    revisions = [rng.randrange(n, 2*n+1) for _ in keys]
    expected = [(k + int(k < r-n)) % 1000 for k, r in zip(keys, revisions)]
    def lookup():
        return [d.get(k, version=r)['score'] for k, r in zip(keys, revisions)]
    def search():
        return [d.find(score=s, version=r) for s, r in zip(scores, revisions)]
    def measure():
        before = resource.getrusage(resource.RUSAGE_SELF)
        collections = sum(x['collections'] for x in gc.get_stats())
        start = perf_counter_ns()
        result = lookup()
        elapsed = (perf_counter_ns()-start)/1e6
        after = resource.getrusage(resource.RUSAGE_SELF)
        if result != expected:
            raise AssertionError('Historical lookup mismatch')
        return dict(ms=elapsed, minor_faults=after.ru_minflt-before.ru_minflt,
                    major_faults=after.ru_majflt-before.ru_majflt,
                    gc_collections=sum(x['collections'] for x in gc.get_stats())-collections)
    report = {'size': n, 'revision': d.revision, 'mode': args.mode}
    if args.mode == 'latency':
        report['first'] = measure()
        report['warm'] = [measure() for _ in range(args.rounds)]
        enabled = gc.isenabled()
        try:
            gc.disable()
            report['gc_disabled'] = [measure() for _ in range(args.rounds)]
        finally:
            if enabled:
                gc.enable()
    else:
        if run('historical_lookup', lookup) != expected:
            raise AssertionError('Historical lookup mismatch')
    results = run('historical_search', search)
    for s, r, actual in zip(scores, revisions, results):
        boundary = r-n
        wanted = {k: {'score': s} for k in range((s-1)%1000, boundary, 1000)}
        wanted.update({k: {'score': s} for k in range(s, n, 1000) if k >= boundary})
        if actual != wanted:
            raise AssertionError('Historical search mismatch')
    del results
    if d.revision != 2*n+len(range(0,n,10)) or len(d) != n-len(range(0,n,10)):
        raise AssertionError('Revision or size mismatch')
    report['peak_rss_mib'] = rss_mib()
    if trace:
        live, peak = tracemalloc.get_traced_memory()
        report['traced_live_mib'] = live/1048576
        report['traced_peak_mib'] = peak/1048576
        report['allocation_hotspots'] = [str(x) for x in tracemalloc.take_snapshot().statistics('lineno')[:args.top]]
        tracemalloc.stop()
    if profiles:
        report['profiles'] = profiles
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--size', type=int, default=10000)
    parser.add_argument('--mode', choices=['latency','cpu','memory'], default='latency')
    parser.add_argument('--queries', type=int, default=100)
    parser.add_argument('--rounds', type=int, default=5)
    parser.add_argument('--repeats', type=int, default=3)
    parser.add_argument('--top', type=int, default=12)
    parser.add_argument('--worker', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    if min(args.size,args.queries,args.rounds,args.repeats,args.top) <= 0:
        parser.error('All numeric arguments must be positive')
    if args.worker:
        print(json.dumps(worker(args)))
        return
    print(f'Python {platform.python_version()} | {platform.system()} | {args.mode} | {args.size:,} keys', flush=True)
    cmd = [sys.executable, __file__, '--worker', '--size', str(args.size), '--mode', args.mode,
           '--queries', str(args.queries), '--rounds', str(args.rounds), '--top', str(args.top)]
    runs = []
    for i in range(args.repeats):
        report = json.loads(subprocess.check_output(cmd, text=True))
        runs.append(report)
        print(f'Run {i+1}: all oracle checks passed | peak RSS {report["peak_rss_mib"]:.2f} MiB', flush=True)
        if args.mode == 'latency':
            print(json.dumps({k: report[k] for k in ('first','warm','gc_disabled')}, indent=2))
        elif args.mode == 'cpu':
            for name, profile in report['profiles'].items():
                print(f'\n{name}\n{profile}')
        else:
            print(f'Traced live / peak: {report["traced_live_mib"]:.2f} / {report["traced_peak_mib"]:.2f} MiB')
            print('\n'.join(report['allocation_hotspots']))
    if args.mode == 'latency':
        for name in ('first','warm','gc_disabled'):
            values = [x['ms'] for r in runs for x in ([r[name]] if name == 'first' else r[name])]
            print(f'{name}: median={statistics.median(values):.3f} ms; max={max(values):.3f} ms')
    print('RSS is process peak, not live allocations. Warm/GC-disabled rounds have ordering bias; page faults and GC counts are diagnostic, not proof of causation.')


if __name__ == '__main__':
    main()
