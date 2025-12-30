"""Enhanced audit log service (NEW file).

Strategy summary:
- Build an in-memory, read-only index of the small metadata table (50k rows) ordered
  by the required sort (created_at DESC, id DESC). This removes per-request
  OFFSET scans and per-request SQLite connection churn.
- Preserve semantics exactly: same filtering, ordering, LIMIT/OFFSET behavior,
  and perform a disk seek + read + json.loads() for *each* returned payload.
- Thread-safe, lock-protected one-time initialization. No payload caching.

Behavioral contract:
- Exposes `init()` and `handle_request()` to mirror the baseline API so the
  existing `runner.py` can benchmark it directly.
- Adds `handle_request_with_query(query_dict)` to allow deterministic
  semantic comparison against the baseline (baseline returns the query it used).

Restrictions: Python stdlib only, baseline files are NOT modified.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

# Reuse baseline constants (paths) for determinism
import audit_log_service as baseline

DB_PATH = "audit.db"
PAYLOAD_FILE = "payloads.jsonl"

_init_lock = threading.Lock()
_inited = False
_index: Optional[Tuple[Tuple[int, ...], ...]] = None


def init() -> None:
    """Ensure DB + payloads exist (delegates seeding to baseline) and build index.

    The index is a read-only tuple-of-tuples stored in module scope for
    lock-free reads after initialization. Additionally build:
      - _created_at_neg: ascending array for fast bisect on the descending sort
      - _positions_by_actor / _positions_by_action: inverted position maps

    These auxiliary structures allow selecting the OFFSET/LIMIT page in
    O(log N + page_size) instead of scanning the entire index on every
    request.
    """
    global _inited, _index, _created_at_neg, _positions_by_actor, _positions_by_action
    if _inited:
        return
    with _init_lock:
        if _inited:
            return
        # Ensure baseline has seeded DB/payload file
        baseline.init()

        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        cur = conn.cursor()
        # Pull the full metadata in the exact ORDER the API requires so slicing
        # in memory matches OFFSET semantics from the DB.
        cur.execute(
            """
            SELECT id, created_at, actor_id, action, resource_type, resource_id,
                   payload_offset, payload_len
            FROM audit_events
            ORDER BY created_at DESC, id DESC
            """
        )
        rows = cur.fetchall()
        conn.close()

        # Immutable primary index (position -> row)
        _index = tuple(tuple(r) for r in rows)

        # Auxiliary structures for fast filtering / pagination
        # created_at is DESC in _index; store negatives to get an ASC array
        _created_at_neg = tuple(-r[1] for r in _index)

        # Inverted position maps (actor_id/action -> tuple(positions...))
        _positions_by_actor = {}
        _positions_by_action = {}
        for pos, row in enumerate(_index):
            actor = row[2]
            act = row[3]
            _positions_by_actor.setdefault(actor, []).append(pos)
            _positions_by_action.setdefault(act, []).append(pos)

        # freeze the lists to tuples for thread-safety
        _positions_by_actor = {k: tuple(v) for k, v in _positions_by_actor.items()}
        _positions_by_action = {k: tuple(v) for k, v in _positions_by_action.items()}

        _inited = True


def _read_payload(offset: int, length: int) -> Any:
    # payloads MUST be read from disk for each returned event (semantics).
    with open(PAYLOAD_FILE, "rb") as f:
        f.seek(offset)
        return json.loads(f.read(length))


def _matches(row: Sequence, from_ts: int, to_ts: int, actor_id: Optional[int], action: Optional[str]) -> bool:
    """Apply the same WHERE predicate as the baseline SQL to a row tuple.

    Row tuple layout: (id, created_at, actor_id, action, resource_type, resource_id, payload_offset, payload_len)
    """
    _, created_at, a_id, act, *_ = row
    if not (from_ts <= created_at <= to_ts):
        return False
    if actor_id is not None and a_id != actor_id:
        return False
    if action is not None and act != action:
        return False
    return True


def handle_request_with_query(q: Dict[str, Any]) -> Dict[str, Any]:
    """Deterministic handler that executes the given query dict against the
    in-memory index and returns results that are semantically equivalent to
    the baseline implementation.

    Uses bisect on the created_at axis + inverted position maps to locate the
    page without scanning the full index.
    """
    import bisect

    assert _inited and _index is not None, "enhanced_audit_log_service.init() must be called first"

    from_ts = int(q["from_ts"])
    to_ts = int(q["to_ts"])
    actor_id = q.get("actor_id")
    action = q.get("action")
    page = int(q["page"])
    page_size = int(q["page_size"])

    offset_rows = (page - 1) * page_size

    # created_at is stored as negatives in _created_at_neg (ascending)
    left = bisect.bisect_left(_created_at_neg, -to_ts)
    right = bisect.bisect_right(_created_at_neg, -from_ts)
    if left >= right:
        return {"query": q, "events": [], "count": 0}

    # Fast-path: no actor/action filters -> direct range slice
    positions = None
    if actor_id is None and action is None:
        total = right - left
        if offset_rows >= total:
            return {"query": q, "events": [], "count": 0}
        start = left + offset_rows
        end = min(start + page_size, right)
        positions = range(start, end)
    else:
        # One or both filters present: obtain position-subranges from inverted maps
        def slice_pos_list(pos_list):
            a = bisect.bisect_left(pos_list, left)
            b = bisect.bisect_left(pos_list, right)
            return pos_list[a:b]

        pos_a = None
        if actor_id is not None:
            pos_a = _positions_by_actor.get(actor_id, ())
            pos_a = slice_pos_list(pos_a)
        pos_b = None
        if action is not None:
            pos_b = _positions_by_action.get(action, ())
            pos_b = slice_pos_list(pos_b)

        # Combine the constraints (intersection when both present)
        if pos_a is not None and pos_b is not None:
            # intersect two sorted sequences, but stop once we've advanced
            # past the requested offset + page_size to limit work
            need = offset_rows + page_size
            ia = ib = 0
            intersect = []
            while ia < len(pos_a) and ib < len(pos_b) and len(intersect) < need:
                va = pos_a[ia]
                vb = pos_b[ib]
                if va == vb:
                    intersect.append(va)
                    ia += 1
                    ib += 1
                elif va < vb:
                    ia += 1
                else:
                    ib += 1
            positions = tuple(intersect)
        elif pos_a is not None:
            positions = pos_a
        else:
            positions = pos_b

        # Apply offset / limit
        if offset_rows >= len(positions):
            return {"query": q, "events": [], "count": 0}
        positions = positions[offset_rows : offset_rows + page_size]

    events = []
    # materialize events by reading payloads from disk (contract)
    for pos in positions:
        row = _index[pos]
        (
            eid,
            created_at,
            actor_id,
            action,
            rtype,
            rid,
            poff,
            plen,
        ) = row
        payload = _read_payload(poff, plen)
        events.append(
            {
                "id": eid,
                "created_at": created_at,
                "actor_id": actor_id,
                "action": action,
                "resource_type": rtype,
                "resource_id": rid,
                "payload": payload,
            }
        )

    return {"query": q, "events": events, "count": len(events)}


def handle_request() -> Dict[str, Any]:
    """Pick a query using the baseline's picker to keep request distribution
    equivalent during benchmarking, then execute it against the in-memory
    index.
    """
    # Use the baseline's picker so the distribution of generated queries
    # matches exactly what the baseline produces.
    q = baseline._pick_query()
    return handle_request_with_query(q.__dict__)
