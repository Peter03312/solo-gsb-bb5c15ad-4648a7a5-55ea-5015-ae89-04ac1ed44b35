"""离线网关成批上报端到端测试。

覆盖：逐条 created / existed、整批一次性区间差异、整批拒绝（批内重复、
无效读数、与库中同号异内容）的原子性、同批次号同内容回执重放（即使随后
更正 / 争议决定）、同批次号异内容冲突、排序规则纳入区间、与既有接口共存。
"""
import pytest
from fastapi.testclient import TestClient

from app.main import app

DEVICE = "truck-001"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("TEMP_LIMIT", "8.0")
    with TestClient(app) as c:
        yield c


def post(client, event_no, ts, temp, device=DEVICE):
    return client.post(
        f"/devices/{device}/readings",
        json={"event_no": event_no, "sampled_at": ts, "temperature": temp},
    )


def batch(client, batch_no, readings, device=DEVICE):
    return client.post(
        f"/devices/{device}/reading-batches",
        json={"batch_no": batch_no, "readings": readings},
    )


def rd(event_no, ts, temp):
    return {"event_no": event_no, "sampled_at": ts, "temperature": temp}


# ---------------------------------------------------------------- 逐条结果与区间

def test_batch_creates_all_and_reports_after_state(client):
    r = batch(client, "B1", [
        rd("E1", "2026-10-01T08:00:00Z", 9.0),
        rd("E2", "2026-10-01T08:10:00Z", 9.5),
        rd("E3", "2026-10-01T08:20:00Z", 6.0),
    ])
    assert r.status_code == 201
    body = r.json()
    assert body["batch_no"] == "B1"
    assert [x["status"] for x in body["results"]] == ["created", "created", "created"]
    assert [x["event_no"] for x in body["results"]] == ["E1", "E2", "E3"]
    # 提交后全量区间：E1/E2 连续超限，E3 闭合
    iv = body["intervals"][0]
    assert iv["start_event_no"] == "E1"
    assert iv["end_event_no"] == "E3"
    assert iv["reading_count"] == 2
    assert iv["max_temperature"] == 9.5


def test_batch_marks_existing_same_content(client):
    post(client, "E1", "2026-10-01T08:00:00Z", 9.0)
    r = batch(client, "B1", [
        rd("E1", "2026-10-01T08:00:00Z", 9.0),   # 同号同内容 → existed
        rd("E2", "2026-10-01T08:10:00Z", 9.5),
    ])
    assert r.status_code == 201
    statuses = {x["event_no"]: x["status"] for x in r.json()["results"]}
    assert statuses == {"E1": "existed", "E2": "created"}
    assert r.json()["changes"]["added"][0]["start_event_no"] == "E1"
    # 已有读数只保留一条
    assert len(client.get(f"/devices/{DEVICE}/readings").json()["readings"]) == 2


def test_batch_all_existing_reports_empty_changes(client):
    readings = [
        rd("E1", "2026-10-01T08:00:00Z", 9.0),
        rd("E2", "2026-10-01T08:10:00Z", 9.5),
    ]
    batch(client, "B1", readings)
    r = batch(client, "B2", readings)
    assert r.status_code == 201  # 新批次号、无新读数，但回执首次生成
    assert [x["status"] for x in r.json()["results"]] == ["existed", "existed"]
    assert r.json()["changes"] == {"added": [], "changed": [], "invalidated": []}


def test_batch_intervals_follow_sort_rule_not_list_order(client):
    # 列表顺序与采样时间顺序不同：区间仍按 (采样时间, 事件号) 排序计算
    r = batch(client, "B1", [
        rd("E3", "2026-10-01T08:20:00Z", 6.0),
        rd("E1", "2026-10-01T08:00:00Z", 9.0),
        rd("E2", "2026-10-01T08:10:00Z", 9.5),
    ])
    iv = r.json()["intervals"][0]
    assert iv["start_event_no"] == "E1"
    assert iv["end_event_no"] == "E3"
    # 回执中的逐条结果仍保持请求列表顺序
    assert [x["event_no"] for x in r.json()["results"]] == ["E3", "E1", "E2"]


def test_batch_diff_is_one_shot_batch_before_vs_after(client):
    # 已有闭合区间 E1..E3；批内先“看似续接”的 E4/E5，最终由 E6 闭合。
    post(client, "E1", "2026-10-01T08:00:00Z", 9.0)
    post(client, "E2", "2026-10-01T08:10:00Z", 9.0)
    post(client, "E3", "2026-10-01T08:20:00Z", 6.0)
    # 若逐条入库，E4/E5 会先生成未闭合的 E4 区间、再被 E6 闭合（changed）；
    # 整批一次性差异只比较批次前后：E4 为新增闭合区间，E1 区间无变化。
    r = batch(client, "B1", [
        rd("E4", "2026-10-01T08:30:00Z", 9.0),
        rd("E5", "2026-10-01T08:40:00Z", 9.0),
        rd("E6", "2026-10-01T08:50:00Z", 6.0),
    ])
    changes = r.json()["changes"]
    assert [i["start_event_no"] for i in changes["added"]] == ["E4"]
    assert changes["changed"] == []
    assert changes["invalidated"] == []
    assert [i["end_event_no"] for i in r.json()["intervals"] if i["start_event_no"] == "E4"] == ["E6"]


def test_batch_late_reading_invalidates_old_interval(client):
    # E2/E3 已构成区间起点 E2；迟到的 E1 成为新起点 → 旧失效、新新增
    post(client, "E2", "2026-10-01T08:10:00Z", 9.0)
    post(client, "E3", "2026-10-01T08:20:00Z", 9.5)
    r = batch(client, "B1", [rd("E1", "2026-10-01T08:00:00Z", 8.5)])
    changes = r.json()["changes"]
    assert [i["start_event_no"] for i in changes["invalidated"]] == ["E2"]
    assert [i["start_event_no"] for i in changes["added"]] == ["E1"]
    assert r.json()["results"][0]["status"] == "created"


# ---------------------------------------------------------------- 整批拒绝（原子性）

def test_batch_duplicate_event_no_within_batch_rejected(client):
    r = batch(client, "B1", [
        rd("E1", "2026-10-01T08:00:00Z", 9.0),
        rd("E1", "2026-10-01T08:10:00Z", 9.5),
    ])
    assert r.status_code == 422
    assert len(client.get(f"/devices/{DEVICE}/readings").json()["readings"]) == 0
    # 批次号未被占用：修正内容后可用同批次号首次提交
    r2 = batch(client, "B1", [
        rd("E1", "2026-10-01T08:00:00Z", 9.0),
        rd("E2", "2026-10-01T08:10:00Z", 9.5),
    ])
    assert r2.status_code == 201


def test_batch_invalid_reading_rejected(client):
    # 任一读数无效（温度、时间、事件号、空列表）→ 422，整批不写
    assert batch(client, "B1", [
        rd("E1", "2026-10-01T08:00:00Z", 9.0),
        rd("E2", "2026-10-01T08:10:00Z", -500),
    ]).status_code == 422
    assert batch(client, "B1", [
        rd("E1", "not-a-time", 9.0),
    ]).status_code == 422
    assert batch(client, "B1", [
        rd("bad/no", "2026-10-01T08:00:00Z", 9.0),
    ]).status_code == 422
    assert batch(client, "B1", []).status_code == 422
    assert batch(client, "bad/batch", [
        rd("E1", "2026-10-01T08:00:00Z", 9.0),
    ]).status_code == 422
    assert client.get(f"/devices/{DEVICE}/readings").json()["readings"] == []


def test_batch_conflict_with_db_different_content_is_atomic(client):
    post(client, "E1", "2026-10-01T08:00:00Z", 9.0)
    r = batch(client, "B1", [
        rd("E2", "2026-10-01T08:10:00Z", 9.5),
        rd("E1", "2026-10-01T08:00:00Z", 7.0),  # 与库中 E1 异内容
    ])
    assert r.status_code == 409
    readings = client.get(f"/devices/{DEVICE}/readings").json()["readings"]
    # 同批中排在冲突项之前的 E2 也不得写入
    assert [x["event_no"] for x in readings] == ["E1"]
    assert readings[0]["temperature"] == 9.0
    # 区间不变；批次号可在修正后重试
    assert client.get(f"/devices/{DEVICE}/intervals").json()["intervals"] == []


def test_rejected_batch_keeps_revision_unchanged(client):
    post(client, "E1", "2026-10-01T08:00:00Z", 9.0)
    post(client, "E2", "2026-10-01T08:10:00Z", 9.5)
    before = client.get(f"/devices/{DEVICE}/intervals").json()["intervals"]
    r = batch(client, "B1", [
        rd("E3", "2026-10-01T08:20:00Z", 9.0),
        rd("E1", "2026-10-01T08:00:00Z", 9.1),  # 异内容冲突
    ])
    assert r.status_code == 409
    after = client.get(f"/devices/{DEVICE}/intervals").json()["intervals"]
    assert after == before
    import os
    import sqlite3
    raw = sqlite3.connect(os.environ["DB_PATH"])
    revisions = dict(
        raw.execute(
            "SELECT event_no, revision FROM readings WHERE device_id = ?",
            (DEVICE,),
        ).fetchall()
    )
    raw.close()
    assert revisions == {"E1": 1, "E2": 1}


# ---------------------------------------------------------------- 回执重放与冲突

def test_same_batch_same_content_replays_first_receipt(client):
    r1 = batch(client, "B1", [
        rd("E1", "2026-10-01T08:00:00Z", 9.0),
        rd("E2", "2026-10-01T08:10:00Z", 9.5),
    ])
    first = r1.json()
    # 等价时区表示、规范化后有序内容相同 → 200 + 首次回执
    r2 = batch(client, "B1", [
        rd("E1", "2026-10-01T16:00:00+08:00", 9.0),
        rd("E2", "2026-10-01T16:10:00+08:00", 9.5),
    ])
    assert r2.status_code == 200
    assert r2.json() == first
    # 重放不重复写入
    assert len(client.get(f"/devices/{DEVICE}/readings").json()["readings"]) == 2


def test_replay_returns_first_receipt_even_after_correction(client):
    first = batch(client, "B1", [
        rd("E1", "2026-10-01T08:00:00Z", 9.0),
        rd("E2", "2026-10-01T08:10:00Z", 9.5),
    ]).json()
    # 随后发生更正：区间消失
    client.put(
        f"/devices/{DEVICE}/readings/E2",
        json={"sampled_at": "2026-10-01T08:10:00Z", "temperature": 5.0},
    )
    assert client.get(f"/devices/{DEVICE}/intervals").json()["intervals"] == []
    # 重放仍返回首次回执（含首次时点的区间与差异）
    again = batch(client, "B1", [
        rd("E1", "2026-10-01T08:00:00Z", 9.0),
        rd("E2", "2026-10-01T08:10:00Z", 9.5),
    ])
    assert again.status_code == 200
    assert again.json() == first


def test_replay_returns_first_receipt_even_after_dispute_decision(client):
    first = batch(client, "B1", [
        rd("E1", "2026-10-01T08:00:00Z", 9.0),
        rd("E2", "2026-10-01T08:10:00Z", 9.5),
    ]).json()
    client.post(
        f"/devices/{DEVICE}/disputes",
        json={
            "dispute_no": "D1", "target_event_no": "E2", "action": "revoke",
            "reason": "duplicate packet",
        },
    )
    client.post(
        f"/devices/{DEVICE}/disputes/D1/decisions",
        json={"decision": "approve"},
    )
    readings = {
        x["event_no"]: x
        for x in client.get(f"/devices/{DEVICE}/readings").json()["readings"]
    }
    assert readings["E2"]["revoked"] is True
    again = batch(client, "B1", [
        rd("E1", "2026-10-01T08:00:00Z", 9.0),
        rd("E2", "2026-10-01T08:10:00Z", 9.5),
    ])
    assert again.status_code == 200
    assert again.json() == first


def test_same_batch_different_content_conflicts(client):
    batch(client, "B1", [
        rd("E1", "2026-10-01T08:00:00Z", 9.0),
        rd("E2", "2026-10-01T08:10:00Z", 9.5),
    ])
    # 温度不同
    assert batch(client, "B1", [
        rd("E1", "2026-10-01T08:00:00Z", 9.0),
        rd("E2", "2026-10-01T08:10:00Z", 9.6),
    ]).status_code == 409
    # 采样时间不同
    assert batch(client, "B1", [
        rd("E1", "2026-10-01T08:00:00Z", 9.0),
        rd("E2", "2026-10-01T08:11:00Z", 9.5),
    ]).status_code == 409
    # 事件号不同
    assert batch(client, "B1", [
        rd("E1", "2026-10-01T08:00:00Z", 9.0),
        rd("E9", "2026-10-01T08:10:00Z", 9.5),
    ]).status_code == 409
    # 有序内容顺序不同（规范化后内容不同）
    assert batch(client, "B1", [
        rd("E2", "2026-10-01T08:10:00Z", 9.5),
        rd("E1", "2026-10-01T08:00:00Z", 9.0),
    ]).status_code == 409
    # 列表长度不同（多 / 少一条）
    assert batch(client, "B1", [
        rd("E1", "2026-10-01T08:00:00Z", 9.0),
    ]).status_code == 409
    assert batch(client, "B1", [
        rd("E1", "2026-10-01T08:00:00Z", 9.0),
        rd("E2", "2026-10-01T08:10:00Z", 9.5),
        rd("E3", "2026-10-01T08:20:00Z", 6.0),
    ]).status_code == 409


def test_conflicting_retry_does_not_change_readings(client):
    batch(client, "B1", [rd("E1", "2026-10-01T08:00:00Z", 9.0)])
    r = batch(client, "B1", [rd("E1", "2026-10-01T08:00:00Z", 9.1)])
    assert r.status_code == 409
    # 异内容重试后，原读数不变，且首次回执仍可取回
    readings = client.get(f"/devices/{DEVICE}/readings").json()["readings"]
    assert readings[0]["temperature"] == 9.0
    again = batch(client, "B1", [rd("E1", "2026-10-01T08:00:00Z", 9.0)])
    assert again.status_code == 200
    assert again.json()["results"][0]["status"] == "created"


# ---------------------------------------------------------------- 设备隔离与共存

def test_batch_numbers_are_isolated_per_device(client):
    payload = [rd("E1", "2026-10-01T08:00:00Z", 9.0)]
    assert batch(client, "B1", payload, device="dev-A").status_code == 201
    # 同批次号在另一设备是独立首次提交
    assert batch(client, "B1", payload, device="dev-B").status_code == 201
    assert client.get("/devices/dev-B/intervals").json()["intervals"] == []
    assert client.get("/devices/dev-A/readings").json()["readings"][0]["event_no"] == "E1"


def test_batches_coexist_with_single_and_dispute_apis(client):
    # 批次 + 单条 + 更正 + 争议共同工作
    batch(client, "B1", [
        rd("E1", "2026-10-01T08:00:00Z", 9.0),
        rd("E2", "2026-10-01T08:10:00Z", 9.5),
    ])
    r = post(client, "E3", "2026-10-01T08:20:00Z", 6.0)
    assert r.json()["intervals"][0]["end_event_no"] == "E3"
    client.post(
        f"/devices/{DEVICE}/disputes",
        json={
            "dispute_no": "D1", "target_event_no": "E2", "action": "revoke",
            "reason": "duplicate packet from gateway",
        },
    )
    d = client.post(
        f"/devices/{DEVICE}/disputes/D1/decisions",
        json={"decision": "approve"},
    ).json()["dispute"]
    assert d["status"] == "approved"
    assert [i["start_event_no"] for i in d["execution"]["changes"]["invalidated"]] == ["E1"]
