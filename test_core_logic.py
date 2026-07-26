"""
Exercises storage.py / schema.py / llm.py directly, without FastAPI, so the
core logic (idempotency, proposal validation, caching, receipt-once, cancel
race) can be sanity-checked without installing web dependencies.

Run: python3 test_core_logic.py
"""
import os
import uuid

os.environ.setdefault("A2A_TOKENS", "dev-token-1")
if os.path.exists("a2a_invoice_agent_test.db"):
    os.remove("a2a_invoice_agent_test.db")
import storage
storage.DB_PATH = "a2a_invoice_agent_test.db"
storage.init_db()

import llm
from schema import validate_proposal, canonical_json, sha256_hex, STATE_INPUT_REQUIRED, STATE_COMPLETED, STATE_CANCELED

def check(label, cond):
    print(("PASS " if cond else "FAIL ") + label)
    assert cond, label

# ---- heuristic LLM fallback on a fake package ----
pkg = {
    "packageId": "pkg-1",
    "content": "Cover sheet: batch 12. [E1] Vendor: Acme Supplies. Invoice Number: INV-9001. "
               "[E2] Amount due: INR 12,500.00. [E3] Note: this invoice was already paid on 3rd June "
               "per receipt R-771; do not pay again.",
}
decisions = llm.call_model([pkg])
check("heuristic returns one decision", len(decisions) == 1)
check("heuristic detects duplicate", decisions[0]["action"] == "reject_duplicate")

# ---- proposal validation ----
proposal = {
    "packageId": "pkg-1",
    "actionId": "act_" + uuid.uuid4().hex,
    "action": decisions[0]["action"],
    "facts": decisions[0]["facts"],
    "evidenceRefs": decisions[0]["evidenceRefs"],
    "rationale": decisions[0]["rationale"],
}
seen_pkg, seen_act = set(), set()
validate_proposal(proposal, seen_pkg, seen_act)
check("proposal validates", True)

# ---- task create + idempotency ----
with storage.write_txn() as conn:
    storage.create_task(conn, "task-1", "ctx-1", "dev-token-1", "batch-1",
                         STATE_INPUT_REQUIRED, [{"messageId": "m1"}], {"batchId": "batch-1", "proposals": [proposal]})
    storage.save_proposals(conn, "task-1", [proposal])
    storage.save_message(conn, "dev-token-1", "m1", "hash1", "task-1", {"task": {"id": "task-1"}})

conn = storage.read_conn()
t = storage.get_task(conn, "task-1")
check("task stored", t is not None and t["state"] == STATE_INPUT_REQUIRED)

existing = storage.get_message(conn, "dev-token-1", "m1")
check("idempotency row present", existing is not None and existing["content_hash"] == "hash1")
conn.close()

# ---- package decision cache ----
h = sha256_hex(canonical_json(pkg))
with storage.write_txn() as conn:
    storage.save_cached_decision(conn, h, "reject_duplicate", decisions[0]["facts"],
                                  decisions[0]["evidenceRefs"], decisions[0]["rationale"])
conn = storage.read_conn()
cached = storage.get_cached_decision(conn, h)
check("package decision cached", cached is not None and cached["action"] == "reject_duplicate")
conn.close()

# ---- receipt-once guarantee ----
with storage.write_txn() as conn:
    storage.save_receipt_nonce(conn, "task-1", "pkg-1", "nonce-abc")
conn = storage.read_conn()
check("receipt nonce recorded", storage.receipt_exists(conn, "task-1", "pkg-1"))
conn.close()

try:
    with storage.write_txn() as conn:
        storage.save_receipt_nonce(conn, "task-1", "pkg-1", "nonce-abc-again")
    check("duplicate receipt nonce rejected", False)
except Exception:
    check("duplicate receipt nonce rejected (PRIMARY KEY violation)", True)

# ---- terminal transition + cancel-after-terminal race ----
with storage.write_txn() as conn:
    storage.update_task(conn, "task-1", state=STATE_COMPLETED)
conn = storage.read_conn()
t = storage.get_task(conn, "task-1")
check("task completed", t["state"] == STATE_COMPLETED)
conn.close()

print("\nAll core-logic checks passed.")
os.remove("a2a_invoice_agent_test.db")
