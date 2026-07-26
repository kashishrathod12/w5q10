"""
Shared constants + lightweight validators for the A2A Invoice Agent.
No pydantic dependency required, so this file has zero external imports.
"""

ACTIONS = {
    "settle_invoice",
    "request_approval",
    "hold_invoice",
    "reject_duplicate",
    "open_exception",
}

STATE_SUBMITTED = "TASK_STATE_SUBMITTED"
STATE_WORKING = "TASK_STATE_WORKING"
STATE_INPUT_REQUIRED = "TASK_STATE_INPUT_REQUIRED"
STATE_COMPLETED = "TASK_STATE_COMPLETED"
STATE_CANCELED = "TASK_STATE_CANCELED"

MEDIA_BATCH = "application/vnd.ga5.invoice-claim-batch+json"
MEDIA_PROPOSALS = "application/vnd.ga5.invoice-action-proposals+json"
MEDIA_RESULTS = "application/vnd.ga5.invoice-action-results+json"
MEDIA_RECEIPTS = "application/vnd.ga5.invoice-action-receipts+json"

A2A_VERSION = "1.0"
A2A_CONTENT_TYPE = "application/a2a+json"

MAX_RESPONSE_BYTES = 512 * 1024


class ValidationError(Exception):
    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


def validate_proposal(p: dict, seen_package_ids: set, seen_action_ids: set) -> None:
    """Raises ValidationError if a single proposal object is malformed."""
    for field in ("packageId", "actionId", "action", "facts", "evidenceRefs", "rationale"):
        if field not in p:
            raise ValidationError("PROPOSAL_SHAPE", f"missing field '{field}'")

    if p["packageId"] in seen_package_ids:
        raise ValidationError("DUPLICATE_PACKAGE_ID", p["packageId"])
    if p["actionId"] in seen_action_ids:
        raise ValidationError("DUPLICATE_ACTION_ID", p["actionId"])
    if not isinstance(p["actionId"], str) or len(p["actionId"]) < 12:
        raise ValidationError("ACTION_ID_TOO_SHORT", p["actionId"])
    if p["action"] not in ACTIONS:
        raise ValidationError("UNKNOWN_ACTION", p["action"])

    facts = p["facts"]
    for f in ("vendorName", "invoiceNumber", "amountMinor", "currency"):
        if f not in facts:
            raise ValidationError("FACTS_SHAPE", f"missing fact '{f}'")
    if not isinstance(facts["amountMinor"], int):
        raise ValidationError("FACTS_SHAPE", "amountMinor must be an integer (minor units)")

    if not isinstance(p["evidenceRefs"], list) or len(p["evidenceRefs"]) < 1:
        raise ValidationError("EVIDENCE_MISSING", p["packageId"])

    rationale = p["rationale"]
    if not isinstance(rationale, str) or not (60 <= len(rationale) <= 1500):
        raise ValidationError("RATIONALE_LENGTH", p["packageId"])

    seen_package_ids.add(p["packageId"])
    seen_action_ids.add(p["actionId"])


def canonical_json(obj) -> str:
    """Recursively key-sorted, compact JSON — used for idempotency hashing
    and for package-content cache keys."""
    import json

    def sort(o):
        if isinstance(o, dict):
            return {k: sort(o[k]) for k in sorted(o.keys())}
        if isinstance(o, list):
            return [sort(x) for x in o]
        return o

    return json.dumps(sort(obj), separators=(",", ":"), ensure_ascii=False)


def sha256_hex(s: str) -> str:
    import hashlib

    return hashlib.sha256(s.encode("utf-8")).hexdigest()
