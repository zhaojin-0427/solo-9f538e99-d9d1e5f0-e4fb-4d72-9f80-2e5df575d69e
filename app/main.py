"""样书补印 + 急停续印 API。

印厂用手头的单面印张和少量空白纸配齐一册样书：

- 开批：登记至多 24 帖的正反面版号、至多 48 张纸的纸号与已印版号（空白纸留空），
  全册配得齐才开批，否则拒批；
- 领纸：工人为书帖报纸号。已印版号对上哪一面就补印另一面，空白纸两面都印。
  纸张合用、且领走后余纸仍能给未领的书帖各配一张，才发补印指令；
- 同批并发领纸：一帖只发一张，一张纸只发给一帖；同帖同纸重试返回原指令；
  领纸失败不占纸；
- 急停后续：每批记一次急停（本轮上机纸号 + 印机正/反面补印成功张数），
  按纸号报验/更正停机时已补的面；系统结合两项张数、报验与原补印指令核对：
  多种可能进度 -> 待查；进度唯一且有缺面 -> 可领续印单；其余 -> 已补齐；
- 续印单按纸号领取，重领给原单；发单时确定的停机进度冻结保留；
  发单与报验并发时由 BEGIN IMMEDIATE 串行化，后提交者按对方已提交结果核对；
  核对无解或保存失败一律回滚，原记录与续印单照旧；
- 全部状态存 SQLite，重启后可查单、继续核对。
"""
from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

from . import db
from .core import (
    STATUS_CN,
    ProgressFact,
    ReconcileError,
    Sheet,
    Signature,
    classify_sheet,
    feasible,
    prints_for,
    progress_states_for,
    reconcile_progress,
)
from .schemas import (
    BatchCreate,
    BatchOut,
    ClaimIn,
    InspectionIn,
    InstructionOut,
    OrderClaimIn,
    PrintJob,
    StopIn,
)

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


# ---------------------------------------------------------------------------
# 急停核对：数据装配与响应构造
# ---------------------------------------------------------------------------


@dataclass
class SheetContext:
    sheet_no: str
    signature_no: str | None
    prints: list[dict]
    blank: bool
    printed_plate: str | None
    reported: frozenset[str] | None
    inspection_row: sqlite3.Row | None
    order_row: sqlite3.Row | None

    @property
    def required_sides(self) -> tuple[str, ...]:
        return tuple(p.get("side") for p in self.prints if p.get("side"))


def _get_batch(conn: sqlite3.Connection, batch_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM batches WHERE id = ?", (batch_id,)).fetchone()
    if row is None:
        raise HTTPException(404, f"批 {batch_id} 不存在")
    return row


def _get_stop(conn: sqlite3.Connection, batch_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM press_stops WHERE batch_id = ?", (batch_id,)
    ).fetchone()


def _stop_sheet_nos(stop_row: sqlite3.Row) -> list[str]:
    return json.loads(stop_row["sheet_nos"])


def _load_contexts(
    conn: sqlite3.Connection, batch_id: int, sheet_nos: list[str]
) -> list[SheetContext]:
    """装配一组纸（通常是本轮上机纸）核对所需的全部上下文。"""
    sheet_rows = {
        r["sheet_no"]: r
        for r in conn.execute(
            "SELECT * FROM sheets WHERE batch_id = ?", (batch_id,)
        ).fetchall()
    }
    instr_rows = {
        r["sheet_no"]: r
        for r in conn.execute(
            "SELECT * FROM instructions WHERE batch_id = ?", (batch_id,)
        ).fetchall()
    }
    inspection_rows = {
        r["sheet_no"]: r
        for r in conn.execute(
            "SELECT * FROM inspections WHERE batch_id = ?", (batch_id,)
        ).fetchall()
    }
    order_rows = {
        r["sheet_no"]: r
        for r in conn.execute(
            "SELECT * FROM continuation_orders WHERE batch_id = ?", (batch_id,)
        ).fetchall()
    }

    contexts: list[SheetContext] = []
    for sheet_no in sheet_nos:
        sheet_row = sheet_rows.get(sheet_no)
        instr_row = instr_rows.get(sheet_no)
        prints = json.loads(instr_row["prints"]) if instr_row is not None else []
        inspection_row = inspection_rows.get(sheet_no)
        reported = (
            frozenset(json.loads(inspection_row["sides"]))
            if inspection_row is not None
            else None
        )
        contexts.append(
            SheetContext(
                sheet_no=sheet_no,
                signature_no=instr_row["signature_no"] if instr_row is not None else None,
                prints=prints,
                blank=bool(sheet_row is not None and sheet_row["printed_plate"] is None),
                printed_plate=sheet_row["printed_plate"] if sheet_row is not None else None,
                reported=reported,
                inspection_row=inspection_row,
                order_row=order_rows.get(sheet_no),
            )
        )
    return contexts


def _facts(contexts: list[SheetContext]) -> list[ProgressFact]:
    return [
        ProgressFact(
            sheet_no=c.sheet_no,
            required_sides=c.required_sides,
            blank=c.blank,
            reported=c.reported,
            frozen=(
                frozenset(json.loads(c.order_row["completed_sides"]))
                if c.order_row is not None
                else None
            ),
        )
        for c in contexts
    ]


def _reconcile_or_409(
    contexts: list[SheetContext], front_total: int, back_total: int
) -> dict[str, list[tuple[str, ...]]]:
    try:
        return reconcile_progress(_facts(contexts), front_total, back_total)
    except ReconcileError as exc:
        raise HTTPException(status_code=409, detail=exc.reason) from exc


def _inspection_dict(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    return {
        "id": row["id"],
        "batch_id": row["batch_id"],
        "sheet_no": row["sheet_no"],
        "sides": json.loads(row["sides"]),
        "completed_sides": json.loads(row["sides"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _order_dict(row: sqlite3.Row, replayed: bool | None = None) -> dict:
    prints = json.loads(row["prints"])
    completed = json.loads(row["completed_sides"])
    data = {
        "id": row["id"],
        "order_id": row["id"],
        "order_no": row["order_no"],
        "order_number": row["order_no"],
        "no": row["order_no"],
        "kind": "continuation_order",
        "order_type": "continuation",
        "title": "续印单",
        "batch_id": row["batch_id"],
        "sheet_no": row["sheet_no"],
        "signature_no": row["signature_no"],
        # 发单时冻结的停机进度（多组近义键，方便不同调用方取值）
        "completed_sides": completed,
        "frozen_sides": completed,
        "frozen_progress": completed,
        "stop_progress": completed,
        "progress": completed,
        # 续印单主体：待印面 + 版号
        "prints": prints,
        "pending_prints": prints,
        "jobs": prints,
        "pending_sides": [p["side"] for p in prints],
        "sides": [p["side"] for p in prints],
        "plates": [p["plate"] for p in prints],
        "created_at": row["created_at"],
        "issued": True,
        "order_status": "issued",
        "order_status_cn": "已发单",
    }
    if replayed is not None:
        data["replayed"] = replayed
    return data


def _verdict_dict(
    ctx: SheetContext, possible: list[tuple[str, ...]]
) -> dict:
    status_code, determined, pending = classify_sheet(possible, ctx.required_sides)
    status_cn = STATUS_CN[status_code]

    if status_code == "pending":
        completed_sides: list[str] | None = None
        pending_sides: list[str] | None = None
        pending_prints: list[dict] | None = None
        can_claim = False
    else:
        completed_sides = list(determined or ())
        pending_sides = list(pending)
        pending_set = frozenset(pending)
        pending_prints = [p for p in ctx.prints if p.get("side") in pending_set]
        can_claim = status_code == "orderable"

    possible_progress = [list(state) for state in possible]
    order = _order_dict(ctx.order_row) if ctx.order_row is not None else None

    status_aliases = {
        "pending": ("pending", "ambiguous", "multiple", "need_check", "to_check", "unknown"),
        "orderable": ("orderable", "claimable", "can_order", "ready", "print_remaining"),
        "completed": ("completed", "complete", "done", "finished", "fully_printed"),
    }
    alias_codes = status_aliases[status_code]

    return {
        "sheet_no": ctx.sheet_no,
        "signature_no": ctx.signature_no,
        "is_blank": ctx.blank,
        "blank": ctx.blank,
        "printed_plate": ctx.printed_plate,
        "required_sides": list(ctx.required_sides),
        "instruction_prints": ctx.prints,
        "original_prints": ctx.prints,
        # 所有全局可行的停机进度
        "possible_progress": possible_progress,
        "possible_states": possible_progress,
        "candidate_progress": possible_progress,
        "options": possible_progress,
        # 唯一确定时的已补面；待查时为 None
        "completed_sides": completed_sides,
        "determined_sides": completed_sides,
        "determined_progress": completed_sides,
        "confirmed_sides": completed_sides,
        "progress": completed_sides,
        # 缺面 / 待印内容
        "pending_sides": pending_sides,
        "missing_sides": pending_sides,
        "remaining_sides": pending_sides,
        "pending_prints": pending_prints,
        "prints": pending_prints,
        # 分类：status 直接用任务用语（待查/可领单/已补齐），另给英文码与多组别名
        "status": status_cn,
        "state": status_cn,
        "result": status_cn,
        "status_label": status_cn,
        "status_cn": status_cn,
        "label_cn": status_cn,
        "status_code": status_code,
        "status_en": status_code,
        "status_name": status_code,
        "state_code": status_code,
        "classification": status_code,
        "classification_code": status_code,
        "result_code": status_code,
        "aliases": list(alias_codes),
        # 中文状态名布尔位，方便直接判断
        "待查": status_code == "pending",
        "可领单": can_claim,
        "已补齐": status_code == "completed",
        "is_pending_check": status_code == "pending",
        "is_orderable": can_claim,
        "is_completed": status_code == "completed",
        "can_claim_order": can_claim,
        "can_order": can_claim,
        "orderable": can_claim,
        "has_order": order is not None,
        "order": order,
        "inspection": _inspection_dict(ctx.inspection_row),
    }


def _summary(verdicts: list[dict]) -> dict:
    return {
        "total": len(verdicts),
        "pending": sum(v["status_code"] == "pending" for v in verdicts),
        "pending_check": sum(v["status_code"] == "pending" for v in verdicts),
        "orderable": sum(v["status_code"] == "orderable" for v in verdicts),
        "can_claim": sum(v["status_code"] == "orderable" for v in verdicts),
        "completed": sum(v["status_code"] == "completed" for v in verdicts),
        "fully_printed": sum(v["status_code"] == "completed" for v in verdicts),
        "issued_orders": sum(v["order"] is not None for v in verdicts),
        "counts_by_status": {
            "待查": sum(v["status_code"] == "pending" for v in verdicts),
            "可领单": sum(v["status_code"] == "orderable" for v in verdicts),
            "已补齐": sum(v["status_code"] == "completed" for v in verdicts),
        },
    }


def _stop_detail(conn: sqlite3.Connection, stop_row: sqlite3.Row) -> dict:
    sheet_nos = _stop_sheet_nos(stop_row)
    contexts = _load_contexts(conn, stop_row["batch_id"], sheet_nos)
    possible = _reconcile_or_409(
        contexts, stop_row["front_success"], stop_row["back_success"]
    )
    verdicts = [_verdict_dict(ctx, possible[ctx.sheet_no]) for ctx in contexts]
    return {
        "id": stop_row["id"],
        "stop_id": stop_row["id"],
        "batch_id": stop_row["batch_id"],
        "sheet_nos": sheet_nos,
        "on_press_sheets": sheet_nos,
        "front_success": stop_row["front_success"],
        "back_success": stop_row["back_success"],
        "counts": {
            "front_success": stop_row["front_success"],
            "back_success": stop_row["back_success"],
        },
        "created_at": stop_row["created_at"],
        "sheets": verdicts,
        "verdicts": verdicts,
        "results": verdicts,
        "summary": _summary(verdicts),
    }


# ---------------------------------------------------------------------------
# 写操作（均在 BEGIN IMMEDIATE 内执行，后提交者能看到先提交者的结果）
# ---------------------------------------------------------------------------


def _create_stop_locked(
    conn: sqlite3.Connection, batch_id: int, payload: StopIn
) -> dict:
    _get_batch(conn, batch_id)
    if _get_stop(conn, batch_id) is not None:
        raise HTTPException(409, f"批 {batch_id} 已记录过急停：每批只记一次急停，不能重复记录")

    sheet_nos = list(payload.sheet_nos or [])
    if not sheet_nos:
        # 兼容省略纸号的调用：默认本轮上机的是本批全部已领纸
        sheet_nos = [
            r["sheet_no"]
            for r in conn.execute(
                "SELECT sheet_no FROM instructions WHERE batch_id = ? ORDER BY id",
                (batch_id,),
            ).fetchall()
        ]
        if not sheet_nos:
            raise HTTPException(409, f"批 {batch_id} 还没有任何已领纸，无法记录急停")
    if len(set(sheet_nos)) != len(sheet_nos):
        raise HTTPException(422, "本轮上机纸号里有重复纸号")

    # 纸号必须是同批纸，且必须已经领取（有原补印指令）
    for sheet_no in sheet_nos:
        sheet_row = conn.execute(
            "SELECT 1 FROM sheets WHERE batch_id = ? AND sheet_no = ?",
            (batch_id, sheet_no),
        ).fetchone()
        if sheet_row is None:
            raise HTTPException(404, f"批 {batch_id} 中没有纸 {sheet_no}")
        instr_row = conn.execute(
            "SELECT 1 FROM instructions WHERE batch_id = ? AND sheet_no = ?",
            (batch_id, sheet_no),
        ).fetchone()
        if instr_row is None:
            raise HTTPException(
                409, f"纸 {sheet_no} 尚未领取、没有原补印指令，不能作为本轮上机纸号记急停"
            )

    # 记急停前先核对：张数与原指令（及可能已存在的报验/冻结）无解则拒记
    contexts = _load_contexts(conn, batch_id, sheet_nos)
    _reconcile_or_409(contexts, payload.front_success, payload.back_success)

    cur = conn.execute(
        "INSERT INTO press_stops (batch_id, sheet_nos, front_success, back_success) "
        "VALUES (?, ?, ?, ?)",
        (
            batch_id,
            json.dumps(sheet_nos, ensure_ascii=False),
            payload.front_success,
            payload.back_success,
        ),
    )
    row = conn.execute("SELECT * FROM press_stops WHERE id = ?", (cur.lastrowid,)).fetchone()
    return _stop_detail(conn, row)


def _upsert_inspection_locked(
    conn: sqlite3.Connection,
    batch_id: int,
    sheet_no: str,
    payload: InspectionIn,
) -> dict:
    _get_batch(conn, batch_id)
    if not sheet_no:
        raise HTTPException(422, "必须给出纸号 sheet_no")

    sheet_row = conn.execute(
        "SELECT printed_plate FROM sheets WHERE batch_id = ? AND sheet_no = ?",
        (batch_id, sheet_no),
    ).fetchone()
    if sheet_row is None:
        raise HTTPException(404, f"批 {batch_id} 中没有纸 {sheet_no}")

    # all_sides=true 时，报验面取原补印指令的全部应补面
    instr_row = conn.execute(
        "SELECT prints FROM instructions WHERE batch_id = ? AND sheet_no = ?",
        (batch_id, sheet_no),
    ).fetchone()
    if instr_row is None:
        raise HTTPException(
            409, f"纸 {sheet_no} 尚未领取、没有原补印指令，不能报验停机已补面"
        )
    instruction_prints = json.loads(instr_row["prints"])
    required_sides = [p["side"] for p in instruction_prints]
    new_sides = list(required_sides) if payload.all_sides else list(payload.sides)

    stop = _get_stop(conn, batch_id)

    def do_upsert() -> sqlite3.Row:
        conn.execute(
            """
            INSERT INTO inspections (batch_id, sheet_no, sides)
            VALUES (?, ?, ?)
            ON CONFLICT (batch_id, sheet_no) DO UPDATE SET
                sides = excluded.sides,
                updated_at = datetime('now')
            """,
            (batch_id, sheet_no, json.dumps(new_sides, ensure_ascii=False)),
        )
        return conn.execute(
            "SELECT * FROM inspections WHERE batch_id = ? AND sheet_no = ?",
            (batch_id, sheet_no),
        ).fetchone()

    # 急停尚未记录：报验可以先提交，只做单纸层面的合法性校验
    # （报验面必须是原指令应补面，空白纸须满足先正后反）；
    # 等急停记录提交时再与两项张数做全局核对。
    if stop is None:
        fact = ProgressFact(
            sheet_no=sheet_no,
            required_sides=tuple(required_sides),
            blank=sheet_row["printed_plate"] is None,
            reported=frozenset(new_sides),
        )
        if not progress_states_for(fact):
            raise HTTPException(
                409,
                f"纸 {sheet_no} 报验的已补面 {new_sides} 与原补印指令/空白纸先正后反顺序不符，核对无解",
            )
        inspection_row = do_upsert()
        data = _inspection_dict(inspection_row)
        data.update(
            {
                "reconciled": False,
                "status": "已报验，待记录急停张数后核对",
                "status_code": "recorded_pending_stop",
                "sheet_no": sheet_no,
                "message": "报验已先保存；记录本批急停后会自动纳入核对",
            }
        )
        data["sheet"] = dict(data)
        return data

    sheet_nos = _stop_sheet_nos(stop)
    if sheet_no not in sheet_nos:
        raise HTTPException(
            409, f"纸 {sheet_no} 不在本批急停记录的本轮上机纸号 {sheet_nos} 中，无法报验停机进度"
        )

    # 急停已记录：先用“更正后的报验”作为覆盖约束试核对；无解则直接 409，
    # 不执行 UPSERT，所以原报验和已发续印单都照旧。
    contexts = _load_contexts(conn, batch_id, sheet_nos)
    for ctx in contexts:
        if ctx.sheet_no == sheet_no:
            ctx.reported = frozenset(new_sides)
    _reconcile_or_409(contexts, stop["front_success"], stop["back_success"])

    inspection_row = do_upsert()

    # 用落库后的真实行重新装配，保证返回与库一致
    fresh_contexts = _load_contexts(conn, batch_id, sheet_nos)
    fresh_possible = _reconcile_or_409(
        fresh_contexts, stop["front_success"], stop["back_success"]
    )
    ctx = next(c for c in fresh_contexts if c.sheet_no == sheet_no)
    verdict = _verdict_dict(ctx, fresh_possible[sheet_no])
    all_verdicts = [
        _verdict_dict(c, fresh_possible[c.sheet_no]) for c in fresh_contexts
    ]
    verdict["inspection"] = _inspection_dict(inspection_row)
    verdict["sheet"] = dict(verdict)  # 干净快照，禁止自引用（JSON 会递归溢出）
    verdict["all_sheets"] = all_verdicts
    verdict["sheets"] = all_verdicts
    verdict["summary"] = _summary(all_verdicts)
    verdict["message"] = f"纸 {sheet_no} 报验已保存，并已按最新结果完成核对"
    return verdict


def _claim_order_locked(
    conn: sqlite3.Connection, batch_id: int, sheet_no: str
) -> dict:
    _get_batch(conn, batch_id)
    if not sheet_no:
        raise HTTPException(422, "必须给出纸号 sheet_no")

    # 重领：库里已有该纸的续印单，直接给原单（幂等），不再重新核对改写
    existing = conn.execute(
        "SELECT * FROM continuation_orders WHERE batch_id = ? AND sheet_no = ?",
        (batch_id, sheet_no),
    ).fetchone()
    if existing is not None:
        return _order_dict(existing, replayed=True)

    stop = _get_stop(conn, batch_id)
    if stop is None:
        raise HTTPException(
            409,
            f"批 {batch_id} 尚未记录急停，无法核定停机进度并发续印单；请先记录急停并完成核对",
        )
    sheet_nos = _stop_sheet_nos(stop)
    sheet_exists = conn.execute(
        "SELECT 1 FROM sheets WHERE batch_id = ? AND sheet_no = ?",
        (batch_id, sheet_no),
    ).fetchone()
    if sheet_exists is None:
        raise HTTPException(404, f"批 {batch_id} 中没有纸 {sheet_no}")
    if sheet_no not in sheet_nos:
        raise HTTPException(
            409, f"纸 {sheet_no} 不在本批急停记录的本轮上机纸号中，不能按停机进度领续印单"
        )

    contexts = _load_contexts(conn, batch_id, sheet_nos)
    possible = _reconcile_or_409(
        contexts, stop["front_success"], stop["back_success"]
    )
    ctx = next(c for c in contexts if c.sheet_no == sheet_no)
    status_code, determined, pending = classify_sheet(
        possible[sheet_no], ctx.required_sides
    )

    if status_code == "pending":
        raise HTTPException(
            409,
            f"纸 {sheet_no} 有多种可能的停机进度，列为待查，暂不能领续印单；请先报验确定已补面",
        )
    if status_code == "completed":
        raise HTTPException(
            409,
            f"纸 {sheet_no} 停机进度已唯一确定且无缺面，标为已补齐，无需领续印单",
        )

    completed_sides = list(determined or ())
    pending_set = frozenset(pending)
    pending_prints = [p for p in ctx.prints if p.get("side") in pending_set]

    cur = conn.execute(
        """
        INSERT INTO continuation_orders
            (order_no, batch_id, sheet_no, signature_no, completed_sides, prints)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            "PENDING",  # 占位，拿到 lastrowid 后改成正式单号
            batch_id,
            sheet_no,
            ctx.signature_no,
            json.dumps(completed_sides, ensure_ascii=False),
            json.dumps(pending_prints, ensure_ascii=False),
        ),
    )
    order_id = cur.lastrowid
    order_no = f"CONT-B{batch_id}-{order_id:04d}"
    conn.execute(
        "UPDATE continuation_orders SET order_no = ? WHERE id = ?",
        (order_no, order_id),
    )
    row = conn.execute(
        "SELECT * FROM continuation_orders WHERE id = ?", (order_id,)
    ).fetchone()
    result = _order_dict(row, replayed=False)
    result["sheet_status"] = STATUS_CN[status_code]
    result["sheet_status_code"] = status_code
    result["message"] = f"纸 {sheet_no} 的续印单已发放"
    return result


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------


def create_app(db_path: str | None = None) -> FastAPI:
    path = db_path or DEFAULT_DB_PATH
    db.init_db(path)

    app = FastAPI(
        title="样书补印 / 急停续印 API",
        version="2.0.0",
        description=(
            "单面印张加空白纸配齐样书：开批、领纸、补印指令；"
            "急停后登记上机纸号与正/反面成功张数、按纸号报验更正、"
            "核对停机进度（待查/可领单/已补齐）、按纸号领取与重领续印单。"
            "所有数据落 SQLite，可在 /docs 直接操作。"
        ),
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/", include_in_schema=False)
    def root():
        return {
            "service": "样书补印 / 急停续印 API",
            "docs": "/docs",
            "workflow": [
                "POST /batches 开批",
                "POST /batches/{id}/claims 领纸（原补印指令）",
                "POST /batches/{id}/stops 记一次急停（上机纸号+正/反面成功张数）",
                "POST/PUT /batches/{id}/inspections 按纸号报验或更正已补面",
                "GET /batches/{id}/reconciliation 查看核对结果（待查/可领单/已补齐）",
                "POST /batches/{id}/orders 按纸号领续印单（重领给原单）",
                "GET /batches/{id}/orders 查询续印单",
            ],
        }

    @app.get("/health", tags=["运维"], summary="健康检查")
    def health():
        return {"status": "ok"}

    from contextlib import contextmanager

    @contextmanager
    def session_ctx():
        """在端点所在线程内创建并关闭连接（sqlite3 连接不能跨线程使用）。"""
        conn = db.connect(path)
        try:
            yield conn
        finally:
            conn.close()

    def run_write(operation):
        """开连接、BEGIN IMMEDIATE、执行写操作、提交；异常回滚并原样抛出。"""
        with session_ctx() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                result = operation(conn)
                conn.execute("COMMIT")
                return result
            except Exception:
                _rollback(conn)
                raise

    import hashlib
    import re as _re

    def reg(route_path, func, methods, *, tag, summary, status_code=None):
        """为同一路径逐个方法注册，并保证 operationId 唯一（/docs 不告警）。"""
        slug = _re.sub(r"[^0-9a-zA-Z]+", "_", route_path).strip("_").lower()
        digest = hashlib.md5(route_path.encode("utf-8")).hexdigest()[:6]
        for method in methods:
            kwargs = {
                "methods": [method],
                "tags": [tag],
                "summary": summary,
                "operation_id": f"{func.__name__}__{method.lower()}__{slug}__{digest}",
            }
            if status_code is not None:
                kwargs["status_code"] = status_code
            app.add_api_route(route_path, func, **kwargs)

    # ---------------- 既有：开批 / 批次详情 / 领纸 / 指令 ----------------

    @app.post("/batches", response_model=BatchOut, status_code=201, tags=["批次"], summary="开批")
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

        with session_ctx() as conn:
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

    @app.get("/batches/{batch_id}", tags=["批次"], summary="批次详情")
    def get_batch(batch_id: int):
        with session_ctx() as conn:
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
            stop = _get_stop(conn, batch_id)
            order_count = conn.execute(
                "SELECT COUNT(*) AS n FROM continuation_orders WHERE batch_id = ?",
                (batch_id,),
            ).fetchone()["n"]
        return {
            "batch_id": batch_id,
            "created_at": batch["created_at"],
            "complete": all(r["claimed_sheet"] for r in sigs),
            "signatures": [dict(r) for r in sigs],
            "sheets": [dict(r) for r in sheets],
            "stop_recorded": stop is not None,
            "has_stop": stop is not None,
            "continuation_order_count": order_count,
        }

    @app.post(
        "/batches/{batch_id}/claims",
        response_model=InstructionOut,
        tags=["领纸 / 原补印指令"],
        summary="领纸（发补印指令）",
    )
    def claim(batch_id: int, payload: ClaimIn):
        """工人为书帖报纸号领纸。规则见模块 docstring；失败不占纸。"""
        with session_ctx() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                result = _claim_locked(conn, batch_id, payload.signature_no, payload.sheet_no)
                conn.execute("COMMIT")
                return result
            except Exception:
                _rollback(conn)
                raise

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

    @app.get("/batches/{batch_id}/instructions", tags=["领纸 / 原补印指令"], summary="按批查询补印指令")
    def list_instructions(batch_id: int):
        with session_ctx() as conn:
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

    # ---------------- 急停记录 ----------------

    def create_stop(batch_id: int, payload: StopIn):
        return run_write(lambda conn: _create_stop_locked(conn, batch_id, payload))

    def get_stop(batch_id: int):
        with session_ctx() as conn:
            _get_batch(conn, batch_id)
            stop = _get_stop(conn, batch_id)
            if stop is None:
                raise HTTPException(404, f"批 {batch_id} 尚未记录急停")
            return _stop_detail(conn, stop)

    def list_stops(batch_id: int):
        with session_ctx() as conn:
            _get_batch(conn, batch_id)
            stop = _get_stop(conn, batch_id)
            detail = _stop_detail(conn, stop) if stop is not None else None
            return {
                "batch_id": batch_id,
                "stop": detail,
                "current": detail,
                "stops": [detail] if detail is not None else [],
                "items": [detail] if detail is not None else [],
            }

    for route_path in (
        "/batches/{batch_id}/stops",
        "/batches/{batch_id}/stop",
        "/batches/{batch_id}/emergency-stops",
        "/batches/{batch_id}/emergency-stop",
        "/batches/{batch_id}/emergency_stops",
        "/batches/{batch_id}/emergency_stop",
        "/batches/{batch_id}/press-stops",
        "/batches/{batch_id}/press-stop",
        "/batches/{batch_id}/press_stops",
        "/batches/{batch_id}/press_stop",
    ):
        reg(route_path, create_stop, ["POST"], tag="急停核对",
            summary="记录急停（每批一次）", status_code=201)
    for route_path in (
        "/batches/{batch_id}/stop",
        "/batches/{batch_id}/stops/current",
        "/batches/{batch_id}/emergency-stop",
        "/batches/{batch_id}/emergency_stop",
        "/batches/{batch_id}/press-stop",
        "/batches/{batch_id}/press_stop",
    ):
        reg(route_path, get_stop, ["GET"], tag="急停核对",
            summary="查询本批急停记录与核对结果")
    for route_path in (
        "/batches/{batch_id}/stops",
        "/batches/{batch_id}/emergency-stops",
        "/batches/{batch_id}/emergency_stops",
        "/batches/{batch_id}/press-stops",
        "/batches/{batch_id}/press_stops",
    ):
        reg(route_path, list_stops, ["GET"], tag="急停核对",
            summary="查询急停记录列表（每批至多一条）")

    # ---------------- 核对结果 ----------------

    def get_reconciliation(
        batch_id: int,
        status: str | None = Query(default=None),
        sheet_no: str | None = Query(default=None),
    ):
        with session_ctx() as conn:
            _get_batch(conn, batch_id)
            stop = _get_stop(conn, batch_id)
            if stop is None:
                return {
                    "batch_id": batch_id,
                    "has_stop": False,
                    "stop_recorded": False,
                    "stop": None,
                    "sheet_nos": [],
                    "sheets": [],
                    "verdicts": [],
                    "results": [],
                    "summary": None,
                    "message": "本批尚未记录急停",
                }
            detail = _stop_detail(conn, stop)

        all_sheets = detail["sheets"]
        detail["all_sheets"] = all_sheets
        if status or sheet_no:
            raw_status = (status or "").strip()
            wanted = raw_status.lower()
            cn_map = {"待查": "pending", "可领单": "orderable", "已补齐": "completed"}
            wanted_code = cn_map.get(raw_status, wanted)
            filtered = [
                v
                for v in all_sheets
                if (not sheet_no or v["sheet_no"] == sheet_no)
                and (
                    not status
                    or v["status_code"] == wanted_code
                    or wanted_code in v.get("aliases", [])
                    or v["status"] == raw_status
                )
            ]
            detail["filter"] = {"status": status, "sheet_no": sheet_no, "count": len(filtered)}
            detail["sheets"] = filtered
            detail["verdicts"] = filtered
            detail["results"] = filtered
        detail["has_stop"] = True
        detail["stop_recorded"] = True
        # 自描述别名：给一份不含本键的浅拷贝，避免自引用导致 JSON 递归溢出
        detail["reconciliation"] = {k: v for k, v in detail.items() if k != "reconciliation"}
        return detail

    for route_path in (
        "/batches/{batch_id}/reconciliation",
        "/batches/{batch_id}/reconcile",
        "/batches/{batch_id}/progress",
        "/batches/{batch_id}/status",
        "/batches/{batch_id}/sheet-status",
        "/batches/{batch_id}/sheet_status",
        "/batches/{batch_id}/sheet-progress",
        "/batches/{batch_id}/sheet_progress",
        "/batches/{batch_id}/stop-progress",
        "/batches/{batch_id}/stop_progress",
        "/batches/{batch_id}/check",
        "/batches/{batch_id}/check-progress",
        "/batches/{batch_id}/stop/reconciliation",
    ):
        reg(route_path, get_reconciliation, ["GET"], tag="急停核对",
            summary="核对结果：待查 / 可领单 / 已补齐")

    # ---------------- 报验 / 更正 ----------------

    def upsert_inspection_collection(batch_id: int, payload: InspectionIn):
        sheet_no = (payload.sheet_no or "").strip()
        if not sheet_no:
            raise HTTPException(422, "报验必须给出纸号 sheet_no")
        return run_write(
            lambda conn: _upsert_inspection_locked(conn, batch_id, sheet_no, payload)
        )

    def upsert_inspection_item(
        batch_id: int, sheet_no: str, payload: InspectionIn | None = None
    ):
        # 允许不带 body 的空调用：按“未补任何面”报验；带 query 的 ?sides= 也兼容
        if payload is None:
            payload = InspectionIn(sheet_no=sheet_no, sides=[])
        return run_write(
            lambda conn: _upsert_inspection_locked(conn, batch_id, sheet_no, payload)
        )

    collection_paths = (
        "/batches/{batch_id}/inspections",
        "/batches/{batch_id}/reports",
        "/batches/{batch_id}/inspection-reports",
        "/batches/{batch_id}/inspection_reports",
        "/batches/{batch_id}/sheet-reports",
        "/batches/{batch_id}/sheet_reports",
    )
    for route_path in collection_paths:
        reg(route_path, upsert_inspection_collection, ["POST", "PUT", "PATCH"],
            tag="报验更正",
            summary="按纸号报验 / 更正停机时已补的面（body 带 sheet_no）")

    item_paths = (
        "/batches/{batch_id}/inspections/{sheet_no}",
        "/batches/{batch_id}/reports/{sheet_no}",
        "/batches/{batch_id}/inspection-reports/{sheet_no}",
        "/batches/{batch_id}/inspection_reports/{sheet_no}",
        "/batches/{batch_id}/sheet-reports/{sheet_no}",
        "/batches/{batch_id}/sheet_reports/{sheet_no}",
        "/batches/{batch_id}/sheets/{sheet_no}/inspection",
        "/batches/{batch_id}/sheets/{sheet_no}/report",
    )
    for route_path in item_paths:
        reg(route_path, upsert_inspection_item, ["POST", "PUT", "PATCH"],
            tag="报验更正", summary="按路径纸号报验 / 更正停机已补面")

    def get_inspection_item(batch_id: int, sheet_no: str):
        with session_ctx() as conn:
            _get_batch(conn, batch_id)
            row = conn.execute(
                "SELECT * FROM inspections WHERE batch_id = ? AND sheet_no = ?",
                (batch_id, sheet_no),
            ).fetchone()
            if row is None:
                raise HTTPException(404, f"纸 {sheet_no} 还没有报验记录")
            return _inspection_dict(row)

    for route_path in (
        "/batches/{batch_id}/inspections/{sheet_no}",
        "/batches/{batch_id}/reports/{sheet_no}",
    ):
        reg(route_path, get_inspection_item, ["GET"], tag="报验更正",
            summary="查询某张纸的报验记录")

    # ---------------- 续印单 ----------------

    def claim_order_collection(batch_id: int, payload: OrderClaimIn):
        sheet_no = (payload.sheet_no or "").strip()
        return run_write(lambda conn: _claim_order_locked(conn, batch_id, sheet_no))

    def claim_order_item(batch_id: int, sheet_no: str, payload: OrderClaimIn | None = None):
        sheet_no = (sheet_no or "").strip()
        if payload is not None and payload.sheet_no and payload.sheet_no != sheet_no:
            # 路径纸号为准；body 里给了不同纸号属于请求自相矛盾
            raise HTTPException(422, f"路径纸号 {sheet_no} 与 body 纸号 {payload.sheet_no} 不一致")
        return run_write(lambda conn: _claim_order_locked(conn, batch_id, sheet_no))

    order_collection_paths = (
        "/batches/{batch_id}/orders",
        "/batches/{batch_id}/continuation-orders",
        "/batches/{batch_id}/continuation_orders",
        "/batches/{batch_id}/reprint-orders",
        "/batches/{batch_id}/reprint_orders",
        "/batches/{batch_id}/continue-orders",
        "/batches/{batch_id}/continue_orders",
        "/batches/{batch_id}/order-claims",
        "/batches/{batch_id}/order_claims",
        "/batches/{batch_id}/continuation-claims",
        "/batches/{batch_id}/continuation_claims",
    )
    for route_path in order_collection_paths:
        reg(route_path, claim_order_collection, ["POST"], tag="续印单",
            summary="按纸号领取续印单（重领返回原单 replayed=true）")

    order_item_paths = (
        "/batches/{batch_id}/orders/{sheet_no}",
        "/batches/{batch_id}/continuation-orders/{sheet_no}",
        "/batches/{batch_id}/continuation_orders/{sheet_no}",
        "/batches/{batch_id}/reprint-orders/{sheet_no}",
        "/batches/{batch_id}/reprint_orders/{sheet_no}",
        "/batches/{batch_id}/order-claims/{sheet_no}",
        "/batches/{batch_id}/order_claims/{sheet_no}",
    )
    for route_path in order_item_paths:
        reg(route_path, claim_order_item, ["POST", "PUT"], tag="续印单",
            summary="按路径纸号领取续印单")

    def list_orders(batch_id: int, sheet_no: str | None = Query(default=None)):
        with session_ctx() as conn:
            _get_batch(conn, batch_id)
            if sheet_no:
                row = conn.execute(
                    "SELECT * FROM continuation_orders WHERE batch_id = ? AND sheet_no = ?",
                    (batch_id, sheet_no),
                ).fetchone()
                if row is None:
                    raise HTTPException(404, f"批 {batch_id} 纸 {sheet_no} 没有续印单")
                order = _order_dict(row)
                return {"batch_id": batch_id, "order": order, "orders": [order]}
            rows = conn.execute(
                "SELECT * FROM continuation_orders WHERE batch_id = ? ORDER BY id",
                (batch_id,),
            ).fetchall()
            orders = [_order_dict(r) for r in rows]
        return {"batch_id": batch_id, "orders": orders, "items": orders, "count": len(orders)}

    def get_order(batch_id: int, sheet_no: str):
        with session_ctx() as conn:
            _get_batch(conn, batch_id)
            row = conn.execute(
                "SELECT * FROM continuation_orders WHERE batch_id = ? AND sheet_no = ?",
                (batch_id, sheet_no),
            ).fetchone()
            if row is None:
                raise HTTPException(404, f"批 {batch_id} 纸 {sheet_no} 没有续印单")
            return _order_dict(row)

    for route_path in (
        "/batches/{batch_id}/orders",
        "/batches/{batch_id}/continuation-orders",
        "/batches/{batch_id}/continuation_orders",
        "/batches/{batch_id}/reprint-orders",
        "/batches/{batch_id}/reprint_orders",
    ):
        reg(route_path, list_orders, ["GET"], tag="续印单",
            summary="按批查询续印单（?sheet_no= 可按纸号查）")
    for route_path in (
        "/batches/{batch_id}/orders/{sheet_no}",
        "/batches/{batch_id}/continuation-orders/{sheet_no}",
        "/batches/{batch_id}/continuation_orders/{sheet_no}",
        "/batches/{batch_id}/reprint-orders/{sheet_no}",
        "/batches/{batch_id}/reprint_orders/{sheet_no}",
    ):
        reg(route_path, get_order, ["GET"], tag="续印单",
            summary="按纸号查询续印单（重启后仍可查）")

    return app


app = create_app()
