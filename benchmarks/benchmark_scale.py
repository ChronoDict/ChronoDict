"""Benchmark ChronoDict scaling with deterministic, validated historical workloads.

Run from the installed project. Each size/repetition runs in a fresh process.
Timing excludes validation and memory tracing; peak RSS includes the interpreter
and workload. Optional traced allocations use a separate run.
"""
import argparse
import json
import platform
import random
import resource
import statistics
import subprocess
import sys
from time import perf_counter

from chronodict import ChronoDict


def workload(size, queries, trace=False):
    """Insert N keys, replace N values, delete every tenth key, validate reads."""
    if trace:
        import tracemalloc
        tracemalloc.start()
    d = ChronoDict(indexes=['score'])
    timings = {}
    def timed(name, operation):
        start = perf_counter()
        result = operation()
        timings[name] = (perf_counter() - start) * 1000
        return result
    def insert():
        for k in range(size):
            d[k] = {'score': k % 1000}
    def update():
        for k in range(size):
            d.patch(k, score=(k + 1) % 1000)
    def delete():
        for k in range(0, size, 10):
            del d[k]
    timed('insert', insert)
    timed('update', update)
    timed('delete', delete)
    rng = random.Random(4102)
    keys = [rng.randrange(size) for _ in range(queries)]
    scores = [rng.randrange(1000) for _ in range(queries)]
    revisions = [rng.randrange(size, 2 * size + 1) for _ in range(queries)]
    lookups = timed('historical_lookup', lambda: [d.get(k, version=r)['score'] for k, r in zip(keys, revisions)])
    def searches():
        return [d.find(score=s, version=r) for s, r in zip(scores, revisions)]
    results = timed('historical_search', searches)
    def current_reads():
        return [d.get(k) for k in keys]
    current = timed('current_lookup', current_reads)
    expected_revision = 2 * size + len(range(0, size, 10))
    if d.revision != expected_revision or len(d) != size - len(range(0, size, 10)):
        raise AssertionError('Incorrect current size or revision')
    # Independent closed-form oracle: revision N+j has updated keys 0..j-1.
    for k, r, actual, now in zip(keys, revisions, lookups, current):
        expected = (k + int(k < r - size)) % 1000
        if actual != expected or now != (None if k % 10 == 0 else {'score': (k + 1) % 1000}):
            raise AssertionError('Lookup mismatch')
    for s, r, actual in zip(scores, revisions, results):
        boundary = r - size
        expected = {k: {'score': s} for k in range((s - 1) % 1000, boundary, 1000)}
        expected.update({k: {'score': s} for k in range(s, size, 1000) if k >= boundary})
        if actual != expected:
            raise AssertionError('Historical search mismatch')
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # macOS reports bytes; Linux reports KiB.
    rss_mib = peak / (1024 ** 2 if sys.platform == 'darwin' else 1024)
    result = {'timings': timings, 'peak_rss_mib': rss_mib, 'revision': d.revision}
    if trace:
        live, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        result['traced_live_mib'] = live / 1024 ** 2
        result['traced_peak_mib'] = peak / 1024 ** 2
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sizes', type=int, nargs='+', default=[10000, 100000])
    parser.add_argument('--queries', type=int, default=100)
    parser.add_argument('--repeats', type=int, default=3)
    parser.add_argument('--trace-memory', action='store_true', help='Separate traced run per size (slow)')
    parser.add_argument('--worker', type=int, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if min(args.sizes + [args.queries, args.repeats]) <= 0 or (args.worker is not None and args.worker <= 0):
        parser.error('Sizes, queries and repeats must be positive')
    if args.worker is not None:
        print(json.dumps(workload(args.worker, args.queries, args.trace_memory)))
        return
    print(f'Python {platform.python_version()} | {platform.system()} | {args.queries} queries | {args.repeats} repeats', flush=True)
    print('Median milliseconds; fresh-process peak RSS in MiB (includes interpreter and query results).', flush=True)
    for size in args.sizes:
        cmd = [sys.executable, __file__, '--worker', str(size), '--queries', str(args.queries)]
        runs = [json.loads(subprocess.check_output(cmd, text=True)) for _ in range(args.repeats)]
        print(f'\n{size:,} keys | {runs[0]["revision"]:,} revisions | all oracle checks passed', flush=True)
        for name in runs[0]['timings']:
            print(f'{name:22} {statistics.median(r["timings"][name] for r in runs):12.3f} ms', flush=True)
        print(f'Peak RSS (median)      {statistics.median(r["peak_rss_mib"] for r in runs):12.2f} MiB', flush=True)
        if args.trace_memory:
            traced = json.loads(subprocess.check_output(cmd + ['--trace-memory'], text=True))
            print(f'Traced live / peak: {traced["traced_live_mib"]:.2f} / {traced["traced_peak_mib"]:.2f} MiB', flush=True)


if __name__ == '__main__':
    main()
