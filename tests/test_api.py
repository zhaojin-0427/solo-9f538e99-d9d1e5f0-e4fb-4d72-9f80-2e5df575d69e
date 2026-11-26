"""补印 API 测试。

覆盖：开批校验、领纸规则、幂等重试、失败不占纸、按批查询，
以及关键并发场景——两笔单独可领，同时提交同一纸号却只能接一笔。
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


def _sig(no, front, back):
    return {"signature_no": no, "front_plate": front, "back_plate": back}


def _sheet(no, plate=None):
    return {"sheet_no": no, "printed_plate": plate}


def _open_batch(client, signatures, sheets):
    resp = client.post("/batches", json={"signatures": signatures, "sheets": sheets})
    assert resp.status_code == 201, resp.text
    return resp.json()["batch_id"]


def _claim(client, batch_id, signature_no, sheet_no):
    return client.post(
        f"/batches/{batch_id}/claims",
        json={"signature_no": signature_no, "sheet_no": sheet_no},
    )


@pytest.fixture()
def client(tmp_path):
    app = create_app(str(tmp_path / "test.db"))
    with TestClient(app) as c:
        yield c


# ---------- 开批 ----------


def test_open_batch_ok(client):
    bid = _open_batch(client, [_sig("S1", "A", "B")], [_sheet("P1")])
    detail = client.get(f"/batches/{bid}").json()
    assert detail["complete"] is False
    assert detail["signatures"][0]["claimed_sheet"] is None
    assert detail["sheets"][0]["printed_plate"] is None  # 空白纸


def test_open_batch_rejected_when_book_cannot_be_completed(client):
    # 唯一的纸已印版号 X，与帖的正反面都对不上 → 全册配不齐，不开批
    resp = client.post(
        "/batches", json={"signatures": [_sig("S1", "A", "B")], "sheets": [_sheet("P1", "X")]}
    )
    assert resp.status_code == 409
    assert "配不齐" in resp.json()["detail"]
    assert client.get("/batches/1").status_code == 404  # 未落库


def test_open_batch_rejected_when_sheets_fewer_than_signatures(client):
    resp = client.post(
        "/batches",
        json={"signatures": [_sig("S1", "A", "B"), _sig("S2", "C", "D")], "sheets": [_sheet("P1")]},
    )
    assert resp.status_code == 409


def test_open_batch_limits(client):
    sigs = [_sig(f"S{i:02d}", "A", "B") for i in range(25)]  # 超过 24 帖
    resp = client.post("/batches", json={"signatures": sigs, "sheets": [_sheet("P1")]})
    assert resp.status_code == 422
    sheets = [_sheet(f"P{i:02d}") for i in range(49)]  # 超过 48 张
    resp = client.post("/batches", json={"signatures": [_sig("S1", "A", "B")], "sheets": sheets})
    assert resp.status_code == 422


def test_open_batch_duplicate_numbers_rejected(client):
    resp = client.post(
        "/batches",
        json={"signatures": [_sig("S1", "A", "B"), _sig("S1", "A", "B")], "sheets": [_sheet("P1")]},
    )
    assert resp.status_code == 422
    resp = client.post(
        "/batches",
        json={"signatures": [_sig("S1", "A", "B")], "sheets": [_sheet("P1"), _sheet("P1")]},
    )
    assert resp.status_code == 422


def test_blank_sheet_plate_left_empty(client):
    # 空白纸版号留空（空串）按空白纸处理
    bid = _open_batch(client, [_sig("S1", "A", "B")], [_sheet("P1", "")])
    assert client.get(f"/batches/{bid}").json()["sheets"][0]["printed_plate"] is None


# ---------- 领纸规则 ----------


def test_blank_sheet_prints_both_sides(client):
    bid = _open_batch(client, [_sig("S1", "A", "B")], [_sheet("P1")])
    resp = _claim(client, bid, "S1", "P1")
    assert resp.status_code == 200
    assert resp.json()["prints"] == [
        {"side": "正面", "plate": "A"},
        {"side": "反面", "plate": "B"},
    ]


def test_printed_side_decides_the_other_side(client):
    # 已印版号对上正面 → 补印反面；对上反面 → 补印正面
    bid = _open_batch(
        client,
        [_sig("S1", "A", "B"), _sig("S2", "A", "B")],
        [_sheet("P1", "A"), _sheet("P2", "B")],
    )
    r1 = _claim(client, bid, "S1", "P1")
    assert r1.json()["prints"] == [{"side": "反面", "plate": "B"}]
    r2 = _claim(client, bid, "S2", "P2")
    assert r2.json()["prints"] == [{"side": "正面", "plate": "A"}]


def test_two_separate_claims_succeed(client):
    """两笔单独可领。"""
    bid = _open_batch(
        client, [_sig("S1", "A", "B"), _sig("S2", "A", "B")], [_sheet("P1"), _sheet("P2")]
    )
    r1 = _claim(client, bid, "S1", "P1")
    r2 = _claim(client, bid, "S2", "P2")
    assert r1.status_code == 200 and r2.status_code == 200
    instructions = client.get(f"/batches/{bid}/instructions").json()["instructions"]
    assert len(instructions) == 2
    assert client.get(f"/batches/{bid}").json()["complete"] is True


def test_same_signature_same_sheet_retry_returns_original(client):
    """同帖同纸重试返回原指令，且不重复落库。"""
    bid = _open_batch(
        client, [_sig("S1", "A", "B"), _sig("S2", "A", "B")], [_sheet("P1"), _sheet("P2")]
    )
    first = _claim(client, bid, "S1", "P1").json()
    assert first["replayed"] is False
    again = _claim(client, bid, "S1", "P1").json()
    assert again["replayed"] is True
    assert {k: v for k, v in again.items() if k != "replayed"} == {
        k: v for k, v in first.items() if k != "replayed"
    }
    assert len(client.get(f"/batches/{bid}/instructions").json()["instructions"]) == 1


def test_one_signature_only_gets_one_sheet(client):
    bid = _open_batch(
        client, [_sig("S1", "A", "B"), _sig("S2", "A", "B")], [_sheet("P1"), _sheet("P2")]
    )
    assert _claim(client, bid, "S1", "P1").status_code == 200
    resp = _claim(client, bid, "S1", "P2")  # 同帖换纸
    assert resp.status_code == 409
    assert "一帖只发一张" in resp.json()["detail"]


def test_one_sheet_only_goes_to_one_signature(client):
    bid = _open_batch(
        client, [_sig("S1", "A", "B"), _sig("S2", "A", "B")], [_sheet("P1"), _sheet("P2")]
    )
    assert _claim(client, bid, "S1", "P1").status_code == 200
    resp = _claim(client, bid, "S2", "P1")  # 同纸给另一帖
    assert resp.status_code == 409
    assert "一张纸只发给一帖" in resp.json()["detail"]


def test_failed_claim_does_not_hold_sheet(client):
    """领纸失败不占纸。"""
    # P1 已印 X：对 S1(A/B) 不合用，对 S2(X/Y) 合用
    bid = _open_batch(
        client, [_sig("S1", "A", "B"), _sig("S2", "X", "Y")], [_sheet("P1", "X"), _sheet("P2")]
    )
    bad = _claim(client, bid, "S1", "P1")
    assert bad.status_code == 409
    assert "不合用" in bad.json()["detail"]
    # P1 未被失败请求占用，S2 可正常领走
    assert _claim(client, bid, "S2", "P1").status_code == 200
    assert _claim(client, bid, "S1", "P2").status_code == 200


def test_claim_rejected_if_remaining_paper_cannot_cover_unclaimed(client):
    """余纸不够给未领的书帖各配一张时，不予发放。"""
    # P1 已印 A（只合 S1），P2 空白（谁都合）
    bid = _open_batch(
        client, [_sig("S1", "A", "B"), _sig("S2", "C", "D")], [_sheet("P1", "A"), _sheet("P2")]
    )
    # S1 若领走空白纸 P2，余下 P1 对 S2 不合用 → 拒发
    resp = _claim(client, bid, "S1", "P2")
    assert resp.status_code == 409
    assert "余纸" in resp.json()["detail"]
    # 拒发不占纸：S1 改领 P1、S2 领 P2 都成功
    assert _claim(client, bid, "S1", "P1").status_code == 200
    assert _claim(client, bid, "S2", "P2").status_code == 200


def test_unknown_batch_signature_sheet(client):
    bid = _open_batch(client, [_sig("S1", "A", "B")], [_sheet("P1")])
    assert _claim(client, 999, "S1", "P1").status_code == 404
    assert _claim(client, bid, "SX", "P1").status_code == 404
    assert _claim(client, bid, "S1", "PX").status_code == 404
    assert client.get("/batches/999").status_code == 404
    assert client.get("/batches/999/instructions").status_code == 404


def test_instructions_queryable_by_batch(client):
    bid1 = _open_batch(client, [_sig("S1", "A", "B")], [_sheet("P1")])
    bid2 = _open_batch(client, [_sig("S1", "A", "B")], [_sheet("P9")])
    _claim(client, bid1, "S1", "P1")
    _claim(client, bid2, "S1", "P9")
    ins1 = client.get(f"/batches/{bid1}/instructions").json()["instructions"]
    ins2 = client.get(f"/batches/{bid2}/instructions").json()["instructions"]
    assert [i["sheet_no"] for i in ins1] == ["P1"]
    assert [i["sheet_no"] for i in ins2] == ["P9"]


# ---------- 并发：同时提交同一纸号，只能接一笔 ----------


@pytest.fixture()
def live_server(tmp_path):
    """在后台线程里跑真实 uvicorn 服务，用于真正的并发请求。"""
    app = create_app(str(tmp_path / "live.db"))
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


def test_concurrent_claims_for_same_sheet_only_one_wins(live_server):
    """两帖同时抢同一张纸：只能接一笔；没抢到的帖不占纸，仍可领另一张。"""
    with httpx.Client(base_url=live_server, timeout=10) as client:
        bid = _open_batch(
            client, [_sig("S1", "A", "B"), _sig("S2", "A", "B")], [_sheet("P1"), _sheet("P2")]
        )
        barrier = threading.Barrier(2)

        def claim(sig_no):
            barrier.wait()  # 两线程对齐后同时提交
            return client.post(
                f"/batches/{bid}/claims", json={"signature_no": sig_no, "sheet_no": "P1"}
            )

        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            codes = sorted(r.status_code for r in pool.map(claim, ["S1", "S2"]))
        assert codes == [200, 409]

        instructions = client.get(f"/batches/{bid}/instructions").json()["instructions"]
        assert len(instructions) == 1  # 只落了一笔
        winner = instructions[0]["signature_no"]
        loser = "S2" if winner == "S1" else "S1"
        # 失败者不占纸，换 P2 可领
        assert (
            client.post(
                f"/batches/{bid}/claims", json={"signature_no": loser, "sheet_no": "P2"}
            ).status_code
            == 200
        )


def test_concurrent_same_signature_same_sheet_both_get_original(live_server):
    """同帖同纸并发重试：都返回同一条原指令，库里只有一笔。"""
    with httpx.Client(base_url=live_server, timeout=10) as client:
        bid = _open_batch(
            client, [_sig("S1", "A", "B"), _sig("S2", "A", "B")], [_sheet("P1"), _sheet("P2")]
        )
        barrier = threading.Barrier(2)

        def claim(_):
            barrier.wait()
            return client.post(
                f"/batches/{bid}/claims", json={"signature_no": "S1", "sheet_no": "P1"}
            )

        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            bodies = [r.json() for r in pool.map(claim, [1, 2])]
        assert all(b["sheet_no"] == "P1" and b["signature_no"] == "S1" for b in bodies)
        assert sorted(b["replayed"] for b in bodies) == [False, True]  # 一笔新发、一笔重放
        assert len(client.get(f"/batches/{bid}/instructions").json()["instructions"]) == 1
