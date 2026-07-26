# A2A Invoice Agent — prototype

A working implementation of the A2A 1.0 HTTP+JSON surface for an invoice
reconciliation agent: Agent Card discovery, batched AI-assisted action
proposals with cited evidence, a result-continuation gate before anything is
"executed", SQLite-backed task storage, message idempotency, per-package
decision caching, and the cancel-vs-receipt race handled via serialized
writes.

## Stack

- **FastAPI** (Python) for the HTTP surface — small, typed, easy to read.
- **SQLite** (stdlib `sqlite3`, WAL mode) for storage — no external DB to
  stand up for a prototype, and `BEGIN IMMEDIATE` transactions give you real
  write-serialization for the cancel/receipt race for free.
- **LLM layer is pluggable** (`llm.py`): if `ANTHROPIC_API_KEY` is set, it
  calls Claude directly over HTTPS (stdlib `urllib`, no SDK dependency) with
  one batched request for the whole package list. If no key is set, a
  deterministic keyword heuristic stands in, so you can run and demo the
  entire protocol with **zero cost and zero network calls**.

## Files

```
app.py        - FastAPI routes: agent card, message:send, tasks, cancel
storage.py    - SQLite layer: tasks, idempotency, package cache, receipts
schema.py     - constants, proposal validation, canonical-JSON hashing
llm.py        - batched AI decision step + heuristic fallback
test_core_logic.py - exercises storage/schema/llm without needing FastAPI installed
requirements.txt
```

## Run it

```bash
pip install -r requirements.txt

# Optional — real LLM reasoning instead of the keyword heuristic:
export ANTHROPIC_API_KEY=sk-ant-...

# Bearer tokens this deployment accepts, comma-separated (each = one principal):
export A2A_TOKENS=dev-token-1,dev-token-2

# The exact public base URL you'll submit — must match supportedInterfaces:
export A2A_BASE_URL=https://your-host.example/a2a/

uvicorn app:app --host 0.0.0.0 --port 8000
```

Sanity-check the logic layer alone (no FastAPI needed) with:

```bash
python3 test_core_logic.py
```

### 1. Discovery (public, no auth)

```bash
curl http://localhost:8000/.well-known/agent-card.json
```

### 2. Send a batch

```bash
curl -sX POST http://localhost:8000/a2a/message:send \
  -H "Authorization: Bearer dev-token-1" \
  -H "A2A-Version: 1.0" \
  -H "Content-Type: application/a2a+json" \
  -d '{
    "message": {
      "messageId": "m-001",
      "role": "ROLE_USER",
      "parts": [{
        "mediaType": "application/vnd.ga5.invoice-claim-batch+json",
        "data": {
          "batchId": "batch-1",
          "policyRevision": "rev-3",
          "packages": [
            {"packageId": "pkg-1",
             "content": "[E1] Vendor: Acme Supplies. Invoice Number: INV-9001. [E2] Amount due: INR 12500.00. [E3] This invoice was already paid on 3 June per receipt R-771; do not pay again."}
          ]
        }
      }]
    },
    "configuration": {"returnImmediately": false, "historyLength": 20,
      "acceptedOutputModes": ["application/vnd.ga5.invoice-action-proposals+json","application/vnd.ga5.invoice-action-receipts+json"]}
  }'
```

The response is `{"task": {...}}` in `TASK_STATE_INPUT_REQUIRED` with one
`invoice-action-proposals+json` artifact part. Grab `task.id` and
`task.contextId` from the response for the next call.

### 3. Send the grader's result back on the same task

```bash
curl -sX POST http://localhost:8000/a2a/message:send \
  -H "Authorization: Bearer dev-token-1" \
  -H "A2A-Version: 1.0" \
  -H "Content-Type: application/a2a+json" \
  -d '{
    "message": {
      "messageId": "m-002",
      "taskId": "<task id from step 2>",
      "contextId": "<context id from step 2>",
      "role": "ROLE_USER",
      "parts": [{
        "mediaType": "application/vnd.ga5.invoice-action-results+json",
        "data": {"batchId": "batch-1", "results": [
          {"packageId": "pkg-1", "actionId": "<actionId from the proposal>",
           "action": "reject_duplicate", "outcome": "ACCEPTED", "receiptNonce": "nonce-xyz"}
        ]}
      }]
    }
  }'
```

Task flips to `TASK_STATE_COMPLETED` with a second artifact part
(`invoice-action-receipts+json`) containing only the accepted execution.

### 4. Read / list / cancel

```bash
curl -H "Authorization: Bearer dev-token-1" -H "A2A-Version: 1.0" \
  http://localhost:8000/a2a/tasks/<task id>

curl -H "Authorization: Bearer dev-token-1" -H "A2A-Version: 1.0" \
  http://localhost:8000/a2a/tasks

curl -X POST -H "Authorization: Bearer dev-token-1" -H "A2A-Version: 1.0" \
  http://localhost:8000/a2a/tasks/<task id>:cancel
```

## How each spec requirement is met

- **Agent Card / auth / version / media type gates** — `require_auth` and
  `require_protocol_headers` in `app.py` run before any route logic; the card
  itself is served unauthenticated.
- **Package understanding + evidence** — `llm.py` batches every package that
  isn't already in `package_cache` into one model call, asking for typed
  JSON with cited bracket references; `schema.validate_proposal` rejects
  anything that doesn't fit the shape (unknown action, missing facts, short
  rationale, duplicate IDs) before it's ever stored.
- **Proposal ≠ permission** — the batch handler only ever writes a task in
  `TASK_STATE_INPUT_REQUIRED` with a proposals artifact; the receipts
  artifact is only added inside `_handle_results`, and only for entries with
  `outcome == "ACCEPTED"`.
- **Exactly-once / idempotency** — `messages` table keys on
  `(principal, messageId)`; `content_hash` is a canonical (recursively
  key-sorted, compact) JSON hash of the `message` object only, so replays and
  reordered-key/`returnImmediately`-only changes return the stored response,
  while genuine content changes on a reused ID get `409 IDEMPOTENCY_CONFLICT`.
- **Result-continuation matching** — `_handle_results` checks principal,
  taskId, contextId, batchId, and per-package `actionId`/`action` against the
  stored proposal before accepting anything, and rejects malformed or
  mismatched continuations with `409`.
- **Cancel-vs-receipt race** — both `cancel_task` and `_handle_results` run
  inside `storage.write_txn()`, which issues `BEGIN IMMEDIATE`. SQLite
  serializes concurrent writers on that lock, so whichever request commits
  first wins and the other observes the now-terminal state and returns `409`.
- **User isolation** — every task read/write filters on
  `task["principal"] == principal`; a mismatch returns a generic `404`
  (never a 403 that would confirm the task exists), and `GET /tasks` only
  ever queries rows for the caller's own principal.
- **No repeat model calls** — `package_cache` is keyed by a content hash of
  the package itself, not by batch/message/task ID, so identical packages
  reused across "Check" and "Save" deliveries (or replayed messages) never
  re-invoke the model.

## Known simplifications / what I'd extend first

1. **Package text extraction is heuristic** (`llm._package_text`) since the
   exact wire shape of a "package" wasn't fully specified. First thing to
   harden: pin down the real package schema you're given and parse it
   directly instead of guessing at `content`/`text`/`documents` keys.
2. **Evidence-ref precision.** Right now nothing checks that the *exact*
   three decisive bracket refs (excluding cover-sheet/example refs) were
   picked — it trusts the model/heuristic. Add a second validation pass that
   diffs `evidenceRefs` against a known-good ref extraction per package type.
3. **Auth is a static token list.** Swap `A2A_TOKENS` for a real identity
   provider (JWT/OAuth) before this touches anything real.
4. **SQLite → Postgres** if you need multi-instance deployment; the
   `write_txn` pattern maps directly to `SELECT ... FOR UPDATE` there.
5. **Response size guard.** Nothing currently enforces the 512 KiB cap on
   very large batches — add a check in `task_to_wire` that trims/report
   errors before serialization.
6. **Structured logging + request tracing**, so a failed grader run is
   reproducible without re-reading raw SQLite rows.
