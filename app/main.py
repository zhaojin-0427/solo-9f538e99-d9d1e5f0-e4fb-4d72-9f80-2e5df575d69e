"""样书补印 API。

印厂用手头的单面印张和少量空白纸配齐一册样书：

- 开批：登记至多 24 帖的正反面版号、至多 48 张纸的纸号与已印版号（空白纸留空），
  全册配得齐才开批，否则拒批；
- 领纸：工人为书帖报纸号。已印版号对上哪一面就补印另一面，空白纸两面都印。
  纸张合用、且领走后余纸仍能给未领的书帖各配一张，才发补印指令；
- 同批并发领纸：一帖只发一张，一张纸只发给一帖；同帖同纸重试返回原指令；
  领纸失败不占纸；
- 指令存 SQLite，可按批查询。
"""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager

from fastapi import FastAPI, HTTPException

from . import db
from .core import Sheet, Signature, feasible, prints_for
from .schemas import BatchCreate, BatchOut, ClaimIn, InstructionOut, PrintJob

DEFAULT_DB_PATH = os.environ.get("REPRINT_DB", "reprint.db")


def _rollback(conn: sqlite3.Connection) -> None:
    try:
        conn.execute("ROLLBACK")
    except sqlite3.OperationalError:
        pass  # 事务已提交或已回滚


def _instruction_from_row(row: sqlite3.Row, replayed: bool) -> InstructionOut:
    return InstructionOut(
        batch_id=row["batch_id"],
        signature_no=row["signature_no"],
        sheet_no=row["sheet_no"],
        prints=[PrintJob(**p) for p in json.loads(row["prints"])],
        replayed=replayed,
    )


def _claim_locked(
    conn: sqlite3.Connection, batch_id: int, signature_no: str, sheet_no: str
) -> InstructionOut:
    """在已持有写锁（BEGIN IMMEDIATE）的连接上处理一次领纸。"""
    if conn.execute("SELECT 1 FROM batches WHERE id = ?", (batch_id,)).fetchone() is None:
        raise HTTPException(404, f"批 {batch_id} 不存在")
    sig_row = conn.execute(
        "SELECT * FROM signatures WHERE batch_id = ? AND signature_no = ?",
        (batch_id, signature_no),
    ).fetchone()
    if sig_row is None:
        raise HTTPException(404, f"批 {batch_id} 中没有帖 {signature_no}")
    sheet_row = conn.execute(
        "SELECT * FROM sheets WHERE batch_id = ? AND sheet_no = ?",
        (batch_id, sheet_no),
    ).fetchone()
    if sheet_row is None:
        raise HTTPException(404, f"批 {batch_id} 中没有纸 {sheet_no}")

    # 同帖同纸重试：返回原指令；同帖换纸：一帖只发一张，拒发
    existing = conn.execute(
        "SELECT * FROM instructions WHERE batch_id = ? AND signature_no = ?",
        (batch_id, signature_no),
    ).fetchone()
    if existing is not None:
        if existing["sheet_no"] == sheet_no:
            return _instruction_from_row(existing, replayed=True)
        raise HTTPException(409, f"帖 {signature_no} 已领纸 {existing['sheet_no']}，一帖只发一张")

    # 一张纸只发给一帖
    taken = conn.execute(
        "SELECT signature_no FROM instructions WHERE batch_id = ? AND sheet_no = ?",
        (batch_id, sheet_no),
    ).fetchone()
    if taken is not None:
        raise HTTPException(409, f"纸 {sheet_no} 已发给帖 {taken['signature_no']}，一张纸只发给一帖")

    # 纸张合用：已印版号对上哪一面就补印另一面，空白纸两面都印
    sig = Signature(sig_row["signature_no"], sig_row["front_plate"], sig_row["back_plate"])
    sheet = Sheet(sheet_row["sheet_no"], sheet_row["printed_plate"])
    prints = prints_for(sig, sheet)
    if prints is None:
        raise HTTPException(
            409,
            f"纸 {sheet_no} 的已印版号「{sheet_row['printed_plate']}」与帖 {signature_no} "
            "的正反面版号都对不上，纸张不合用",
        )

    # 余纸还能给未领的书帖各配一张，才发指令
    claimed = conn.execute(
        "SELECT signature_no, sheet_no FROM instructions WHERE batch_id = ?", (batch_id,)
    ).fetchall()
    claimed_sigs = {r["signature_no"] for r in claimed} | {signature_no}
    claimed_sheets = {r["sheet_no"] for r in claimed} | {sheet_no}
    rest_sigs = [
        Signature(r["signature_no"], r["front_plate"], r["back_plate"])
        for r in conn.execute(
            "SELECT * FROM signatures WHERE batch_id = ?", (batch_id,)
        ).fetchall()
        if r["signature_no"] not in claimed_sigs
    ]
    rest_sheets = [
        Sheet(r["sheet_no"], r["printed_plate"])
        for r in conn.execute("SELECT * FROM sheets WHERE batch_id = ?", (batch_id,)).fetchall()
        if r["sheet_no"] not in claimed_sheets
    ]
    if not feasible(rest_sigs, rest_sheets):
        raise HTTPException(409, "领走此纸后，余纸不够给未领的书帖各配一张，不予发放")

    cur = conn.execute(
        "INSERT INTO instructions (batch_id, signature_no, sheet_no, prints) VALUES (?, ?, ?, ?)",
        (batch_id, signature_no, sheet_no, json.dumps(prints, ensure_ascii=False)),
    )
    row = conn.execute("SELECT * FROM instructions WHERE id = ?", (cur.lastrowid,)).fetchone()
    return _instruction_from_row(row, replayed=False)


def create_app(db_path: str | None = None) -> FastAPI:
    path = db_path or DEFAULT_DB_PATH
    db.init_db(path)

    app = FastAPI(
        title="样书补印 API",
        version="1.0.0",
        description="单面印张加空白纸配齐一册样书：开批、领纸、补印指令管理。",
    )

    @contextmanager
    def session():
        """在端点所在线程内创建并关闭连接（sqlite3 连接不能跨线程使用）。"""
        conn = db.connect(path)
        try:
            yield conn
        finally:
            conn.close()

    @app.post("/batches", response_model=BatchOut, status_code=201, summary="开批")
    def create_batch(payload: BatchCreate):
        """登记书帖与纸张并开批。全册配得齐才开批，否则返回 409 且不落库。"""
        sig_nos = [s.signature_no for s in payload.signatures]
        sheet_nos = [s.sheet_no for s in payload.sheets]
        if len(set(sig_nos)) != len(sig_nos):
            raise HTTPException(422, "帖号重复")
        if len(set(sheet_nos)) != len(sheet_nos):
            raise HTTPException(422, "纸号重复")

        signatures = [
            Signature(s.signature_no, s.front_plate, s.back_plate) for s in payload.signatures
        ]
        sheets = [Sheet(s.sheet_no, s.printed_plate) for s in payload.sheets]
        if not feasible(signatures, sheets):
            raise HTTPException(409, "全册配不齐：现有纸张无法给每帖各配一张合用纸张，不予开批")

        with session() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                batch_id = conn.execute("INSERT INTO batches DEFAULT VALUES").lastrowid
                conn.executemany(
                    "INSERT INTO signatures (batch_id, signature_no, front_plate, back_plate) "
                    "VALUES (?, ?, ?, ?)",
                    [(batch_id, s.signature_no, s.front_plate, s.back_plate) for s in signatures],
                )
                conn.executemany(
                    "INSERT INTO sheets (batch_id, sheet_no, printed_plate) VALUES (?, ?, ?)",
                    [(batch_id, s.sheet_no, s.printed_plate) for s in sheets],
                )
                conn.execute("COMMIT")
            except Exception:
                _rollback(conn)
                raise
            created_at = conn.execute(
                "SELECT created_at FROM batches WHERE id = ?", (batch_id,)
            ).fetchone()["created_at"]
        return BatchOut(
            batch_id=batch_id,
            created_at=created_at,
            signature_count=len(signatures),
            sheet_count=len(sheets),
        )

    @app.get("/batches/{batch_id}", summary="批次详情")
    def get_batch(batch_id: int):
        with session() as conn:
            batch = conn.execute("SELECT * FROM batches WHERE id = ?", (batch_id,)).fetchone()
            if batch is None:
                raise HTTPException(404, f"批 {batch_id} 不存在")
            sigs = conn.execute(
                """
                SELECT s.signature_no, s.front_plate, s.back_plate, i.sheet_no AS claimed_sheet
                FROM signatures s
                LEFT JOIN instructions i
                  ON i.batch_id = s.batch_id AND i.signature_no = s.signature_no
                WHERE s.batch_id = ? ORDER BY s.signature_no
                """,
                (batch_id,),
            ).fetchall()
            sheets = conn.execute(
                """
                SELECT sh.sheet_no, sh.printed_plate, i.signature_no AS claimed_by
                FROM sheets sh
                LEFT JOIN instructions i
                  ON i.batch_id = sh.batch_id AND i.sheet_no = sh.sheet_no
                WHERE sh.batch_id = ? ORDER BY sh.sheet_no
                """,
                (batch_id,),
            ).fetchall()
        return {
            "batch_id": batch_id,
            "created_at": batch["created_at"],
            "complete": all(r["claimed_sheet"] for r in sigs),
            "signatures": [dict(r) for r in sigs],
            "sheets": [dict(r) for r in sheets],
        }

    @app.post(
        "/batches/{batch_id}/claims", response_model=InstructionOut, summary="领纸（发补印指令）"
    )
    def claim(batch_id: int, payload: ClaimIn):
        """工人为书帖报纸号领纸。规则见模块 docstring；失败不占纸。"""
        with session() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                result = _claim_locked(conn, batch_id, payload.signature_no, payload.sheet_no)
                conn.execute("COMMIT")
                return result
            except Exception:
                _rollback(conn)
                raise

    @app.get("/batches/{batch_id}/instructions", summary="按批查询补印指令")
    def list_instructions(batch_id: int):
        with session() as conn:
            if conn.execute("SELECT 1 FROM batches WHERE id = ?", (batch_id,)).fetchone() is None:
                raise HTTPException(404, f"批 {batch_id} 不存在")
            rows = conn.execute(
                "SELECT * FROM instructions WHERE batch_id = ? ORDER BY id", (batch_id,)
            ).fetchall()
        return {
            "batch_id": batch_id,
            "instructions": [
                {
                    "signature_no": r["signature_no"],
                    "sheet_no": r["sheet_no"],
                    "prints": json.loads(r["prints"]),
                    "created_at": r["created_at"],
                }
                for r in rows
            ],
        }

    return app


app = create_app()
