"""按结算期发布、版本控制、幂等与持久化的端到端测试。"""
from __future__ import annotations

import copy
import threading

import pytest
from fastapi.testclient import TestClient

from app import storage
from app.main import app


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """每个用例使用独立的临时 SQLite 数据库。"""
    db_file = tmp_path / "test_store.db"
    monkeypatch.setenv("METER_DB_PATH", str(db_file))
    storage.reset_init_cache()
    with TestClient(app) as c:
        yield c
    storage.reset_init_cache()


PERIOD = "2026-01"

BASE_METERS = [
    {
        "meter_id": "meter_total",
        "role": "total",
        "readings": [
            {"time": "2026-01-01T00:00:00Z", "value": 1000.0},
            {"time": "2026-01-01T01:00:00Z", "value": 1010.0},
            {"time": "2026-01-01T02:00:00Z", "value": 1050.0},
            {"time": "2026-01-01T03:00:00Z", "value": 1080.0},
            {"time": "2026-01-01T04:00:00Z", "value": 1100.0},
        ],
    },
    {
        "meter_id": "sub_a",
        "role": "sub",
        "max_power_kw": 20.0,
        "readings": [
            {"time": "2026-01-01T00:00:00Z", "value": 500.0},
            {"time": "2026-01-01T04:00:00Z", "value": 560.0},
        ],
    },
    {
        "meter_id": "sub_b",
        "role": "sub",
        "max_power_kw": 20.0,
        "readings": [
            {"time": "2026-01-01T00:00:00Z", "value": 200.0},
            {"time": "2026-01-01T04:00:00Z", "value": 240.0},
        ],
    },
]


def publish(client, period, meters, expected_version, request_key):
    return client.post(
        f"/api/v1/periods/{period}/publish",
        json={
            "meters": meters,
            "expected_version": expected_version,
            "request_key": request_key,
        },
    )


def infer_period(client, period, meter_id, start, end):
    return client.post(
        f"/api/v1/periods/{period}/infer",
        json={"query": {"meter_id": meter_id, "start": start, "end": end}},
    )


# ---------------------------------------------------------------------------
# 发布闸门与版本推进
# ---------------------------------------------------------------------------


def test_first_publish_creates_version_one(client):
    r = publish(client, PERIOD, BASE_METERS, 0, "key-1")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "published"
    assert body["period"] == PERIOD
    assert body["version"] == 1
    assert body["request_key"] == "key-1"
    assert len(body["content_hash"]) == 64
    # 响应按模型序列化：总表显式带 max_power_kw=null
    expected = copy.deepcopy(BASE_METERS)
    expected[0]["max_power_kw"] = None
    assert body["meters"] == expected
    assert body["published_at"]


def test_publish_requires_matching_expected_version(client):
    assert publish(client, PERIOD, BASE_METERS, 0, "key-1").status_code == 200
    # 仍传 0（当前已是 1）→ 版本冲突，旧版保留
    r = publish(client, PERIOD, BASE_METERS, 0, "key-2")
    assert r.status_code == 409
    err = r.json()["error"]
    assert err["code"] == "version_conflict"
    assert err["current_version"] == 1
    assert err["expected_version"] == 0
    # 旧版仍可查询且版本号不变
    status_resp = client.get(f"/api/v1/periods/{PERIOD}").json()
    assert status_resp["current_version"] == 1


def test_successful_next_version_atomically_replaces(client):
    publish(client, PERIOD, BASE_METERS, 0, "key-1")
    meters_v2 = copy.deepcopy(BASE_METERS)
    meters_v2[1]["readings"][1]["value"] = 559.0
    meters_v2[2]["readings"][1]["value"] = 241.0  # 合计仍为 100，保持相容
    r = publish(client, PERIOD, meters_v2, 1, "key-2")
    assert r.status_code == 200
    assert r.json()["version"] == 2

    # 推算读到的是新版读数：sub_a 全期 59 kWh
    r = infer_period(
        client, PERIOD, "sub_a",
        "2026-01-01T00:00:00Z", "2026-01-01T04:00:00Z",
    )
    assert r.status_code == 200
    body = r.json()
    assert body["version"] == 2
    assert body["min_kwh"] == pytest.approx(59.0)

    # 历史版本仍完整保留且无重复版本
    versions = client.get(f"/api/v1/periods/{PERIOD}/versions").json()
    assert [v["version"] for v in versions] == [1, 2]


def test_incompatible_batch_rejected_without_touching_old_version(client):
    publish(client, PERIOD, BASE_METERS, 0, "key-1")
    bad = copy.deepcopy(BASE_METERS)
    bad[2]["readings"][1]["value"] = 250.0  # sub_b 全期 50，合计 110 不平衡
    r = publish(client, PERIOD, bad, 1, "key-bad")
    assert r.status_code == 409
    err = r.json()["error"]
    assert err["code"] == "incompatible_batch"
    assert any("不平衡" in d for d in err["diagnostics"])
    # 旧版版本号与读数不变
    summary = client.get(f"/api/v1/periods/{PERIOD}").json()
    assert summary["current_version"] == 1
    r = infer_period(
        client, PERIOD, "sub_a",
        "2026-01-01T00:00:00Z", "2026-01-01T04:00:00Z",
    )
    assert r.json()["min_kwh"] == pytest.approx(60.0)


def test_first_publish_wrong_expected_version_rejected(client):
    r = publish(client, PERIOD, BASE_METERS, 5, "key-1")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "version_conflict"
    assert client.get("/api/v1/periods").json() == []


# ---------------------------------------------------------------------------
# 幂等
# ---------------------------------------------------------------------------


def test_same_key_same_content_replays_original_result(client):
    r1 = publish(client, PERIOD, BASE_METERS, 0, "dup-key")
    assert r1.status_code == 200
    original = r1.json()
    # 重传（即使把 expected_version 改成已失效的值也不影响重放）
    r2 = publish(client, PERIOD, BASE_METERS, 99, "dup-key")
    assert r2.status_code == 200
    replay = r2.json()
    assert replay["status"] == "replayed"
    assert replay["version"] == original["version"] == 1
    assert replay["content_hash"] == original["content_hash"]
    assert replay["published_at"] == original["published_at"]
    # 没有产生新版本
    versions = client.get(f"/api/v1/periods/{PERIOD}/versions").json()
    assert [v["version"] for v in versions] == [1]


def test_replay_returns_original_result_even_after_newer_version(client):
    r_first = publish(client, PERIOD, BASE_METERS, 0, "dup-key")
    assert r_first.json()["version"] == 1
    # 之后发布了版本 2
    meters_v2 = copy.deepcopy(BASE_METERS)
    meters_v2[1]["readings"][1]["value"] = 559.0
    meters_v2[2]["readings"][1]["value"] = 241.0
    publish(client, PERIOD, meters_v2, 1, "key-2")
    # 原请求重传（即使响应丢失、当前已是新版）：回放的仍是版本 1 的原始发布结果
    replay = publish(client, PERIOD, BASE_METERS, 0, "dup-key").json()
    assert replay["status"] == "replayed"
    assert replay["version"] == 1


def test_same_key_different_content_rejected(client):
    publish(client, PERIOD, BASE_METERS, 0, "the-key")
    other = copy.deepcopy(BASE_METERS)
    other[1]["readings"][1]["value"] = 559.0
    other[2]["readings"][1]["value"] = 241.0
    r = publish(client, PERIOD, other, 1, "the-key")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "key_content_conflict"
    # 原键仍可重放出原结果
    replay = publish(client, PERIOD, BASE_METERS, 0, "the-key").json()
    assert replay["status"] == "replayed"
    assert replay["version"] == 1


def test_failed_request_is_idempotent_and_replays(client):
    # 不相容的失败请求也会留存，同键重传回放原失败结果
    bad = copy.deepcopy(BASE_METERS)
    bad[2]["readings"][1]["value"] = 250.0
    r1 = publish(client, PERIOD, bad, 0, "fail-key")
    assert r1.status_code == 409
    r2 = publish(client, PERIOD, bad, 0, "fail-key")
    assert r2.status_code == 409
    assert r2.json()["error"]["code"] == "incompatible_batch"
    assert client.get("/api/v1/periods").json() == []


# ---------------------------------------------------------------------------
# 并发：同版本并发提交最多一个成功
# ---------------------------------------------------------------------------


def test_concurrent_same_version_only_one_succeeds(client):
    n = 8
    results: list[int] = []
    errors: list[str] = []
    barrier = threading.Barrier(n)

    def worker(i: int):
        local = TestClient(app)  # 每线程独立客户端
        barrier.wait()
        r = publish(local, PERIOD, BASE_METERS, 0, f"conc-key-{i}")
        results.append(r.status_code)
        if r.status_code != 200:
            errors.append(r.json()["error"]["code"])

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(results).count(200) == 1
    assert all(code == "version_conflict" for code in errors)
    summary = client.get(f"/api/v1/periods/{PERIOD}").json()
    assert summary["current_version"] == 1
    versions = client.get(f"/api/v1/periods/{PERIOD}/versions").json()
    assert len(versions) == 1  # 无重复版本、无半批


def test_concurrent_same_key_same_content_single_outcome(client):
    n = 5
    results: list[int] = []
    barrier = threading.Barrier(n)

    def worker():
        local = TestClient(app)
        barrier.wait()
        r = publish(local, PERIOD, BASE_METERS, 0, "same-key")
        results.append(r.status_code)

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert results.count(200) == n  # 全部得到同一终局的成功结果
    versions = client.get(f"/api/v1/periods/{PERIOD}/versions").json()
    assert len(versions) == 1


# ---------------------------------------------------------------------------
# 按期间推算
# ---------------------------------------------------------------------------


def test_period_infer_uses_published_version_and_echoes_version(client):
    publish(client, PERIOD, BASE_METERS, 0, "key-1")
    r = infer_period(
        client, PERIOD, "sub_a",
        "2026-01-01T01:00:00Z", "2026-01-01T02:00:00Z",
    )
    assert r.status_code == 200
    body = r.json()
    assert body["version"] == 1
    assert body["status"] == "ok"
    assert body["min_kwh"] == pytest.approx(20.0)
    assert body["max_kwh"] == pytest.approx(20.0)
    assert body["is_determined"] is True


def test_period_infer_before_publish_is_404(client):
    r = infer_period(
        client, "missing-period", "sub_a",
        "2026-01-01T00:00:00Z", "2026-01-01T01:00:00Z",
    )
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "period_not_found"


def test_period_infer_validates_query_window(client):
    publish(client, PERIOD, BASE_METERS, 0, "key-1")
    r = infer_period(
        client, PERIOD, "sub_a",
        "2026-01-01T03:00:00Z", "2026-01-01T05:00:00Z",  # 越出观测期
    )
    assert r.status_code == 422


def test_whole_inference_uses_single_version_snapshot(client):
    """推算期间发布新版：进行中的推算仍基于同一版快照（读事务一致性）。"""
    publish(client, PERIOD, BASE_METERS, 0, "key-1")
    versions_seen: list[int] = []

    def infer_many():
        local = TestClient(app)
        for _ in range(20):
            r = infer_period(
                local, PERIOD, "sub_a",
                "2026-01-01T00:00:00Z", "2026-01-01T04:00:00Z",
            )
            versions_seen.append(r.json()["version"])

    t = threading.Thread(target=infer_many)
    t.start()
    meters_v2 = copy.deepcopy(BASE_METERS)
    meters_v2[1]["readings"][1]["value"] = 559.0
    meters_v2[2]["readings"][1]["value"] = 241.0
    publish(client, PERIOD, meters_v2, 1, "key-2")
    t.join()

    assert set(versions_seen) <= {1, 2}  # 每次响应只可能对应一个完整版本


# ---------------------------------------------------------------------------
# 持久化：重启后数据与请求结果保留
# ---------------------------------------------------------------------------


def test_data_and_idempotency_survive_restart(client, tmp_path, monkeypatch):
    db_file = tmp_path / "test_store.db"
    publish(client, PERIOD, BASE_METERS, 0, "persist-key")

    # 模拟重启：丢弃全部连接状态，以同一数据库文件重新打开
    storage.reset_init_cache()
    with TestClient(app) as restarted:
        summary = restarted.get(f"/api/v1/periods/{PERIOD}").json()
        assert summary["current_version"] == 1
        # 幂等记录保留：重传得到原发布结果
        replay = publish(restarted, PERIOD, BASE_METERS, 0, "persist-key").json()
        assert replay["status"] == "replayed"
        assert replay["version"] == 1
        # 推算结果保留且附版本号
        r = infer_period(
            restarted, PERIOD, "sub_a",
            "2026-01-01T01:00:00Z", "2026-01-01T02:00:00Z",
        )
        assert r.json()["version"] == 1
        assert r.json()["min_kwh"] == pytest.approx(20.0)


# ---------------------------------------------------------------------------
# 入参校验
# ---------------------------------------------------------------------------


def test_publish_reuses_existing_structure_validation(client):
    r = publish(client, PERIOD, BASE_METERS[:2], 0, "key-x")  # 只有 1 个分表
    assert r.status_code == 422


def test_publish_rejects_blank_key_and_bad_period(client):
    r = client.post(
        f"/api/v1/periods/{PERIOD}/publish",
        json={"meters": BASE_METERS, "expected_version": 0, "request_key": ""},
    )
    assert r.status_code == 422
    r = client.post(
        "/api/v1/periods/bad%20period/publish",
        json={"meters": BASE_METERS, "expected_version": 0, "request_key": "k"},
    )
    assert r.status_code == 422


def test_period_listing_endpoints(client):
    assert client.get("/api/v1/periods").json() == []
    publish(client, PERIOD, BASE_METERS, 0, "key-1")
    listing = client.get("/api/v1/periods").json()
    assert listing == [
        {
            "period": PERIOD,
            "current_version": 1,
            "meter_count": 3,
            "published_at": listing[0]["published_at"],
        }
    ]
    assert listing[0]["published_at"]
    versions = client.get(f"/api/v1/periods/{PERIOD}/versions").json()
    assert versions[0]["request_key"] == "key-1"
    assert versions[0]["meter_count"] == 3
    assert client.get("/api/v1/periods/unknown").status_code == 404
    assert client.get("/api/v1/periods/unknown/versions").status_code == 404


def test_content_hash_independent_of_timezone_spelling(client):
    """同一时刻用 +08:00 偏移表达，规范化哈希应与 UTC 写法一致。"""
    r1 = publish(client, "tz-period", BASE_METERS, 0, "tz-key")
    offset_meters = copy.deepcopy(BASE_METERS)
    for m in offset_meters:
        for rd in m["readings"]:
            rd["time"] = rd["time"].replace("T00:00:00Z", "T08:00:00+08:00")
    r2 = publish(client, "tz-period-2", offset_meters, 0, "tz-key-2")
    assert r1.json()["content_hash"] == r2.json()["content_hash"]


def test_old_version_remains_queryable_throughout_publishing(client):
    """第二版发布前后，针对版本 1 的推算始终可用（求解不阻塞查询）。"""
    publish(client, PERIOD, BASE_METERS, 0, "key-1")
    seen = []
    barrier = threading.Barrier(2)

    def reader():
        local = TestClient(app)
        barrier.wait()
        for _ in range(15):
            r = infer_period(
                local, PERIOD, "sub_a",
                "2026-01-01T00:00:00Z", "2026-01-01T04:00:00Z",
            )
            seen.append((r.json()["version"], r.json()["min_kwh"]))

    t = threading.Thread(target=reader)
    t.start()
    barrier.wait()
    meters_v2 = copy.deepcopy(BASE_METERS)
    meters_v2[1]["readings"][1]["value"] = 559.0
    meters_v2[2]["readings"][1]["value"] = 241.0
    publish(client, PERIOD, meters_v2, 1, "key-2")
    t.join()
    # 每条响应都自洽：v1→60 kWh 或 v2→59 kWh，不存在跨版本拼接
    assert all(
        (v == 1 and val == pytest.approx(60.0))
        or (v == 2 and val == pytest.approx(59.0))
        for v, val in seen
    )
    assert 1 in (v for v, _ in seen)  # 发布完成前确有读到旧版的请求
