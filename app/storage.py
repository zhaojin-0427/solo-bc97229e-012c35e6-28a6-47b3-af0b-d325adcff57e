"""本地持久化存储（SQLite）。

设计要点：

- 数据文件位于本地数据目录（``METER_DB_PATH``，默认 ``./data/meter_store.db``），
  通过 Docker 命名卷挂载，重启后数据与请求结果完整保留。
- 开启 WAL：读不阻塞写、写不阻塞读。求解（LP）在写事务之外完成，事务只做
  轻量的元数据/JSON 落盘，因此**求解期间旧版读数仍可查询**。
- 所有状态变更使用单条 ``BEGIN IMMEDIATE`` 事务：先抢占写锁，再在锁内完成
  幂等键检查、版本匹配检查、整行插入与版本推进。同版本的并发提交只有一个
  能拿到写锁并成功，其余在锁内看到版本已递增而失败；任何失败都整体回滚，
  旧版本完整保留，绝不出现半批数据或重复版本。
- 终局结果（成功发布与业务失败）连同响应体按 ``(period, request_key)``
  持久化，重放不依赖当前版本，即使响应丢失后已发布新版也能取回原结果。
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS periods (
    period          TEXT PRIMARY KEY,
    current_version INTEGER NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS meter_versions (
    period       TEXT NOT NULL,
    version      INTEGER NOT NULL,
    content_hash TEXT NOT NULL,
    request_key  TEXT NOT NULL,
    published_at TEXT NOT NULL,
    meters_json  TEXT NOT NULL,
    PRIMARY KEY (period, version),
    FOREIGN KEY (period) REFERENCES periods(period)
);

CREATE TABLE IF NOT EXISTS idempotency (
    period      TEXT NOT NULL,
    request_key TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    http_status INTEGER NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (period, request_key)
);
"""

_init_lock = threading.Lock()
_initialized: set[str] = set()


def db_path() -> Path:
    return Path(os.environ.get("METER_DB_PATH", "data/meter_store.db"))


def get_conn() -> sqlite3.Connection:
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(
        str(path),
        timeout=30.0,
        isolation_level=None,  # 手动事务
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=FULL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    with _init_lock:
        key = str(path.resolve())
        if key not in _initialized:
            conn.executescript(SCHEMA)
            _initialized.add(key)
    return conn


def reset_init_cache() -> None:
    """测试支持：更换数据库文件后清掉建表缓存。"""
    with _init_lock:
        _initialized.clear()


@contextmanager
def write_tx() -> Iterator[sqlite3.Connection]:
    """立即抢占写锁的事务；异常自动回滚，正常退出自动提交。"""
    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


@contextmanager
def read_tx() -> Iterator[sqlite3.Connection]:
    """只读事务：保证一次读取看到同一版本的一致快照。"""
    conn = get_conn()
    try:
        conn.execute("BEGIN")
        yield conn
        conn.execute("COMMIT")
    finally:
        conn.close()


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# 读取
# ---------------------------------------------------------------------------


def get_period_row(conn: sqlite3.Connection, period: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM periods WHERE period = ?", (period,)
    ).fetchone()


def get_idempotency_row(
    conn: sqlite3.Connection, period: str, request_key: str
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM idempotency WHERE period = ? AND request_key = ?",
        (period, request_key),
    ).fetchone()


def get_version_row(
    conn: sqlite3.Connection, period: str, version: int
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM meter_versions WHERE period = ? AND version = ?",
        (period, version),
    ).fetchone()


def list_periods(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT p.period, p.current_version, p.updated_at, "
            "v.published_at, v.meters_json "
            "FROM periods p LEFT JOIN meter_versions v "
            "ON v.period = p.period AND v.version = p.current_version "
            "ORDER BY p.period"
        ).fetchall()
    )


def list_versions(conn: sqlite3.Connection, period: str) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT version, content_hash, request_key, published_at, meters_json "
            "FROM meter_versions WHERE period = ? ORDER BY version",
            (period,),
        ).fetchall()
    )


# ---------------------------------------------------------------------------
# 写入（仅在 write_tx 内调用）
# ---------------------------------------------------------------------------


def insert_idempotency(
    conn: sqlite3.Connection,
    period: str,
    request_key: str,
    content_hash: str,
    http_status: int,
    response: dict[str, Any],
) -> None:
    conn.execute(
        "INSERT INTO idempotency "
        "(period, request_key, content_hash, http_status, response_json, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            period,
            request_key,
            content_hash,
            http_status,
            json.dumps(response, ensure_ascii=False, sort_keys=True),
            utc_now_iso(),
        ),
    )


def insert_version(
    conn: sqlite3.Connection,
    period: str,
    version: int,
    content_hash: str,
    request_key: str,
    published_at: str,
    meters: list[dict[str, Any]],
) -> None:
    conn.execute(
        "INSERT INTO meter_versions "
        "(period, version, content_hash, request_key, published_at, meters_json) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            period,
            version,
            content_hash,
            request_key,
            published_at,
            json.dumps(meters, ensure_ascii=False, sort_keys=True),
        ),
    )


def upsert_period(
    conn: sqlite3.Connection, period: str, current_version: int
) -> None:
    conn.execute(
        "INSERT INTO periods (period, current_version, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(period) DO UPDATE SET current_version = excluded.current_version, "
        "updated_at = excluded.updated_at",
        (period, current_version, utc_now_iso()),
    )
