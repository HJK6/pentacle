"""Extract reviewed, exact-test occurrences from scorecard first attempts.

This is deliberately a read-only projection. Retries and bare suite names are
not evidence that a particular test is safe to remove from the blocking tier.
"""
from __future__ import annotations

from collections import defaultdict
import re

from .reliability_receipts import instant


def candidates(rows, test_index, boundaries=None):
    """Return distinct reviewed occurrences keyed by exact collected test ID.

    ``test_index`` maps classifier identities to unambiguous collected IDs.
    A restoration boundary excludes every earlier occurrence of that test.
    """
    boundaries = boundaries or {}
    result = defaultdict(dict)
    for row in rows:
        if (row.get("first") != "failure" or row.get("missing")
                or not re.fullmatch(r"[0-9]+", str(row.get("id", "")))):
            continue
        if not re.fullmatch(r"[a-f0-9]{40}", str(row.get("sha", ""))):
            continue
        try:
            created = instant(row["created"])
        except (KeyError, ValueError, TypeError, AttributeError):
            continue
        def resolve(cause):
            found = test_index.get((str(row['id']), cause.get('job'), cause.get('step'), cause.get('test')))
            if found is None:
                found = test_index.get(cause.get('test'))
            return found.get('test') if isinstance(found, dict) else found
        grouped = defaultdict(list)
        for cause in row.get("causes", []):
            test = resolve(cause)
            if isinstance(test, str) and "::" in test:
                grouped[test].append(cause)
        for test, causes in grouped.items():
            if test in boundaries and created <= instant(boundaries[test]):
                continue
            if any(c.get("kind") != "non_code" for c in causes):
                continue
            for cause in causes:
                if any(other.get("kind") != "non_code" and not resolve(other)
                       and other.get("job") == cause.get("job") and other.get("step") == cause.get("step")
                       for other in row.get("causes", [])):
                    continue
                signature = cause.get("assertion_digest") or cause.get("cause")
                if (not signature or not all(cause.get(k) for k in ("reviewer", "reason", "revision", "evidence", "job", "step"))
                        or not re.fullmatch(r"[a-f0-9]{40}", str(cause["revision"]))):
                    continue
                matching = [f for f in row.get("failures", [])
                            if f.get("job") == cause["job"] and f.get("step") == cause["step"]]
                if len(matching) != 1 or not matching[0].get("runner"):
                    continue
                resolved = test_index.get((str(row['id']), cause['job'], cause['step'], cause.get('test')))
                phase = resolved.get('phase') if isinstance(resolved, dict) else None
                occurrence = {
                    "id": f"public-checks/{row['id']}/1", "created": row["created"],
                    "sha": row["sha"], "source": cause["evidence"],
                    "signature": signature, "job": cause["job"], "step": cause["step"],
                    "runner": matching[0]["runner"], "reviewer": cause["reviewer"],
                    "reason": cause["reason"], "revision": cause["revision"],
                    "later_attempts": row.get("later_attempts", []),
                }
                if phase:
                    occurrence['fixture_phase'] = phase
                    occurrence['signature'] = phase + ':' + signature
                if isinstance(resolved, dict) and 'parents' in resolved:
                    occurrence['parents'] = resolved['parents']
                previous = result[test].get(occurrence["id"])
                if previous is not None and previous != occurrence:
                    raise ValueError("conflicting first-attempt occurrence " + occurrence["id"])
                result[test][occurrence["id"]] = occurrence
    return {test: sorted(items.values(), key=lambda x: (instant(x["created"]), x["id"]))
            for test, items in result.items()}


def merge_seed_occurrences(register, observed, active_tests):
    """Adopt existing active decisions without creating or recounting them."""
    merged = {test: {row["id"]: row for row in rows} for test, rows in observed.items()}
    seen = set()
    for record in register.get("records", []):
        test = record.get("test")
        if test not in active_tests:
            continue
        if test in seen or not record.get("item"):
            raise ValueError("ambiguous existing quarantine decision")
        seen.add(test)
        target = merged.setdefault(test, {})
        for occurrence in record.get("occurrences", []):
            match = re.fullmatch(r"public-checks/([0-9]+)/1", occurrence)
            if not match:
                raise ValueError("invalid existing quarantine occurrence")
            target.setdefault(occurrence, {"id": occurrence,
                "source": f"https://github.com/HJK6/pentacle/actions/runs/{match[1]}/attempts/1"})
    return {test: [rows[key] for key in sorted(rows)] for test, rows in merged.items()}
