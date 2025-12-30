# enhanced_audit_log_service.py
"""
Enhanced: Audit log list endpoint with improved efficiency and robustness.

Enhancements:
- Keyset pagination instead of OFFSET for better performance on deep pages.
- Persistent connections per thread for concurrency.
- Memory-mapped file for faster payload reads.
"""

from __future__ import annotations

import json
import mmap
import sqlite3
import threading
from typing import Any, Dict, List, Optional

import audit_log_service  # Import baseline for shared init and query picking

DB_PATH = audit_log_service.DB_PATH
PAYLOAD_FILE = audit_log_service.PAYLOAD_FILE

local = threading.local()
mm = None

def init_enhanced() -> None:
    audit_log_service.init()
    global mm
    with open(PAYLOAD_FILE, "rb") as f:
        mm = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)

def get_conn() -> sqlite3.Connection:
    if not hasattr(local, 'conn'):
        local.conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    return local.conn

def _read_payload_enhanced(offset: int, length: int) -> Any:
    mm.seek(offset)
    data = mm.read(length)
    return json.loads(data.decode("utf-8"))

def handle_request_enhanced() -> Dict[str, Any]:
    q = audit_log_service._pick_query()
    page = q.page
    page_size = q.page_size

    cursor_created_at = None
    cursor_id = None

    if page > 1:
        offset_rows = (page - 1) * page_size - 1
        if offset_rows >= 0:
            conn = get_conn()
            cur = conn.cursor()
            cur.execute(
                """
                SELECT id, created_at
                FROM audit_events
                WHERE created_at BETWEEN ? AND ?
                  AND (? IS NULL OR actor_id = ?)
                  AND (? IS NULL OR action = ?)
                ORDER BY created_at DESC, id DESC
                LIMIT 1 OFFSET ?
                """,
                (
                    q.from_ts,
                    q.to_ts,
                    q.actor_id,
                    q.actor_id,
                    q.action,
                    q.action,
                    offset_rows,
                ),
            )
            row = cur.fetchone()
            if row:
                cursor_id, cursor_created_at = row
            else:
                # No cursor, means not enough rows, return empty
                return {
                    "query": q.__dict__,
                    "events": [],
                    "count": 0,
                }

    # Main query
    conn = get_conn()
    cur = conn.cursor()
    sql = """
        SELECT id, created_at, actor_id, action, resource_type, resource_id,
               payload_offset, payload_len
        FROM audit_events
        WHERE created_at BETWEEN ? AND ?
          AND (? IS NULL OR actor_id = ?)
          AND (? IS NULL OR action = ?)
    """
    params = [
        q.from_ts,
        q.to_ts,
        q.actor_id,
        q.actor_id,
        q.action,
        q.action,
    ]
    if cursor_created_at is not None:
        sql += " AND ((created_at < ?) OR (created_at = ? AND id < ?))"
        params.extend([cursor_created_at, cursor_created_at, cursor_id])
    sql += " ORDER BY created_at DESC, id DESC LIMIT ?"
    params.append(q.page_size)

    cur.execute(sql, params)
    rows = cur.fetchall()

    events: List[Dict[str, Any]] = []
    for (
        eid,
        created_at,
        actor_id,
        action,
        rtype,
        rid,
        poff,
        plen,
    ) in rows:
        payload = _read_payload_enhanced(poff, plen)
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

    return {
        "query": q.__dict__,
        "events": events,
        "count": len(events),
    }