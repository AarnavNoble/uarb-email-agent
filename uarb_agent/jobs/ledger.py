"""SQLite ledger: one row per inbound Message-ID, plus per-thread state and watches.

Job lifecycle (every change goes through `transition`, a compare-and-set):

    received -> working (lease) -> composed (.eml + reply Message-ID on disk) -> sent -> done
    received -> ignored
    working  -> received (lease expired, retry) | failed
    composed -> failed (SMTP permanently refused it)

Terminal states are done, ignored and failed. `composed` and `sent` survive a crash and are
settled by `Outbox.reconcile`, which looks for the stored reply Message-ID in Sent Mail.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections.abc import Callable, Iterable
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, Field


class Status(StrEnum):
    RECEIVED = "received"
    WORKING = "working"
    COMPOSED = "composed"
    SENT = "sent"
    DONE = "done"
    IGNORED = "ignored"
    FAILED = "failed"


TERMINAL = frozenset({Status.DONE, Status.IGNORED, Status.FAILED})

ALLOWED: dict[Status, frozenset[Status]] = {
    Status.RECEIVED: frozenset({Status.WORKING, Status.IGNORED}),
    Status.WORKING: frozenset({Status.COMPOSED, Status.RECEIVED, Status.FAILED}),
    Status.COMPOSED: frozenset({Status.SENT, Status.FAILED}),  # failed: SMTP refused the message for good
    Status.SENT: frozenset({Status.DONE}),
}

# Rows that count as "we answered (or are answering) this sender/thread" for rate limits.
_REPLIED = (Status.WORKING, Status.COMPOSED, Status.SENT, Status.DONE)


class IllegalTransition(ValueError):
    pass


class Job(BaseModel):
    message_id: str
    uid: str | None = None
    sender: str = ""
    subject: str = ""
    thread_key: str = ""
    status: Status = Status.RECEIVED
    attempts: int = 0
    lease_until: float | None = None
    reply_message_id: str | None = None
    eml_path: str | None = None
    trace_id: str | None = None
    reason: str | None = None
    created_at: float = 0.0
    updated_at: float = 0.0


class ThreadState(BaseModel):
    """What a follow-up in this thread needs: the last request, where MORE resumes, and any open question."""

    thread_key: str
    matter: str | None = None
    doc_type: str | None = None
    next_offset: int = 0
    sent_doc_nos: list[str] = Field(default_factory=list)
    pending: dict | None = None  # e.g. {"slot": "doc_type", "matter": "M12205"}
    updated_at: float = 0.0


class Watch(BaseModel):
    sender: str
    matter: str
    snapshot: dict = Field(default_factory=dict)  # counts + newest doc_no per tab
    last_checked: float | None = None
    created_at: float = 0.0


_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    message_id       TEXT PRIMARY KEY,
    uid              TEXT,
    sender           TEXT NOT NULL DEFAULT '',
    subject          TEXT NOT NULL DEFAULT '',
    thread_key       TEXT NOT NULL DEFAULT '',
    status           TEXT NOT NULL,
    attempts         INTEGER NOT NULL DEFAULT 0,
    lease_until      REAL,
    reply_message_id TEXT,
    eml_path         TEXT,
    trace_id         TEXT,
    reason           TEXT,
    created_at       REAL NOT NULL,
    updated_at       REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS jobs_sender ON jobs(sender, created_at);
CREATE INDEX IF NOT EXISTS jobs_thread ON jobs(thread_key);

CREATE TABLE IF NOT EXISTS threads (
    thread_key   TEXT PRIMARY KEY,
    matter       TEXT,
    doc_type     TEXT,
    next_offset  INTEGER NOT NULL DEFAULT 0,
    sent_doc_nos TEXT NOT NULL DEFAULT '[]',
    pending      TEXT,
    updated_at   REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS watches (
    sender       TEXT NOT NULL,
    matter       TEXT NOT NULL,
    snapshot     TEXT NOT NULL DEFAULT '{}',
    last_checked REAL,
    created_at   REAL NOT NULL,
    PRIMARY KEY (sender, matter)
);
"""

# Columns a transition may update alongside the status.
_JOB_FIELDS = frozenset({"uid", "lease_until", "reply_message_id", "eml_path", "trace_id", "reason", "attempts"})


class Ledger:
    """Thread-safe wrapper over one SQLite connection in WAL mode."""

    def __init__(self, path: Path | str, *, clock: Callable[[], float] = time.time):
        self.path = Path(path)
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._clock = clock
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False, timeout=10)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")  # a 'composed' row must survive a power cut
        self._db.execute("PRAGMA busy_timeout=10000")
        self._db.executescript(_SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def now(self) -> float:
        return self._clock()

    # jobs

    def record(
        self,
        message_id: str,
        *,
        uid: str | None = None,
        sender: str = "",
        subject: str = "",
        thread_key: str = "",
    ) -> bool:
        """Insert a new job in `received`; False if this Message-ID was already seen (duplicate delivery)."""
        now = self.now()
        with self._lock:
            cur = self._db.execute(
                "INSERT OR IGNORE INTO jobs"
                " (message_id, uid, sender, subject, thread_key, status, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (message_id, uid, sender.lower(), subject, thread_key or message_id, Status.RECEIVED, now, now),
            )
            return cur.rowcount == 1

    def get(self, message_id: str) -> Job | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM jobs WHERE message_id = ?", (message_id,)).fetchone()
        return Job(**dict(row)) if row else None

    def transition(self, message_id: str, from_states: Iterable[Status] | Status, to: Status, **fields) -> bool:
        """Move `message_id` to `to` only if it is currently in one of `from_states`. Returns whether it moved.

        Raises IllegalTransition if no listed from-state may legally reach `to`: that is a programming error,
        whereas a False return is a lost race and is normal.
        """
        froms = (from_states,) if isinstance(from_states, Status) else tuple(from_states)
        if not froms or any(to not in ALLOWED.get(f, ()) for f in froms):
            raise IllegalTransition(f"{'/'.join(froms)} -> {to}")
        unknown = set(fields) - _JOB_FIELDS
        if unknown:
            raise ValueError(f"unknown job fields: {sorted(unknown)}")
        sets = ", ".join(f"{k} = ?" for k in fields)
        sql = f"UPDATE jobs SET status = ?, updated_at = ?{', ' + sets if sets else ''}"
        sql += f" WHERE message_id = ? AND status IN ({', '.join('?' * len(froms))})"
        with self._lock:
            cur = self._db.execute(sql, (to, self.now(), *fields.values(), message_id, *froms))
            return cur.rowcount == 1

    def claim(self, message_id: str, *, lease_s: float = 300) -> bool:
        """received -> working, counting an attempt and taking a lease. False if someone else has it."""
        with self._lock:
            cur = self._db.execute(
                "UPDATE jobs SET status = ?, attempts = attempts + 1, lease_until = ?, updated_at = ?"
                " WHERE message_id = ? AND status = ?",
                (Status.WORKING, self.now() + lease_s, self.now(), message_id, Status.RECEIVED),
            )
            return cur.rowcount == 1

    def renew(self, message_id: str, *, lease_s: float = 300) -> bool:
        with self._lock:
            cur = self._db.execute(
                "UPDATE jobs SET lease_until = ?, updated_at = ? WHERE message_id = ? AND status = ?",
                (self.now() + lease_s, self.now(), message_id, Status.WORKING),
            )
            return cur.rowcount == 1

    def in_states(self, *states: Status) -> list[Job]:
        with self._lock:
            rows = self._db.execute(
                f"SELECT * FROM jobs WHERE status IN ({', '.join('?' * len(states))}) ORDER BY created_at",
                states,
            ).fetchall()
        return [Job(**dict(r)) for r in rows]

    def expired_leases(self) -> list[Job]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM jobs WHERE status = ? AND (lease_until IS NULL OR lease_until < ?) ORDER BY created_at",
                (Status.WORKING, self.now()),
            ).fetchall()
        return [Job(**dict(r)) for r in rows]

    def replies_to_sender_since(self, sender: str, since: float) -> int:
        with self._lock:
            row = self._db.execute(
                f"SELECT COUNT(*) FROM jobs WHERE sender = ? AND created_at >= ?"
                f" AND status IN ({', '.join('?' * len(_REPLIED))})",
                (sender.lower(), since, *_REPLIED),
            ).fetchone()
        return row[0]

    def replies_in_thread(self, thread_key: str) -> int:
        with self._lock:
            row = self._db.execute(
                f"SELECT COUNT(*) FROM jobs WHERE thread_key = ? AND status IN ({', '.join('?' * len(_REPLIED))})",
                (thread_key, *_REPLIED),
            ).fetchone()
        return row[0]

    # threads

    def get_thread(self, thread_key: str) -> ThreadState | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM threads WHERE thread_key = ?", (thread_key,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["sent_doc_nos"] = json.loads(d["sent_doc_nos"])
        d["pending"] = json.loads(d["pending"]) if d["pending"] else None
        return ThreadState(**d)

    def save_thread(self, state: ThreadState) -> None:
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO threads (thread_key, matter, doc_type, next_offset, sent_doc_nos, pending,"
                " updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    state.thread_key,
                    state.matter,
                    state.doc_type,
                    state.next_offset,
                    json.dumps(state.sent_doc_nos),
                    json.dumps(state.pending) if state.pending is not None else None,
                    self.now(),
                ),
            )

    # watches

    def add_watch(self, sender: str, matter: str, snapshot: dict) -> bool:
        """Create or refresh a watch; True if it is new."""
        with self._lock:
            existed = self._db.execute(
                "SELECT 1 FROM watches WHERE sender = ? AND matter = ?", (sender.lower(), matter)
            ).fetchone()
            self._db.execute(
                "INSERT INTO watches (sender, matter, snapshot, last_checked, created_at) VALUES (?, ?, ?, ?, ?)"
                " ON CONFLICT(sender, matter) DO UPDATE SET snapshot = excluded.snapshot,"
                " last_checked = excluded.last_checked",
                (sender.lower(), matter, json.dumps(snapshot), self.now(), self.now()),
            )
            return existed is None

    def remove_watch(self, sender: str, matter: str) -> bool:
        with self._lock:
            cur = self._db.execute("DELETE FROM watches WHERE sender = ? AND matter = ?", (sender.lower(), matter))
            return cur.rowcount == 1

    def list_watches(self, sender: str | None = None) -> list[Watch]:
        sql, args = "SELECT * FROM watches", ()
        if sender is not None:
            sql, args = sql + " WHERE sender = ?", (sender.lower(),)
        with self._lock:
            rows = self._db.execute(sql + " ORDER BY created_at", args).fetchall()
        return [Watch(**{**dict(r), "snapshot": json.loads(r["snapshot"])}) for r in rows]

    def update_watch(self, sender: str, matter: str, snapshot: dict) -> None:
        with self._lock:
            self._db.execute(
                "UPDATE watches SET snapshot = ?, last_checked = ? WHERE sender = ? AND matter = ?",
                (json.dumps(snapshot), self.now(), sender.lower(), matter),
            )
