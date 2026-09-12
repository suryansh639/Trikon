"""Change Intelligence — convert a git diff into a structured impact set.

Pipeline:
    diff_parser.parse_diff()      → ChangeSet
    ast_indexer.index_files()     → per-file symbol table (cached in SQLite)
    symbol_resolver.resolve()     → cross-file references
    dep_graph.build()             → directed graph of "uses" edges
    blast_radius.compute_impact() → ImpactSet (impacted modules, symbols, tests, score)

The dep graph is incremental: files are re-indexed only when their SHA-256 changes.
This is the same design as the OSS ``impact-radius`` package.
"""
