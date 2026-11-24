# Project roadmap

| Phase | Scope | Status |
|---|---|---|
| 1 | Mapping API, revision history, temporal equality indexes, rollback, tests | Prototype implemented |
| 2 | Formal mutation semantics, dynamic indexes, range indexes, randomized reference-model tests | Planned |
| 3 | Structural sharing, delta encoding, retention, compaction | Planned |
| 4 | Atomic transactions, concurrent snapshots, isolation | Planned |
| 5 | Performance benchmarks against dict and existing indexed/persistent mappings | Planned |
| 6 | Packaging, CI, compatibility review, security and release documentation | Planned |
| 7 | Evaluate C/Rust acceleration and potential Python enhancement proposal | Research |

## Architecture

Primary hash table -> per-key revision history -> temporal secondary indexes -> historical query engine.

Historical point lookup uses binary search over key revisions. Indexed equality search visits keys ever associated with the indexed value and binary-searches their intervals. It is not guaranteed to be O(1 + results), especially with long histories. Range queries, persistence and transactions are not implemented.

## Development Roadmap
Phase	Development scope	                               Status
1	    Core dictionary implementation	                   Implemented

2	    Advanced indexing and mutation tracking	           Completed
    2.1	Dynamic index creation and removal                 Completed
    2.2	Range-based indexing (gt, lt, gte, lte)	           Completed
    2.3	Nested mutation tracking	                       Completed
    2.4	Composite indexes	                               Completed
    2.5	Historical timestamp-based retrieval	           Completed
    2.6	Comprehensive regression testing	               Completed

3	    Memory optimization and delta compression	       Completed
        3.1	Structural sharing	                           Completed
        3.2	Eliminate redundant deep copies	               Completed
        3.3	Delta-based revision storage	               Completed
        3.4	Configurable history retention	               Completed
        3.5	History compaction and garbage collection	   Completed
        3.6	Memory benchmarking and regression testing	   Completed

4	    Transactions and concurrency	                        Completed
        4.1	Atomic transactions	                                Completed
        4.2	Transaction isolation	                            Completed
        4.3	Concurrent read and write operations	            Completed
        4.4	Concurrent historical snapshots	                    Completed
        4.5	Conflict detection and resolution	                Completed
        4.6	Concurrency benchmarking and comprehensive testing	Completed

5	    Performance benchmarking	                                Completed
        5.1	Standard Python dict performance comparison             Completed
        5.2	OrderedDict and defaultdict comparison                  Completed
        5.3	Pandas and alternative temporal storage comparison      Completed
        5.4	Large-scale dataset benchmarking                        Completed
        5.5	CPU and memory profiling                                Completed
        5.6	Performance optimization and final benchmark report     Completed       

6	    a. Production hardening and b. PyPI publishing	    Pending
7	    Native C/Rust acceleration and PEP evaluation	    Research
