from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator


def uid() -> str:
    return uuid.uuid4().hex


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def dump(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def rowdict(row):
    return dict(row) if row is not None else None


def is_database_busy(error: BaseException) -> bool:
    """Only lock contention is retryable; schema/integrity errors need attention."""
    if not isinstance(error, sqlite3.OperationalError):
        return False
    code = getattr(error, 'sqlite_errorcode', None)
    if code is not None:
        return code & 0xff in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)
    return str(error).lower() in ('database is locked', 'database table is locked',
                                 'database schema is locked')


_MUTATION = re.compile(r'^\s*(?:INSERT(?:\s+OR\s+\w+)?\s+INTO|REPLACE\s+INTO|DELETE\s+FROM|UPDATE(?:\s+OR\s+\w+)?)\s+["`\[]?(\w+)', re.I)


class _ObservedConnection(sqlite3.Connection):
    """Publish only successful committed changes, with no SQL values in signals."""
    def _record(self, sql, changed):
        command = sql.lstrip().split(None, 1)[0].upper() if sql.strip() else ''
        if command in ('COMMIT', 'END') or command == 'RELEASE' and not self.in_transaction:
            self._publish()
        elif command == 'ROLLBACK' and not self.in_transaction:
            self._pending = {}
        elif changed:
            match = _MUTATION.match(sql)
            table = match[1].lower() if match else '*'
            columns = None
            if command == 'UPDATE' and re.search(r'\bSET\b', sql, re.I):
                assignments = re.split(r'\bSET\b', sql, maxsplit=1, flags=re.I)[1]
                assignments = re.split(r'\bWHERE\b', assignments, maxsplit=1, flags=re.I)[0]
                columns = {name.lower() for name in re.findall(r'\b(\w+)\s*=', assignments)} or None
            if table not in self._pending:
                self._pending[table] = columns
            elif columns is None or self._pending[table] is None:
                self._pending[table] = None
            else:
                self._pending[table].update(columns)
            if not self.in_transaction:
                self._publish()

    def _publish(self):
        changes, self._pending = self._pending, {}
        if changes and self._on_change:
            self._on_change(changes)

    def execute(self, sql, parameters=(), /):
        before = self.total_changes
        result = super().execute(sql, parameters)
        self._record(sql, self.total_changes != before)
        return result

    def executemany(self, sql, parameters, /):
        before = self.total_changes
        try:
            return super().executemany(sql, parameters)
        finally:
            self._record(sql, self.total_changes != before)

    def executescript(self, sql, /):
        # executescript can commit a preceding transaction before its own SQL.
        if self.in_transaction:
            self.commit()
        before = self.total_changes
        try:
            return super().executescript(sql)
        finally:
            if self.total_changes != before:
                self._pending['*'] = None
                if not self.in_transaction:
                    self._publish()

    def commit(self):
        super().commit()
        self._publish()

    def rollback(self):
        super().rollback()
        self._pending = {}


class Database:
    def __init__(self, path: Path):
        self.path = path
        self._reads = threading.local()
        self._subscribers = []
        from .background import BackgroundRuntime
        self.runtime = BackgroundRuntime(self)

    def subscribe(self, tables, callback):
        self._subscribers.append((tables, callback))

    def _publish(self, changes):
        for tables, callback in tuple(self._subscribers):
            if '*' in changes or any(table in changes and (
                    columns is None or changes[table] is None or set(columns) & changes[table])
                    for table, columns in tables.items()):
                try:
                    callback()
                except Exception:
                    logging.getLogger(__name__).exception('Background change notification failed')

    def connection_count(self):
        return getattr(self._reads, 'opened', 0)

    def connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.path, timeout=30, isolation_level=None, factory=_ObservedConnection)
        con._pending, con._on_change = {}, self._publish
        self._reads.opened = self.connection_count() + 1
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys=ON")
        con.execute("PRAGMA busy_timeout=30000")
        return con

    @contextmanager
    def connection_scope(self):
        """Thread-local leases for one unit of work; no connection crosses threads."""
        if getattr(self._reads, 'pool', None) is not None:
            yield
            return
        pool = {'idle': [], 'closed': False}
        self._reads.pool = pool
        try:
            yield
        finally:
            self._reads.pool = None
            pool['closed'] = True
            for con in pool['idle']:
                con.close()

    @contextmanager
    def _lease(self):
        pool = getattr(self._reads, 'pool', None)
        con = pool['idle'].pop() if pool and pool['idle'] else self.connect()
        discard = False
        try:
            yield con
        except BaseException:
            discard = True
            raise
        finally:
            try:
                if con.in_transaction:
                    con.rollback()
            except BaseException:
                con.close()
                raise
            if not discard and pool is not None and not pool['closed'] and len(pool['idle']) < 3:
                pool['idle'].append(con)
            else:
                con.close()

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        snapshot = getattr(self._reads, "snapshot", None)
        if snapshot is not None:
            yield snapshot
            return
        with self._lease() as con:
            yield con

    @contextmanager
    def read_snapshot(self) -> Iterator[sqlite3.Connection]:
        """Reuse one consistent, read-only connection within this worker thread."""
        snapshot = getattr(self._reads, "snapshot", None)
        if snapshot is not None:
            yield snapshot
            return
        with self._lease() as con:
            try:
                con.execute("PRAGMA query_only=ON")
                con.execute("BEGIN")
                self._reads.snapshot = con
                yield con
            finally:
                self._reads.snapshot = None
                con.rollback()
                con.execute("PRAGMA query_only=OFF")

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        if getattr(self._reads, "snapshot", None) is not None:
            raise RuntimeError("Cannot write inside a read-only database snapshot")
        with self.read() as con:
            con.execute("BEGIN IMMEDIATE")
            try:
                yield con
                con.commit()
            except BaseException:
                con.rollback()
                raise

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.read() as con:
            con.execute("PRAGMA journal_mode=WAL")
            con.execute("CREATE TABLE IF NOT EXISTS schema_version(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)")
            current = con.execute("SELECT COALESCE(MAX(version),0) FROM schema_version").fetchone()[0]
            for file in sorted((Path(__file__).parent / "migrations").glob("*.sql")):
                version = int(file.name.split("_", 1)[0])
                if version > current:
                    script = file.read_text(encoding="utf-8")
                    try:
                        con.executescript("BEGIN IMMEDIATE;\n" + script)
                        if version == 7:
                            from .case_codes import sync_project_codes
                            for project in con.execute('SELECT id,short_prefix FROM projects ORDER BY created_at,id').fetchall():
                                sync_project_codes(con, project['id'], project['short_prefix'], actor='migration')
                        if version == 8:
                            from .domain import GROUP_FIELDS
                            old = con.execute("SELECT requirements FROM templates WHERE id='insurer-v2'").fetchone()
                            original = [{'photo_group': group, 'stage': stage, 'role': role, 'minimum': 1, 'per_visit': False}
                                        for group, (stage, role) in GROUP_FIELDS.items()]
                            # Only upgrade the untouched built-in rules; keep custom requirements.
                            if old and json.loads(old[0]) == original:
                                updated = [{**rule, 'per_visit': rule['photo_group'] in ('work_checkin', 'construction')} for rule in original]
                                con.execute("UPDATE templates SET requirements=?,updated_at=? WHERE id='insurer-v2'", (dump(updated), now()))
                                audit(con, 'template', 'insurer-v2', 'visit_requirements', original, updated, 'migration')
                        if version == 30:
                            from .checkin_copies import migrate_legacy_copies
                            migrate_legacy_copies(con)
                        con.execute('INSERT INTO schema_version VALUES(?,?)', (version, now()))
                        con.commit()
                    except BaseException:
                        if con.in_transaction:
                            con.rollback()
                        raise
        from .domain import DEFAULT_REQUIREMENTS, GROUP_FIELDS
        from .case_codes import issue_code, unique_prefix
        with self.tx() as con:
            stamp = now()
            con.execute("INSERT OR IGNORE INTO templates VALUES(?,?,?,?,?,?)", ("general", "通用照片资料要求", "", dump(DEFAULT_REQUIREMENTS), stamp, stamp))
            requirements = [{"photo_group": group, "stage": stage, "role": role, "minimum": 1, "per_visit": group in ('work_checkin', 'construction')}
                            for group, (stage, role) in GROUP_FIELDS.items()]
            con.execute("INSERT OR IGNORE INTO templates VALUES(?,?,?,?,?,?)", ("insurer-v2", "保司基础过程资料", "", dump(requirements), stamp, stamp))
            con.execute("INSERT OR IGNORE INTO settings VALUES('recognition',?)", (dump({"local_ocr": True, "vision_enabled": False, "base_url": "", "model": "", "key_env": "IDI_VISION_API_KEY"}),))
            for row in con.execute("SELECT id,name FROM projects WHERE short_prefix='' ORDER BY created_at,id").fetchall():
                con.execute("UPDATE projects SET short_prefix=? WHERE id=?", (unique_prefix(con, row['name'], row['id']), row['id']))
            for row in con.execute("SELECT * FROM cases WHERE short_code IS NULL OR short_code='' ORDER BY created_at,id").fetchall():
                code = issue_code(con, dict(row))
                if code:
                    con.execute("UPDATE cases SET short_code=? WHERE id=?", (code, row['id']))

    def one(self, sql: str, values=()):
        with self.read() as con:
            return rowdict(con.execute(sql, values).fetchone())

    def all(self, sql: str, values=()):
        with self.read() as con:
            return [dict(row) for row in con.execute(sql, values).fetchall()]

    def setting(self, key: str, default=None):
        row = self.one("SELECT value FROM settings WHERE key=?", (key,))
        return json.loads(row["value"]) if row else default

    def set_setting(self, key: str, value) -> None:
        with self.tx() as con:
            con.execute("INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, dump(value)))

    def backup(self, destination: Path) -> None:
        with self.read() as source:
            dest = sqlite3.connect(destination)
            try:
                source.backup(dest)
            finally:
                dest.close()


def audit(con, entity_type: str, entity_id: str, action: str, before=None, after=None, actor="local"):
    from .identity import current
    person = current()
    if person:
        actor = 'user:' + person.id
    con.execute("INSERT INTO audit VALUES(?,?,?,?,?,?,?,?)", (uid(), entity_type, entity_id, action, dump(before) if before is not None else None, dump(after) if after is not None else None, actor, now()))
