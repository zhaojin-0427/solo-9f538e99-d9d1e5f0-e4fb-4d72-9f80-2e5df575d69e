"""SQLite 存取层：连接与建表。"""
from __future__ import annotations

import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS signatures (
    batch_id INTEGER NOT NULL REFERENCES batches(id),
    signature_no TEXT NOT NULL,
    front_plate TEXT NOT NULL,
    back_plate TEXT NOT NULL,
    PRIMARY KEY (batch_id, signature_no)
);

CREATE TABLE IF NOT EXISTS sheets (
    batch_id INTEGER NOT NULL REFERENCES batches(id),
    sheet_no TEXT NOT NULL,
    printed_plate TEXT,  -- NULL 表示空白纸
    PRIMARY KEY (batch_id, sheet_no)
);

CREATE TABLE IF NOT EXISTS instructions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id INTEGER NOT NULL REFERENCES batches(id),
    signature_no TEXT NOT NULL,
    sheet_no TEXT NOT NULL,
    prints TEXT NOT NULL,  -- JSON: [{"side": "正面", "plate": "..."}]
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (batch_id, signature_no),  -- 一帖只发一张
    UNIQUE (batch_id, sheet_no)       -- 一张纸只发给一帖
);
"""


def connect(db_path: str) -> sqlite3.Connection:
    """新建连接。isolation_level=None 走自动提交，事务用显式 BEGIN IMMEDIATE 控制。

    timeout=10：并发写时等待锁最长 10 秒，避免直接报 database is locked。
    """
    conn = sqlite3.connect(db_path, timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(db_path: str) -> None:
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
    finally:
        conn.close()
