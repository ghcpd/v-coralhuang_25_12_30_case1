"""
Enhanced audit log service (new file).

Strategy:
- Reuse a single persistent sqlite3.Connection to avoid per-request connection churn.
- Keep the payload file opened once and perform a seek+read per returned event (to preserve the "disk-backed payload read" semantic).
- Protect shared resources with threading.Lock() for concurrency safety.
- Preserve exact SQL semantics and ordering (created_at DESC, id DESC) and same returned fields.

No baseline files are modified.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional

# reuse constants from baseline by importing names where safe
import audit_log_service as baseline

DB_PATH = baseline.DB_PATH
PAYLOAD_FILE = baseline.PAYLOAD_FILE

# use thread-local resources to avoid global contention under concurrency
_thread_local = threading.local()

_init_lock = threading.Lock()
_inited = False


def _ensure_thread_resources() -> None:
    """Ensure each worker thread has its own sqlite connection and payload file handle."""
    if getattr(_thread_local, "conn", None) is None:
        # keep check_same_thread=False so connection can be created on one thread and used only by that thread
        _thread_local.conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    if getattr(_thread_local, "payload_f", None) is None:
        _thread_local.payload_f = open(PAYLOAD_FILE, "rb")


# in-memory metadata index to avoid OFFSET scans (rows: id, created_at, actor_id, action, resource_type, resource_id, payload_offset, payload_len)
_in_memory_index: List[tuple] = []


def init() -> None:
    """Initialize baseline data and build an in-memory metadata index.

    The in-memory index stores all table rows (except payload contents). Queries
    are served by filtering this index (fast, avoids OFFSET scans) and payloads
    are still read from disk per event to preserve semantics.
    """
    global _inited, _in_memory_index
    if _inited:
        return
    with _init_lock:
        if _inited:
            return
        baseline.init()

        # build the in-memory index once
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
        _in_memory_index = cur.fetchall()
        conn.close()

        _inited = True


def _read_payload(offset: int, length: int) -> Any:
    """Perform a seek+read for the calling thread's payload file handle.

    This preserves the contract that each returned event's payload is read from disk,
    while avoiding a shared seek lock across threads.
    """
    f = getattr(_thread_local, "payload_f", None)
    if f is None:
        _ensure_thread_resources()
        f = _thread_local.payload_f
    f.seek(offset)
    return json.loads(f.read(length))


def _get_conn():
    conn = getattr(_thread_local, "conn", None)
    if conn is None:
        _ensure_thread_resources()
        conn = _thread_local.conn
    return conn


def handle_request_for_query(qdict: Dict[str, Any]) -> Dict[str, Any]:
    init()
    from_ts = qdict["from_ts"]
    to_ts = qdict["to_ts"]
    actor_id = qdict["actor_id"]
    action = qdict["action"]
    page = qdict["page"]
    page_size = qdict["page_size"]

    offset_rows = (page - 1) * page_size

    # Filter the in-memory index (preserves ORDER: created_at DESC, id DESC)
    matched = []
    for (
        eid,
        created_at,
        a_id,
        a_action,
        rtype,
        rid,
        poff,
        plen,
    ) in _in_memory_index:
        if created_at < from_ts:
            # since index is sorted DESC by created_at, we can stop scanning
            break
        if created_at > to_ts:
            continue
        if actor_id is not None and a_id != actor_id:
            continue
        if action is not None and a_action != action:
            continue
        matched.append((eid, created_at, a_id, a_action, rtype, rid, poff, plen))

    rows = matched[offset_rows : offset_rows + page_size]

    events: List[Dict[str, Any]] = []
    for (
        eid,
        created_at,
        a_id,
        a_action,
        rtype,
        rid,
        poff,
        plen,
    ) in rows:
        payload = _read_payload(poff, plen)
        events.append(
            {
                "id": eid,
                "created_at": created_at,
                "actor_id": a_id,
                "action": a_action,
                "resource_type": rtype,
                "resource_id": rid,
                "payload": payload,
            }
        )

    return {"query": qdict, "events": events, "count": len(events)}


# convenience wrapper: pick a random query using baseline's RNG and handle it
def handle_request() -> Dict[str, Any]:
    init()
    q = baseline._pick_query()
    return handle_request_for_query(q.__dict__)
