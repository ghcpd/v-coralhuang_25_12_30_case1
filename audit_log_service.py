# audit_log_service.py (BASELINE — DO NOT EDIT)
"""
Baseline: Audit log list endpoint using LIMIT/OFFSET pagination.

This baseline intentionally exhibits poor tail latency due to:
- Deep pagination with OFFSET
- Per-request SQLite connection churn
- Per-row disk-backed payload reads with JSON decoding

Payload storage model (optimized for seeding speed):
- All payloads are stored in a single append-only JSONL file.
- Database stores (payload_offset, payload_len).
- Each request performs a disk seek + read + json decode per row.

Semantics contract (MUST NOT CHANGE):
- Sort order: created_at DESC, id DESC
- Required fields per event:
  id, created_at, actor_id, action, resource_type, resource_id, payload
- Payload must be read from disk for each event
"""

from __future__ import annotations

import json
import os
import random
import sqlite3
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

DB_PATH = "audit.db"
PAYLOAD_FILE = "payloads.jsonl"

_rng = random.Random(1337)

SEED_ROWS = 50_000
MAX_PAGE = 2000
NOTE_MIN_CHARS = 256
NOTE_MAX_CHARS = 1024

_init_lock = threading.Lock()
_inited = False


@dataclass(frozen=True)
class Query:
    from_ts: int
    to_ts: int
    actor_id: Optional[int]
    action: Optional[str]
    page: int
    page_size: int


def init() -> None:
    global _inited
    if _inited:
        return
    with _init_lock:
        if _inited:
            return
        _seed_if_needed()
        _inited = True


def _seed_if_needed() -> None:
    need_init = not os.path.exists(DB_PATH)

    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    cur = conn.cursor()

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS audit_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at INTEGER NOT NULL,
            actor_id INTEGER NOT NULL,
            action TEXT NOT NULL,
            resource_type TEXT NOT NULL,
            resource_id TEXT NOT NULL,
            payload_offset INTEGER NOT NULL,
            payload_len INTEGER NOT NULL
        )
        """
    )
    cur.execute("CREATE INDEX IF NOT EXISTS idx_created_at ON audit_events(created_at)")
    conn.commit()

    if need_init:
        now = int(time.time())
        actions = ["LOGIN", "LOGOUT", "UPDATE", "DELETE", "CREATE", "EXPORT"]
        resource_types = ["ORDER", "USER", "PRODUCT", "INVOICE", "WAREHOUSE"]

        with open(PAYLOAD_FILE, "wb") as pf:
            batch = []
            offset = 0

            for _ in range(SEED_ROWS):
                created_at = now - _rng.randint(0, 60 * 60 * 24 * 30)
                actor_id = _rng.randint(1, 2000)
                action = _rng.choice(actions)
                rtype = _rng.choice(resource_types)
                rid = f"{rtype}-{_rng.randint(1, 5_000_000)}"

                payload = {
                    "diff": {
                        f"field_{j}": [_rng.randint(0, 1000), _rng.randint(0, 1000)]
                        for j in range(20)
                    },
                    "meta": {
                        "ip": f"10.{_rng.randint(0,255)}.{_rng.randint(0,255)}.{_rng.randint(0,255)}"
                    },
                    "tags": [_rng.choice(["a", "b", "c", "d", "e"]) for _ in range(10)],
                    "note": "x" * _rng.randint(NOTE_MIN_CHARS, NOTE_MAX_CHARS),
                }

                data = json.dumps(payload).encode("utf-8") + b"\n"
                pf.write(data)

                batch.append(
                    (
                        created_at,
                        actor_id,
                        action,
                        rtype,
                        rid,
                        offset,
                        len(data),
                    )
                )
                offset += len(data)

                if len(batch) >= 2000:
                    cur.executemany(
                        """
                        INSERT INTO audit_events
                        (created_at, actor_id, action, resource_type, resource_id,
                         payload_offset, payload_len)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        batch,
                    )
                    conn.commit()
                    batch.clear()

            if batch:
                cur.executemany(
                    """
                    INSERT INTO audit_events
                    (created_at, actor_id, action, resource_type, resource_id,
                     payload_offset, payload_len)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    batch,
                )
                conn.commit()

    conn.close()


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


def _read_payload(offset: int, length: int) -> Any:
    with open(PAYLOAD_FILE, "rb") as f:
        f.seek(offset)
        return json.loads(f.read(length))


def handle_request() -> Dict[str, Any]:
    q = _pick_query()
    offset_rows = (q.page - 1) * q.page_size

    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    cur = conn.cursor()

    cur.execute(
        """
        SELECT id, created_at, actor_id, action, resource_type, resource_id,
               payload_offset, payload_len
        FROM audit_events
        WHERE created_at BETWEEN ? AND ?
          AND (? IS NULL OR actor_id = ?)
          AND (? IS NULL OR action = ?)
        ORDER BY created_at DESC, id DESC
        LIMIT ? OFFSET ?
        """,
        (
            q.from_ts,
            q.to_ts,
            q.actor_id,
            q.actor_id,
            q.action,
            q.action,
            q.page_size,
            offset_rows,
        ),
    )
    rows = cur.fetchall()
    conn.close()

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

    return {
        "query": q.__dict__,
        "events": events,
        "count": len(events),
    }
