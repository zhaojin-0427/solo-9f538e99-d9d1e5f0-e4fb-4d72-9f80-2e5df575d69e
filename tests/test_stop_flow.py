"""急停 -> 报验 -> 核对 -> 续印单 全流程测试。

重点覆盖任务要求：
- 每批只记一次急停；上机纸必须是同批已领纸；
- 空白纸“先正后反”的进度前缀约束；
- 多解 -> 待查；唯一且有缺面 -> 可领单；唯一且无缺面 -> 已补齐；
- 续印单按纸号领取、重领给原单；
- 发单后更正报验：矛盾更正被 409 拒绝，原报验与原单照旧；
  一致/涉它纸的更正可提交，但已发单的冻结进度与原单不变；
- 发单与报验并发时，后提交者按先提交者结果核对；
- 核对无解 / 保存失败 -> 回滚，原记录与续印单照旧；
- 重启（同库新建 app）后可查单、继续核对。
"""
from __future__ import annotations

import concurrent.futures
import socket
import threading
import time

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient

from app.main import create_app


def sig(no, front=None, back=None):
    return {"signature_no": no, "front_plate": front or f"{no}-F", "back_plate": back or f"{no}-B"}


def blank_sheet(no):
    return {"sheet_no": no}


def plate_sheet(no, plate):
    return {"sheet_no": no, "printed_plate": plate}


def open_batch(client, sigs, sheets):
    r = client.post("/batches", json={"signatures": sigs, "sheets": sheets})
    assert r.status_code == 201, r.text
    return r.json()["batch_id"]


def claim(client, bid, s, p):
    return client.post(f"/batches/{bid}/claims", json={"signature_no": s, "sheet_no": p})


def stop(client, bid, sheets, f, b, **extra):
    body = {"sheet_nos": sheets, "front_success": f, "back_success": b, **extra}
    return client.post(f"/batches/{bid}/stops", json=body)


def inspect(client, bid, sheet, sides=None, **extra):
    body = {"sheet_no": sheet}
    if sides is not None:
        body["sides"] = sides
    body.update(extra)
    return client.post(f"/batches/{bid}/inspections", json=body)


def order(client, bid, sheet):
    return client.post(f"/batches/{bid}/orders", json={"sheet_no": sheet})


@pytest.fixture()
def client(tmp_path):
    app = create_app(str(tmp_path / "stop.db"))
    with TestClient(app) as c:
        yield c


def three_blank_batch(client):
    bid = open_batch(
        client,
        [sig("S1", "A1", "A2"), sig("S2", "B1", "B2"), sig("S3", "C1", "C2")],
        [blank_sheet("P1"), blank_sheet("P2"), blank_sheet("P3")],
    )
    for s, p in (("S1", "P1"), ("S2", "P2"), ("S3", "P3")):
        assert claim(client, bid, s, p).status_code == 200
    return bid


# ---------------- 急停记录 ----------------


def test_stop_requires_claimed_same_batch_sheets(client):
    bid = open_batch(client, [sig("S1")], [blank_sheet("P1"), blank_sheet("P2")])
    claim(client, bid, "S1", "P1")

    # P2 未领（无原补印指令）-> 409
    r = stop(client, bid, ["P2"], 0, 0)
    assert r.status_code == 409
    assert "尚未领取" in r.json()["detail"]

    # PX 不在批内 -> 404
    r = stop(client, bid, ["PX"], 0, 0)
    assert r.status_code == 404

    # 合法记录
    assert stop(client, bid, ["P1"], 0, 0).status_code == 201


def test_stop_only_once_per_batch(client):
    bid = three_blank_batch(client)
    assert stop(client, bid, ["P1", "P2", "P3"], 2, 1).status_code == 201
    r = stop(client, bid, ["P1"], 1, 0)
    assert r.status_code == 409
    assert "每批只记一次" in r.json()["detail"]


def test_impossible_counts_rejected_and_not_saved(client):
    bid = three_blank_batch(client)
    # 三张空白纸最多 3 个正面；报 9 -> 无解
    r = stop(client, bid, ["P1", "P2", "P3"], 9, 0)
    assert r.status_code == 409
    assert "核对无解" in r.json()["detail"]
    # 急停未落库：可以用正确张数重新记录成功
    assert stop(client, bid, ["P1", "P2", "P3"], 2, 1).status_code == 201


def test_blank_sheet_back_without_front_is_impossible(client):
    bid = open_batch(client, [sig("S1", "A", "B")], [blank_sheet("P1")])
    claim(client, bid, "S1", "P1")
    # 空白纸先正后反：反面成功 1 而正面 0 不可能
    r = stop(client, bid, ["P1"], 0, 1)
    assert r.status_code == 409
    assert "核对无解" in r.json()["detail"]


# ---------------- 核对分类 ----------------


def test_all_ambiguous_with_only_counts(client):
    bid = three_blank_batch(client)
    j = stop(client, bid, ["P1", "P2", "P3"], 2, 1).json()
    statuses = {v["sheet_no"]: v["status_code"] for v in j["sheets"]}
    assert statuses == {"P1": "pending", "P2": "pending", "P3": "pending"}
    assert j["summary"]["counts_by_status"] == {"待查": 3, "可领单": 0, "已补齐": 0}


def test_report_determines_progress_and_classifies(client):
    bid = three_blank_batch(client)
    stop(client, bid, ["P1", "P2", "P3"], 2, 1)

    # P1 报正面已补 -> P1 唯一为 {正}（可领反面）；P2/P3 为 空/正反 互换，仍待查
    j = inspect(client, bid, "P1", ["正面"]).json()
    assert j["status_code"] == "orderable"
    assert j["status"] == "可领单"
    assert j["completed_sides"] == ["正面"]
    assert [p["side"] for p in j["pending_prints"]] == ["反面"]

    recon = client.get(f"/batches/{bid}/reconciliation").json()
    by = {v["sheet_no"]: v for v in recon["sheets"]}
    assert by["P1"]["status_code"] == "orderable"
    assert by["P2"]["status_code"] == "pending"
    assert by["P3"]["status_code"] == "pending"

    # P2 报正反都补 -> P3 被计数唯一确定为什么都没补，仍可领两面；P2 已补齐
    j = inspect(client, bid, "P2", ["正面", "反面"]).json()
    assert j["status_code"] == "completed"
    p3 = next(x for x in j["all_sheets"] if x["sheet_no"] == "P3")
    assert p3["status_code"] == "orderable"
    assert p3["completed_sides"] == []
    assert [p["plate"] for p in p3["pending_prints"]] == ["C1", "C2"]


def test_report_back_only_on_blank_sheet_rejected(client):
    bid = open_batch(client, [sig("S1", "A", "B")], [blank_sheet("P1")])
    claim(client, bid, "S1", "P1")
    stop(client, bid, ["P1"], 1, 1)  # 计数本身允许正反都补
    # 但报验“只补反面”违反先正后反 -> 无解，报验不落库
    r = inspect(client, bid, "P1", ["反面"])
    assert r.status_code == 409
    assert "核对无解" in r.json()["detail"]
    assert client.get(f"/batches/{bid}/inspections/P1").status_code == 404


def test_one_sided_sheet_flow(client):
    # P1 已印 A1（对 S1）-> 原指令只补反面 A2
    bid = open_batch(
        client,
        [sig("S1", "A1", "A2"), sig("S2", "C1", "C2")],
        [plate_sheet("P1", "A1"), blank_sheet("P2")],
    )
    claim(client, bid, "S1", "P1")
    claim(client, bid, "S2", "P2")

    # 一张单面、一张空白；正面成功 1、反面成功 1 时有两组解：
    #  (P1=反, P2=正) 或 (P1=空, P2=正反)，故两张都仍待查。
    j = stop(client, bid, ["P1", "P2"], 1, 1).json()
    by = {v["sheet_no"]: v for v in j["sheets"]}
    assert by["P1"]["status_code"] == "pending"
    assert by["P2"]["status_code"] == "pending"

    # 报验 P1 反面已补 -> P2 被计数唯一确定为只补了正面：
    # P1 已补齐（原指令只补反面），P2 还缺反面 C2，可领单。
    j = inspect(client, bid, "P1", ["反面"]).json()
    assert j["status_code"] == "completed"
    assert j["completed_sides"] == ["反面"]
    assert j["pending_prints"] == []

    recon = client.get(f"/batches/{bid}/reconciliation").json()
    by = {v["sheet_no"]: v for v in recon["sheets"]}
    assert by["P2"]["completed_sides"] == ["正面"]
    assert [p["plate"] for p in by["P2"]["pending_prints"]] == ["C2"]


# ---------------- 续印单 ----------------


def test_order_requires_orderable_state_and_replays(client):
    bid = three_blank_batch(client)
    stop(client, bid, ["P1", "P2", "P3"], 2, 1)

    # 全待查 -> 不能领单
    r = order(client, bid, "P1")
    assert r.status_code == 409
    assert "待查" in r.json()["detail"]

    inspect(client, bid, "P1", ["正面"])  # P1 可领反面
    r = order(client, bid, "P1")
    assert r.status_code == 200, r.text
    o1 = r.json()
    assert o1["replayed"] is False
    assert o1["sheet_no"] == "P1"
    assert o1["signature_no"] == "S1"
    assert o1["completed_sides"] == ["正面"]
    assert o1["prints"] == [{"side": "反面", "plate": "A2"}]

    # 重领（不同写法）-> 原单
    o2 = client.post(f"/batches/{bid}/orders/P1").json()
    assert o2["replayed"] is True
    assert o2["order_no"] == o1["order_no"]
    assert o2["prints"] == o1["prints"]

    # 已补齐纸不能领单
    inspect(client, bid, "P2", ["正面", "反面"])
    r = order(client, bid, "P2")
    assert r.status_code == 409
    assert "已补齐" in r.json()["detail"]


def test_order_for_empty_progress_prints_both_sides_in_order(client):
    bid = three_blank_batch(client)
    stop(client, bid, ["P1", "P2", "P3"], 2, 1)
    inspect(client, bid, "P1", ["正面"])
    inspect(client, bid, "P2", ["正面", "反面"])  # 迫使 P3=未补
    r = order(client, bid, "P3")
    assert r.status_code == 200
    assert [p["side"] for p in r.json()["prints"]] == ["正面", "反面"]
    assert [p["plate"] for p in r.json()["prints"]] == ["C1", "C2"]


# ---------------- 发单后更正报验（任务点名的实测场景） ----------------


def test_correct_inspection_after_order_conflict_rejected_and_state_kept(client):
    bid = three_blank_batch(client)
    stop(client, bid, ["P1", "P2", "P3"], 2, 1)
    inspect(client, bid, "P1", ["正面"])
    o1 = order(client, bid, "P1").json()
    assert o1["replayed"] is False

    # 把 P1 更正为正反都补：与发单冻结的 {正面} 冲突 -> 核对无解 -> 409
    r = inspect(client, bid, "P1", ["正面", "反面"])
    assert r.status_code == 409
    assert "核对无解" in r.json()["detail"]

    # 原报验照旧（仍是正面）
    got = client.get(f"/batches/{bid}/inspections/P1").json()
    assert got["sides"] == ["正面"]

    # 续印单照旧：重领仍是同一张原单
    again = order(client, bid, "P1").json()
    assert again["replayed"] is True
    assert again["order_no"] == o1["order_no"]
    assert again["completed_sides"] == ["正面"]
    assert again["prints"] == [{"side": "反面", "plate": "A2"}]

    # 核对接口里 P1 的冻结进度也保持发单时结论
    recon = client.get(f"/batches/{bid}/reconciliation").json()
    p1 = next(v for v in recon["sheets"] if v["sheet_no"] == "P1")
    assert p1["completed_sides"] == ["正面"]
    assert p1["order"]["order_no"] == o1["order_no"]


def test_consistent_correction_after_order_ok_but_order_unchanged(client):
    bid = three_blank_batch(client)
    stop(client, bid, ["P1", "P2", "P3"], 2, 1)
    inspect(client, bid, "P1", ["正面"])
    o1 = order(client, bid, "P1").json()

    # 用英文别名做“一致更正”：front -> 正面，与冻结进度相同 -> 接受
    r = client.put(f"/batches/{bid}/reports/P1", json={"sides": ["front"]})
    assert r.status_code == 200, r.text
    assert r.json()["inspection"]["sides"] == ["正面"]

    again = client.post(f"/batches/{bid}/orders", json={"sheet_no": "P1"}).json()
    assert again["order_no"] == o1["order_no"]
    assert again["prints"] == o1["prints"]


def test_correct_other_sheet_after_order_reconciles_against_freeze(client):
    bid = three_blank_batch(client)
    stop(client, bid, ["P1", "P2", "P3"], 2, 1)
    inspect(client, bid, "P1", ["正面"])
    order(client, bid, "P1")  # 冻结 P1={正}

    # 更正 P2=正反：必须把 P1 冻结进度计入后核对；可行 -> 200
    r = client.post(f"/batches/{bid}/inspections/P2", json={"sides": ["正面", "反面"]})
    assert r.status_code == 200
    p3 = next(x for x in r.json()["all_sheets"] if x["sheet_no"] == "P3")
    assert p3["completed_sides"] == []

    # 再把 P3 错报成正面：总计数变成 (3,1) != (2,1) -> 无解拒绝，P3 无报验
    bad = inspect(client, bid, "P3", ["正面"])
    assert bad.status_code == 409
    assert client.get(f"/batches/{bid}/inspections/P3").status_code == 404

    # P1 原单不受影响
    p1_order = client.get(f"/batches/{bid}/orders/P1").json()
    assert p1_order["prints"] == [{"side": "反面", "plate": "A2"}]


def test_report_first_blocks_order_for_now_completed_sheet(client):
    """报验先提交：纸已被核定为已补齐，后发单的一方应被拒。"""
    bid = open_batch(client, [sig("S1", "A", "B")], [blank_sheet("P1")])
    claim(client, bid, "S1", "P1")
    stop(client, bid, ["P1"], 1, 1)
    r = inspect(client, bid, "P1", ["正面", "反面"])
    assert r.json()["status_code"] == "completed"
    r = order(client, bid, "P1")
    assert r.status_code == 409
    assert "已补齐" in r.json()["detail"]


# ---------------- 持久化 / 重启 ----------------


def test_orders_and_progress_survive_restart(tmp_path):
    db_path = str(tmp_path / "restart.db")
    bid = None
    with TestClient(create_app(db_path)) as c:
        bid = three_blank_batch(c)
        stop(c, bid, ["P1", "P2", "P3"], 2, 1)
        inspect(c, bid, "P1", ["正面"])
        no = order(c, bid, "P1").json()["order_no"]

    # “重启”：用同一个 SQLite 文件新建 app
    with TestClient(create_app(db_path)) as c:
        o = c.get(f"/batches/{bid}/orders/P1").json()
        assert o["order_no"] == no
        assert o["prints"] == [{"side": "反面", "plate": "A2"}]

        recon = c.get(f"/batches/{bid}/reconciliation").json()
        p1 = next(v for v in recon["sheets"] if v["sheet_no"] == "P1")
        assert p1["completed_sides"] == ["正面"]
        assert p1["order"]["order_no"] == no

        # 重启后仍可继续核对：补 P2 报验
        r = inspect(c, bid, "P2", ["正面", "反面"])
        assert r.status_code == 200


# ---------------- 保存失败回滚 ----------------


def test_save_failure_rolls_back_and_keeps_state(tmp_path):
    """续印单保存时数据库报错：事务回滚，已有续印单与报验照旧，且不产生半截单。"""
    import sqlite3

    db_path = str(tmp_path / "savefail.db")
    with TestClient(create_app(db_path), raise_server_exceptions=False) as c:
        bid = three_blank_batch(c)
        stop(c, bid, ["P1", "P2", "P3"], 2, 1)
        inspect(c, bid, "P1", ["正面"])
        o1 = order(c, bid, "P1").json()

        # 注入一个必然失败的 INSERT 触发器，模拟“保存失败”
        inj = sqlite3.connect(db_path)
        inj.execute(
            "CREATE TRIGGER fail_order_insert BEFORE INSERT ON continuation_orders "
            "BEGIN SELECT RAISE(ABORT, 'injected save failure'); END"
        )
        inj.commit()
        inj.close()

        # P3 已被计数唯一确定为未补、本可领单，但保存必失败 -> 500
        inspect(c, bid, "P2", ["正面", "反面"])
        resp = order(c, bid, "P3")
        assert resp.status_code == 500

        # 原续印单照旧，P3 没有落库
        listing = c.get(f"/batches/{bid}/orders").json()
        assert listing["count"] == 1
        assert listing["orders"][0]["order_no"] == o1["order_no"]
        assert c.get(f"/batches/{bid}/orders/P3").status_code == 404

        # 原报验照旧
        assert c.get(f"/batches/{bid}/inspections/P1").json()["sides"] == ["正面"]


# ---------------- 接口别名 / 宽松字段 ----------------


def test_path_and_field_aliases_work(client):
    bid = three_blank_batch(client)
    # emergency-stop 别名 + 近义字段名
    r = client.post(
        f"/batches/{bid}/emergency-stops",
        json={"sheets": ["P1", "P2", "P3"], "front_ok": 2, "back_ok": 1},
    )
    assert r.status_code == 201, r.text

    # /reports 集合 + paper_no 别名 + 布尔标量
    r = client.post(
        f"/batches/{bid}/reports",
        json={"paper_no": "P1", "front_completed": True},
    )
    assert r.status_code == 200, r.text
    assert r.json()["completed_sides"] == ["正面"]

    # continuation-orders 别名
    r = client.post(f"/batches/{bid}/continuation-orders", json={"sheet_no": "P1"})
    assert r.status_code == 200
    assert r.json()["prints"] == [{"side": "反面", "plate": "A2"}]

    # 中文键报验
    r = client.post(f"/batches/{bid}/inspections/P2", json={"sides": ["正面", "反面"]})
    assert r.status_code == 200


def test_docs_and_health_available(client):
    assert client.get("/health").status_code == 200
    assert client.get("/openapi.json").status_code == 200
    assert client.get("/docs").status_code == 200


# ---------------- 真实并发：发单 vs 更正报验 ----------------


@pytest.fixture()
def live_server(tmp_path):
    app = create_app(str(tmp_path / "live_stop.db"))
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(250):
        if server.started:
            break
        time.sleep(0.02)
    else:
        pytest.fail("服务未启动")
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=10)


def test_concurrent_order_and_correction_later_checks_against_committed(live_server):
    """发 P1 单 与 把 P1 更正为正反都补 并发。

    两种串行次序都合法：
    - 发单先：冻结 {正}，更正 {正,反} 核对无解 -> 报验 409，单保留；
    - 更正先：P1 已补齐，随后发单 409，无单。
    不允许两个都 2xx（那会破坏“后提交者按先提交结果核对”）。
    """
    with httpx.Client(base_url=live_server, timeout=10) as h:
        bid = open_batch(
            h,
            [sig("S1", "A1", "A2"), sig("S2"), sig("S3")],
            [blank_sheet("P1"), blank_sheet("P2"), blank_sheet("P3")],
        )
        for s, p in (("S1", "P1"), ("S2", "P2"), ("S3", "P3")):
            claim(h, bid, s, p)
        stop(h, bid, ["P1", "P2", "P3"], 2, 1)
        inspect(h, bid, "P1", ["正面"])  # 先让 P1 可领单

        barrier = threading.Barrier(2)

        def do_order():
            barrier.wait()
            return order(h, bid, "P1").status_code

        def do_correct():
            barrier.wait()
            return inspect(h, bid, "P1", ["正面", "反面"]).status_code

        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            codes = list(pool.map(lambda f: f(), [do_order, do_correct]))

        assert sorted(codes) == [200, 409], codes

        orders = h.get(f"/batches/{bid}/orders").json()["orders"]
        p1_report = h.get(f"/batches/{bid}/inspections/P1").json()["sides"]

        if orders:  # 发单赢：冻结 {正}，更正被拒，报验仍是 [正]
            assert len(orders) == 1
            assert orders[0]["completed_sides"] == ["正面"]
            assert orders[0]["prints"] == [{"side": "反面", "plate": "A2"}]
            assert p1_report == ["正面"]
        else:  # 更正赢：P1=[正,反]，无单
            assert orders == []
            assert p1_report == ["正面", "反面"]


def test_stop_defaults_to_all_claimed_sheets_and_accepts_object_list(client):
    bid = three_blank_batch(client)
    # 不给 sheet_nos：默认全部已领纸；sheets 用对象数组并自带张数时汇总
    r = client.post(f"/batches/{bid}/stops", json={
        "sheets": [
            {"sheet_no": "P1", "front_success": 1, "back_success": 0},
            {"sheet_no": "P2", "front_success": 1, "back_success": 1},
            {"sheet_no": "P3", "front_success": 0, "back_success": 0},
        ]
    })
    assert r.status_code == 201, r.text
    j = r.json()
    assert sorted(j["sheet_nos"]) == ["P1", "P2", "P3"]
    assert j["front_success"] == 2 and j["back_success"] == 1


def test_reconciliation_status_and_sheet_filters(client):
    bid = three_blank_batch(client)
    stop(client, bid, ["P1", "P2", "P3"], 2, 1)
    inspect(client, bid, "P1", ["正面"])
    inspect(client, bid, "P2", ["正面", "反面"])

    r = client.get(f"/batches/{bid}/reconciliation?status=completed")
    assert [v["sheet_no"] for v in r.json()["sheets"]] == ["P2"]
    r = client.get(f"/batches/{bid}/reconciliation?status=orderable")
    assert {v["sheet_no"] for v in r.json()["sheets"]} == {"P1", "P3"}
    r = client.get(f"/batches/{bid}/reconciliation?sheet_no=P1")
    assert len(r.json()["sheets"]) == 1 and r.json()["sheets"][0]["sheet_no"] == "P1"
    # all_sheets 始终保留全量
    assert len(r.json()["all_sheets"]) == 3


def test_inspection_before_stop_is_allowed_and_checked_later(client):
    bid = three_blank_batch(client)
    # 急停还没记：报验可先存（本地合法性通过），返回未全局核对标记
    r = inspect(client, bid, "P1", ["正面"])
    assert r.status_code == 200, r.text
    assert r.json()["status_code"] == "recorded_pending_stop"

    # 与先前所报面矛盾的张数会让记急停无解 -> 409 且急停不落库
    bad = stop(client, bid, ["P1", "P2", "P3"], 0, 0)  # P1 已报正面，却报正面成功 0
    assert bad.status_code == 409
    # 用一致张数记录成功，且先前报验被纳入核对：P1 可领反面
    j = stop(client, bid, ["P1", "P2", "P3"], 2, 1).json()
    p1 = next(v for v in j["sheets"] if v["sheet_no"] == "P1")
    assert p1["status_code"] == "orderable" and p1["completed_sides"] == ["正面"]


def test_inspection_unknown_batch_and_unclaimed_sheet(client):
    bid = three_blank_batch(client)
    assert client.post("/batches/999/inspections", json={"sheet_no": "P1", "sides": []}).status_code == 404
    assert client.post("/batches/999/stops", json={"sheet_nos": [], "front_success": 0, "back_success": 0}).status_code == 404
    assert client.get("/batches/999/reconciliation").status_code == 404
    assert client.get(f"/batches/{bid}/orders/P1").status_code == 404

    # 未领纸不能报验（无原补印指令）
    bid2 = open_batch(client, [sig("T1")], [blank_sheet("U1")])
    r = client.post(f"/batches/{bid2}/inspections", json={"sheet_no": "U1", "sides": []})
    assert r.status_code == 409


def test_concurrent_duplicate_orders_both_return_original(live_server):
    with httpx.Client(base_url=live_server, timeout=10) as h:
        bid = open_batch(h, [sig("S1", "A", "B")], [blank_sheet("P1")])
        claim(h, bid, "S1", "P1")
        stop(h, bid, ["P1"], 1, 0)  # 单纸正 1 -> 唯一 {正}，可领反面

        barrier = threading.Barrier(2)

        def do(_):
            barrier.wait()
            r = h.post(f"/batches/{bid}/orders", json={"sheet_no": "P1"})
            return r.status_code, r.json()

        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            results = list(pool.map(do, [1, 2]))

        assert all(code == 200 for code, _ in results)
        nos = {body["order_no"] for _, body in results}
        assert len(nos) == 1
        assert sorted(body["replayed"] for _, body in results) == [False, True]
