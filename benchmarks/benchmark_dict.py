"""Compare ChronoDict and built-in dict using repeatable, validated workloads.

Run from the project root: python benchmarks/benchmark_dict.py
Times exclude setup, except insertion; memory includes construction.
"""
import argparse
import gc
import platform
import statistics
import time
import tracemalloc
from chronodict import ChronoDict


def build(factory, n):
    return factory((i, {'score': i % 100}) for i in range(n))


def chrono(items=()):
    return ChronoDict(items, indexes=['score'])


def measure(setup, operation, validate, repeats):
    samples = []
    for _ in range(repeats):
        state = setup()
        start = time.perf_counter()
        result = operation(state)
        samples.append(time.perf_counter() - start)
        if not validate(state, result):
            raise AssertionError('Benchmark correctness check failed')
    return statistics.median(samples) * 1000


def memory(factory, n):
    gc.collect()
    tracemalloc.start()
    try:
        state = build(factory, n)
        live, peak = tracemalloc.get_traced_memory()
        if len(state) != n:
            raise AssertionError('Memory workload lost entries')
        return live / 1024, peak / 1024
    finally:
        tracemalloc.stop()


def run(n, repeats):
    print(f'Python {platform.python_version()} | {n:,} entries | {repeats} repeats')
    print(f'{"Workload (median ms)":32} {"dict":>12} {"ChronoDict":>12}')
    for name, operation, check in [
        ('Insert', lambda d: [d.__setitem__(i, {'score': i % 100}) for i in range(n)], lambda d, r: len(d) == n and d[n-1]['score'] == (n-1)%100),
        ('Read by key (scalar field)', lambda d: sum(d[i]['score'] for i in range(n)), lambda d,r: r == sum(i%100 for i in range(n))),
        ('Replace values', lambda d: [d.__setitem__(i, {'score': 101}) for i in range(n)], lambda d,r: all(d[i]['score']==101 for i in range(n))),
        ('Delete', lambda d: [d.__delitem__(i) for i in range(n)], lambda d,r: not d),
        ('Equality search (100 queries)', None, None),
    ]:
        results = []
        for factory in (dict, chrono):
            setup = factory if name == 'Insert' else lambda f=factory: build(f,n)
            if operation is None:
                def query(d):
                    return [d.find(score=s) if isinstance(d, ChronoDict) else {k:v.copy() for k,v in d.items() if v['score']==s} for s in range(100)]
                def verify(d, result):
                    return all(group == {i:{'score':s} for i in range(s,n,100)} for s,group in enumerate(result))
                results.append(measure(setup, query, verify, repeats))
            else:
                results.append(measure(setup, operation, check, repeats))
        print(f'{name:32} {results[0]:12.3f} {results[1]:12.3f}')
    print('\nTraced Python allocations after insertion (KiB):')
    for factory in (dict, chrono):
        live, peak = memory(factory,n)
        print(f'{factory.__name__:12} live={live:.2f} peak={peak:.2f}')
    print('dict retains no history; ChronoDict retains history and a secondary index.')
    print('Memory is traced Python allocations, not total process RSS.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--size', type=int, default=5000)
    parser.add_argument('--repeats', type=int, default=5)
    args = parser.parse_args()
    if args.size < 1 or args.repeats < 1:
        parser.error('size and repeats must be positive')
    run(args.size, args.repeats)
