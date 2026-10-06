"""Database connection, schema bootstrap and transaction handling.

The schema in db/schema.sql is the source of truth (ADR-01). This module executes it
on an empty database and otherwise only opens connections; it never defines a table.

Two rules hold everywhere:
  * Writes run inside `transaction()`, so a change and its audit event commit together
    or not at all (CR-08).
  * Statements are parameterized. No caller builds SQL by string concatenation.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Sequence

SCHEMA_VERSION_TABLE = "app_user"        # presence of this table means the schema is applied


class DatabaseError(Exception):
    """A database problem the caller cannot recover from."""


class AppendOnlyViolation(DatabaseError):
    """An attempt to update or delete an append-only record (CR-09)."""


class PeriodLockedError(DatabaseError):
    """An attempt to write to a closed period (FR-PER-03, CR-18)."""


def _translate(error: sqlite3.Error) -> Exception:
    """Turn trigger messages into typed errors so services can react to the right one."""
    message = str(error)
    if "append-only" in message:
        return AppendOnlyViolation(message)
    if "period is closed" in message:
        return PeriodLockedError(message)
    return DatabaseError(message)


class Database:
    """One SQLite database. Create once per process and share the instance."""

    def __init__(self, path: Path, schema_path: Path):
        self.path = Path(path)
        self.schema_path = Path(schema_path)
        self._connection: sqlite3.Connection | None = None

    # -- lifecycle ----------------------------------------------------------
    def connect(self) -> sqlite3.Connection:
        if self._connection is not None:
            return self._connection

        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(
            self.path,
            isolation_level=None,        # transactions are explicit, never implicit
            detect_types=0,
            # The web server may open the connection on one thread and serve requests on
            # another. Requests are handled one at a time (ADR-21), so the connection is
            # never used by two threads at once; this flag only lifts sqlite3's
            # same-thread check, it does not make concurrent use safe.
            check_same_thread=False,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA busy_timeout = 5000")
        self._connection = connection
        return connection

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    # -- schema -------------------------------------------------------------
    def schema_applied(self) -> bool:
        row = self.connect().execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
            (SCHEMA_VERSION_TABLE,),
        ).fetchone()
        return row is not None

    def apply_schema(self) -> bool:
        """Execute db/schema.sql if the database is empty. Returns True if it ran."""
        if self.schema_applied():
            return False
        if not self.schema_path.exists():
            raise DatabaseError(f"{self.schema_path} not found; run from the repository root")
        connection = self.connect()
        connection.executescript(self.schema_path.read_text(encoding="utf-8"))
        if not self.schema_applied():
            raise DatabaseError("schema executed but expected tables are missing")
        return True

    def table_names(self) -> list[str]:
        rows = self.connect().execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
        ).fetchall()
        return [row["name"] for row in rows]

    def trigger_names(self) -> list[str]:
        rows = self.connect().execute(
            "SELECT name FROM sqlite_master WHERE type = 'trigger' ORDER BY name"
        ).fetchall()
        return [row["name"] for row in rows]

    # -- transactions -------------------------------------------------------
    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """One unit of work.

        BEGIN IMMEDIATE takes the write lock up front, so two writers cannot allocate
        the same audit sequence number (ADR-09). Any exception rolls the whole unit
        back, including the audit event, which is what makes CR-08 enforceable.
        """
        connection = self.connect()
        if connection.in_transaction:
            raise DatabaseError("a transaction is already open; do not nest transactions")
        connection.execute("BEGIN IMMEDIATE")
        try:
            yield connection
        except sqlite3.Error as error:
            connection.execute("ROLLBACK")
            raise _translate(error) from error
        except Exception:
            connection.execute("ROLLBACK")
            raise
        else:
            connection.execute("COMMIT")

    # -- read helpers -------------------------------------------------------
    def query(self, sql: str, parameters: Sequence[Any] = ()) -> list[sqlite3.Row]:
        try:
            return self.connect().execute(sql, parameters).fetchall()
        except sqlite3.Error as error:
            raise _translate(error) from error

    def query_one(self, sql: str, parameters: Sequence[Any] = ()) -> sqlite3.Row | None:
        try:
            return self.connect().execute(sql, parameters).fetchone()
        except sqlite3.Error as error:
            raise _translate(error) from error

    def scalar(self, sql: str, parameters: Sequence[Any] = ()) -> Any:
        row = self.query_one(sql, parameters)
        return None if row is None else row[0]


# ---------------------------------------------------------------------------
# Statement helpers used by repositories
# ---------------------------------------------------------------------------

def insert(connection: sqlite3.Connection, table: str, values: dict[str, Any]) -> int:
    """Insert one row and return its identifier.

    Column names come from repository code, never from user input; values are always
    bound parameters. Nothing here concatenates a value into SQL.
    """
    columns = list(values)
    placeholders = ", ".join("?" for _ in columns)
    sql = f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders})"
    try:
        cursor = connection.execute(sql, [values[column] for column in columns])
    except sqlite3.Error as error:
        raise _translate(error) from error
    return int(cursor.lastrowid)


def insert_many(connection: sqlite3.Connection, table: str, rows: list[dict[str, Any]]) -> int:
    """Insert several rows with identical keys. Returns the number inserted."""
    if not rows:
        return 0
    columns = list(rows[0])
    for row in rows:
        if list(row) != columns:
            raise DatabaseError("insert_many requires every row to have the same columns")
    placeholders = ", ".join("?" for _ in columns)
    sql = f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders})"
    try:
        connection.executemany(sql, [[row[column] for column in columns] for row in rows])
    except sqlite3.Error as error:
        raise _translate(error) from error
    return len(rows)


def update(connection: sqlite3.Connection, table: str, values: dict[str, Any],
           where: str, parameters: Sequence[Any]) -> int:
    """Update rows and return the count.

    Only mutable state uses this: item status, recommendation status, run status.
    Evidence tables reject it at the database level (CR-09).
    """
    assignments = ", ".join(f"{column} = ?" for column in values)
    sql = f"UPDATE {table} SET {assignments} WHERE {where}"
    try:
        cursor = connection.execute(sql, [*values.values(), *parameters])
    except sqlite3.Error as error:
        raise _translate(error) from error
    return cursor.rowcount


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------

def open_database(database_path: Path, schema_path: Path, *, create: bool = True) -> Database:
    """Open the database, applying the schema when it is empty."""
    database = Database(database_path, schema_path)
    database.connect()
    if create:
        database.apply_schema()
    elif not database.schema_applied():
        raise DatabaseError(f"{database_path} has no schema; run the import command first")
    return database
