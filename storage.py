"""
Storage layer. SQLite file, WAL mode, BEGIN IMMEDIATE transactions for every
state-changing operation so that concurrent writers (e.g. a cancel racing a
result continuation) serialize instead of interleaving. Whichever transaction
commits first wins; the loser sees a stale row and returns 409 at the call site.
"""

import json
import sqlite3
import time
from contextlib import contextmanager

DB_PATH = "a2a_invoice_agent.db"


def _connect():
    conn = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=30000;")
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = _connect()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS tasks (
            id TEXT PRIMARY KEY,
            context_id TEXT NOT NULL,
            principal TEXT NOT NULL,
            batch_id TEXT NOT NULL,
            state TEXT NOT NULL,
            history TEXT NOT NULL,        -- JSON list of messages
            proposals TEXT,               -- JSON proposals data part
            receipts TEXT,                -- JSON receipts data part (null until completed)
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        );

        CREATE TABLE IF NOT EXISTS messages (
            principal TEXT NOT NULL,
            message_id TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            task_id TEXT NOT NULL,
            response TEXT NOT NULL,       -- exact JSON body returned last time
            created_at REAL NOT NULL,
            PRIMARY KEY (principal, message_id)
        );

        CREATE TABLE IF NOT EXISTS proposals (
            task_id TEXT NOT NULL,
            package_id TEXT NOT NULL,
            action_id TEXT NOT NULL,
            action TEXT NOT NULL,
            facts TEXT NOT NULL,
            evidence_refs TEXT NOT NULL,
            rationale TEXT NOT NULL,
            PRIMARY KEY (task_id, package_id)
        );

        CREATE TABLE IF NOT EXISTS package_cache (
            content_hash TEXT PRIMARY KEY,
            action TEXT NOT NULL,
            facts TEXT NOT NULL,
            evidence_refs TEXT NOT NULL,
            rationale TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS receipt_nonces (
            task_id TEXT NOT NULL,
            package_id TEXT NOT NULL,
            receipt_nonce TEXT NOT NULL,
            PRIMARY KEY (task_id, package_id)
        );
        """
    )
    conn.close()


@contextmanager
def write_txn():
    """A serialized write transaction. SQLite's BEGIN IMMEDIATE takes the
    write lock immediately, so a second concurrent writer blocks (then sees
    the first writer's committed state) instead of racing on the same row."""
    conn = _connect()
    conn.execute("BEGIN IMMEDIATE;")
    try:
        yield conn
        conn.execute("COMMIT;")
    except Exception:
        conn.execute("ROLLBACK;")
        raise
    finally:
        conn.close()


def read_conn():
    return _connect()


# ---------- idempotency ----------

def get_message(conn, principal: str, message_id: str):
    row = conn.execute(
        "SELECT * FROM messages WHERE principal=? AND message_id=?",
        (principal, message_id),
    ).fetchone()
    return dict(row) if row else None


def save_message(conn, principal, message_id, content_hash, task_id, response_obj):
    conn.execute(
        "INSERT INTO messages (principal, message_id, content_hash, task_id, response, created_at) "
        "VALUES (?,?,?,?,?,?)",
        (principal, message_id, content_hash, task_id, json.dumps(response_obj), time.time()),
    )


# ---------- tasks ----------

def get_task(conn, task_id: str):
    row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    return dict(row) if row else None


def list_tasks(conn, principal: str):
    rows = conn.execute(
        "SELECT * FROM tasks WHERE principal=? ORDER BY created_at DESC", (principal,)
    ).fetchall()
    return [dict(r) for r in rows]


def create_task(conn, task_id, context_id, principal, batch_id, state, history, proposals):
    now = time.time()
    conn.execute(
        "INSERT INTO tasks (id, context_id, principal, batch_id, state, history, proposals, receipts, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (task_id, context_id, principal, batch_id, state, json.dumps(history),
         json.dumps(proposals), None, now, now),
    )


def update_task(conn, task_id, **fields):
    """Only mutates a task if it is still non-terminal (state check happens
    at the call site inside the same write_txn, so this is safe under the
    write lock)."""
    sets = ", ".join(f"{k}=?" for k in fields)
    values = list(fields.values()) + [time.time(), task_id]
    conn.execute(f"UPDATE tasks SET {sets}, updated_at=? WHERE id=?", values)


# ---------- proposals (per package, used to validate result continuations) ----------

def save_proposals(conn, task_id, proposals: list):
    for p in proposals:
        conn.execute(
            "INSERT INTO proposals (task_id, package_id, action_id, action, facts, evidence_refs, rationale) "
            "VALUES (?,?,?,?,?,?,?)",
            (task_id, p["packageId"], p["actionId"], p["action"],
             json.dumps(p["facts"]), json.dumps(p["evidenceRefs"]), p["rationale"]),
        )


def get_proposal(conn, task_id, package_id):
    row = conn.execute(
        "SELECT * FROM proposals WHERE task_id=? AND package_id=?", (task_id, package_id)
    ).fetchone()
    return dict(row) if row else None


# ---------- package decision cache (avoids repeat model calls for identical content) ----------

def get_cached_decision(conn, content_hash):
    row = conn.execute(
        "SELECT * FROM package_cache WHERE content_hash=?", (content_hash,)
    ).fetchone()
    return dict(row) if row else None


def save_cached_decision(conn, content_hash, action, facts, evidence_refs, rationale):
    conn.execute(
        "INSERT OR IGNORE INTO package_cache (content_hash, action, facts, evidence_refs, rationale) "
        "VALUES (?,?,?,?,?)",
        (content_hash, action, json.dumps(facts), json.dumps(evidence_refs), rationale),
    )


# ---------- receipt nonces (execute-once guarantee) ----------

def receipt_exists(conn, task_id, package_id):
    row = conn.execute(
        "SELECT 1 FROM receipt_nonces WHERE task_id=? AND package_id=?", (task_id, package_id)
    ).fetchone()
    return row is not None


def save_receipt_nonce(conn, task_id, package_id, nonce):
    conn.execute(
        "INSERT INTO receipt_nonces (task_id, package_id, receipt_nonce) VALUES (?,?,?)",
        (task_id, package_id, nonce),
    )
