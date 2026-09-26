"""Deterministic hashing and stable ID generators for SQL Audit.

- Stable finding_id: Identifies the architectural/catalog issue on a specific database object.
  Does NOT change when runtime counters or metric values fluctuate.
- Evidence ID: Deterministic SHA-256 hash representing the exact observed runtime values.
  Changes whenever runtime counters change.
"""

import hashlib
import json

from pydantic import JsonValue


def compute_stable_finding_id(
    check_code: str,
    object_type: str,
    object_name: str,
    sub_object: str | None = None,
) -> str:
    """Computes a stable, counter-independent identity for a finding.

    Format:
        {CHECK_CODE}:{object_name} or {CHECK_CODE}:{object_name}:{sub_object}
    """
    clean_code = check_code.strip().upper()
    clean_obj = object_name.strip()
    if sub_object:
        clean_sub = sub_object.strip()
        return f"{clean_code}:{clean_obj}:{clean_sub}"
    return f"{clean_code}:{clean_obj}"


def compute_evidence_id(
    source: str,
    query_name: str,
    values: dict[str, JsonValue],
) -> str:
    """Computes a deterministic SHA-256 digest of observed runtime evidence."""
    payload = {
        "source": source,
        "query_name": query_name,
        "values": values,
    }
    encoded = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()
    return f"sha256:{digest}"
