"""
AI decision layer — kept fully separate from the protocol/storage layers so a
retry, poll, or replay never re-invokes the model (see storage.package_cache).

Set ANTHROPIC_API_KEY to use Claude for real reasoning. With no key set, a
deterministic keyword heuristic stands in so the whole system is runnable and
demoable with zero cost / zero network access. Swap in any other provider by
rewriting `call_model()` only — nothing else in the app needs to change.
"""

import json
import os
import re

from schema import ACTIONS

SYSTEM_PROMPT = """You are an invoice-reconciliation agent. For each invoice \
package you receive, choose exactly one action from this fixed set:

- settle_invoice: valid, reconciled, and within autonomous authority.
- request_approval: commercially valid, but outside delegated (autonomous) authority.
- hold_invoice: payment must pause until a stated verification completes.
- reject_duplicate: the same commercial invoice was already paid.
- open_exception: material records conflict and need an exception workflow.

The package text mixes decisive facts with irrelevant cover-sheet notes, \
stale historical examples, negated statements, and decoy keywords. Only cite \
the bracketed reference tags (e.g. "[E3]") that come from the paragraph that \
actually determines the action — never a cover-sheet tag or an example tag.

Return ONLY a JSON array (no prose, no markdown fences). One object per \
package, in this exact shape:

[{
  "packageId": "<echo the packageId>",
  "action": "<one of the five actions above>",
  "facts": {"vendorName": "...", "invoiceNumber": "...", "amountMinor": <int minor units>, "currency": "..."},
  "evidenceRefs": ["[Ex]", "[Ey]"],
  "rationale": "60-1500 chars, names the action and cites at least two evidence refs"
}]
"""


def _package_text(pkg: dict) -> str:
    """Packages may arrive with various shapes; flatten anything textual so
    both the model and the heuristic fallback can read it."""
    for key in ("content", "text", "documentText", "body"):
        if isinstance(pkg.get(key), str):
            return pkg[key]
    if isinstance(pkg.get("documents"), list):
        parts = []
        for d in pkg["documents"]:
            if isinstance(d, str):
                parts.append(d)
            elif isinstance(d, dict):
                parts.append(json.dumps(d, ensure_ascii=False))
        return "\n".join(parts)
    # last resort: the whole package as text, minus the id
    rest = {k: v for k, v in pkg.items() if k != "packageId"}
    return json.dumps(rest, ensure_ascii=False)


def call_model(packages: list) -> list:
    """Returns a list of raw decision dicts (pre-validation), one per package,
    via whichever provider is configured. Never called for a package whose
    content hash is already cached — see agent.py."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if api_key:
        return _call_anthropic(packages, api_key)
    return _heuristic_batch(packages)


def _call_anthropic(packages, api_key):
    import urllib.request

    user_content = "Packages:\n\n" + "\n\n---\n\n".join(
        f"packageId: {p.get('packageId')}\n{_package_text(p)}" for p in packages
    )
    body = json.dumps(
        {
            "model": "claude-sonnet-4-6",
            "max_tokens": 4000,
            "system": SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": user_content}],
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=body,
        headers={
            "content-type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=40) as resp:
        data = json.loads(resp.read())
    text = "".join(b["text"] for b in data["content"] if b.get("type") == "text")
    text = re.sub(r"^```(json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
    return json.loads(text)


# ---------- zero-dependency heuristic fallback ----------

_KEYWORD_RULES = [
    ("already paid", "reject_duplicate"),
    ("already been paid", "reject_duplicate"),
    ("duplicate", "reject_duplicate"),
    ("pending verification", "hold_invoice"),
    ("verification of", "hold_invoice"),
    ("hold payment", "hold_invoice"),
    ("exceeds", "request_approval"),
    ("outside delegated authority", "request_approval"),
    ("requires approval", "request_approval"),
    ("discrepancy", "open_exception"),
    ("conflict", "open_exception"),
    ("mismatch", "open_exception"),
]


def _heuristic_batch(packages):
    out = []
    for pkg in packages:
        text = _package_text(pkg)
        lower = text.lower()
        action = "settle_invoice"
        for kw, act in _KEYWORD_RULES:
            if kw in lower:
                action = act
                break

        refs = re.findall(r"\[[A-Za-z]?\d+\]", text)
        # crude "decisive paragraph" pick: the paragraph containing the
        # keyword that fired, else the first paragraph with any ref
        evidence = refs[:2] if refs else ["[E1]"]

        vendor_m = re.search(r"[Vv]endor[:\s]+([A-Za-z0-9 &.,'-]{2,40})", text)
        inv_m = re.search(r"[Ii]nvoice\s*(?:No\.?|Number)?[:\s#]+([A-Za-z0-9-]{2,20})", text)
        amt_m = re.search(r"(?:INR|Rs\.?|₹)\s?([\d,]+(?:\.\d{1,2})?)", text)
        cur_m = "INR"

        amount_minor = 0
        if amt_m:
            try:
                amount_minor = int(round(float(amt_m.group(1).replace(",", "")) * 100))
            except ValueError:
                amount_minor = 0

        rationale = (
            f"Heuristic fallback (no ANTHROPIC_API_KEY set) selected '{action}' "
            f"based on keyword match against the package text, citing "
            f"{', '.join(evidence)} as the decisive references for package "
            f"{pkg.get('packageId')}. Replace with a real model call for production use."
        )

        out.append(
            {
                "packageId": pkg.get("packageId"),
                "action": action if action in ACTIONS else "open_exception",
                "facts": {
                    "vendorName": vendor_m.group(1).strip() if vendor_m else "UNKNOWN_VENDOR",
                    "invoiceNumber": inv_m.group(1) if inv_m else "UNKNOWN_INVOICE",
                    "amountMinor": amount_minor,
                    "currency": cur_m,
                },
                "evidenceRefs": evidence,
                "rationale": rationale,
            }
        )
    return out
