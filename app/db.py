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

-- 急停记录：每批至多一条（UNIQUE batch_id）。
-- sheet_nos 为本轮上机的同批已领纸号（JSON 数组）。
CREATE TABLE IF NOT EXISTS press_stops (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id INTEGER NOT NULL REFERENCES batches(id),
    sheet_nos TEXT NOT NULL,          -- JSON: ["P1", "P2"]
    front_success INTEGER NOT NULL,   -- 印机记录：正面补印成功张数
    back_success INTEGER NOT NULL,    -- 印机记录：反面补印成功张数
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (batch_id)                 -- 每批只记一次急停
);

-- 报验 / 更正：每张上机纸至多一条，更正即整体覆盖 sides。
CREATE TABLE IF NOT EXISTS inspections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id INTEGER NOT NULL REFERENCES batches(id),
    sheet_no TEXT NOT NULL,
    sides TEXT NOT NULL,              -- JSON: ["正面"] 停机时已补成功的面
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (batch_id, sheet_no)
);

-- 续印单：按纸号领取，一张纸至多一张；保存发单时冻结的停机进度
-- completed_sides 与当时核定的待印内容 prints，之后重领仍给原单。
CREATE TABLE IF NOT EXISTS continuation_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_no TEXT NOT NULL,
    batch_id INTEGER NOT NULL REFERENCES batches(id),
    sheet_no TEXT NOT NULL,
    signature_no TEXT NOT NULL,
    completed_sides TEXT NOT NULL,    -- JSON: 发单时确定的停机已补面（冻结）
    prints TEXT NOT NULL,             -- JSON: 待印面 + 版号
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (batch_id, sheet_no)
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
