"""端到端冒烟测试：不依赖网络，直接用 FastAPI TestClient 跑全流程。"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".pylibs"))

_tmp = tempfile.mkdtemp()
os.environ["DB_PATH"] = os.path.join(_tmp, "test.db")

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

with TestClient(app) as client:
    # --- 健康检查 ---
    assert client.get("/health").json() == {"status": "ok"}

    # --- 1. 循环依赖被拒绝 ---
    r = client.post("/api/releases", json={
        "version": "bad",
        "nodes": [{"name": "x"}, {"name": "y"}],
        "dependencies": [
            {"from_node": "x", "to_node": "y"},
            {"from_node": "y", "to_node": "x"},
        ],
    })
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "cycle_detected"
    print("[OK] 循环依赖 -> 422 cycle_detected:", r.json()["error"].get("cycle_nodes"))

    # --- 2. 依赖边引用不存在节点 ---
    r = client.post("/api/releases", json={
        "version": "bad2",
        "nodes": [{"name": "a"}],
        "dependencies": [{"from_node": "a", "to_node": "ghost"}],
    })
    assert r.status_code == 422 and r.json()["error"]["code"] == "unknown_node"
    print("[OK] 未知节点引用 -> 422 unknown_node")

    # --- 3. 重复节点名 ---
    r = client.post("/api/releases", json={
        "version": "bad3",
        "nodes": [{"name": "a"}, {"name": "a"}],
        "dependencies": [],
    })
    assert r.status_code == 422 and r.json()["error"]["code"] == "duplicate_node"
    print("[OK] 重复节点 -> 422 duplicate_node")

    # --- 4. 正常创建批次，验证拓扑分层 ---
    r = client.post("/api/releases", json={
        "version": "v1",
        "nodes": [
            {"name": "a", "node_type": "service"},
            {"name": "b"}, {"name": "c"}, {"name": "d"},
        ],
        "dependencies": [
            {"from_node": "a", "to_node": "c"},
            {"from_node": "b", "to_node": "c"},
            {"from_node": "c", "to_node": "d"},
        ],
    })
    assert r.status_code == 201, r.text
    body = r.json()
    rid = body["id"]
    assert body["status"] == "pending"
    assert body["levels"] == [["a", "b"], ["c"], ["d"]], body["levels"]
    assert body["next_nodes"] == ["a", "b"]
    # 重试参数默认值：max_attempts=1 / retry_delay_seconds=0 / attempt_count=0
    n0 = body["nodes"][0]
    assert (n0["max_attempts"], n0["retry_delay_seconds"]) == (1, 0)
    assert (n0["attempt_count"], n0["retry_scheduled"], n0["next_retry_at"]) == (0, False, None)
    print("[OK] 创建批次", rid, "分层:", body["levels"])

    # 计划接口
    plan = client.get(f"/api/releases/{rid}/plan").json()
    assert plan["total_levels"] == 3
    assert plan["plan"] == [
        {"level": 0, "parallel_nodes": ["a", "b"]},
        {"level": 1, "parallel_nodes": ["c"]},
        {"level": 2, "parallel_nodes": ["d"]},
    ]
    # 未填 estimated_seconds：旧批次语义，工期全部按 0
    s0 = plan["schedule"]
    assert s0["shortest_duration_seconds"] == 0
    assert s0["deadline_seconds"] is None and s0["feasible"] is True
    assert s0["gap_seconds"] is None
    assert s0["critical_nodes"] == ["a", "b", "c", "d"]
    assert s0["critical_path"] == ["a", "c", "d"]  # 多条零长路径中字典序最小
    assert all(n["estimated_seconds"] == 0 and n["total_slack"] == 0 for n in plan["nodes"])
    print("[OK] 执行计划接口（缺省工期按 0，CPM 零工期分析）")

    # --- 4b. 带工期的 CPM 分析：前向 / 后向 DP、零余量关键节点 ---
    r = client.post("/api/releases", json={
        "version": "v-cpm",
        "nodes": [
            {"name": "a", "estimated_seconds": 2},
            {"name": "b", "estimated_seconds": 5},
            {"name": "c", "estimated_seconds": 3},
            {"name": "d", "estimated_seconds": 1},
        ],
        "dependencies": [
            {"from_node": "a", "to_node": "c"},
            {"from_node": "b", "to_node": "c"},
            {"from_node": "c", "to_node": "d"},
            {"from_node": "b", "to_node": "d"},
        ],
    })
    cid = r.json()["id"]
    cp = client.get(f"/api/releases/{cid}/plan").json()
    sch = cp["schedule"]
    tt = {n["name"]: n for n in cp["nodes"]}
    # 前向：a/b 从 0 开始；c 等 b（ES=5）；d 等 c（ES=8）；最短总工期 9
    assert tt["a"]["earliest_start"] == 0 and tt["a"]["latest_start"] == 3
    assert tt["b"]["earliest_start"] == 0 and tt["b"]["latest_start"] == 0
    assert tt["c"]["earliest_start"] == 5 and tt["c"]["latest_start"] == 5
    assert tt["d"]["earliest_start"] == 8 and tt["d"]["latest_start"] == 8
    assert tt["a"]["total_slack"] == 3
    assert {n for n in ("b", "c", "d") if tt[n]["total_slack"] == 0} == {"b", "c", "d"}
    assert sch["shortest_duration_seconds"] == 9
    assert sch["critical_nodes"] == ["b", "c", "d"]
    assert sch["critical_path"] == ["b", "c", "d"]  # b->d 仅 6 秒，非最长
    print("[OK] CPM：最短工期", sch["shortest_duration_seconds"],
          "关键节点", sch["critical_nodes"], "路径", sch["critical_path"])

    # --- 4c. 并列最长路径取节点名序列字典序最小 ---
    r = client.post("/api/releases", json={
        "version": "v-tie",
        "nodes": [
            {"name": "s", "estimated_seconds": 1},
            {"name": "m", "estimated_seconds": 2},
            {"name": "n", "estimated_seconds": 2},
            {"name": "e", "estimated_seconds": 1},
        ],
        "dependencies": [
            {"from_node": "s", "to_node": "m"},
            {"from_node": "s", "to_node": "n"},
            {"from_node": "m", "to_node": "e"},
            {"from_node": "n", "to_node": "e"},
        ],
    })
    tid = r.json()["id"]
    tsch = client.get(f"/api/releases/{tid}/plan").json()["schedule"]
    assert tsch["shortest_duration_seconds"] == 4
    assert tsch["critical_nodes"] == ["s", "m", "n", "e"]
    assert tsch["critical_path"] == ["s", "m", "e"], tsch["critical_path"]
    print("[OK] 并列最长路径 -> 字典序最小:", tsch["critical_path"])

    # --- 4d. deadline_seconds：可行 / 恰好 / 不可行（完整分析 + 缺口）---
    bad = client.get(f"/api/releases/{tid}/plan?deadline_seconds=3").json()["schedule"]
    assert bad["feasible"] is False and bad["gap_seconds"] == 1
    assert bad["deadline_seconds"] == 3
    assert bad["shortest_duration_seconds"] == 4  # 仍返回完整分析
    assert bad["critical_path"] == ["s", "m", "e"] and bad["critical_nodes"] == ["s", "m", "n", "e"]
    exact = client.get(f"/api/releases/{tid}/plan?deadline_seconds=4").json()["schedule"]
    assert exact["feasible"] is True and exact["gap_seconds"] == 0
    loose = client.get(f"/api/releases/{tid}/plan?deadline_seconds=10").json()["schedule"]
    assert loose["feasible"] is True and loose["gap_seconds"] == 0
    # 查询不得改变批次状态
    assert client.get(f"/api/releases/{tid}").json()["status"] == "pending"
    # deadline 非非负整数 -> 422（请求校验，不进入业务逻辑）
    assert client.get(f"/api/releases/{tid}/plan?deadline_seconds=-1").status_code == 422
    print("[OK] deadline 可行性分析：infeasible gap=1，且查询不改变批次状态")

    # --- 4e. estimated_seconds 必须为正整数 ---
    assert client.post("/api/releases", json={
        "version": "bad-dur", "nodes": [{"name": "a", "estimated_seconds": 0}]}).status_code == 422
    assert client.post("/api/releases", json={
        "version": "bad-dur", "nodes": [{"name": "a", "estimated_seconds": -5}]}).status_code == 422
    r = client.post("/api/releases", json={
        "version": "ok-dur", "nodes": [{"name": "a", "estimated_seconds": 7}]})
    assert r.status_code == 201 and r.json()["nodes"][0]["estimated_seconds"] == 7
    rid_dur = r.json()["id"]
    print("[OK] estimated_seconds 正整数校验；不传按 0")

    # --- 5. 乱序回报：a 成功后直接回报 c（b 还没成功）---
    r = client.post(f"/api/releases/{rid}/report",
                    json={"node": "a", "status": "success", "version": "v1"})
    assert r.status_code == 200 and r.json()["release"]["status"] == "running"

    r = client.post(f"/api/releases/{rid}/report",
                    json={"node": "c", "status": "success", "version": "v1"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "out_of_order"
    assert r.json()["error"]["blockers"] == [{"node": "b", "status": "pending"}]
    print("[OK] 乱序回报 -> 409 out_of_order, blockers:", r.json()["error"]["blockers"])

    # --- 6. 跨版本回报 ---
    r = client.post(f"/api/releases/{rid}/report",
                    json={"node": "b", "status": "success", "version": "v9-old"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "version_mismatch"
    print("[OK] 跨版本回报 -> 409 version_mismatch")

    # --- 7. 重复回报 ---
    r = client.post(f"/api/releases/{rid}/report",
                    json={"node": "a", "status": "success", "version": "v1"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "already_reported"
    print("[OK] 重复回报 -> 409 already_reported")

    # --- 8. b 成功 -> c 失败 -> 仅回滚 a、b（逆序）---
    r = client.post(f"/api/releases/{rid}/report",
                    json={"node": "b", "status": "success", "version": "v1"})
    assert r.status_code == 200

    r = client.post(f"/api/releases/{rid}/report", json={
        "node": "c", "status": "failed", "version": "v1",
        "error_code": "DEPLOY_TIMEOUT", "error_message": "健康检查超时",
    })
    assert r.status_code == 200, r.text
    fb = r.json()
    assert fb["result"] == "failed_and_rolled_back"
    assert fb["rollback_scope"] == "current_release_only"
    # 同层按 reported_at 倒序：b 后成功先回滚，a 随后
    assert [x["node"] for x in fb["rolled_back_nodes"]] == ["b", "a"], fb["rolled_back_nodes"]
    assert fb["resumable"] is True
    cp = fb["checkpoint"]
    assert cp["retry_endpoint"] == f"/api/releases/{rid}/retry"
    assert cp["restart_nodes"] == ["a", "b"] and cp["restart_level"] == 0
    print("[OK] c 失败 -> 回滚", [x["node"] for x in fb["rolled_back_nodes"]], "断点:", cp)

    st = fb["release"]
    assert st["status"] == "partially_rolled_back"
    assert st["failed_node"] == "c"
    node_status = {n["name"]: n["status"] for n in st["nodes"]}
    assert node_status == {"a": "rolled_back", "b": "rolled_back",
                           "c": "failed", "d": "pending"}, node_status

    # 回滚后不能直接继续回报
    r = client.post(f"/api/releases/{rid}/report",
                    json={"node": "a", "status": "success", "version": "v1"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "release_aborted"
    print("[OK] 回滚后继续回报 -> 409 release_aborted")

    # --- 9. 断点重试：failed/rolled_back 重置为 pending ---
    r = client.post(f"/api/releases/{rid}/retry")
    assert r.status_code == 200, r.text
    rb = r.json()
    assert sorted(rb["reset_nodes"]) == ["a", "b", "c"]
    assert rb["checkpoint"]["next_nodes"] == ["a", "b"]
    assert rb["release"]["status"] == "running"
    print("[OK] 断点重试 -> 重置", sorted(rb["reset_nodes"]), "next:", rb["checkpoint"]["next_nodes"])

    # 非失败状态不能重试
    r = client.post(f"/api/releases/{rid}/retry")
    assert r.status_code == 409 and r.json()["error"]["code"] == "retry_not_allowed"
    print("[OK] 非失败状态重试 -> 409 retry_not_allowed")

    # --- 10. 重试后走完全部节点 -> succeeded ---
    for node in ("a", "b", "c", "d"):
        r = client.post(f"/api/releases/{rid}/report",
                        json={"node": node, "status": "success", "version": "v1"})
    assert r.status_code == 200
    assert r.json()["release"]["status"] == "succeeded"
    assert r.json()["release"]["progress"] == {"succeeded": 4, "total": 4, "completed": True}
    print("[OK] 重试后全部成功 -> succeeded")

    # 已完成批次不再接受回报
    r = client.post(f"/api/releases/{rid}/report",
                    json={"node": "a", "status": "success", "version": "v1"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "release_finished"
    print("[OK] 完成后回报 -> 409 release_finished")

    # --- 11. 第二批失败且此前无成功节点 -> failed（而非 partially_rolled_back），只影响本批次 ---
    r = client.post("/api/releases", json={
        "version": "v2",
        "nodes": [{"name": "solo"}],
        "dependencies": [],
    })
    rid2 = r.json()["id"]
    r = client.post(f"/api/releases/{rid2}/report",
                    json={"node": "solo", "status": "failed", "version": "v2"})
    assert r.json()["release"]["status"] == "failed"
    assert r.json()["rolled_back_nodes"] == []
    # 第一批仍然 succeeded —— 回滚严格限定本批次
    assert client.get(f"/api/releases/{rid}").json()["status"] == "succeeded"
    print("[OK] 无成功节点失败 -> failed；历史批次不受影响")

    # --- 12. 部分回滚：只回滚成功的 a，无关的 pending 节点 b 不受影响 ---
    r = client.post("/api/releases", json={
        "version": "v3",
        "nodes": [{"name": "a"}, {"name": "b"}, {"name": "c"}],
        "dependencies": [{"from_node": "a", "to_node": "c"}],  # level0=[a,b], level1=[c]
    })
    rid3 = r.json()["id"]
    assert client.post(f"/api/releases/{rid3}/report",
                       json={"node": "a", "status": "success", "version": "v3"}).status_code == 200
    r = client.post(f"/api/releases/{rid3}/report",
                    json={"node": "c", "status": "failed", "version": "v3"})
    fb3 = r.json()
    assert [x["node"] for x in fb3["rolled_back_nodes"]] == ["a"]
    assert fb3["checkpoint"]["restart_nodes"] == ["a"]  # b 仍 pending，不在重置列表
    ns3 = {n["name"]: n["status"] for n in fb3["release"]["nodes"]}
    assert ns3 == {"a": "rolled_back", "b": "pending", "c": "failed"}, ns3
    rt = client.post(f"/api/releases/{rid3}/retry").json()
    # 重试后 a 重新可执行，b 本来就 pending，二者同为 level0 断点
    assert rt["checkpoint"]["next_nodes"] == ["a", "b"]
    assert sorted(rt["reset_nodes"]) == ["a", "c"]
    print("[OK] 部分回滚 -> 仅回滚 [a]；重试断点 next=[a,b]，重置=[a,c]")

    # --- 12b. 自动重试参数校验（1～10 / 0～3600）---
    for bad_node in (
        {"name": "a", "max_attempts": 0},
        {"name": "a", "max_attempts": 11},
        {"name": "a", "retry_delay_seconds": -1},
        {"name": "a", "retry_delay_seconds": 3601},
    ):
        rr = client.post("/api/releases", json={"version": "bad-retry", "nodes": [bad_node]})
        assert rr.status_code == 422, bad_node
    print("[OK] 重试参数校验：max_attempts 1～10、retry_delay_seconds 0～3600")

    # --- 12c. 有限自动重试：失败等待 -> 409/排除 -> 到期重出现 -> 成功清除标记 ---
    import time as _time  # noqa: E402

    r = client.post("/api/releases", json={
        "version": "v-retry",
        "nodes": [{"name": "r", "max_attempts": 2, "retry_delay_seconds": 1}],
    })
    assert r.status_code == 201, r.text
    rid_r = r.json()["id"]

    # 第 1 次失败：不回滚，恢复 pending，安排 1 秒后重试
    r = client.post(f"/api/releases/{rid_r}/report",
                    json={"node": "r", "status": "failed", "version": "v-retry",
                          "error_code": "BOOM"})
    assert r.status_code == 200, r.text
    sch = r.json()
    assert sch["result"] == "retry_scheduled"
    assert sch["rollback_triggered"] is False
    assert (sch["attempt_count"], sch["max_attempts"], sch["attempts_remaining"]) == (1, 2, 1)
    assert sch["retry_delay_seconds"] == 1 and sch["retry_scheduled"] is True
    assert sch["next_retry_at"] is not None
    rel = sch["release"]
    assert rel["status"] == "pending" and rel["next_nodes"] == []
    rn = next(n for n in rel["nodes"] if n["name"] == "r")
    assert rn["status"] == "pending" and rn["retry_scheduled"] is True
    assert rn["attempt_count"] == 1 and rn["max_attempts"] == 2
    assert rn["next_retry_at"] == sch["next_retry_at"]
    assert rn["error_code"] == "BOOM"  # 最近一次失败信息保留
    print("[OK] 首次失败 -> retry_scheduled（attempt 1/2），批次不回滚")

    # 等待期：GET 排除未到期节点；提前回报（成功/失败）均 409 retry_not_due 且状态不变
    assert client.get(f"/api/releases/{rid_r}").json()["next_nodes"] == []
    r = client.post(f"/api/releases/{rid_r}/report",
                    json={"node": "r", "status": "success", "version": "v-retry"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "retry_not_due", r.text
    err = r.json()["error"]
    assert err["node"] == "r" and err["attempt_count"] == 1
    assert err["max_attempts"] == 2 and err["next_retry_at"] == sch["next_retry_at"]
    r = client.post(f"/api/releases/{rid_r}/report",
                    json={"node": "r", "status": "failed", "version": "v-retry"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "retry_not_due"
    still = client.get(f"/api/releases/{rid_r}").json()
    sr = next(n for n in still["nodes"] if n["name"] == "r")
    assert sr["attempt_count"] == 1 and sr["retry_scheduled"] is True
    print("[OK] 未到期 -> next_nodes 排除且提前回报 409 retry_not_due（状态不变）")

    # 到期：节点重新出现；前置（无前置）天然满足
    _time.sleep(1.1)
    due = client.get(f"/api/releases/{rid_r}").json()
    assert due["next_nodes"] == ["r"]
    # GET 是只读接口：到期不清标记
    dr = next(n for n in due["nodes"] if n["name"] == "r")
    assert dr["retry_scheduled"] is True and dr["attempt_count"] == 1

    # 到期后成功：清除重试标记与 next_retry_at，批次完成；attempt_count 保留为历史值
    r = client.post(f"/api/releases/{rid_r}/report",
                    json={"node": "r", "status": "success", "version": "v-retry"})
    assert r.status_code == 200, r.text
    done = r.json()["release"]
    assert done["status"] == "succeeded"
    fr = next(n for n in done["nodes"] if n["name"] == "r")
    assert fr["status"] == "success" and fr["retry_scheduled"] is False
    assert fr["next_retry_at"] is None and fr["attempt_count"] == 1
    print("[OK] 到期重新出现，重试成功 -> 清除重试标记（attempt_count 保留 1）")

    # --- 12d. 达到上限 -> 保持失败回滚 / checkpoint 语义；/retry 清零计数与标记 ---
    r = client.post("/api/releases", json={
        "version": "v-retry-ex",
        "nodes": [{"name": "e", "max_attempts": 2, "retry_delay_seconds": 0}],
    })
    rid_e = r.json()["id"]
    r = client.post(f"/api/releases/{rid_e}/report",
                    json={"node": "e", "status": "failed", "version": "v-retry-ex"})
    s1 = r.json()
    # 延迟 0：立即到期，next_nodes 当批次视图中即可见
    assert s1["result"] == "retry_scheduled" and s1["attempts_remaining"] == 1
    assert s1["release"]["next_nodes"] == ["e"]
    assert s1["next_retry_at"] is not None

    # 第 2 次失败（达到上限）：failed + 回滚语义 + checkpoint
    r = client.post(f"/api/releases/{rid_e}/report",
                    json={"node": "e", "status": "failed", "version": "v-retry-ex"})
    assert r.status_code == 200, r.text
    ex = r.json()
    assert ex["result"] == "failed_and_rolled_back"
    assert ex["failed_node"] == "e" and ex["rolled_back_nodes"] == []
    en = ex["release"]["nodes"][0]
    assert en["status"] == "failed" and en["attempt_count"] == 2
    assert en["retry_scheduled"] is False and en["next_retry_at"] is None
    assert ex["release"]["status"] == "failed"
    print("[OK] attempt 达到 max_attempts=2 -> 失败回滚 + checkpoint（延迟 0 立即到期）")

    # 断点 /retry：计数清零、标记清除，节点重新可执行
    rt = client.post(f"/api/releases/{rid_e}/retry").json()
    assert rt["checkpoint"]["next_nodes"] == ["e"]
    ren = rt["release"]["nodes"][0]
    assert ren["status"] == "pending" and ren["attempt_count"] == 0
    assert ren["retry_scheduled"] is False and ren["next_retry_at"] is None
    assert client.post(f"/api/releases/{rid_e}/report",
                       json={"node": "e", "status": "success",
                             "version": "v-retry-ex"}).json()["release"]["status"] == "succeeded"
    print("[OK] /retry 断点续发清零 attempt_count / 重试标记")

    # --- 12e. 重试等待节点阻塞后继；到期且前置成功后继方可出现 ---
    r = client.post("/api/releases", json={
        "version": "v-retry-dep",
        "nodes": [
            {"name": "p", "max_attempts": 1},
            {"name": "q", "max_attempts": 3, "retry_delay_seconds": 1},
        ],
        "dependencies": [{"from_node": "p", "to_node": "q"}],
    })
    rid_q = r.json()["id"]
    assert client.post(f"/api/releases/{rid_q}/report",
                       json={"node": "p", "status": "success",
                             "version": "v-retry-dep"}).status_code == 200
    assert client.post(f"/api/releases/{rid_q}/report",
                       json={"node": "q", "status": "failed",
                             "version": "v-retry-dep"}).json()["result"] == "retry_scheduled"
    assert client.get(f"/api/releases/{rid_q}").json()["next_nodes"] == []
    _time.sleep(1.1)
    assert client.get(f"/api/releases/{rid_q}").json()["next_nodes"] == ["q"]
    assert client.post(f"/api/releases/{rid_q}/report",
                       json={"node": "q", "status": "success",
                             "version": "v-retry-dep"}).json()["release"]["status"] == "succeeded"
    print("[OK] 等待重试节点阻塞后继；到期且前置成功后重新出现")

    # --- 12f. 回滚后 /retry：等待重试中的 pending 节点一并解除调度 ---
    r = client.post("/api/releases", json={
        "version": "v-retry-mix",
        "nodes": [
            {"name": "wa", "max_attempts": 3, "retry_delay_seconds": 3600},
            {"name": "wb", "max_attempts": 1},
        ],
    })
    rid_m = r.json()["id"]
    # wa 首次失败 -> 长时间等待；wb 达到上限失败 -> 触发批次回滚（wa 仍 pending 等待）
    assert client.post(f"/api/releases/{rid_m}/report",
                       json={"node": "wa", "status": "failed",
                             "version": "v-retry-mix"}).json()["result"] == "retry_scheduled"
    fb = client.post(f"/api/releases/{rid_m}/report",
                     json={"node": "wb", "status": "failed",
                           "version": "v-retry-mix"}).json()
    assert fb["result"] == "failed_and_rolled_back"
    assert fb["release"]["status"] == "failed"
    wa_after = next(n for n in fb["release"]["nodes"] if n["name"] == "wa")
    assert wa_after["status"] == "pending" and wa_after["retry_scheduled"] is True
    assert fb["release"]["next_nodes"] == []  # wa 未到期、wb failed
    rt = client.post(f"/api/releases/{rid_m}/retry").json()
    assert sorted(rt["reset_nodes"]) == ["wb"]
    assert rt["cleared_waiting_nodes"] == ["wa"]
    assert rt["checkpoint"]["next_nodes"] == ["wa", "wb"]
    wa_rt = next(n for n in rt["release"]["nodes"] if n["name"] == "wa")
    assert wa_rt["retry_scheduled"] is False and wa_rt["next_retry_at"] is None
    assert wa_rt["attempt_count"] == 0
    print("[OK] /retry 解除退避等待节点：wa 立即回到 next_nodes 且计数清零")

    # --- 13. 列表 & 404 ---
    items = client.get("/api/releases").json()["items"]
    assert {i["id"] for i in items} == {
        rid, rid2, rid3, cid, tid, rid_dur, rid_r, rid_e, rid_q, rid_m,
    }
    assert client.get("/api/releases/nope").status_code == 404
    assert client.post("/api/releases/nope/report",
                       json={"node": "a", "status": "success", "version": "v"}).status_code == 404
    print("[OK] 列表接口 / 404")

# --- 14. 进程重启场景：对同一 SQLite 文件重新 init_db，数据仍在且表结构幂等 ---
from app import database  # noqa: E402

database.init_db(os.environ["DB_PATH"])
conn = database.get_conn()
count = conn.execute("SELECT COUNT(*) AS c FROM releases").fetchone()["c"]
assert count == 10, count
with database.lock():
    v1 = conn.execute("SELECT status, version FROM releases WHERE version='v1'").fetchone()
assert dict(v1) == {"status": "succeeded", "version": "v1"}
print("[OK] SQLite 重新初始化幂等，历史数据保留（10 个批次）")

# --- 15. 旧版 DB 文件（nodes 无 estimated_seconds 列）启动自动迁移，旧工期按 0 ---
import sqlite3  # noqa: E402

old_db = os.path.join(_tmp, "legacy.db")
_legacy = sqlite3.connect(old_db)
_legacy.executescript("""
CREATE TABLE releases (id TEXT PRIMARY KEY, version TEXT NOT NULL, status TEXT NOT NULL,
  levels_json TEXT NOT NULL DEFAULT '[]', error_code TEXT, error_message TEXT,
  failed_node TEXT, failed_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE nodes (id INTEGER PRIMARY KEY AUTOINCREMENT, release_id TEXT NOT NULL,
  name TEXT NOT NULL, node_type TEXT NOT NULL DEFAULT 'service', level INTEGER NOT NULL,
  metadata_json TEXT NOT NULL DEFAULT '{}', status TEXT NOT NULL DEFAULT 'pending',
  reported_version TEXT, error_code TEXT, error_message TEXT, reported_at TEXT,
  updated_at TEXT NOT NULL, UNIQUE(release_id, name));
CREATE TABLE edges (id INTEGER PRIMARY KEY AUTOINCREMENT, release_id TEXT NOT NULL,
  from_node TEXT, to_node TEXT, UNIQUE(release_id, from_node, to_node));
INSERT INTO releases VALUES ('leg1','ver','pending','[["x"],["y"]]',NULL,NULL,NULL,NULL,'t','t');
INSERT INTO nodes (release_id,name,node_type,level,metadata_json,status,updated_at)
  VALUES ('leg1','x','service',0,'{}','pending','t'),
         ('leg1','y','service',1,'{}','pending','t');
INSERT INTO edges (release_id,from_node,to_node) VALUES ('leg1','x','y');
""")
_legacy.commit()
_legacy.close()

from app import config  # noqa: E402

config.DB_PATH = old_db
database.init_db(old_db)
cols = {row["name"] for row in database.get_conn().execute("PRAGMA table_info(nodes)")}
assert "estimated_seconds" in cols, cols
# 重试列已迁移：旧节点 max_attempts=1（失败即回滚）、attempt_count=0、无等待标记
for col in ("max_attempts", "retry_delay_seconds", "attempt_count",
            "next_retry_at", "retry_scheduled"):
    assert col in cols, col
with TestClient(app) as legacy_client:
    detail = legacy_client.get("/api/releases/leg1").json()
    lx = detail["nodes"][0]
    assert lx["max_attempts"] == 1 and lx["retry_delay_seconds"] == 0
    assert lx["attempt_count"] == 0 and lx["retry_scheduled"] is False
    assert lx["next_retry_at"] is None
    # 旧批次失败：直接进入 failed + 回滚语义（无自动重试）
    rr = legacy_client.post("/api/releases/leg1/report",
                            json={"node": "x", "status": "failed", "version": "ver"})
    assert rr.status_code == 200, rr.text
    rb = rr.json()
    assert rb["result"] == "failed_and_rolled_back"
    assert rb["release"]["status"] == "failed"
    assert rb["release"]["nodes"][0]["attempt_count"] == 1
    print("[OK] 旧批次兼容 max_attempts=1：失败即回滚，无自动重试")

    lp = legacy_client.get("/api/releases/leg1/plan").json()
    assert lp["schedule"]["shortest_duration_seconds"] == 0
    assert lp["schedule"]["critical_nodes"] == ["x", "y"]
    assert lp["schedule"]["critical_path"] == ["x", "y"]
    assert [n["estimated_seconds"] for n in lp["nodes"]] == [0, 0]
    zero_deadline = legacy_client.get(
        "/api/releases/leg1/plan?deadline_seconds=0"
    ).json()["schedule"]
    assert zero_deadline["feasible"] is True and zero_deadline["gap_seconds"] == 0
print("[OK] 旧库自动迁移 estimated_seconds 列，旧批次工期按 0，CPM 分析完整")

print("\n全部测试通过 ✔")
