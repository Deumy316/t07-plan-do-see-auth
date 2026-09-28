"""Offline administrator CLI for a complete task-6 export. No web endpoint.

Default is a read-only dry run. Apply requires explicit existing account ID,
username, source digest, and creates a SQLite backup under the target's directory.
Repeat imports are rejected as conflicts, never skipped or overwritten.
"""
import argparse
from contextlib import closing
from datetime import date, datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
from uuid import uuid4

ROOT = Path(__file__).resolve().parent
CONTRACT = json.loads((ROOT / 'contracts/pds-schema-v2.json').read_text(encoding='utf-8'))
FIELDS = {table: tuple(fields) for table, fields in CONTRACT['export_format']['data_fields'].items()}
KEYS = {
    'plans': [('id',)],
    'plan_versions': [('plan_id', 'version')],
    'tasks': [('id',)],
    'task_tags': [('task_id', 'tag')],
    'task_state_requests': [('request_id',)],
    'task_completion_events': [('id',), ('request_id',), ('task_id', 'task_version')],
    'execution_records': [('id',), ('request_id',)],
    'review_improvements': [('id',)],
    'next_plan_links': [('next_plan_id',)],
}
TOP_KEYS = {'schema_version', 'export_format_version', 'exported_at', 'aggregation_as_of',
            'timezone', 'time_units', 'review_rules', 'data', 'plan_reviews'}
MAX_BYTES = 50 * 1024 * 1024


class ImportRejected(ValueError):
    """Messages contain only structural diagnostics, never source field values."""


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ImportRejected('Duplicate JSON object key.')
        result[key] = value
    return result


def reject_constant(value):
    raise ImportRejected('Non-finite JSON number.')


def read_export(source):
    try:
        with Path(source).open('rb') as handle:
            raw = handle.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ImportRejected('Export exceeds the 50 MiB limit.')
        document = json.loads(raw.decode('utf-8'), object_pairs_hook=unique_object, parse_constant=reject_constant)
    except (OSError, UnicodeError, ValueError, RecursionError) as error:
        raise ImportRejected('Cannot read a valid UTF-8 JSON export.') from error
    if type(document) is not dict or set(document) != TOP_KEYS:
        raise ImportRejected('Export top-level fields do not match the task-6 format.')
    if document['schema_version'] != '2' or document['export_format_version'] != '1':
        raise ImportRejected('Only task-6 schema 2 / export format 1 is supported.')
    if type(document['data']) is not dict or set(document['data']) != set(FIELDS):
        raise ImportRejected('Export must contain exactly the nine source data tables.')
    for key in ('timezone', 'time_units', 'review_rules'):
        if type(document[key]) is not dict:
            raise ImportRejected('Invalid metadata object.')
    if type(document['plan_reviews']) is not list:
        raise ImportRejected('Invalid aggregate metadata array.')
    for key in ('exported_at', 'aggregation_as_of'):
        validate_timestamp(document[key])
    # plan_reviews and metadata describe the snapshot; they are NEVER inserted.
    return document['data'], hashlib.sha256(raw).hexdigest()


def validate_timestamp(value):
    if type(value) is not str or not re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z', value):
        raise ImportRejected('Invalid UTC timestamp format.')
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError as error:
        raise ImportRejected('Invalid UTC timestamp value.') from error


def insert_rows(db, data, user_id):
    for table, fields in FIELDS.items():
        columns = (*fields, 'owner_id') if table == 'plans' else fields
        sql = 'INSERT INTO ' + table + ' (' + ','.join(columns) + ') VALUES (' + ','.join('?' for _ in columns) + ')'
        for row in data[table]:
            values = tuple(row[field] for field in fields)
            db.execute(sql, (*values, user_id) if table == 'plans' else values)


def validate_data(data):
    """An isolated in-memory DB prevents references into existing target rows."""
    with closing(sqlite3.connect(':memory:')) as staged:
        staged.execute('PRAGMA foreign_keys=ON')
        staged.execute('CREATE TABLE users (id TEXT PRIMARY KEY)')
        staged.execute("INSERT INTO users VALUES ('import-target')")
        staged.executescript((ROOT / 'schema.sql').read_text(encoding='utf-8'))
        for table, fields in FIELDS.items():
            if type(data[table]) is not list:
                raise ImportRejected('Expected a row array: ' + table)
            specs = {r[1]: (r[2], bool(r[3])) for r in staged.execute('PRAGMA table_info(' + table + ')')}
            for row in data[table]:
                if type(row) is not dict or set(row) != set(fields):
                    raise ImportRejected('Incorrect row fields: ' + table)
                for field, value in row.items():
                    kind, required = specs[field]
                    if value is None:
                        if required:
                            raise ImportRejected('Required field is null: ' + table + '.' + field)
                        continue
                    valid = ((kind == 'TEXT' and type(value) is str) or
                             (kind == 'INTEGER' and type(value) is int and -(2**63) <= value < 2**63) or
                             (kind == 'REAL' and type(value) in (int, float) and -1e308 < value < 1e308 and math.isfinite(value)))
                    if not valid:
                        raise ImportRejected('Incorrect value type: ' + table + '.' + field)
                    if field == 'id' or field.endswith('_id'):
                        if not value or value != value.strip():
                            raise ImportRejected('Empty or padded identifier: ' + table + '.' + field)
                    if field.endswith('_date'):
                        try:
                            if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
                                raise ValueError()
                            date.fromisoformat(value)
                        except ValueError as error:
                            raise ImportRejected('Invalid calendar date: ' + table + '.' + field) from error
                    if field.endswith('_at'):
                        validate_timestamp(value)
        try:
            insert_rows(staged, data, 'import-target')
        except (sqlite3.Error, OverflowError) as error:
            raise ImportRejected('Source has duplicate keys, missing references, or invalid constraints.') from error
        if staged.execute('PRAGMA foreign_key_check').fetchall():
            raise ImportRejected('Source foreign-key check failed.')
        if staged.execute('''SELECT 1 FROM task_completion_events e
            JOIN task_state_requests r ON r.request_id=e.request_id
            WHERE e.task_id<>r.task_id OR e.task_version<>r.result_version
               OR r.target_status<>'completed' OR r.changed<>1 LIMIT 1''').fetchone():
            raise ImportRejected('Completion event does not match its source request.')
        versions = {(r['plan_id'], r['version']): r for r in data['plan_versions']}
        for plan in data['plans']:
            history = versions.get((plan['id'], plan['version']))
            if history is None or any(history[field] != plan[field] for field in FIELDS['plans'] if field != 'id'):
                raise ImportRejected('Current plan and its version snapshot do not match.')


def counts(db):
    return {table: db.execute('SELECT count(*) FROM ' + table).fetchone()[0] for table in FIELDS}


def check_target(db, user_id, username, expected_plan_title=None, expected_other_user=None):
    # Read identities only, never passwords, password hashes, or session values.
    row = db.execute('SELECT id, username FROM users WHERE id=? AND username=? COLLATE NOCASE', (user_id, username)).fetchone()
    if row is None:
        raise ImportRejected('Existing target account ID and username do not match.')
    if expected_other_user and not db.execute('SELECT 1 FROM users WHERE username=? COLLATE NOCASE', (expected_other_user,)).fetchone():
        raise ImportRejected('Expected second account is missing.')
    if expected_plan_title and not db.execute('SELECT 1 FROM plans WHERE owner_id=? AND title=?', (user_id, expected_plan_title)).fetchone():
        raise ImportRejected('Expected existing target plan is missing.')
    for table, fields in FIELDS.items():
        actual = {r[1] for r in db.execute('PRAGMA table_info(' + table + ')')}
        expected = set(fields) | ({'owner_id'} if table == 'plans' else set())
        if actual != expected:
            raise ImportRejected('Target schema mismatch: ' + table)
    if db.execute('PRAGMA foreign_key_check').fetchall():
        raise ImportRejected('Target has existing foreign-key violations.')


def check_conflicts(db, data):
    for table, key_sets in KEYS.items():
        for row in data[table]:
            for keys in key_sets:
                where = ' AND '.join(key + '=?' for key in keys)
                if db.execute('SELECT 1 FROM ' + table + ' WHERE ' + where, tuple(row[key] for key in keys)).fetchone():
                    raise ImportRejected('Existing key conflict in ' + table + '; nothing imported. Repeat imports are rejected.')


def consistent_backup(path):
    folder = path.parent / 'backups'
    folder.mkdir(exist_ok=True)
    backup = folder / (path.stem + '-before-import-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ') + '-' + uuid4().hex[:8] + '.sqlite3')
    # Exclusive creation prevents accidentally replacing a previous backup.
    with backup.open('xb'):
        pass
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as source:
        with closing(sqlite3.connect(backup)) as target:
            source.backup(target)
            if target.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise ImportRejected('Backup integrity check failed; nothing imported.')
    return backup


def verify_imported(db, data, user_id):
    """Compare all original fields and relationships, with no private output."""
    for table, fields in FIELDS.items():
        keys = KEYS[table][0]
        for row in data[table]:
            where = ' AND '.join(key + '=?' for key in keys)
            columns = (*fields, 'owner_id') if table == 'plans' else fields
            saved = db.execute('SELECT ' + ','.join(columns) + ' FROM ' + table + ' WHERE ' + where,
                               tuple(row[key] for key in keys)).fetchone()
            expected = tuple(row[field] for field in fields) + ((user_id,) if table == 'plans' else ())
            if saved is None or tuple(saved) != expected:
                raise ImportRejected('Post-import preservation check failed: ' + table)


def import_export(source, database, user_id, username, *, apply=False, expected_sha256=None,
                  expected_plan_title=None, expected_other_user=None):
    path = Path(database).resolve()
    if not path.is_file():
        raise ImportRejected('Existing target database is required; no database was created.')
    data, source_digest = read_export(source)
    if expected_sha256 is not None and source_digest != expected_sha256:
        raise ImportRejected('Source digest changed since review.')
    if apply and not expected_sha256:
        raise ImportRejected('Apply requires the SHA-256 from the dry run.')
    validate_data(data)
    try:
        with closing(sqlite3.connect(path.as_uri() + ('?mode=rw' if apply else '?mode=ro'), uri=True, timeout=10)) as db:
            db.execute('PRAGMA foreign_keys=ON')
            # Reserve the sole writer before checking collisions and backing up.
            # The backup uses a separate read connection before any target writes.
            db.execute('BEGIN IMMEDIATE' if apply else 'BEGIN')
            try:
                check_target(db, user_id, username, expected_plan_title, expected_other_user)
                before = counts(db)
                check_conflicts(db, data)
                incoming = {table: len(rows) for table, rows in data.items()}
                report = {'mode': 'applied' if apply else 'dry-run', 'target_user': username,
                          'source_sha256': source_digest, 'before': before, 'imported': incoming}
                if not apply:
                    db.rollback()
                    return report
                backup = consistent_backup(path)
                insert_rows(db, data, user_id)
                verify_imported(db, data, user_id)
                after = counts(db)
                if any(after[t] != before[t] + incoming[t] for t in FIELDS) or db.execute('PRAGMA foreign_key_check').fetchall():
                    raise ImportRejected('Post-import counts or relationships failed.')
                db.commit()
                report.update(after=after, backup=str(backup), preserved=True)
                return report
            except BaseException:
                db.rollback()
                raise
    except sqlite3.Error as error:
        raise ImportRejected('Target database operation failed; transaction rolled back.') from error


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True, type=Path)
    parser.add_argument('--db', required=True, type=Path)
    parser.add_argument('--target-user-id', required=True)
    parser.add_argument('--target-user', required=True)
    parser.add_argument('--expect-plan-title')
    parser.add_argument('--expect-other-user')
    parser.add_argument('--expect-sha256')
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args(argv)
    try:
        result = import_export(args.source, args.db, args.target_user_id, args.target_user,
                               apply=args.apply, expected_sha256=args.expect_sha256,
                               expected_plan_title=args.expect_plan_title, expected_other_user=args.expect_other_user)
    except (ImportRejected, OSError) as error:
        # No traceback or raw source records/SQL values in administrator output.
        print('Import rejected: ' + (str(error) if isinstance(error, ImportRejected) else 'File operation failed.'))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
