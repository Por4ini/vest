import asyncio
import json
import logging
from logging.handlers import RotatingFileHandler
from functools import lru_cache
from io import BytesIO
import os
import queue
from collections import deque
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

import urllib.error
import urllib.request
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response
from fastapi.websockets import WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from psycopg import connect
from psycopg.errors import UniqueViolation
from psycopg.rows import dict_row

from get_id import _build_csv_row, _load_payload_rows, _parse_raw_proxy, run_from_payload_table, PROXY_POOL


BASE_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = Path(os.getenv("UPLOAD_DIR", str(BASE_DIR / "uploads")))
RESULT_DIR = Path(os.getenv("RESULT_DIR", str(BASE_DIR / "results")))
LOG_DIR = Path(os.getenv("LOG_DIR", str(BASE_DIR / "logs")))
STATIC_DIR = BASE_DIR / "web"

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://vestwell:vestwell@db:5432/vestwell")
TELEGRAM_BOT_TOKEN = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
TELEGRAM_CHAT_ID = (os.getenv("TELEGRAM_CHAT_ID") or "").strip()

for directory in (UPLOAD_DIR, RESULT_DIR, LOG_DIR):
    directory.mkdir(parents=True, exist_ok=True)
STATIC_DIR.mkdir(parents=True, exist_ok=True)

LOG_FILE = LOG_DIR / "dashboard.log"

APP_NAME = "Vestwell Control"

TASK_LOCK = threading.Lock()
ACTIVE_TASKS: dict[str, dict[str, Any]] = {}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _configure_logging() -> None:
    logger = logging.getLogger("vestwell")
    if logger.handlers:
        return
    logger.setLevel(logging.INFO)

    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(name)s | %(message)s"
    )
    stdout = logging.StreamHandler()
    stdout.setFormatter(formatter)
    file_handler = RotatingFileHandler(LOG_FILE, maxBytes=8 * 1024 * 1024, backupCount=5)
    file_handler.setFormatter(formatter)

    logger.addHandler(stdout)
    logger.addHandler(file_handler)
    logger.propagate = False


_configure_logging()
logger = logging.getLogger("vestwell")


app = FastAPI(title=APP_NAME)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


def _safe_float(value: str | float | int, default: float) -> float:
    try:
        parsed = float(value)
    except Exception:
        return default
    if parsed < 0:
        return default
    return parsed


def _safe_int(value: str | int | None, default: int | None) -> int | None:
    if value in (None, "", "null", "undefined"):
        return default
    try:
        parsed = int(value)
    except Exception:
        return default
    return parsed if parsed > 0 else default


def _coerce_status_code(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    try:
        return int(str(value))
    except Exception:
        return None


def _clean_telegram_chat_id(value: Any) -> str | None:
    if value is None:
        return None
    candidate = str(value).strip()
    if not candidate:
        return None
    if candidate == "None":
        return None
    if candidate.startswith("+"):
        candidate = candidate[1:]
    if candidate.lstrip("-").isdigit():
        return candidate
    return None


def _clean_telegram_bot_token(value: Any) -> str | None:
    if value is None:
        return None
    token = str(value).strip()
    if not token or token == "None":
        return None
    return token


def _build_status_message(status_label: str, row: Any | None = None) -> str:
    if row is None:
        return status_label
    return f"{status_label} row={row}"


@contextmanager
def db_connection():
    conn = connect(DATABASE_URL)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _run_db_migrations() -> None:
    with db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS proxies (
                    id TEXT PRIMARY KEY,
                    raw TEXT UNIQUE NOT NULL,
                    enabled BOOLEAN NOT NULL DEFAULT TRUE,
                    last_status TEXT NOT NULL DEFAULT 'unknown',
                    last_ip TEXT,
                    last_checked_at TIMESTAMPTZ,
                    last_error TEXT,
                    server TEXT,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    filename TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'queued',
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    started_at TIMESTAMPTZ,
                    completed_at TIMESTAMPTZ,
                    pause_min REAL NOT NULL DEFAULT 1.2,
                    pause_max REAL NOT NULL DEFAULT 3.5,
                    max_rows INTEGER,
                    total_rows INTEGER,
                    processed_rows INTEGER NOT NULL DEFAULT 0,
                    ok_rows INTEGER NOT NULL DEFAULT 0,
                    error_rows INTEGER NOT NULL DEFAULT 0,
                    current_row INTEGER NOT NULL DEFAULT 0,
                    use_proxy BOOLEAN NOT NULL DEFAULT TRUE,
                    proxy_count INTEGER NOT NULL DEFAULT 0,
                    selected_proxies JSONB DEFAULT '[]'::JSONB,
                    result_file_json TEXT,
                    payload_path TEXT,
                    last_message TEXT,
                    last_error TEXT,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
            cur.execute("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS telegram_chat_id TEXT")
            cur.execute("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS telegram_bot_token TEXT")

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS task_rows (
                    id BIGSERIAL PRIMARY KEY,
                    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                    row_number INTEGER,
                    source_row INTEGER,
                    status_code INTEGER,
                    ok BOOLEAN,
                    proxy TEXT,
                    session_cookie TEXT,
                    body_preview TEXT,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS task_events (
                    id BIGSERIAL PRIMARY KEY,
                    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                    type TEXT NOT NULL,
                    payload JSONB NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )

            cur.execute("CREATE INDEX IF NOT EXISTS idx_task_rows_task_id ON task_rows(task_id)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_task_events_task_id ON task_events(task_id)")

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS app_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )


def _seed_default_proxies() -> None:
    existing: set[str] = set()
    with db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT raw FROM proxies")
            existing = {row[0] for row in cur.fetchall()}

    for raw in PROXY_POOL:
        if raw in existing:
            continue
        parsed = _parse_raw_proxy(raw)
        if not parsed:
            continue
        proxy_id = str(uuid.uuid4())
        with db_connection() as conn:
            with conn.cursor() as cur:
                try:
                    cur.execute(
                        """
                        INSERT INTO proxies (id, raw, enabled, last_status, server, updated_at)
                        VALUES (%s, %s, TRUE, 'unknown', %s, NOW())
                        ON CONFLICT (raw) DO NOTHING
                        """,
                        (proxy_id, raw, parsed.get("server")),
                    )
                except Exception:
                    pass


def _serialize_proxy(proxy: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": proxy["id"],
        "raw": proxy["raw"],
        "enabled": bool(proxy["enabled"]),
        "last_status": proxy["last_status"],
        "last_ip": proxy["last_ip"],
        "last_checked_at": proxy["last_checked_at"].isoformat() if hasattr(proxy["last_checked_at"], "isoformat") else proxy["last_checked_at"],
        "last_error": proxy["last_error"],
    }


def _serialize_task(task: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "id": task["id"],
        "name": task["name"],
        "filename": task["filename"],
        "status": task["status"],
        "created_at": task["created_at"].isoformat() if hasattr(task["created_at"], "isoformat") else task["created_at"],
        "started_at": task["started_at"].isoformat() if task.get("started_at") and hasattr(task["started_at"], "isoformat") else task.get("started_at"),
        "completed_at": task["completed_at"].isoformat() if task.get("completed_at") and hasattr(task["completed_at"], "isoformat") else task.get("completed_at"),
        "updated_at": task["updated_at"].isoformat() if task.get("updated_at") and hasattr(task["updated_at"], "isoformat") else task.get("updated_at"),
        "pause_min": task["pause_min"],
        "pause_max": task["pause_max"],
        "max_rows": task["max_rows"],
        "total_rows": task["total_rows"],
        "processed_rows": task["processed_rows"],
        "ok_rows": task["ok_rows"],
        "error_rows": task["error_rows"],
        "current_row": task["current_row"],
        "use_proxy": task["use_proxy"],
        "proxy_count": task["proxy_count"],
        "last_message": task["last_message"],
        "rows": rows,
        "result_file_json": task["result_file_json"],
        "telegram_chat_id": task.get("telegram_chat_id"),
        "telegram_bot_token": task.get("telegram_bot_token"),
        "error": task.get("last_error"),
    }


def _read_log_tail(path: Path, lines: int = 200) -> list[str]:
    if not path.exists():
        return []

    limited = max(1, min(lines, 2000))
    buffered = deque(maxlen=limited)

    with path.open("r", encoding="utf-8", errors="ignore") as file:
        for line in file:
            buffered.append(line.rstrip("\n"))

    return list(buffered)


def _db_list_proxies() -> list[dict[str, Any]]:
    with db_connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT id, raw, enabled, last_status, last_ip, last_checked_at, last_error
                FROM proxies
                ORDER BY created_at DESC
                """
            )
            return [dict(row) for row in cur.fetchall()]


def _db_get_proxy(proxy_id: str) -> dict[str, Any] | None:
    with db_connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT * FROM proxies WHERE id=%s LIMIT 1", (proxy_id,))
            row = cur.fetchone()
            return dict(row) if row else None


def _db_create_proxy(raw: str) -> dict[str, Any]:
    parsed = _parse_raw_proxy(raw)
    if not parsed:
        raise ValueError("Invalid proxy format")
    proxy_id = str(uuid.uuid4())
    with db_connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            try:
                cur.execute(
                    """
                    INSERT INTO proxies (id, raw, enabled, last_status, server, updated_at)
                    VALUES (%s, %s, TRUE, 'unknown', %s, NOW())
                    """,
                    (proxy_id, raw.strip(), parsed.get("server")),
                )
            except UniqueViolation as error:
                raise error
            cur.execute("SELECT id, raw, enabled, last_status, last_ip, last_checked_at, last_error FROM proxies WHERE id=%s", (proxy_id,))
            row = cur.fetchone()
            return dict(row)


def _db_delete_proxy(proxy_id: str) -> bool:
    with db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM proxies WHERE id=%s", (proxy_id,))
            return cur.rowcount > 0


def _db_update_proxy_after_check(proxy_id: str, result: dict[str, Any]) -> dict[str, Any] | None:
    now = _utcnow()
    with db_connection() as conn:
        with conn.cursor() as cur:
            if result.get("ok"):
                cur.execute(
                    """
                    UPDATE proxies
                    SET last_status='ok', last_ip=%s, last_error=NULL, last_checked_at=%s, updated_at=%s
                    WHERE id=%s
                    """,
                    (result.get("ip"), now, now, proxy_id),
                )
            else:
                cur.execute(
                    """
                    UPDATE proxies
                    SET last_status='failed', last_ip=NULL, last_error=%s, enabled=FALSE, last_checked_at=%s, updated_at=%s
                    WHERE id=%s
                    """,
                    (result.get("error"), now, now, proxy_id),
                )

        return _db_get_proxy(proxy_id)


def _db_list_tasks(limit: int | None = None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with db_connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            query = """
                SELECT id, name, filename, status, created_at, started_at, completed_at,
                       updated_at, pause_min, pause_max, max_rows, total_rows, processed_rows,
                       ok_rows, error_rows, current_row, use_proxy, proxy_count,
                       result_file_json, last_message, last_error,
                       telegram_chat_id, telegram_bot_token
                FROM tasks
                ORDER BY created_at DESC
                """
            params: tuple[Any, ...] = ()
            if limit and limit > 0:
                query += " LIMIT %s"
                params = (limit,)
            cur.execute(query, params)
            rows = [dict(r) for r in cur.fetchall()]

    return [_serialize_task(row, []) for row in rows]


def _db_get_task_record(task_id: str) -> dict[str, Any] | None:
    with db_connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT id, name, filename, status, created_at, started_at, completed_at,
                       updated_at, pause_min, pause_max, max_rows, total_rows, processed_rows,
                       ok_rows, error_rows, current_row, use_proxy, proxy_count,
                       result_file_json, payload_path, last_message, last_error,
                       telegram_chat_id, telegram_bot_token
                FROM tasks
                WHERE id=%s
                LIMIT 1
                """,
                (task_id,),
            )
            row = cur.fetchone()
    return dict(row) if row else None


def _db_get_latest_telegram_config() -> tuple[str | None, str | None]:
    with db_connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT telegram_chat_id, telegram_bot_token
                FROM tasks
                WHERE telegram_chat_id IS NOT NULL OR telegram_bot_token IS NOT NULL
                ORDER BY created_at DESC
                LIMIT 1
                """
            )
            row = cur.fetchone()
    if not row:
        return None, None
    return row.get("telegram_chat_id"), row.get("telegram_bot_token")


def _db_get_setting(key: str) -> str | None:
    with db_connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT value FROM app_settings WHERE key = %s LIMIT 1", (key,))
            row = cur.fetchone()
    return row["value"] if row else None


def _db_set_setting(key: str, value: str | None) -> None:
    with db_connection() as conn:
        with conn.cursor() as cur:
            if value is None:
                cur.execute("DELETE FROM app_settings WHERE key=%s", (key,))
                return
            cur.execute(
                """
                INSERT INTO app_settings (key, value, updated_at)
                VALUES (%s, %s, NOW())
                ON CONFLICT (key) DO UPDATE SET
                    value = EXCLUDED.value,
                    updated_at = NOW()
                """,
                (key, value),
            )


def _db_get_telegram_defaults() -> tuple[str | None, str | None]:
    chat_id = _clean_telegram_chat_id(_db_get_setting("telegram_chat_id"))
    bot_token = _clean_telegram_bot_token(_db_get_setting("telegram_bot_token"))
    return chat_id, bot_token


def _db_get_task(task_id: str) -> dict[str, Any] | None:
    row = _db_get_task_record(task_id)
    if not row:
        return None
    return _serialize_task(row, _db_get_task_rows(task_id))


@lru_cache(maxsize=32)
def _cached_payload_rows(payload_path: str, mtime_ns: int, max_rows: int | None) -> tuple[dict[str, Any], ...]:
    return tuple(_load_payload_rows(payload_path, max_rows=max_rows))


def _load_task_payload_rows(task: dict[str, Any]) -> list[dict[str, Any]]:
    payload_path = task.get("payload_path")
    if not payload_path:
        return []

    path = Path(str(payload_path))
    if not path.exists():
        return []

    try:
        stat = path.stat()
    except OSError:
        return []

    try:
        cached_rows = _cached_payload_rows(str(path), stat.st_mtime_ns, _safe_int(task.get("max_rows"), None))
    except Exception:
        return []

    return [dict(row) for row in cached_rows]


def _row_status_key(processed_row: dict[str, Any] | None) -> str:
    if not processed_row:
        return "queued"
    status_code = _coerce_status_code(processed_row.get("status"))
    if status_code == 403:
        return "registered"
    if status_code == 201:
        return "unregistered"
    if status_code == 404:
        return "not_found"
    if processed_row.get("ok"):
        return "ok"
    if status_code == 202:
        return "retry"
    if status_code is None:
        return "unknown"
    return "error"


def _row_status_label(status_key: str, status_code: int | None) -> str:
    if status_key == "queued":
        return "Queued"
    if status_key == "ok":
        return "OK"
    if status_key == "registered":
        return "Registered"
    if status_key == "unregistered":
        return "Unregistered"
    if status_key == "not_found":
        return "Not found"
    if status_key == "blocked":
        return "Blocked"
    if status_key == "retry":
        return "Retry"
    if status_key == "unknown":
        return "Unknown"
    if status_code is None:
        return "Error"
    return f"HTTP {status_code}"


def _db_create_task(
    task_id: str,
    name: str,
    filename: str,
    pause_min: float,
    pause_max: float,
    max_rows: int | None,
    use_proxy: bool,
    proxy_count: int,
    payload_path: str,
    result_json: str,
    telegram_chat_id: str | None,
    telegram_bot_token: str | None,
) -> dict[str, Any]:
    with db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO tasks (
                    id, name, filename, status, created_at, pause_min, pause_max, max_rows,
                    use_proxy, proxy_count, result_file_json, payload_path, telegram_chat_id,
                    telegram_bot_token, last_message
                )
                VALUES (%s, %s, %s, 'queued', NOW(), %s, %s, %s, %s, %s, %s, %s, %s, %s, 'Awaiting start')
                """,
                (
                    task_id,
                    name,
                    filename,
                    pause_min,
                    pause_max,
                    max_rows,
                    use_proxy,
                    proxy_count,
                    result_json,
                    payload_path,
                    telegram_chat_id,
                    telegram_bot_token,
                ),
            )

    task = _db_get_task(task_id)
    if not task:
        raise RuntimeError("Failed to create task in DB")
    return task


def _db_append_task_row(task_id: str, event: dict[str, Any]) -> None:
    if event.get("type") != "row_finished":
        return
    row = int(event.get("row") or 0)
    source_row = event.get("source_row")
    if source_row is None:
        source_row = row
    try:
        source_row = int(source_row)
    except Exception:
        source_row = None

    status_code = _coerce_status_code(event.get("status"))
    with db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO task_rows (task_id, row_number, source_row, status_code, ok, proxy, session_cookie, body_preview)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    task_id,
                    row,
                    source_row,
                    status_code,
                    bool(event.get("ok")),
                    event.get("proxy"),
                    event.get("session_cookie"),
                    event.get("body_preview"),
                ),
            )


def _db_get_task_rows(task_id: str, limit: int | None = 100) -> list[dict[str, Any]]:
    with db_connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            query = """
                SELECT row_number, source_row, status_code, ok, proxy, session_cookie, body_preview
                FROM task_rows
                WHERE task_id=%s
                ORDER BY id DESC
            """
            params: tuple[Any, ...] = (task_id,)
            if limit is not None and limit > 0:
                query += " LIMIT %s"
                params = (task_id, limit)
            cur.execute(query, params)
            rows = [dict(row) for row in cur.fetchall()]
    rows.reverse()
    return [
        {
            "row": row["row_number"],
            "source_row": row["source_row"],
            "status": row["status_code"],
            "ok": row["ok"],
            "proxy": row["proxy"],
            "session": row["session_cookie"],
            "body_preview": row["body_preview"],
        }
        for row in rows
    ]


def _db_append_task_event(task_id: str, event: dict[str, Any]) -> None:
    with db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO task_events (task_id, type, payload) VALUES (%s, %s, CAST(%s AS JSONB))",
                (task_id, event.get("type"), json.dumps(event, ensure_ascii=False)),
            )


def _db_get_task_events(task_id: str, limit: int = 200) -> list[dict[str, Any]]:
    with db_connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT created_at, type, payload
                FROM task_events
                WHERE task_id=%s
                ORDER BY id DESC
                LIMIT %s
                """,
                (task_id, limit),
            )
            rows = [dict(row) for row in cur.fetchall()]
    rows.reverse()
    return [
        {
            "created_at": r["created_at"].isoformat() if hasattr(r["created_at"], "isoformat") else r["created_at"],
            "type": r["type"],
            "payload": r["payload"],
        }
        for r in rows
    ]


def _db_update_task_metrics(task_id: str, event: dict[str, Any]) -> None:
    etype = event.get("type")
    now = _utcnow()
    with db_connection() as conn:
        with conn.cursor() as cur:
            if etype == "task_started":
                cur.execute(
                    """
                    UPDATE tasks
                    SET status='running',
                        started_at=COALESCE(started_at, NOW()),
                        total_rows=%s,
                        pause_min=%s,
                        pause_max=%s,
                        proxy_count=%s,
                        last_message='Processing started',
                        updated_at=%s
                    WHERE id=%s
                    """,
                    (
                        event.get("total"),
                        float(event.get("pause_min") or 0),
                        float(event.get("pause_max") or 0),
                        int(event.get("proxy_count") or 0),
                        now,
                        task_id,
                    ),
                )
                return

            if etype == "row_started":
                cur.execute(
                    """
                    UPDATE tasks
                    SET status='running',
                        current_row=%s,
                        last_message=%s,
                        updated_at=%s
                    WHERE id=%s
                    """,
                    (
                        event.get("row"),
                        f"Processing row {event.get('row')}...",
                        now,
                        task_id,
                    ),
                )
                return

            if etype == "row_pause":
                cur.execute(
                    """
                    UPDATE tasks
                    SET status='running',
                        last_message=%s,
                        updated_at=%s
                    WHERE id=%s
                    """,
                    (f"Pause {(event.get('pause_seconds') or 0):.2f}s before the next row", now, task_id),
                )
                return

            if etype == "row_finished":
                ok = bool(event.get("ok"))
                status_code = _coerce_status_code(event.get("status"))
                status_key = _row_status_key({"ok": ok, "status": status_code})
                status_label = _row_status_label(status_key, status_code)
                if status_code == 202:
                    last_message = f"HTTP 202 for row {event.get('row')} - retry required"
                else:
                    last_message = _build_status_message(status_label, event.get("row"))
                cur.execute(
                    """
                    UPDATE tasks
                    SET status='running',
                        processed_rows=processed_rows + 1,
                        ok_rows=ok_rows + %s,
                        error_rows=error_rows + %s,
                        current_row=%s,
                        last_message=%s,
                        updated_at=%s
                    WHERE id=%s
                    """,
                    (
                        1 if ok else 0,
                        0 if ok else 1,
                        event.get("row"),
                        last_message,
                        now,
                        task_id,
                    ),
                )
                _db_append_task_row(task_id, event)
                return

            if etype == "task_finished":
                total = event.get("total")
                cur.execute(
                    """
                    UPDATE tasks
                    SET status='finished',
                        completed_at=NOW(),
                        total_rows=COALESCE(%s, total_rows),
                        last_message='Completed',
                        updated_at=%s
                    WHERE id=%s
                    """,
                    (total, now, task_id),
                )
                return

            if etype == "task_failed":
                cur.execute(
                    """
                    UPDATE tasks
                    SET status='failed',
                        completed_at=NOW(),
                        last_error=%s,
                        last_message='Execution failed',
                        updated_at=%s
                    WHERE id=%s
                    """,
                    (event.get("error"), now, task_id),
                )
                return


def _load_task_result_rows(task: dict[str, Any]) -> list[dict[str, Any]]:
    result_path = task.get("result_file_json")
    if not result_path or not Path(result_path).exists():
        return []

    with open(result_path, "r", encoding="utf-8") as file:
        rows = json.load(file)

    if isinstance(rows, dict):
        rows = [rows]
    if not isinstance(rows, list):
        return []
    return rows


def _build_task_xlsx_bytes(task_id: str, result_rows: list[dict[str, Any]] | None = None) -> bytes:
    try:
        from openpyxl import Workbook
    except ImportError as exc:
        raise RuntimeError("openpyxl is required to export XLSX") from exc

    if result_rows is None:
        task = _db_get_task(task_id)
        if not task:
            raise RuntimeError("Task not found")
        result_rows = _load_task_result_rows(task)

    flat_rows = [_build_csv_row(result) for result in result_rows]
    headers: list[str] = []
    for row in flat_rows:
        for key in row.keys():
            if key not in headers:
                headers.append(key)

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Results"

    if headers:
        sheet.append(headers)
        for row in flat_rows:
            sheet.append([row.get(header, "") for header in headers])

    buffer = BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def _post_telegram_document(
    chat_id: str,
    token: str,
    file_bytes: bytes,
    filename: str,
    caption: str | None = None,
) -> None:
    boundary = uuid.uuid4().hex
    api_url = f"https://api.telegram.org/bot{token}/sendDocument"
    parts: list[bytes] = []

    def add_field(name: str, value: str) -> None:
        parts.append(f"--{boundary}\r\n".encode())
        parts.append(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
        parts.append(f"{value}\r\n".encode())

    def add_file(name: str, filename_value: str, content: bytes) -> None:
        parts.append(f"--{boundary}\r\n".encode())
        parts.append(
            f'Content-Disposition: form-data; name="{name}"; filename="{filename_value}"\r\n'.encode()
        )
        parts.append(b"Content-Type: application/vnd.openxmlformats-officedocument.spreadsheetml.sheet\r\n\r\n")
        parts.append(content)
        parts.append(b"\r\n")

    add_field("chat_id", chat_id)
    if caption:
        add_field("caption", caption)
    add_file("document", filename, file_bytes)
    parts.append(f"--{boundary}--\r\n".encode())
    payload = b"".join(parts)

    req = urllib.request.Request(api_url, data=payload, method="POST")
    req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    with urllib.request.urlopen(req, timeout=30) as response:
        raw = response.read().decode("utf-8", errors="replace")
        data = json.loads(raw or "{}")
        if not data.get("ok"):
            raise RuntimeError(f"Telegram API error: {data}")


def _send_task_result_to_telegram(
    task_id: str,
    task: dict[str, Any],
    telegram_chat_id: str | None,
    telegram_bot_token: str | None,
) -> None:
    chat_id = (
        _clean_telegram_chat_id(telegram_chat_id)
        or _clean_telegram_chat_id(task.get("telegram_chat_id"))
        or _clean_telegram_chat_id(TELEGRAM_CHAT_ID)
    )
    token = (
        _clean_telegram_bot_token(telegram_bot_token)
        or _clean_telegram_bot_token(task.get("telegram_bot_token"))
        or _clean_telegram_bot_token(TELEGRAM_BOT_TOKEN)
    )
    if not chat_id or not token:
        return

    result_rows = _load_task_result_rows(task)
    if not result_rows:
        logger.info("Task %s has no rows for Telegram output", task_id)
        return

    xlsx_bytes = _build_task_xlsx_bytes(task_id, result_rows=result_rows)
    total = task.get("total_rows") or len(result_rows)
    ok_rows = task.get("ok_rows") or 0
    error_rows = task.get("error_rows") or 0
    caption = f"Task {task_id} completed. total={total}, ok={ok_rows}, errors={error_rows}"
    _post_telegram_document(chat_id, token, xlsx_bytes, f"{task_id}_result.xlsx", caption=caption)


def _check_proxy_sync(raw: str, timeout: int = 12) -> dict[str, Any]:
    parsed = _parse_raw_proxy(raw)
    if not parsed:
        return {
            "ok": False,
            "error": "Invalid proxy format",
            "ip": None,
            "status": "invalid",
            "latency_ms": None,
        }

    req = urllib.request.Request("https://api.ipify.org?format=json")
    handlers: list[object] = [urllib.request.HTTPSHandler()]
    handlers.insert(0, urllib.request.ProxyHandler({"http": parsed["http"], "https": parsed["https"]}))
    opener = urllib.request.build_opener(*handlers)
    start = time.perf_counter()
    try:
        with opener.open(req, timeout=timeout) as response:
            body = response.read().decode("utf-8", errors="replace")
            payload = json.loads(body)
            ip = payload.get("ip")
            return {
                "ok": bool(ip),
                "ip": ip,
                "status": "ok" if ip else "no_ip",
                "error": None,
                "latency_ms": int((time.perf_counter() - start) * 1000),
            }
    except urllib.error.HTTPError as exc:
        return {
            "ok": False,
            "ip": None,
            "status": "http_error",
            "error": f"HTTP {getattr(exc, 'code', 'unknown')}: {exc.reason}",
            "latency_ms": int((time.perf_counter() - start) * 1000),
        }
    except Exception as exc:
        return {
            "ok": False,
            "ip": None,
            "status": "failed",
            "error": str(exc),
            "latency_ms": int((time.perf_counter() - start) * 1000),
        }


def _emit(task_id: str, payload: dict[str, Any]) -> None:
    queue_payload = json.dumps(payload, ensure_ascii=False)
    active = ACTIVE_TASKS.get(task_id)
    if not active:
        return
    try:
        active["events"].put_nowait(queue_payload)
    except queue.Full:
        # drop oldest message and enqueue latest, keeping stream alive
        try:
            active["events"].get_nowait()
        except queue.Empty:
            pass
        try:
            active["events"].put_nowait(queue_payload)
        except Exception:
            pass


def _on_task_event(task_id: str, event: dict[str, Any]) -> None:
    event["task_id"] = task_id
    _db_update_task_metrics(task_id, event)
    _db_append_task_event(task_id, event)
    _emit(task_id, event)


def _run_task_background(
    task_id: str,
    xlsx_path: str,
    pause_min: float,
    pause_max: float,
    max_rows: int | None,
    use_proxy: bool,
    proxy_raws: list[str],
    result_json: str,
    telegram_chat_id: str | None = None,
    telegram_bot_token: str | None = None,
) -> None:
    with TASK_LOCK:
        ACTIVE_TASKS.setdefault(task_id, {"events": queue.Queue(maxsize=250), "created": _utcnow()})

    _on_task_event(
        task_id,
        {
            "type": "task_started",
            "task_id": task_id,
            "file": xlsx_path,
            "total": None,
            "pause_min": pause_min,
            "pause_max": pause_max,
            "proxy_count": len(proxy_raws),
        },
    )

    final_event: dict[str, Any]

    try:
        run_from_payload_table(
            xlsx_path=xlsx_path,
            max_rows=max_rows,
            pause_min=pause_min,
            pause_max=pause_max,
            proxy_pool=proxy_raws,
            use_proxy=use_proxy and bool(proxy_raws),
            progress_callback=lambda event: _on_task_event(task_id, event),
            task_id=task_id,
            verbose=False,
            result_json=result_json,
        )
        final_event = {
            "type": "task_finished",
            "task_id": task_id,
            "total": _db_get_task(task_id).get("processed_rows") if _db_get_task(task_id) else None,
        }
    except Exception as exc:
        logger.exception("Task %s failed", task_id)
        final_event = {
            "type": "task_failed",
            "task_id": task_id,
            "error": str(exc),
        }
        _on_task_event(task_id, final_event)
    else:
        task_record = _db_get_task(task_id) or {}
        try:
            _send_task_result_to_telegram(
                task_id=task_id,
                task=task_record,
                telegram_chat_id=telegram_chat_id,
                telegram_bot_token=telegram_bot_token,
            )
        except Exception as error:
            logger.exception("Telegram notification failed for task %s", task_id)
            _on_task_event(
                task_id,
                {
                    "type": "task_notification_failed",
                    "task_id": task_id,
                    "error": f"Telegram notification failed: {error}",
                },
            )
        _on_task_event(task_id, final_event)
    finally:
        with TASK_LOCK:
            ACTIVE_TASKS.pop(task_id, None)


def _initialize_from_db() -> None:
    initialized = False
    for _ in range(60):
        try:
            _run_db_migrations()
            _seed_default_proxies()
            initialized = True
            break
        except Exception as exc:
            logger.warning("Waiting for database: %s", str(exc))
            time.sleep(1.5)
    if not initialized:
        raise RuntimeError("Database is unavailable after startup retries")

    with db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE tasks
                SET status='failed',
                    completed_at=COALESCE(completed_at, NOW()),
                        last_error=COALESCE(last_error || ' | ', '') || 'Interrupted by service restart'
                WHERE status IN ('running', 'queued')
                """
            )

    logger.info("Database initialized, schema ready")


@app.middleware("http")
async def log_requests(request, call_next):  # type: ignore[override]
    start = time.perf_counter()
    response = await call_next(request)
    elapsed = (time.perf_counter() - start) * 1000
    logger.info("%s %s - %s - %.2fms", request.method, request.url.path, response.status_code, elapsed)
    return response


@app.on_event("startup")
def startup() -> None:
    _initialize_from_db()


@app.get("/")
def root():
    return FileResponse(str(STATIC_DIR / "index.html"))


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/api/proxies")
def get_proxies():
    return {"items": [_serialize_proxy(item) for item in _db_list_proxies()]}


@app.post("/api/proxies")
def add_proxy(raw: str = Form(...)):
    raw = (raw or "").strip()
    if not raw:
        raise HTTPException(status_code=400, detail="raw is required")
    try:
        row = _db_create_proxy(raw)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error))
    except UniqueViolation:
        raise HTTPException(status_code=409, detail="Proxy already exists")

    return {"item": _serialize_proxy(row)}


@app.delete("/api/proxies/{proxy_id}")
def remove_proxy(proxy_id: str):
    if _db_delete_proxy(proxy_id):
        return {"ok": True}
    raise HTTPException(status_code=404, detail="Proxy not found")


@app.post("/api/proxies/{proxy_id}/check")
def check_proxy(proxy_id: str):
    proxy = _db_get_proxy(proxy_id)
    if not proxy:
        raise HTTPException(status_code=404, detail="Proxy not found")

    result = _check_proxy_sync(proxy["raw"])
    updated_proxy = _db_update_proxy_after_check(proxy_id, result)
    response = dict(result)
    response.update({"proxy": _serialize_proxy(updated_proxy)})
    return response


@app.post("/api/tasks")
async def create_task(
    name: str = Form(""),
    payloadFile: UploadFile = File(...),
    useProxy: bool = Form(True),
    pauseMin: float = Form(1.2),
    pauseMax: float = Form(3.5),
    maxRows: int | None = Form(None),
    proxyIds: list[str] = Form(default=[]),
    telegramChatId: str = Form(""),
    telegramBotToken: str = Form(""),
):
    filename = (payloadFile.filename or "table.xlsx").strip()
    if not filename.lower().endswith(".xlsx"):
        raise HTTPException(status_code=400, detail="Only .xlsx files are supported")

    max_rows = _safe_int(maxRows, None)
    pause_min = _safe_float(pauseMin, 1.2)
    pause_max = _safe_float(pauseMax, 3.5)
    if pause_max < pause_min:
        pause_max = pause_min

    resolved_chat_id = _clean_telegram_chat_id(telegramChatId)
    resolved_token = _clean_telegram_bot_token(telegramBotToken)
    if not resolved_chat_id or not resolved_token:
        default_chat_id, default_token = _db_get_telegram_defaults()
        latest_chat_id, latest_token = _db_get_latest_telegram_config()
        if not resolved_chat_id:
            resolved_chat_id = (
                default_chat_id
                or _clean_telegram_chat_id(latest_chat_id)
                or _clean_telegram_chat_id(TELEGRAM_CHAT_ID)
            )
        if not resolved_token:
            resolved_token = (
                default_token
                or _clean_telegram_bot_token(latest_token)
                or _clean_telegram_bot_token(TELEGRAM_BOT_TOKEN)
            )

    selected_proxy_ids = [pid for pid in proxyIds if pid]
    all_proxies = [p for p in _db_list_proxies() if p["enabled"]]
    selected_raws = [proxy["raw"] for proxy in all_proxies if (not selected_proxy_ids or proxy["id"] in selected_proxy_ids)]

    task_id = str(uuid.uuid4())
    safe_file_name = quote(filename)
    upload_path = UPLOAD_DIR / f"{task_id}_{safe_file_name}"
    result_json_path = RESULT_DIR / f"{task_id}_result.json"

    with open(upload_path, "wb") as f:
        content = await payloadFile.read()
        f.write(content)

    task = _db_create_task(
        task_id=task_id,
        name=name.strip() or filename,
        filename=filename,
        pause_min=pause_min,
        pause_max=pause_max,
        max_rows=max_rows,
        use_proxy=useProxy,
        proxy_count=len(selected_raws),
        payload_path=str(upload_path),
        result_json=str(result_json_path),
        telegram_chat_id=resolved_chat_id,
        telegram_bot_token=resolved_token,
    )

    with TASK_LOCK:
        ACTIVE_TASKS[task_id] = {"events": queue.Queue(maxsize=250), "created": _utcnow()}

    threading.Thread(
        target=_run_task_background,
        kwargs={
            "task_id": task_id,
            "xlsx_path": str(upload_path),
            "pause_min": pause_min,
            "pause_max": pause_max,
            "max_rows": max_rows,
            "use_proxy": useProxy,
            "proxy_raws": selected_raws,
            "result_json": str(result_json_path),
            "telegram_chat_id": resolved_chat_id,
            "telegram_bot_token": resolved_token,
        },
        daemon=True,
    ).start()

    return task


@app.get("/api/settings/telegram")
def get_telegram_settings():
    chat_id, token = _db_get_telegram_defaults()
    if chat_id is None and TELEGRAM_CHAT_ID:
        chat_id = _clean_telegram_chat_id(TELEGRAM_CHAT_ID)
    if token is None and TELEGRAM_BOT_TOKEN:
        token = _clean_telegram_bot_token(TELEGRAM_BOT_TOKEN)
    return {"telegram_chat_id": chat_id, "telegram_bot_token": token}


@app.post("/api/settings/telegram")
def set_telegram_settings(
    telegramChatId: str = Form(""),
    telegramBotToken: str = Form(""),
):
    resolved_chat_id = _clean_telegram_chat_id(telegramChatId)
    resolved_token = _clean_telegram_bot_token(telegramBotToken)
    _db_set_setting("telegram_chat_id", resolved_chat_id)
    _db_set_setting("telegram_bot_token", resolved_token)
    return {"ok": True, "telegram_chat_id": resolved_chat_id, "telegram_bot_token": resolved_token}


@app.get("/api/tasks")
def list_tasks(limit: int = 0):
    return {"items": _db_list_tasks(limit=limit)}


@app.get("/api/tasks/{task_id}")
def task_by_id(task_id: str):
    task = _db_get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return task


@app.get("/api/tasks/{task_id}/events")
def task_events(task_id: str, limit: int = 200):
    if not _db_get_task(task_id):
        raise HTTPException(status_code=404, detail="Task not found")
    return {"items": _db_get_task_events(task_id, limit=limit)}


@app.get("/api/tasks/{task_id}/rows")
def task_rows(task_id: str, page: int = 1, page_size: int = 25, status: str = "all", search: str = ""):
    task_record = _db_get_task_record(task_id)
    if not task_record:
        raise HTTPException(status_code=404, detail="Task not found")

    task = _serialize_task(task_record, [])
    payload_rows = _load_task_payload_rows(task_record)
    processed_rows = _db_get_task_rows(task_id, limit=None)

    processed_by_source: dict[int, dict[str, Any]] = {}
    for processed in processed_rows:
        source_key = _safe_int(processed.get("source_row"), None)
        if source_key is None:
            source_key = _safe_int(processed.get("row"), None)
        if source_key is not None:
            processed_by_source[source_key] = processed

    merged_rows: list[dict[str, Any]] = []
    seen_keys: set[int] = set()

    def build_row_item(
        source_row: int | None,
        payload_row: dict[str, Any] | None,
        processed_row: dict[str, Any] | None,
    ) -> dict[str, Any]:
        payload = payload_row.get("payload", {}) if payload_row else {}
        raw = payload_row.get("raw", {}) if payload_row else {}
        status_code = _coerce_status_code(processed_row.get("status")) if processed_row else None
        status_key = _row_status_key(processed_row)
        status_label = _row_status_label(status_key, status_code)
        ok_value = None if not processed_row else bool(processed_row.get("ok"))

        if processed_row:
            if status_code == 202:
                message = processed_row.get("body_preview") or "Retry required"
            else:
                message = processed_row.get("body_preview") or "—"
            session = processed_row.get("session")
            proxy = processed_row.get("proxy")
        else:
            message = "Pending"
            session = None
            proxy = None

        row_value = source_row
        if row_value is None:
            row_value = _safe_int(processed_row.get("row"), None) if processed_row else None
        if row_value is None:
            row_value = 0

        item = {
            "row": row_value,
            "source_row": source_row,
            "birthDate": payload.get("birthDate") if payload else None,
            "lastName": payload.get("lastName") if payload else None,
            "ssn": payload.get("ssn") if payload else None,
            "status": status_code,
            "status_key": status_key,
            "status_label": status_label,
            "ok": ok_value,
            "session": session,
            "proxy": proxy,
            "message": message,
            "processed": bool(processed_row),
        }

        searchable_parts: list[Any] = [
            item["row"],
            item["source_row"],
            item["birthDate"],
            item["lastName"],
            item["ssn"],
            item["status"],
            item["status_label"],
            item["session"],
            item["proxy"],
            item["message"],
        ]
        searchable_parts.extend(payload.values() if payload else [])
        searchable_parts.extend(raw.values() if raw else [])
        item["_search"] = " ".join(str(value) for value in searchable_parts if value not in (None, "")).lower()
        return item

    if payload_rows:
        for payload_row in payload_rows:
            source_row = _safe_int(payload_row.get("source_row"), None)
            processed_row = None
            if source_row is not None:
                processed_row = processed_by_source.pop(source_row, None)
                seen_keys.add(source_row)
            merged_rows.append(build_row_item(source_row, payload_row, processed_row))

    for source_row, processed_row in sorted(processed_by_source.items(), key=lambda item: item[0]):
        if source_row in seen_keys:
            continue
        merged_rows.append(build_row_item(source_row, None, processed_row))

    status_key = (status or "all").strip().lower()
    search_query = (search or "").strip().lower()
    page_size = max(1, min(_safe_int(page_size, 25) or 25, 100))
    page = max(1, _safe_int(page, 1) or 1)

    def matches_status(item: dict[str, Any]) -> bool:
        if status_key in {"", "all"}:
            return True
        if status_key == item["status_key"]:
            return True
        if status_key.startswith("code:"):
            code = _safe_int(status_key.split(":", 1)[1], None)
            return code is not None and item["status"] == code
        return False

    filtered_rows = [item for item in merged_rows if matches_status(item) and (not search_query or search_query in item["_search"])]

    total_rows = len(merged_rows)
    visible_rows = len(filtered_rows)
    total_pages = max(1, (visible_rows + page_size - 1) // page_size)
    page = min(page, total_pages)
    start = (page - 1) * page_size
    page_rows = filtered_rows[start : start + page_size]

    status_counts = {
        "queued": 0,
        "retry": 0,
        "ok": 0,
        "registered": 0,
        "unregistered": 0,
        "not_found": 0,
        "unknown": 0,
        "error": 0,
    }
    code_counts: dict[int, int] = {}
    for item in merged_rows:
        status_counts[item["status_key"]] = status_counts.get(item["status_key"], 0) + 1
        if item["status"] is not None:
            code = int(item["status"])
            code_counts[code] = code_counts.get(code, 0) + 1

    status_filters: list[dict[str, Any]] = []
    if status_counts.get("queued"):
        status_filters.append({"key": "queued", "label": "Queued", "count": status_counts["queued"]})
    if status_counts.get("retry"):
        status_filters.append({"key": "retry", "label": "Retry", "count": status_counts["retry"]})
    if status_counts.get("ok"):
        status_filters.append({"key": "ok", "label": "Success", "count": status_counts["ok"]})
    if status_counts.get("registered"):
        status_filters.append({"key": "registered", "label": "Registered", "count": status_counts["registered"]})
    if status_counts.get("unregistered"):
        status_filters.append({"key": "unregistered", "label": "Unregistered", "count": status_counts["unregistered"]})
    if status_counts.get("not_found"):
        status_filters.append({"key": "not_found", "label": "Not found", "count": status_counts["not_found"]})
    if status_counts.get("error"):
        status_filters.append({"key": "error", "label": "Errors", "count": status_counts["error"]})
    if status_counts.get("unknown"):
        status_filters.append({"key": "unknown", "label": "Unknown", "count": status_counts["unknown"]})
    for code, count in sorted(code_counts.items()):
        status_filters.append({"key": f"code:{code}", "label": f"HTTP {code}", "count": count})

    for item in page_rows:
        item.pop("_search", None)

    summary = {
        "total": total_rows,
        "processed": status_counts["ok"] + status_counts["registered"] + status_counts["error"] + status_counts["unregistered"] + status_counts["not_found"] + status_counts["unknown"] + status_counts["retry"],
        "success": status_counts["ok"] + status_counts["registered"],
        "failed": status_counts["error"] + status_counts["unregistered"] + status_counts["not_found"],
        "visible": visible_rows,
    }

    return {
        "task": task,
        "items": page_rows,
        "page": page,
        "page_size": page_size,
        "total": visible_rows,
        "total_pages": total_pages,
        "status_filters": status_filters,
        "summary": summary,
    }


@app.get("/api/logs")
def get_logs(lines: int = 200):
    return {
        "items": _read_log_tail(LOG_FILE, lines=max(1, min(lines, 2000))),
        "file": str(LOG_FILE),
    }


@app.get("/api/tasks/{task_id}/result/json")
def download_task_json(task_id: str):
    task = _db_get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    result_path = task.get("result_file_json")
    if not result_path or not Path(result_path).exists():
        raise HTTPException(status_code=404, detail="Result json not ready")
    return FileResponse(str(result_path), media_type="application/json", filename=f"{task_id}_result.json")


@app.get("/api/tasks/{task_id}/result/xlsx")
def download_task_xlsx(task_id: str):
    task = _db_get_task_record(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    result_path = task.get("result_file_json")
    if not result_path or not Path(result_path).exists():
        raise HTTPException(status_code=404, detail="Result json not ready")

    try:
        xlsx_data = _build_task_xlsx_bytes(task_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail="openpyxl is required to export XLSX") from exc

    return Response(
        xlsx_data,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{task_id}_result.xlsx"'},
    )


@app.websocket("/ws/{task_id}")
async def websocket_task_stream(websocket: WebSocket, task_id: str):
    task = _db_get_task(task_id)
    if not task:
        await websocket.close(code=1008)
        return

    await websocket.accept()
    await websocket.send_text(
        json.dumps({"type": "state", "task": _db_get_task(task_id)}, ensure_ascii=False)
    )

    cache = ACTIVE_TASKS.get(task_id)
    if not cache:
        await websocket.send_text(json.dumps({"type": "task_closed", "task_id": task_id}, ensure_ascii=False))
        return
    q = cache["events"]

    try:
        while True:
            payload = None
            try:
                payload = q.get(timeout=1.0)
            except queue.Empty:
                pass

            if payload:
                await websocket.send_text(payload)

            refreshed = _db_get_task(task_id)
            if not refreshed:
                break
            if refreshed["status"] in {"finished", "failed"} and q.empty():
                await websocket.send_text(json.dumps({"type": "task_closed", "task_id": task_id}, ensure_ascii=False))
                break
            if not payload:
                await asyncio.sleep(0.2)

    except WebSocketDisconnect:
        pass
