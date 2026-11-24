"""Compare revision-aware scalar workloads in ChronoDict, pandas and SQLite.

All adapters keep every committed revision; pandas uses an explicit append-only
log with a MultiIndex, SQLite uses indexed revision intervals. Not full API
equivalence. Run from the installed ChronoDict project. No core changes needed.
"""
import argparse
import gc
import platform
import sqlite3
from statistics import median
from time import perf_counter

from chronodict import ChronoDict


class Chrono:
    def __init__(self, events):
        self.data = ChronoDict(indexes=['score'])
        for key, score in events:
            self.data[key] = {'score': score}

    def lookup(self, key, rev):
        value = self.data.get(key, version=rev)
        return None if value is None else value['score']

    def search(self, score, rev):
        return {k for k in self.data.find(score=score, version=rev)}

    def close(self):
        pass


class PandasLog:
    def __init__(self, events):
        import pandas as pd
        # Batch ingestion, not one DataFrame append per mutation.
        self.frame = pd.DataFrame([(k, r, s) for r, (k, s) in
                                   enumerate(events, 1)],
                                  columns=['key', 'revision', 'score'])
        self.frame = self.frame.set_index(['key', 'revision']).sort_index()

    def lookup(self, key, rev):
        try:
            rows = self.frame.loc[key]
        except KeyError:
            return None
        pos = rows.index.searchsorted(rev, side='right') - 1
        return None if pos < 0 else int(rows['score'].iloc[pos])

    def search(self, score, rev):
        rows = self.frame.reset_index()
        latest = rows[rows.revision <= rev].drop_duplicates('key', keep='last')
        return set(latest.loc[latest.score == score, 'key'])

    def close(self):
        pass


class SQLiteIntervals:
    def __init__(self, events):
        self.db = sqlite3.connect(':memory:')
        self.db.executescript('''CREATE TABLE history (
            key INTEGER NOT NULL, start INTEGER NOT NULL, stop INTEGER,
            score INTEGER NOT NULL, PRIMARY KEY(key,start));
            CREATE INDEX score_interval ON history(score,start,stop);
            CREATE INDEX open_key ON history(key) WHERE stop IS NULL;''')
        with self.db:
            for rev, (key, score) in enumerate(events, 1):
                self.db.execute('UPDATE history SET stop=? WHERE key=? AND stop IS NULL', (rev,key))
                self.db.execute('INSERT INTO history VALUES (?,?,NULL,?)', (key,rev,score))

    def lookup(self, key, rev):
        row = self.db.execute('SELECT score FROM history WHERE key=? AND start<=? ORDER BY start DESC LIMIT 1', (key,rev)).fetchone()
        return None if row is None else row[0]

    def search(self, score, rev):
        return {row[0] for row in self.db.execute('SELECT key FROM history WHERE score=? AND start<=? AND (stop IS NULL OR stop>?)', (score,rev,rev))}

    def close(self):
        self.db.close()


def workload(size, updates, queries):
    events = [(k,k % 100) for k in range(size)]
    events += [(i % size,(i * 17 + 1) % 100) for i in range(updates)]
    revisions = [1 + i * (len(events)-1) // max(1,queries-1) for i in range(queries)]
    lookups = [(i % size,r) for i,r in enumerate(revisions)]
    searches = [(i % 100,r) for i,r in enumerate(revisions)]
    state = {}
    expected_lookup, expected_search = [], []
    cursor = 0
    for (key,rev),(score,_) in zip(lookups,searches):
        while cursor < rev:
            k,s = events[cursor]
            state[k] = s
            cursor += 1
        expected_lookup.append(state.get(key))
        expected_search.append({k for k,s in state.items() if s == score})
    return events,lookups,searches,expected_lookup,expected_search


def measure(factory, work, repeats):
    events,lookups,searches,expected_lookup,expected_search = work
    samples = [[],[],[]]
    for _ in range(repeats):
        gc.collect()
        start = perf_counter()
        obj = factory(events)
        samples[0].append(perf_counter()-start)
        try:
            start = perf_counter()
            actual = [obj.lookup(k,r) for k,r in lookups]
            samples[1].append(perf_counter()-start)
            if actual != expected_lookup:
                raise AssertionError(f'{factory.__name__}: lookup mismatch')
            start = perf_counter()
            actual = [obj.search(s,r) for s,r in searches]
            samples[2].append(perf_counter()-start)
            if actual != expected_search:
                raise AssertionError(f'{factory.__name__}: search mismatch')
        finally:
            obj.close()
    return [median(s)*1000 for s in samples]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--size',type=int,default=5000)
    parser.add_argument('--updates',type=int,default=5000)
    parser.add_argument('--queries',type=int,default=100)
    parser.add_argument('--repeats',type=int,default=5)
    args = parser.parse_args()
    if min(args.size,args.queries,args.repeats)<1 or args.updates<0:
        parser.error('size, queries and repeats must be positive; updates nonnegative')
    work = workload(args.size,args.updates,args.queries)
    print(f'Python {platform.python_version()} | SQLite {sqlite3.sqlite_version} | {args.size:,} keys | {args.updates:,} updates | {args.queries} queries per operation | {args.repeats} repeats')
    factories = [Chrono,SQLiteIntervals]
    try:
        import pandas as pd
    except ImportError:
        print('pandas unavailable; install pandas to include its results.')
    else:
        print(f'pandas {pd.__version__}')
        factories.insert(1,PandasLog)
    print(f'{"Adapter (median ms)":24}{"Build":>12}{"Lookup":>12}{"Search":>12}')
    for factory in factories:
        b,l,s = measure(factory,work,args.repeats)
        print(f'{factory.__name__:24}{b:12.3f}{l:12.3f}{s:12.3f}')
    print('All historical query results matched the independent replay oracle.')
    print('Pandas uses batch ingestion; SQLite uses one SQL transaction; ChronoDict commits each event separately. No disk durability or full API equivalence. Memory/RSS deferred to profiling.')


if __name__ == '__main__':
    main()
    