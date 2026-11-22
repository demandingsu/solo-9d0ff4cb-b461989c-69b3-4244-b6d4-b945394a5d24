"""SQLite access helpers.

Every request uses its own connection.  Writes run inside ``BEGIN IMMEDIATE``
transactions so that concurrent reviews are serialized by SQLite itself and a
review, its recomputed sheet result and the aggregate counts become visible
together (or not at all).
"""

from __future__ import annotations

import os
import sqlite3
import time
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

T = TypeVar("T")

DEFAULT_DB_PATH = "./data/answers.db"


def database_path() -> str:
    return os.environ.get("DATABASE_PATH", DEFAULT_DB_PATH)


def busy_timeout_ms() -> int:
    try:
        return max(1, int(os.environ.get("SQLITE_BUSY_TIMEOUT_MS", "15000")))
    except ValueError:
        return 15000


SCHEMA = """
CREATE TABLE IF NOT EXISTS forms (
    id            TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    display_order INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS standard_questions (
    id            TEXT PRIMARY KEY,
    prompt        TEXT NOT NULL,
    display_order INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS standard_options (
    id            TEXT PRIMARY KEY,
    question_id   TEXT NOT NULL REFERENCES standard_questions(id) ON DELETE CASCADE,
    label         TEXT NOT NULL,
    text          TEXT NOT NULL,
    display_order INTEGER NOT NULL,
    UNIQUE (question_id, label),
    UNIQUE (id, question_id)
);

-- One row per (form, paper number): which standard question was printed there.
CREATE TABLE IF NOT EXISTS form_questions (
    form_id     TEXT NOT NULL REFERENCES forms(id) ON DELETE CASCADE,
    paper_no    INTEGER NOT NULL CHECK (paper_no > 0),
    question_id TEXT NOT NULL REFERENCES standard_questions(id),
    PRIMARY KEY (form_id, paper_no),
    UNIQUE (form_id, question_id)
);

-- One row per (form, standard question, standard option): the printed label.
CREATE TABLE IF NOT EXISTS form_options (
    form_id       TEXT NOT NULL,
    question_id   TEXT NOT NULL,
    option_id     TEXT NOT NULL,
    printed_label TEXT NOT NULL,
    PRIMARY KEY (form_id, question_id, option_id),
    UNIQUE (form_id, question_id, printed_label),
    FOREIGN KEY (form_id, question_id)
        REFERENCES form_questions(form_id, question_id) ON DELETE CASCADE,
    FOREIGN KEY (option_id, question_id)
        REFERENCES standard_options(id, question_id)
);

CREATE TABLE IF NOT EXISTS sheets (
    id            TEXT PRIMARY KEY,
    label         TEXT NOT NULL,
    known_form_id TEXT REFERENCES forms(id),
    created_at    TEXT NOT NULL
);

-- Raw OCR result: the printed label selected at each paper question number.
CREATE TABLE IF NOT EXISTS sheet_answers (
    sheet_id      TEXT NOT NULL REFERENCES sheets(id) ON DELETE CASCADE,
    paper_no      INTEGER NOT NULL CHECK (paper_no > 0),
    printed_label TEXT NOT NULL,
    PRIMARY KEY (sheet_id, paper_no)
);

-- Active reviewer evidence.  Withdrawing deletes the row.
CREATE TABLE IF NOT EXISTS reviews (
    sheet_id    TEXT NOT NULL REFERENCES sheets(id) ON DELETE CASCADE,
    question_id TEXT NOT NULL,
    option_id   TEXT NOT NULL,
    reviewer    TEXT,
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (sheet_id, question_id),
    FOREIGN KEY (option_id, question_id)
        REFERENCES standard_options(id, question_id)
);

CREATE TABLE IF NOT EXISTS sheet_results (
    sheet_id              TEXT PRIMARY KEY REFERENCES sheets(id) ON DELETE CASCADE,
    status                TEXT NOT NULL CHECK (status IN ('complete', 'partial', 'pending')),
    candidate_form_ids    TEXT NOT NULL,
    candidate_evaluation  TEXT NOT NULL,
    pending_reasons       TEXT NOT NULL,
    question_count        INTEGER NOT NULL CHECK (question_count >= 0),
    counted_question_count INTEGER NOT NULL CHECK (counted_question_count >= 0),
    computed_at           TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sheet_question_results (
    sheet_id            TEXT NOT NULL REFERENCES sheet_results(sheet_id) ON DELETE CASCADE,
    question_id         TEXT NOT NULL REFERENCES standard_questions(id),
    status              TEXT NOT NULL CHECK (
        status IN ('counted', 'no_candidate_form', 'candidate_incomplete', 'ambiguous_candidates')
    ),
    derived_option_id   TEXT,
    counted             INTEGER NOT NULL CHECK (counted IN (0, 1)),
    reason              TEXT,
    detail              TEXT NOT NULL,
    derived_by_form     TEXT NOT NULL,
    PRIMARY KEY (sheet_id, question_id),
    FOREIGN KEY (derived_option_id, question_id)
        REFERENCES standard_options(id, question_id),
    CHECK ((counted = 0 AND derived_option_id IS NULL) OR (counted = 1 AND derived_option_id IS NOT NULL))
);

CREATE TABLE IF NOT EXISTS question_option_counts (
    question_id TEXT NOT NULL,
    option_id   TEXT NOT NULL,
    count       INTEGER NOT NULL DEFAULT 0 CHECK (count >= 0),
    PRIMARY KEY (question_id, option_id),
    FOREIGN KEY (option_id, question_id)
        REFERENCES standard_options(id, question_id)
);

CREATE INDEX IF NOT EXISTS idx_sheet_question_results_counted
    ON sheet_question_results(sheet_id, counted);
CREATE INDEX IF NOT EXISTS idx_sheet_results_status
    ON sheet_results(status);
CREATE INDEX IF NOT EXISTS idx_reviews_question
    ON reviews(question_id);
"""


def connect(db_path: str | None = None, *, read_only: bool = False) -> sqlite3.Connection:
    """Open a connection.

    Read connections never write to the database file.  In particular they
    must not execute ``PRAGMA synchronous`` or ``PRAGMA journal_mode``: SQLite
    treats those as writes and a read-only file would make every query fail.
    """

    path = db_path or database_path()
    if read_only:
        uri = f"file:{os.path.abspath(path)}?mode=ro"
        conn = sqlite3.connect(
            uri,
            uri=True,
            timeout=busy_timeout_ms() / 1000,
            isolation_level=None,
            check_same_thread=False,
        )
    else:
        conn = sqlite3.connect(
            path,
            timeout=busy_timeout_ms() / 1000,
            isolation_level=None,
            check_same_thread=False,
        )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(f"PRAGMA busy_timeout = {busy_timeout_ms()}")
    if not read_only:
        conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def init_db(db_path: str | None = None) -> str:
    path = db_path or database_path()
    Path(path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
    conn = connect(path)
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)
    finally:
        conn.close()
    return path


def _safe_rollback(conn: sqlite3.Connection) -> None:
    try:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
    except sqlite3.Error:
        pass


def _is_busy(exc: sqlite3.OperationalError) -> bool:
    msg = str(exc).lower()
    return "locked" in msg or "busy" in msg


def run_write(db_path: str | None, fn: Callable[[sqlite3.Connection], T], attempts: int = 8) -> T:
    """Run ``fn`` in one serialized write transaction.

    A failure rolls everything back: the old review, sheet result and counts
    are left untouched.  ``SQLITE_BUSY`` is retried with a short backoff.
    """
    path = db_path or database_path()
    last_error: sqlite3.OperationalError | None = None
    for attempt in range(attempts):
        conn = connect(path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            result = fn(conn)
            conn.execute("COMMIT")
            return result
        except sqlite3.OperationalError as exc:
            _safe_rollback(conn)
            last_error = exc
            if _is_busy(exc) and attempt + 1 < attempts:
                time.sleep(min(0.05 * (2**attempt), 0.5))
                continue
            raise
        except BaseException:
            _safe_rollback(conn)
            raise
        finally:
            conn.close()
    assert last_error is not None
    raise last_error


def run_read(db_path: str | None, fn: Callable[[sqlite3.Connection], T]) -> T:
    """Run a read against one consistent snapshot."""
    path = db_path or database_path()
    conn = connect(path, read_only=True)
    try:
        conn.execute("BEGIN")
        result = fn(conn)
        conn.execute("COMMIT")
        return result
    except BaseException:
        _safe_rollback(conn)
        raise
    finally:
        conn.close()


