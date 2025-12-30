# audit_log_service.py (BASELINE — DO NOT EDIT)
"""
Baseline: Audit log list endpoint using LIMIT/OFFSET pagination.

This intentionally exhibits poor tail latency under concurrency due to:
- Deep pagination with OFFSET
- Per-request DB connection open/close
- Per-row disk payload load (open/read/json decode)

Semantics contract:
- Sort order: created_at DESC, id DESC
- Required fields in each event: id, created_at, actor_id, action, resource_type, resource_id, payload
- Payload must be loaded from disk-backed storage (no dropping I/O)
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
PAYLOAD_DIR = "payloads"
_rng = random.Random(1337)

# Tuned baseline defaults (faster seeding but still stresses deep OFFSET)
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
    os.makedirs(PAYLOAD_DIR, exist_ok=True)
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
            payload_path TEXT NOT NULL
        )
        """
    )
    # Intentionally insufficient indexing for deep pagination
    cur.execute("CREATE INDEX IF NOT EXISTS idx_created_at ON audit_events(created_at)")
    conn.commit()

    if need_init:
        now = int(time.time())
        actions = ["LOGIN", "LOGOUT", "UPDATE", "DELETE", "CREATE", "EXPORT"]
        resource_types = ["ORDER", "USER", "PRODUCT", "INVOICE", "WAREHOUSE"]

        batch = []
        for i in range(SEED_ROWS):
            created_at = now - _rng.randint(0, 60 * 60 * 24 * 30)  # last 30 days
            actor_id = _rng.randint(1, 2000)
            action = _rng.choice(actions)
            rtype = _rng.choice(resource_types)
            rid = f"{rtype}-{_rng.randint(1, 5_000_000)}"

            payload = {
                "diff": {f"field_{j}": [_rng.randint(0, 1000), _rng.randint(0, 1000)] for j in range(20)},
                "meta": {"ip": f"10.{_rng.randint(0,255)}.{_rng.randint(0,255)}.{_rng.randint(0,255)}"},
                "tags": [_rng.choice(["a", "b", "c", "d", "e"]) for _ in range(10)],
                # Reduced payload size to speed up seeding while retaining disk I/O + JSON costs
                "note": "x" * _rng.randint(NOTE_MIN_CHARS, NOTE_MAX_CHARS),
            }
            ppath = os.path.join(PAYLOAD_DIR, f"p_{i}.json")
            with open(ppath, "wb") as f:
                f.write(json.dumps(payload).encode("utf-8"))

            batch.append((created_at, actor_id, action, rtype, rid, ppath))
            if len(batch) >= 2000:
                cur.executemany(
                    "INSERT INTO audit_events(created_at, actor_id, action, resource_type, resource_id, payload_path) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    batch,
                )
                conn.commit()
                batch.clear()

        if batch:
            cur.executemany(
                "INSERT INTO audit_events(created_at, actor_id, action, resource_type, resource_id, payload_path) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                batch,
            )
            conn.commit()

    conn.close()


def _pick_query() -> Query:
    now = int(time.time())
    to_ts = now
    from_ts = now - 7 * 24 * 3600  # last 7 days
    actor_id = _rng.choice([None, _rng.randint(1, 2000)])
    action = _rng.choice([None, "UPDATE", "CREATE", "DELETE"])
    page_size = _rng.choice([10, 20, 50])
    # Deep pagination stress (tuned down from 5000 -> 2000 to match smaller dataset)
    page = _rng.randint(1, MAX_PAGE)
    return Query(from_ts, to_ts, actor_id, action, page, page_size)


def handle_request() -> Dict[str, Any]:
    """
    Simulates: GET /v1/audit-events?page=...&page_size=...

    Baseline pagination:
        LIMIT page_size OFFSET (page-1)*page_size
    """
    q = _pick_query()
    offset = (q.page - 1) * q.page_size

    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    cur = conn.cursor()

    sql = """
        SELECT id, created_at, actor_id, action, resource_type, resource_id, payload_path
        FROM audit_events
        WHERE created_at BETWEEN ? AND ?
          AND (? IS NULL OR actor_id = ?)
          AND (? IS NULL OR action = ?)
        ORDER BY created_at DESC, id DESC
        LIMIT ? OFFSET ?
    """
    cur.execute(sql, (q.from_ts, q.to_ts, q.actor_id, q.actor_id, q.action, q.action, q.page_size, offset))
    rows = cur.fetchall()
    conn.close()

    events: List[Dict[str, Any]] = []
    for (eid, created_at, actor_id, action, rtype, rid, ppath) in rows:
        with open(ppath, "rb") as f:
            payload = json.loads(f.read().decode("utf-8"))
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
        "query": {
            "from_ts": q.from_ts,
            "to_ts": q.to_ts,
            "actor_id": q.actor_id,
            "action": q.action,
            "page": q.page,
            "page_size": q.page_size,
        },
        "events": events,
        "count": len(events),
    }
