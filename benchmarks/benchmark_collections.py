"""Benchmark standard collections and ChronoDict with validated workloads.

Run from the project root. Historical baseline retains independent full
snapshots; it matches the tested query semantics, not the entire API.
"""
import argparse
from collections import OrderedDict, defaultdict
from copy import deepcopy
import gc
import platform
from statistics import median
from time import perf_counter
import tracemalloc
from chronodict import ChronoDict


def ordered():
    return OrderedDict()


def default():
    return defaultdict(dict)


def chrono():
    return ChronoDict(indexes=['score'])


def build(factory, n):
    d = factory()
    for i in range(n):
        d[i] = {'score': i % 100}
    return d


def measure(setup, operation, check, repeats):
    samples = []
    for _ in range(repeats):
        d = setup()
        start = perf_counter()
        result = operation(d)
        samples.append((perf_counter() - start) * 1000)
        if not check(d, result):
            raise AssertionError('Incorrect benchmark result')
    return median(samples)


def insert(d, n):
    for i in range(n):
        d[i] = {'score': i % 100}


def replace(d, n):
    for i in range(n):
        d[i] = {'score': 101}


def delete(d, n):
    for i in range(n):
        del d[i]


def search(d, s):
    if isinstance(d, ChronoDict):
        return d.find(score=s)
    return {k: v.copy() for k, v in d.items() if v['score'] == s}


class SnapshotHistory:
    """Simple independent full-snapshot reference, with matching revisions."""
    def __init__(self):
        self.data = {}
        self.states = [{}]

    def __setitem__(self, key, value):
        self.data[key] = deepcopy(value)
        self.states.append(deepcopy(self.data))

    def get(self, key, *, version):
        return deepcopy(self.states[version].get(key))

    def find(self, *, version, score):
        return search(self.states[version], score)


def history_workload(factory, n, updates):
    d = build(factory, n)
    for i in range(updates):
        d[i % n] = {'score': 100 + i}
    return d


def memory(setup):
    gc.collect()
    tracemalloc.start()
    try:
        state = setup()
        live, peak = tracemalloc.get_traced_memory()
        return live / 1024, peak / 1024
    finally:
        tracemalloc.stop()


def run(n, repeats, history_size, updates):
    print(f'Python {platform.python_version()} | {n} entries | {repeats} repeats')
    factories = (ordered, default, chrono)
    print(f'{"Median ms":28} {"OrderedDict":>14} {"defaultdict":>14} {"ChronoDict":>14}')
    workloads = [
        ('Insert', True, lambda d: insert(d,n), lambda d,r: len(d)==n and all(d[i]['score']==i%100 for i in range(n))),
        ('Read scalar field', False, lambda d: sum(d[i]['score'] for i in range(n)), lambda d,r:r==sum(i%100 for i in range(n))),
        ('Replace', False, lambda d: replace(d,n), lambda d,r:all(d[i]['score']==101 for i in range(n))),
        ('Delete', False, lambda d:delete(d,n), lambda d,r:len(d)==0),
        ('100 equality searches', False, lambda d:[search(d,s) for s in range(100)], lambda d,r:r==[{i:{'score':s} for i in range(s,n,100)} for s in range(100)]),
    ]
    for label, empty, op, check in workloads:
        times = [measure(f if empty else lambda f=f:build(f,n),op,check,repeats) for f in factories]
        print(f'{label:28}' + ''.join(f'{t:14.3f}' for t in times))
    print('Traced allocations (KiB), live / peak:')
    for f in factories:
        print(f'{f.__name__:12} {memory(lambda:build(f,n))}')
    h = history_size
    reference = history_workload(SnapshotHistory,h,updates)
    revisions = range(h,h+updates+1)
    expected = [reference.find(version=r,score=0) for r in revisions]
    expected_keys = [reference.get(0,version=r) for r in revisions]
    print(f'Historical workload: {h} entries, {updates} updates; median ms')
    for f in (SnapshotHistory, chrono):
        setup = lambda f=f:history_workload(f,h,updates)
        write = measure(f,lambda d: (insert(d,h),replace_history(d,h,updates)),lambda d,r:d.get(0,version=h)=={'score':0},repeats)
        lookup = measure(setup,lambda d:[d.get(0,version=r) for r in revisions],lambda d,r:r==expected_keys,repeats)
        query = measure(setup,lambda d:[d.find(version=r,score=0) for r in revisions],lambda d,r:r==expected,repeats)
        print(f'{f.__name__:16} build={write:.3f} lookup={lookup:.3f} query={query:.3f} memory KiB={memory(setup)}')
    print('Standard collections do not maintain history; full snapshots match only tested historical queries. Tracemalloc is not RSS.')


def replace_history(d,n,updates):
    for i in range(updates):
        d[i%n]={'score':100+i}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--size',type=int,default=5000)
    parser.add_argument('--repeats',type=int,default=5)
    parser.add_argument('--history-size',type=int,default=100)
    parser.add_argument('--updates',type=int,default=100)
    args=parser.parse_args()
    if min(args.size,args.repeats,args.history_size,args.updates)<1:
        parser.error('arguments must be positive')
    run(args.size,args.repeats,args.history_size,args.updates)
