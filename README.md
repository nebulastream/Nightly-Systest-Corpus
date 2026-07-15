# Nightly Systest Corpus

This repository stores generated NebulaStream systest files that are too large or too expensive to keep in the main NebulaStream repository. The nightly workflow checks out this repository and runs the generated suites from `slt/`.
Currently, all files in this repo are proted from SQL Logic Test.

## Ported test suites

| Suite | Link | Generated files | Queries | Status | Notes |
| --- | --- | ---: | ---: | --- | --- |
| `between_1` | [`slt/between_1/`](slt/between_1/) | 9 | 8,400 | Ported | SQL Logic Test predicate corpus with 1 input row per table. |
| `between_10` | [`slt/between_10/`](slt/between_10/) | 54 | 51,730 | Ported | SQL Logic Test predicate corpus with 10 input rows per table. |
| `between_100` | [`slt/between_100/`](slt/between_100/) | 45 | 42,830 | Ported | SQL Logic Test predicate corpus with 100 input rows per table. |
| `between_1000` | [`slt/between_1000/`](slt/between_1000/) | 3 | 2,370 | Ported | SQL Logic Test predicate corpus with 1,000 input rows per table. |
| `in_10` | [`slt/in_10/`](slt/in_10/) | 54 | 51,360 | Ported | SQL Logic Test `IN` predicate corpus with 10 input rows per table. |
| `in_100` | [`slt/in_100/`](slt/in_100/) | 45 | 43,305 | Ported | SQL Logic Test `IN` predicate corpus with 100 input rows per table. |
| `in_1000` | [`slt/in_1000/`](slt/in_1000/) | 18 | 17,265 | Ported | SQL Logic Test `IN` predicate corpus with 1,000 input rows per table. |

## Filtered query patterns

| Pattern | Example | Scope | Reason | Skipped queries | Suites |
| --- | --- | --- | --- | ---: | --- |
| Non-correlated `IN` subquery predicates | `WHERE col0 IN (SELECT col3 FROM tab0 WHERE col3 >= 9)` | Outer `WHERE` clause; SQLite reports `LIST SUBQUERY`, not correlated | not supported | 35,576 | `between_*`, `in_*` |

## Converter

The `slt_to_systest.py` script converts SQL Logic Test files to NebulaStream systest files. It internally maintains a list of filters to filter out currently unsupported query patterns.

## Command line arguments

```text
usage: slt_to_systest.py [-h] [--format {schema-only,fields,json,replay,test}]
                         [--limit-queries LIMIT_QUERIES] [--output OUTPUT]
                         [--output-dir OUTPUT_DIR]
                         [--queries-per-file QUERIES_PER_FILE]
                         [--skip-unsupported-queries] [--test-name TEST_NAME]
                         input
```

| Argument | Description |
| --- | --- |
| `input` | SQL Logic Test file to convert or inspect. |
| `--format {schema-only,fields,json,replay,test}` | Output mode. Defaults to `test`, which emits runnable NebulaStream systest files. Use `schema-only` to emit schema definitions and `replay` to inspect replayed query data. |
| `--limit-queries LIMIT_QUERIES` | Stop after replaying this many supported queries. |
| `--output OUTPUT` | Write single-file output to this path instead of stdout. |
| `--output-dir OUTPUT_DIR` | Write split systest output files into this directory. |
| `--queries-per-file QUERIES_PER_FILE` | Number of replayed queries per split output file. Defaults to `100`. |
| `--skip-unsupported-queries` | Skip queries matching known unsupported patterns. |
| `--test-name TEST_NAME` | Override the generated systest name or split-file prefix. |
| `-h`, `--help` | Print the CLI help. |
