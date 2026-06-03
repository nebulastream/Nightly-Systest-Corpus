#!/usr/bin/env python3

import argparse
import json
import re
import sqlite3
import sys
from collections import OrderedDict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Field:
    name: str
    sqlite_type: str
    systest_type: str
    nullable: bool
    primary_key: bool


@dataclass(frozen=True)
class TableSchema:
    name: str
    fields: tuple[Field, ...]


@dataclass(frozen=True)
class StatementEvent:
    line: int
    expectation: str
    sql: str


@dataclass(frozen=True)
class HashExpectation:
    value_count: int
    digest: str
    raw: str


@dataclass(frozen=True)
class QueryEvent:
    line: int
    header: str
    result_types: str
    sort_mode: str | None
    label: str | None
    sql: str
    expected_values: tuple[str, ...]
    hash_expectation: HashExpectation | None


@dataclass(frozen=True)
class TableSnapshot:
    table: str
    fields: tuple[Field, ...]
    rows: tuple[str, ...]


@dataclass(frozen=True)
class ReplayedQuery:
    index: int
    line: int
    header: str
    sql: str
    referenced_tables: tuple[str, ...]
    snapshots: tuple[TableSnapshot, ...]
    expected_rows: tuple[str, ...]
    slt_values: tuple[str, ...]
    hash_expectation: HashExpectation | None


@dataclass(frozen=True)
class ReplayError:
    line: int
    kind: str
    message: str


@dataclass(frozen=True)
class ReplaySummary:
    input: str
    total_events: int
    total_query_events: int
    replayed_query_events: int
    skipped_query_events: int
    executed_statement_events: int
    skipped_statement_events: int
    error_count: int


@dataclass(frozen=True)
class ReplayOutput:
    summary: ReplaySummary
    errors: tuple[ReplayError, ...]
    queries: tuple[ReplayedQuery, ...]


HASH_EXPECTATION_RE = re.compile(r'^(?P<count>\d+)\s+values\s+hashing\s+to\s+(?P<digest>[0-9a-fA-F]+)$')
IDENTIFIER_RE = r'(?:`[^`]+`|"[^"]+"|\[[^\]]+\]|[A-Za-z_]\w*)'
TABLE_REFERENCE_RE = re.compile(rf'\b(?:FROM|JOIN)\s+(?P<table>{IDENTIFIER_RE})\b', re.IGNORECASE)
TABLE_REWRITE_RE = re.compile(
    rf'\b(?P<keyword>FROM|JOIN)\s+(?P<table>{IDENTIFIER_RE})\b'
    rf'(?P<alias>\s+(?:AS\s+)?(?!(?:WHERE|ON|JOIN|ORDER|GROUP|HAVING|LIMIT|OFFSET|WINDOW|INTO|CROSS|INNER|LEFT|RIGHT|FULL|NATURAL)\b){IDENTIFIER_RE})?',
    re.IGNORECASE,
)
DROP_TABLE_RE = re.compile(r'(?is)^DROP\s+TABLE\s+(?:IF\s+EXISTS\s+)?(?P<name>`[^`]+`|"[^"]+"|\[[^\]]+\]|\w+)\s*;?\s*$')
NUMERIC_LITERAL_RE = r'[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?'


def strip_identifier(identifier: str) -> str:
    identifier = identifier.strip()
    if len(identifier) >= 2 and identifier[0] == identifier[-1] and identifier[0] in {'`', '"'}:
        return identifier[1:-1].replace(identifier[0] * 2, identifier[0])
    if len(identifier) >= 2 and identifier[0] == '[' and identifier[-1] == ']':
        return identifier[1:-1]
    return identifier


def quote_sqlite_identifier(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def split_top_level(text: str, delimiter: str = ',') -> list[str]:
    parts: list[str] = []
    start = 0
    depth = 0
    quote: str | None = None
    i = 0
    while i < len(text):
        char = text[i]
        if quote is not None:
            if char == quote:
                if i + 1 < len(text) and text[i + 1] == quote and quote in {'"', '`'}:
                    i += 2
                    continue
                quote = None
            elif char == '\\' and quote in {'"', "'"}:
                i += 2
                continue
            i += 1
            continue
        if char in {'"', "'", '`'}:
            quote = char
        elif char == '[':
            quote = ']'
        elif char == '(':
            depth += 1
        elif char == ')':
            depth -= 1
            if depth < 0:
                raise ValueError(f"unbalanced parenthesis in: {text}")
        elif char == delimiter and depth == 0:
            parts.append(text[start:i].strip())
            start = i + 1
        i += 1
    if quote is not None:
        raise ValueError(f"unterminated quote in: {text}")
    if depth != 0:
        raise ValueError(f"unbalanced parenthesis in: {text}")
    tail = text[start:].strip()
    if tail:
        parts.append(tail)
    return parts


def tokenize_definition(definition: str) -> list[str]:
    return re.findall(r'`[^`]*`|"[^"]*"|\[[^\]]*\]|\([^)]*\)|\S+', definition)


def normalize_type(tokens: list[str]) -> str:
    constraint_words = {
        'PRIMARY',
        'NOT',
        'NULL',
        'UNIQUE',
        'CHECK',
        'DEFAULT',
        'COLLATE',
        'REFERENCES',
        'GENERATED',
        'AS',
        'CONSTRAINT',
    }
    type_tokens: list[str] = []
    for token in tokens:
        if token.upper() in constraint_words:
            break
        type_tokens.append(token)
    if not type_tokens:
        return ''
    return re.sub(r'\s+', ' ', ' '.join(type_tokens)).upper()


def map_sqlite_type(sqlite_type: str) -> str:
    type_name = sqlite_type.upper()
    if 'INT' in type_name:
        return 'INT64'
    if any(marker in type_name for marker in ('REAL', 'FLOA', 'DOUB', 'NUM', 'DEC')):
        return 'FLOAT64'
    if any(marker in type_name for marker in ('CHAR', 'CLOB', 'TEXT', 'VARCHAR')):
        return 'VARSIZED'
    if type_name in {'BOOL', 'BOOLEAN'}:
        return 'BOOLEAN'
    raise ValueError(f"unsupported SQLite type: {sqlite_type}")


def parse_column(definition: str) -> Field | None:
    tokens = tokenize_definition(definition)
    if not tokens:
        return None
    head = tokens[0].upper()
    if head in {'CONSTRAINT', 'PRIMARY', 'UNIQUE', 'CHECK', 'FOREIGN'}:
        return None
    name = strip_identifier(tokens[0])
    sqlite_type = normalize_type(tokens[1:])
    if not sqlite_type:
        raise ValueError(f"missing type in column definition: {definition}")
    upper_definition = definition.upper()
    primary_key = bool(re.search(r'\bPRIMARY\s+KEY\b', upper_definition))
    nullable = not primary_key and not bool(re.search(r'\bNOT\s+NULL\b', upper_definition))
    return Field(name=name, sqlite_type=sqlite_type, systest_type=map_sqlite_type(sqlite_type), nullable=nullable, primary_key=primary_key)


def parse_create_table(statement: str) -> TableSchema | None:
    normalized = statement.strip().rstrip(';').strip()
    match = re.match(
        r'(?is)^CREATE\s+(?:TEMP(?:ORARY)?\s+)?TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(?P<name>`[^`]+`|"[^"]+"|\[[^\]]+\]|\w+)\s*\((?P<fields>.*)\)$',
        normalized,
    )
    if not match:
        return None
    fields = tuple(filter(None, (parse_column(part) for part in split_top_level(match.group('fields')))))
    if not fields:
        raise ValueError(f"no fields found in CREATE TABLE statement: {statement}")
    return TableSchema(name=strip_identifier(match.group('name')), fields=fields)


def parse_hash_expectation(line: str) -> HashExpectation | None:
    match = HASH_EXPECTATION_RE.match(line.strip())
    if not match:
        return None
    return HashExpectation(value_count=int(match.group('count')), digest=match.group('digest'), raw=line.strip())


def is_slt_control_line(line: str) -> bool:
    stripped = line.strip()
    return stripped.startswith(('statement ', 'query ', 'hash-threshold', 'skipif ', 'onlyif '))


def parse_query_header(header: str) -> tuple[str, str | None, str | None]:
    parts = header.split()
    if len(parts) < 2:
        raise ValueError(f"invalid query header: {header}")
    result_types = parts[1]
    sort_mode = next((part for part in parts[2:] if part in {'nosort', 'rowsort', 'valuesort'}), None)
    label = next((part for part in parts[2:] if part.startswith('label-')), None)
    return result_types, sort_mode, label


def parse_slt_events(path: Path) -> tuple[StatementEvent | QueryEvent, ...]:
    lines = path.read_text(encoding='utf-8', errors='replace').splitlines()
    events: list[StatementEvent | QueryEvent] = []
    i = 0
    while i < len(lines):
        stripped = lines[i].strip()
        if not stripped or stripped.startswith(('hash-threshold', 'skipif ', 'onlyif ')):
            i += 1
            continue
        if stripped.startswith('statement '):
            line_number = i + 1
            parts = stripped.split(maxsplit=2)
            expectation = parts[1] if len(parts) >= 2 else ''
            i += 1
            while i < len(lines) and not lines[i].strip():
                i += 1
            statement_lines: list[str] = []
            while i < len(lines):
                line = lines[i].strip()
                if not line or is_slt_control_line(line):
                    break
                statement_lines.append(line)
                i += 1
            if statement_lines:
                events.append(StatementEvent(line=line_number, expectation=expectation, sql=' '.join(statement_lines)))
            continue
        if stripped.startswith('query '):
            line_number = i + 1
            header = stripped
            result_types, sort_mode, label = parse_query_header(header)
            i += 1
            while i < len(lines) and not lines[i].strip():
                i += 1
            query_lines: list[str] = []
            while i < len(lines) and lines[i].strip() != '----':
                query_lines.append(lines[i].strip())
                i += 1
            if i >= len(lines):
                raise ValueError(f"query at line {line_number} has no result delimiter")
            i += 1
            expected_values: list[str] = []
            while i < len(lines):
                line = lines[i].strip()
                if not line or is_slt_control_line(line):
                    break
                expected_values.append(line)
                i += 1
            hash_expectation = parse_hash_expectation(expected_values[0]) if len(expected_values) == 1 else None
            events.append(
                QueryEvent(
                    line=line_number,
                    header=header,
                    result_types=result_types,
                    sort_mode=sort_mode,
                    label=label,
                    sql=' '.join(query_lines),
                    expected_values=tuple(() if hash_expectation else expected_values),
                    hash_expectation=hash_expectation,
                )
            )
            continue
        i += 1
    return tuple(events)


def collect_table_schemas(path: Path) -> OrderedDict[str, TableSchema]:
    schemas: OrderedDict[str, TableSchema] = OrderedDict()
    for event in parse_slt_events(path):
        if not isinstance(event, StatementEvent) or event.expectation != 'ok':
            continue
        schema = parse_create_table(event.sql)
        if schema is None:
            continue
        existing = schemas.get(schema.name)
        if existing is not None and existing != schema:
            raise ValueError(f"table {schema.name} has multiple schemas")
        schemas[schema.name] = schema
    return schemas


def render_field(field: Field) -> str:
    suffix = '' if field.nullable else ' NOT NULL'
    return f"{field.name} {field.systest_type}{suffix}"


def render_logical_source(schema: TableSchema) -> str:
    return f"CREATE LOGICAL SOURCE {schema.name}({', '.join(render_field(field) for field in schema.fields)});"


def is_index_statement(sql: str) -> bool:
    return bool(re.match(r'(?is)^CREATE\s+(?:UNIQUE\s+)?INDEX\b', sql.strip()))


def parse_drop_table(sql: str) -> str | None:
    match = DROP_TABLE_RE.match(sql)
    if not match:
        return None
    return strip_identifier(match.group('name'))


def resolve_table_name(name: str, schemas: OrderedDict[str, TableSchema]) -> str:
    for table_name in schemas:
        if table_name.lower() == name.lower():
            return table_name
    return name


def referenced_tables(sql: str, schemas: OrderedDict[str, TableSchema]) -> tuple[str, ...]:
    found: list[str] = []
    for match in TABLE_REFERENCE_RE.finditer(sql):
        table = resolve_table_name(strip_identifier(match.group('table')), schemas)
        if table not in found:
            found.append(table)
    return tuple(found)


def format_source_value(value: Any) -> str:
    if value is None:
        return ''
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, float):
        return str(value)
    return str(value)


def format_systest_result_value(value: Any) -> str:
    if value is None:
        return 'NULL'
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, float):
        return str(value)
    return str(value)


def format_slt_value(value: Any, result_type: str | None) -> str:
    if value is None:
        return 'NULL'
    if result_type == 'I':
        return str(int(value))
    if result_type == 'R':
        return f"{float(value):.3f}"
    if isinstance(value, bytes):
        return value.hex()
    return str(value)


def sorted_result_rows(rows: tuple[tuple[Any, ...], ...], sort_mode: str | None) -> tuple[tuple[Any, ...], ...]:
    if sort_mode == 'rowsort':
        return tuple(sorted(rows, key=lambda row: tuple(format_systest_result_value(value) for value in row)))
    return rows


def flatten_values(rows: tuple[tuple[str, ...], ...]) -> tuple[str, ...]:
    return tuple(value for row in rows for value in row)


def format_slt_values(rows: tuple[tuple[Any, ...], ...], result_types: str, sort_mode: str | None) -> tuple[str, ...]:
    width = len(result_types)
    if rows and len(rows[0]) != width:
        raise ValueError(f"query type '{result_types}' expects {width} columns, got {len(rows[0])}")
    formatted_rows = tuple(
        tuple(format_slt_value(value, result_types[index] if index < width else None) for index, value in enumerate(row)) for row in rows
    )
    if sort_mode == 'rowsort':
        formatted_rows = tuple(sorted(formatted_rows))
    values = flatten_values(formatted_rows)
    if sort_mode == 'valuesort':
        return tuple(sorted(values))
    return values


def normalize_expected_values(values: tuple[str, ...], result_types: str, sort_mode: str | None) -> tuple[str, ...]:
    if sort_mode == 'valuesort':
        return tuple(sorted(values))
    if sort_mode != 'rowsort':
        return values
    width = len(result_types)
    if width == 0 or len(values) % width != 0:
        raise ValueError(f"cannot rowsort {len(values)} values with result type '{result_types}'")
    rows = tuple(tuple(values[index:index + width]) for index in range(0, len(values), width))
    return flatten_values(tuple(sorted(rows)))


def format_expected_rows(rows: tuple[tuple[Any, ...], ...], sort_mode: str | None) -> tuple[str, ...]:
    rows = sorted_result_rows(rows, sort_mode)
    return tuple(','.join(format_systest_result_value(value) for value in row) for row in rows)


def snapshot_table(cursor: sqlite3.Cursor, table: str, schema: TableSchema) -> TableSnapshot:
    table_sql = quote_sqlite_identifier(table)
    rows = cursor.execute(f'SELECT * FROM {table_sql} ORDER BY rowid').fetchall()
    return TableSnapshot(
        table=table,
        fields=schema.fields,
        rows=tuple(','.join(format_source_value(value) for value in row) for row in rows),
    )


def has_unsupported_predicate_subquery(sql: str) -> bool:
    return bool(re.search(r'(?is)\b(?:IN|EXISTS)\s*\(\s*SELECT\b|(?:=|==|<>|!=|<=|>=|<|>)\s*\(\s*SELECT\b', sql))


def replay_slt(path: Path, limit_queries: int | None, skip_unsupported_queries: bool = False) -> ReplayOutput:
    events = parse_slt_events(path)
    connection = sqlite3.connect(':memory:')
    cursor = connection.cursor()
    schemas: OrderedDict[str, TableSchema] = OrderedDict()
    errors: list[ReplayError] = []
    queries: list[ReplayedQuery] = []
    total_query_events = sum(isinstance(event, QueryEvent) for event in events)
    executed_statement_events = 0
    skipped_statement_events = 0
    skipped_query_events = 0
    query_index = 0

    for event in events:
        if isinstance(event, StatementEvent):
            if event.expectation != 'ok':
                errors.append(ReplayError(line=event.line, kind='unsupported_statement', message=f"unsupported statement expectation: {event.expectation}"))
                break
            schema = parse_create_table(event.sql)
            dropped_table = parse_drop_table(event.sql)
            if is_index_statement(event.sql):
                skipped_statement_events += 1
                continue
            try:
                cursor.execute(event.sql)
                connection.commit()
            except sqlite3.Error as exception:
                errors.append(ReplayError(line=event.line, kind='statement_execution', message=f"{exception}: {event.sql}"))
                break
            executed_statement_events += 1
            if schema is not None:
                schemas[schema.name] = schema
            if dropped_table is not None:
                schemas.pop(resolve_table_name(dropped_table, schemas), None)
            continue

        query_index += 1
        if skip_unsupported_queries and has_unsupported_predicate_subquery(event.sql):
            skipped_query_events += 1
            continue
        if limit_queries is not None and len(queries) >= limit_queries:
            break

        refs = referenced_tables(event.sql, schemas)
        snapshots: list[TableSnapshot] = []
        missing_schema = False
        for table in refs:
            schema = schemas.get(table)
            if schema is None:
                errors.append(ReplayError(line=event.line, kind='missing_schema', message=f"no active schema for referenced table {table}"))
                missing_schema = True
                break
            snapshots.append(snapshot_table(cursor, table, schema))
        if missing_schema:
            break

        try:
            sqlite_rows = tuple(tuple(row) for row in cursor.execute(event.sql).fetchall())
        except sqlite3.Error as exception:
            errors.append(ReplayError(line=event.line, kind='query_execution', message=f"{exception}: {event.sql}"))
            break

        try:
            actual_slt_values = format_slt_values(sqlite_rows, event.result_types, event.sort_mode)
            expected_values = normalize_expected_values(event.expected_values, event.result_types, event.sort_mode)
        except ValueError as exception:
            errors.append(ReplayError(line=event.line, kind='result_format', message=str(exception)))
            break

        if event.hash_expectation is None and expected_values != actual_slt_values:
            errors.append(
                ReplayError(
                    line=event.line,
                    kind='result_mismatch',
                    message=f"expected {list(expected_values)}, got {list(actual_slt_values)}",
                )
            )
        elif event.hash_expectation is not None and event.hash_expectation.value_count != len(actual_slt_values):
            errors.append(
                ReplayError(
                    line=event.line,
                    kind='hash_count_mismatch',
                    message=f"expected {event.hash_expectation.value_count} hashed values, got {len(actual_slt_values)}",
                )
            )

        queries.append(
            ReplayedQuery(
                index=query_index,
                line=event.line,
                header=event.header,
                sql=event.sql,
                referenced_tables=refs,
                snapshots=tuple(snapshots),
                expected_rows=format_expected_rows(sqlite_rows, event.sort_mode),
                slt_values=actual_slt_values,
                hash_expectation=event.hash_expectation,
            )
        )

    summary = ReplaySummary(
        input=str(path),
        total_events=len(events),
        total_query_events=total_query_events,
        replayed_query_events=len(queries),
        skipped_query_events=skipped_query_events,
        executed_statement_events=executed_statement_events,
        skipped_statement_events=skipped_statement_events,
        error_count=len(errors),
    )
    return ReplayOutput(summary=summary, errors=tuple(errors), queries=tuple(queries))


def sanitize_identifier(identifier: str) -> str:
    sanitized = re.sub(r'\W+', '_', identifier)
    sanitized = sanitized.strip('_') or 'x'
    if not re.match(r'[A-Za-z_]', sanitized):
        sanitized = f'x_{sanitized}'
    return sanitized


def snapshot_source_name(query_index: int, table: str) -> str:
    return f'slt_q{query_index:04d}_{sanitize_identifier(table)}'


def snapshot_field_types(snapshots: tuple[TableSnapshot, ...]) -> dict[str, str]:
    field_types: dict[str, str] = {}
    for snapshot in snapshots:
        for field in snapshot.fields:
            field_types.setdefault(field.name.lower(), field.systest_type)
    return field_types


def wrap_numeric_literal(field_type: str, literal: str) -> str:
    return f'{field_type}({literal})'


def wrap_numeric_if_needed(field_types: dict[str, str], field_name: str, literal: str) -> str:
    if not re.fullmatch(NUMERIC_LITERAL_RE, literal.strip()):
        return literal
    field_type = field_types.get(strip_identifier(field_name).lower())
    if field_type is None:
        return literal
    return wrap_numeric_literal(field_type, literal.strip())


def normalize_query_literals(query: str, snapshots: tuple[TableSnapshot, ...]) -> str:
    field_types = snapshot_field_types(snapshots)
    field_pattern = r'(?P<field>\b[A-Za-z_]\w*\b)'

    def replace_between(match: re.Match[str]) -> str:
        field = match.group('field')
        not_token = match.group('not') or ''
        lower = wrap_numeric_if_needed(field_types, field, match.group('lower'))
        upper = wrap_numeric_if_needed(field_types, field, match.group('upper'))
        return f'{field} {not_token}BETWEEN {lower} AND {upper}'

    def replace_in(match: re.Match[str]) -> str:
        field = match.group('field')
        not_token = match.group('not') or ''
        values = [wrap_numeric_if_needed(field_types, field, value.strip()) for value in split_top_level(match.group('values'))]
        return f'{field} {not_token}IN ({','.join(values)})'

    def replace_comparison(match: re.Match[str]) -> str:
        field = match.group('field')
        op = match.group('op')
        literal = wrap_numeric_if_needed(field_types, field, match.group('literal'))
        return f'{field} {op} {literal}'

    query = re.sub(
        rf'{field_pattern}\s+(?P<not>NOT\s+)?BETWEEN\s+(?P<lower>{NUMERIC_LITERAL_RE})\s+AND\s+(?P<upper>{NUMERIC_LITERAL_RE})',
        replace_between,
        query,
        flags=re.IGNORECASE,
    )
    query = re.sub(
        rf'{field_pattern}\s+(?P<not>NOT\s+)?IN\s*\((?P<values>[^()]+)\)',
        replace_in,
        query,
        flags=re.IGNORECASE,
    )
    query = re.sub(
        rf'{field_pattern}\s*(?P<op><=|>=|<>|!=|==|=|<|>)\s*(?P<literal>{NUMERIC_LITERAL_RE})',
        replace_comparison,
        query,
    )
    return query


def resolve_snapshot_table(name: str, snapshots: tuple[TableSnapshot, ...]) -> str:
    for snapshot in snapshots:
        if snapshot.table.lower() == name.lower():
            return snapshot.table
    return name


def find_top_level_query_organization(query: str) -> int | None:
    depth = 0
    quote: str | None = None
    i = 0
    while i < len(query):
        char = query[i]
        if quote is not None:
            if char == quote:
                if i + 1 < len(query) and query[i + 1] == quote and quote in {'"', '`'}:
                    i += 2
                    continue
                quote = None
            elif char == '\\' and quote in {'"', "'"}:
                i += 2
                continue
            i += 1
            continue
        if char in {'"', "'", '`'}:
            quote = char
            i += 1
            continue
        if char == '(':
            depth += 1
            i += 1
            continue
        if char == ')':
            depth -= 1
            i += 1
            continue
        if depth == 0:
            tail = query[i:]
            match = re.match(r'(?is)\s+(ORDER\s+BY|LIMIT|OFFSET)\b', tail)
            if match:
                return i
        i += 1
    return None


def append_sink_clause(query: str) -> str:
    organization_start = find_top_level_query_organization(query)
    if organization_start is None:
        return f'{query} INTO File();'
    return f'{query[:organization_start]} INTO File(){query[organization_start:]};'


def rewrite_query_for_systest(query: ReplayedQuery, snapshot_names: dict[str, str]) -> str:
    rewritten = normalize_query_literals(query.sql.strip().rstrip(';'), query.snapshots)

    def replace_table(match: re.Match[str]) -> str:
        original_table = strip_identifier(match.group('table'))
        table = resolve_snapshot_table(original_table, query.snapshots)
        alias = match.group('alias')
        if alias:
            return f"{match.group('keyword')} {snapshot_names.get(table, table)}{alias}"
        return f"{match.group('keyword')} {snapshot_names.get(table, table)} AS {sanitize_identifier(original_table)}"

    rewritten = TABLE_REWRITE_RE.sub(replace_table, rewritten)
    return append_sink_clause(rewritten)


def render_snapshot_source(source_name: str, snapshot: TableSnapshot) -> list[str]:
    lines = [
        f"CREATE LOGICAL SOURCE {source_name}({', '.join(render_field(field) for field in snapshot.fields)});",
        f"CREATE PHYSICAL SOURCE FOR {source_name} TYPE File;",
        'ATTACH INLINE',
    ]
    lines.extend(snapshot.rows)
    lines.append('')
    return lines


def render_systest_test_file(replay: ReplayOutput, test_name: str) -> str:
    lines = [
        f'# name: {test_name}',
        f'# description: Generated from {Path(replay.summary.input).name}',
        '# groups: [SLT]',
        '',
    ]
    for query in replay.queries:
        snapshot_names = {snapshot.table: snapshot_source_name(query.index, snapshot.table) for snapshot in query.snapshots}
        for snapshot in query.snapshots:
            lines.extend(render_snapshot_source(snapshot_names[snapshot.table], snapshot))
        lines.append(rewrite_query_for_systest(query, snapshot_names))
        lines.append('----')
        lines.extend(query.expected_rows)
        lines.append('')
    return '\n'.join(lines)


def emit_or_write(content: str, output: Path | None) -> None:
    if output is None:
        print(content)
        return
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(content + '\n', encoding='utf-8')


def chunk_queries(queries: tuple[ReplayedQuery, ...], queries_per_file: int) -> list[tuple[ReplayedQuery, ...]]:
    if queries_per_file <= 0:
        raise ValueError('queries per file must be greater than zero')
    return [queries[index:index + queries_per_file] for index in range(0, len(queries), queries_per_file)]


def write_split_systest_files(replay: ReplayOutput, output_dir: Path, queries_per_file: int, test_name_prefix: str) -> list[Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    stem = sanitize_identifier(Path(replay.summary.input).stem)
    for chunk_index, query_chunk in enumerate(chunk_queries(replay.queries, queries_per_file), start=1):
        output = output_dir / f'{stem}_{chunk_index:03d}.test'
        chunk_replay = ReplayOutput(summary=replay.summary, errors=(), queries=query_chunk)
        content = render_systest_test_file(chunk_replay, f'{test_name_prefix}/{output.name}')
        output.write_text(content + '\n', encoding='utf-8')
        written.append(output)
    return written


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('input', type=Path)
    parser.add_argument('--format', choices=('systest', 'fields', 'json', 'replay', 'test'), default='systest')
    parser.add_argument('--limit-queries', type=int)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--output-dir', type=Path)
    parser.add_argument('--queries-per-file', type=int, default=100)
    parser.add_argument('--skip-unsupported-queries', action='store_true')
    parser.add_argument('--test-name')
    args = parser.parse_args()

    if args.format == 'replay':
        replay = replay_slt(args.input, args.limit_queries, args.skip_unsupported_queries)
        print(json.dumps(asdict(replay), indent=2))
        return 1 if replay.summary.error_count else 0

    if args.format == 'test':
        replay = replay_slt(args.input, args.limit_queries, args.skip_unsupported_queries)
        if replay.summary.error_count:
            print(json.dumps([asdict(error) for error in replay.errors], indent=2), file=sys.stderr)
            return 1
        if args.output_dir is not None:
            test_name_prefix = args.test_name or args.output_dir.name
            written = write_split_systest_files(replay, args.output_dir, args.queries_per_file, test_name_prefix)
            print('\n'.join(str(path) for path in written))
            return 0
        test_name = args.test_name or f"generated/{args.input.stem}.test"
        emit_or_write(render_systest_test_file(replay, test_name), args.output)
        return 0

    schemas = collect_table_schemas(args.input)
    if args.format == 'json':
        print(json.dumps([asdict(schema) for schema in schemas.values()], indent=2))
    elif args.format == 'fields':
        for schema in schemas.values():
            print(f"{schema.name}({', '.join(render_field(field) for field in schema.fields)})")
    else:
        for schema in schemas.values():
            print(render_logical_source(schema))
    return 0


if __name__ == '__main__':
    sys.exit(main())
