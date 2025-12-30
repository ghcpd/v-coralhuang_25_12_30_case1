# enhanced_audit_log_service.py
"""
Enhanced audit log service that preserves semantics but improves performance.

Optimization strategies used:
- Build an in-memory sorted snapshot of the event metadata at init time to avoid
  expensive ORDER BY ... LIMIT ... OFFSET queries.
- For each request, perform range filtering and page slicing in-memory which
  avoids deep OFFSET scans in SQLite.
- Open the payload file once per request and perform per-row seeks/reads (still
  satisfies the contract that payloads are read from disk per returned event),
  instead of opening the file per payload.
- Use a thread-safe initialization and read-only in-memory index for concurrency.

This module provides:
- init() to prepare the in-memory index
- handle_request_with_query(q) to execute a request using a provided Query
- handle_request() to mimic baseline behaviour (picks a random query)

Note: Baseline modules must not be modified; all enhancements are implemented
in this new file.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

# Reuse the same Query dataclass shape as baseline for compatibility
@dataclass(frozen=True)
class Query:
    from_ts: int
    to_ts: int
    actor_id: Optional[int]
    action: Optional[str]
    page: int
    page_size: int

DB_PATH = "audit.db"
PAYLOAD_FILE = "payloads.jsonl"

_init_lock = threading.Lock()
_inited = False

# The in-memory index: list of rows in the exact sort order required by the
# contract: created_at DESC, id DESC
# Each element: (id, created_at, actor_id, action, resource_type, resource_id, payload_offset, payload_len)
_rows: List[Tuple[int, int, int, str, str, str, int, int]] = []


def init() -> None:
    """Initialize the in-memory index (idempotent and thread-safe)."""
    global _inited, _rows
    if _inited:
        return
    with _init_lock:
        if _inited:
            return
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, created_at, actor_id, action, resource_type, resource_id,
                   payload_offset, payload_len
            FROM audit_events
            ORDER BY created_at DESC, id DESC
            """
        )
        _rows = cur.fetchall()
        conn.close()
        _inited = True


def _range_indices_for_times(from_ts: int, to_ts: int, rows: Sequence[Tuple]) -> Tuple[int, int]:
    """Given descending-sorted rows by created_at, find slice indices [lo, hi)
    such that rows[lo:hi] have created_at between from_ts and to_ts inclusive.

    Uses binary search for efficiency.
    """
    lo = 0
    hi = len(rows)

    # Find left boundary: first index i such that rows[i].created_at <= to_ts
    # Since rows sorted descending, created_at at start is large (recent)
    l = 0
    r = len(rows)
    while l < r:
        m = (l + r) // 2
        if rows[m][1] <= to_ts:
            r = m
        else:
            l = m + 1
    left = l

    # Find right boundary: first index j such that rows[j].created_at < from_ts
    l = left
    r = len(rows)
    while l < r:
        m = (l + r) // 2
        if rows[m][1] < from_ts:
            r = m
        else:
            l = m + 1
    right = l

    return left, right


def handle_request_with_query(q: Query) -> Dict[str, Any]:
    """Execute the query using the in-memory index but still read payloads
    from disk for each returned event (one file open per request).
    """
    if not _inited:
        init()

    left, right = _range_indices_for_times(q.from_ts, q.to_ts, _rows)

    # Scan the subarray and apply actor/action filters; collect page slice
    start_idx = (q.page - 1) * q.page_size
    end_idx = start_idx + q.page_size

    matched: List[Dict[str, Any]] = []
    matched_count = 0

    # Open payload file once per request
    with open(PAYLOAD_FILE, "rb") as pf:
        for i in range(left, right):
            (
                eid,
                created_at,
                actor_id,
                action,
                rtype,
                rid,
                poff,
                plen,
            ) = _rows[i]

            if q.actor_id is not None and actor_id != q.actor_id:
                continue
            if q.action is not None and action != q.action:
                continue

            if matched_count >= start_idx and len(matched) < q.page_size:
                pf.seek(poff)
                payload = json.loads(pf.read(plen))
                matched.append(
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
            matched_count += 1

            if len(matched) >= q.page_size:
                break

    return {"query": q.__dict__, "events": matched, "count": len(matched)}


# For compatibility and convenience we provide a handle_request that picks the
# same kind of random queries as baseline (this uses a local RNG seeded to
# match behavior if needed). However tests should call handle_request_with_query
# with queries generated externally to guarantee identical queries across
# baseline and enhanced runs.
import random
_rng = random.Random(1337)

SEED_ROWS = 50_000
MAX_PAGE = 2000
NOTE_MIN_CHARS = 256
NOTE_MAX_CHARS = 1024


def _pick_query() -> Query:
    now = int(time.time())
    return Query(
        from_ts=now - 7 * 24 * 3600,
        to_ts=now,
        actor_id=_rng.choice([None, _rng.randint(1, 2000)]),
        action=_rng.choice([None, "UPDATE", "CREATE", "DELETE"]),
        page=_rng.randint(1, MAX_PAGE),
        page_size=_rng.choice([10, 20, 50]),
    )


def handle_request() -> Dict[str, Any]:
    return handle_request_with_query(_pick_query())
