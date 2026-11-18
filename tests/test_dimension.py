"""可回溯排口维表测试：状态号快照、水样归属回溯、交错稳定性与失败原子性。"""
from __future__ import annotations


# ---------- 辅助 ----------

def open_batch(client, total_shards: int, cutoff: int) -> int:
    r = client.post("/batches", json={"total_shards": total_shards, "source_cutoff_seq": cutoff})
    assert r.status_code == 201, r.text
    return r.json()["batch_id"]


def shard(client, batch_id: int, shard_no: int, items: list[dict]):
    return client.post(
        f"/batches/{batch_id}/shards", json={"shard_no": shard_no, "items": items}
    )


def change(client, seq: int, ctype: str, outlet: str, district=None, active=None):
    return client.post(
        "/changes",
        json={
            "seq": seq,
            "change_type": ctype,
            "outlet_no": outlet,
            "district": district,
            "active": active,
        },
    )


def publish(client, batch_id: int, catch_up_seq: int):
    return client.post(
        f"/batches/{batch_id}/publish", json={"catch_up_seq": catch_up_seq}
    )


def enable_dim(client):
    return client.post("/dimension/enable")


def attribute(client, samples):
    return client.post("/samples/attribute", json={"samples": samples})


def states(client):
    return client.get("/dimension/states").json()


def state_detail(client, state_no):
    return client.get(f"/dimension/states/{state_no}")


def publish_v1(client, items=None):
    items = items or [
        {"outlet_no": "O1", "district": "A", "active": True},
        {"outlet_no": "O2", "district": "A", "active": False},
        {"outlet_no": "O3", "district": "B", "active": True},
    ]
    bid = open_batch(client, 1, 0)
    shard(client, bid, 1, items)
    publish(client, bid, 0)
    return bid


# ---------- 1. 启用与初始状态 ----------

def test_enable_before_first_publish_records_empty_initial_state(client):
    # 尚无发布版本（version=0）时启用：初始状态号 1，对应空清单
    r = enable_dim(client)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["enabled"] is True and body["idempotent"] is False
    assert body["state_no"] == 1 and body["source_version"] == 0
    assert body["outlet_count"] == 0 and body["active_count"] == 0

    # 重复启用幂等，不产生新状态
    r2 = enable_dim(client)
    assert r2.status_code == 200 and r2.json()["idempotent"] is True
    assert r2.json()["state_no"] == 1
    assert states(client)["count"] == 1

    health = client.get("/health").json()
    assert health["dimension_enabled"] is True and health["current_state_no"] == 1


def test_enable_after_publish_snapshots_current_live_list(client):
    publish_v1(client)
    r = enable_dim(client)
    assert r.status_code == 200
    body = r.json()
    assert body["state_no"] == 1 and body["source_version"] == 1
    assert body["outlet_count"] == 3 and body["active_count"] == 2

    detail = state_detail(client, 1).json()
    rows = {o["outlet_no"]: o for o in detail["items"]}
    assert rows["O1"]["district"] == "A" and rows["O1"]["active"] is True
    assert rows["O2"]["active"] is False  # 停用排口也完整保留在快照里

    # 现役查询返回状态号
    listed = client.get("/outlets").json()
    assert listed["state_no"] == 1
    assert client.get("/stats").json()["state_no"] == 1


def test_attribute_requires_enabled_dimension(client):
    r = attribute(client, [
        {"sample_no": "S1", "outlet_no": "O1", "state_no": 1}
    ])
    assert r.status_code == 409 and r.json()["code"] == "DIMENSION_NOT_ENABLED"


# ---------- 2. 已生效增量产生状态，暂挂/阻断不产生 ----------

def test_effective_change_creates_state_blocked_does_not(client):
    publish_v1(client)
    enable_dim(client)
    assert states(client)["current_state_no"] == 1

    # seq=1 对不存在排口 move -> 阻断，不产生新状态
    r = change(client, 1, "move", "GHOST", district="Z")
    assert r.status_code == 201 and r.json()["blocked"] is True
    assert r.json()["current_state_no"] == 1
    assert states(client)["count"] == 1

    # seq=2 被断点挡住，暂挂不套用，同样不产生新状态
    r = change(client, 2, "deactivate", "O3")
    assert r.status_code == 201 and r.json()["applied"] is False
    assert r.json()["current_state_no"] == 1
    assert states(client)["count"] == 1

    # 幂等重试也不产生状态
    r = change(client, 2, "deactivate", "O3")
    assert r.json()["idempotent"] is True and states(client)["count"] == 1


def test_effective_incremental_change_creates_traceable_state(client):
    publish_v1(client)
    enable_dim(client)

    # seq=1 迁区生效 -> 状态号 2
    r = change(client, 1, "move", "O1", district="C")
    assert r.status_code == 201 and r.json()["applied"] is True
    assert r.json()["current_state_no"] == 2

    # seq=2 停用生效 -> 状态号 3
    r = change(client, 2, "deactivate", "O3")
    assert r.json()["current_state_no"] == 3

    flow = states(client)["items"]
    sources = [(s["state_no"], s["source"], s["ref_seq"]) for s in flow]
    # 列表按状态号倒序
    assert sources == [(3, "change", 2), (2, "change", 1), (1, "initial", None)]

    # 状态 2 的快照里 O1 已在 C 区、O3 仍启用；状态 1 的历史快照保持 A 区不变
    s1 = {o["outlet_no"]: o for o in state_detail(client, 1).json()["items"]}
    s2 = {o["outlet_no"]: o for o in state_detail(client, 2).json()["items"]}
    s3 = {o["outlet_no"]: o for o in state_detail(client, 3).json()["items"]}
    assert s1["O1"]["district"] == "A"
    assert s2["O1"]["district"] == "C" and s2["O3"]["active"] is True
    assert s3["O3"]["active"] is False
    # 每个状态都是完整清单
    assert all(len(d) == 3 for d in (s1, s2, s3))


# ---------- 3. 水样归属回溯 ----------

def test_sample_attribution_uses_state_at_sampling_time(client):
    publish_v1(client)
    enable_dim(client)  # state 1: O1 在 A

    # 水样 S1 在状态 1 时采样
    s1_snapshot = 1
    change(client, 1, "move", "O1", district="C")  # state 2: O1 迁到 C
    change(client, 2, "deactivate", "O1")          # state 3: O1 停用

    # 后来迁区+停用不影响旧样本：S1 仍按状态 1 归 A 且当时启用
    r = attribute(client, [
        {"sample_no": "S1", "outlet_no": "O1", "state_no": s1_snapshot},
        {"sample_no": "S2", "outlet_no": "O1", "state_no": 2},
        {"sample_no": "S3", "outlet_no": "O1", "state_no": 3},
    ])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["count"] == 3 and body["attributed_count"] == 3
    by_sample = {x["sample_no"]: x for x in body["results"]}
    assert by_sample["S1"]["district"] == "A" and by_sample["S1"]["active"] is True
    assert by_sample["S2"]["district"] == "C" and by_sample["S2"]["active"] is True
    assert by_sample["S3"]["district"] == "C" and by_sample["S3"]["active"] is False
    assert all(x["reason"] is None for x in body["results"])


def test_attribution_unattributable_reasons(client):
    publish_v1(client)
    enable_dim(client)  # state 1
    change(client, 1, "move", "O1", district="C")  # state 2

    r = attribute(client, [
        {"sample_no": "S1", "outlet_no": "O1", "state_no": 99},   # 状态号不存在
        {"sample_no": "S2", "outlet_no": "GHOST", "state_no": 1},  # 该状态快照无此排口
        {"sample_no": "S3", "outlet_no": "O1", "state_no": 1},     # 正常
    ])
    assert r.status_code == 200
    body = r.json()
    assert body["attributed_count"] == 1 and body["unattributable_count"] == 2
    by_sample = {x["sample_no"]: x for x in body["results"]}
    assert by_sample["S1"]["attributable"] is False
    assert by_sample["S1"]["reason"] == "STATE_NOT_FOUND"
    assert by_sample["S2"]["reason"] == "OUTLET_NOT_IN_STATE"
    assert by_sample["S2"]["district"] is None
    assert by_sample["S3"]["attributable"] is True and by_sample["S3"]["district"] == "A"


def test_attribution_never_infers_from_live_rows(client):
    # 旧状态中不存在的排口，即使后来通过新增进入现役清单，也不能用现役行推断旧状态
    publish_v1(client)
    enable_dim(client)  # state 1（无 O9）
    change(client, 1, "add", "O9", district="D", active=True)  # state 2 含 O9

    r = attribute(client, [
        {"sample_no": "OLD", "outlet_no": "O9", "state_no": 1},
        {"sample_no": "NEW", "outlet_no": "O9", "state_no": 2},
    ])
    body = r.json()
    by_sample = {x["sample_no"]: x for x in body["results"]}
    assert by_sample["OLD"]["reason"] == "OUTLET_NOT_IN_STATE"
    assert by_sample["NEW"]["attributable"] is True
    assert by_sample["NEW"]["district"] == "D"


def test_state_detail_404(client):
    enable_dim(client)
    r = state_detail(client, 77)
    assert r.status_code == 404 and r.json()["code"] == "STATE_NOT_FOUND"


# ---------- 4. 整批换版与增量交错：旧样本归属稳定，新状态对应完整清单 ----------

def test_interleaved_publish_and_changes_keep_history_stable(client):
    # v1 + 启用 -> state 1
    publish_v1(client)
    enable_dim(client)

    # 增量迁区 -> state 2
    change(client, 1, "move", "O1", district="C")
    assert client.get("/outlets").json()["state_no"] == 2

    # 整批换版：新快照含 O4，窗口重放 seq1，发布生成 state 3（完整清单）
    bid2 = open_batch(client, 1, cutoff=0)
    shard(client, bid2, 1, [
        {"outlet_no": "O1", "district": "A", "active": True},
        {"outlet_no": "O2", "district": "A", "active": False},
        {"outlet_no": "O3", "district": "B", "active": True},
        {"outlet_no": "O4", "district": "D", "active": True},
    ])
    r = publish(client, bid2, 1)
    assert r.status_code == 200, r.text
    assert r.json()["version"] == 2 and r.json()["current_state_no"] == 3

    st3 = state_detail(client, 3).json()
    assert st3["source"] == "publish" and st3["ref_version"] == 2
    rows3 = {o["outlet_no"]: o for o in st3["items"]}
    assert len(rows3) == 4  # 新状态对应完整清单
    assert rows3["O1"]["district"] == "C"  # 窗口重放后 O1 在 C
    assert rows3["O4"]["district"] == "D"

    # 换版后增量继续 -> state 4
    r = change(client, 2, "deactivate", "O4")
    assert r.status_code == 201 and r.json()["current_state_no"] == 4

    # 再换版一次 -> state 5
    bid3 = open_batch(client, 1, cutoff=2)
    shard(client, bid3, 1, [
        {"outlet_no": "O1", "district": "C", "active": True},
        {"outlet_no": "O2", "district": "A", "active": False},
        {"outlet_no": "O3", "district": "B", "active": True},
        {"outlet_no": "O4", "district": "D", "active": False},
    ])
    r = publish(client, bid3, 2)
    assert r.status_code == 200 and r.json()["current_state_no"] == 5

    # 交错全程结束后，各历史状态原封不动，旧样本按采样状态稳定归属
    flow = states(client)
    assert flow["count"] == 5 and flow["current_state_no"] == 5
    r = attribute(client, [
        {"sample_no": "AT-1", "outlet_no": "O1", "state_no": 1},  # 最早：A 区
        {"sample_no": "AT-2", "outlet_no": "O1", "state_no": 2},  # 迁区后：C 区
        {"sample_no": "AT-3", "outlet_no": "O4", "state_no": 3},  # 换版引入：D 区启用
        {"sample_no": "AT-4", "outlet_no": "O4", "state_no": 4},  # 停用后
        {"sample_no": "AT-5", "outlet_no": "O4", "state_no": 5},
    ])
    by_sample = {x["sample_no"]: x for x in r.json()["results"]}
    assert by_sample["AT-1"]["district"] == "A" and by_sample["AT-1"]["active"] is True
    assert by_sample["AT-2"]["district"] == "C"
    assert by_sample["AT-3"]["active"] is True and by_sample["AT-4"]["active"] is False
    assert by_sample["AT-5"]["district"] == "D" and by_sample["AT-5"]["active"] is False


def test_publish_failure_leaves_no_visible_half_history(client, monkeypatch):
    publish_v1(client)
    enable_dim(client)  # state 1
    change(client, 1, "deactivate", "GHOST")  # 阻断，不产生状态，仍只有 state 1

    # 构造必然失败的换版（新快照不含 GHOST，窗口重放失败整体回滚）
    bid2 = open_batch(client, 1, cutoff=0)
    shard(client, bid2, 1, [
        {"outlet_no": "O1", "district": "A", "active": True},
        {"outlet_no": "O2", "district": "A", "active": False},
        {"outlet_no": "O3", "district": "B", "active": True},
    ])
    r = publish(client, bid2, 1)
    assert r.status_code == 422 and r.json()["code"] == "PUBLISH_CHANGE_FAILED"

    # 失败后没有任何新状态、没有半份快照可见
    flow = states(client)
    assert flow["count"] == 1 and flow["current_state_no"] == 1
    assert state_detail(client, 2).status_code == 404
    # 现役版本与水位不变
    health = client.get("/health").json()
    assert health["version"] == 1 and health["current_state_no"] == 1


def test_snapshot_write_failure_rolls_back_entire_publish(client, monkeypatch):
    publish_v1(client)
    enable_dim(client)  # state 1

    bid2 = open_batch(client, 1, cutoff=0)
    shard(client, bid2, 1, [
        {"outlet_no": "O1", "district": "A", "active": True},
        {"outlet_no": "O2", "district": "A", "active": False},
        {"outlet_no": "O3", "district": "B", "active": True},
        {"outlet_no": "O9", "district": "X", "active": True},
    ])

    # 在维表快照写入瞬间注入故障：整事务回滚，旧版与维表状态都不动
    from app import service
    real_snapshot = service._dim_snapshot
    calls = {"n": 0}

    def flaky_snapshot(conn, source, version, ref_seq=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("simulated snapshot disk failure")
        return real_snapshot(conn, source, version, ref_seq)

    monkeypatch.setattr(service, "_dim_snapshot", flaky_snapshot)
    r = publish(client, bid2, 0)
    assert r.status_code == 500
    monkeypatch.setattr(service, "_dim_snapshot", real_snapshot)

    # 无半份历史：仍只有状态 1；现役仍为 v1
    flow = states(client)
    assert flow["count"] == 1 and flow["current_state_no"] == 1
    assert client.get("/stats").json()["version"] == 1
    assert client.get("/batches/current").json()["batch_id"] == bid2

    # 恢复后重试发布成功并生成状态 2
    r = publish(client, bid2, 0)
    assert r.status_code == 200 and r.json()["current_state_no"] == 2
    detail = state_detail(client, 2).json()
    assert detail["outlet_count"] == 4 and detail["source"] == "publish"


def test_blocked_change_resolved_by_publish_then_state_chain_resumes(client):
    publish_v1(client)
    enable_dim(client)  # state 1

    # 阻断变更不产生状态
    change(client, 1, "move", "GHOST", district="Z")
    assert states(client)["count"] == 1

    # 换版补齐 GHOST 并在窗口重放解除阻断 -> 换版本身生成一个完整状态
    bid2 = open_batch(client, 1, cutoff=0)
    shard(client, bid2, 1, [
        {"outlet_no": "O1", "district": "A", "active": True},
        {"outlet_no": "O2", "district": "A", "active": False},
        {"outlet_no": "O3", "district": "B", "active": True},
        {"outlet_no": "GHOST", "district": "Q", "active": True},
    ])
    r = publish(client, bid2, 1)
    assert r.status_code == 200 and r.json()["current_state_no"] == 2
    rows = {o["outlet_no"]: o for o in state_detail(client, 2).json()["items"]}
    assert rows["GHOST"]["district"] == "Z"  # seq1 在窗口重放生效后的结果入快照

    # 此后增量状态链继续递增
    r = change(client, 2, "deactivate", "O1")
    assert r.json()["current_state_no"] == 3


# ---------- 5. 停用排口与边界 ----------

def test_inactive_outlet_remains_attributable_with_active_flag(client):
    # O2 初始即停用，仍可按状态归属，只是 active=false
    publish_v1(client)
    enable_dim(client)
    r = attribute(client, [
        {"sample_no": "S", "outlet_no": "O2", "state_no": 1},
    ])
    item = r.json()["results"][0]
    assert item["attributable"] is True
    assert item["district"] == "A" and item["active"] is False


def test_empty_samples_rejected_by_validation(client):
    enable_dim(client)
    r = attribute(client, [])
    assert r.status_code == 422


def test_dimension_disabled_endpoints_before_enable(client):
    # 未启用时状态列表 enabled=false，现役查询 state_no 为 None
    flow = client.get("/dimension/states").json()
    assert flow["enabled"] is False and flow["current_state_no"] is None and flow["items"] == []
    assert client.get("/outlets").json()["state_no"] is None
    assert client.get("/health").json()["dimension_enabled"] is False
