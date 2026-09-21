"""核心业务逻辑：发布批次、拓扑分层、节点回报、失败回滚、重试断点。"""
from __future__ import annotations

import json
import uuid
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from typing import Any

from .database import get_conn, lock, transaction
from .errors import APIError
from .models import CreateReleaseRequest, ReportRequest

# 节点状态
PENDING, SUCCESS, FAILED, ROLLED_BACK = "pending", "success", "failed", "rolled_back"
# 发布状态
R_PENDING, R_RUNNING = "pending", "running"
R_SUCCEEDED, R_FAILED, R_ROLLED_BACK = "succeeded", "failed", "partially_rolled_back"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


# --------------------------------------------------------------------------- #
# 拓扑分层
# --------------------------------------------------------------------------- #
def compute_levels(node_names: list[str], edges: list[tuple[str, str]]) -> list[list[str]]:
    """Kahn 算法按依赖拓扑分层。

    边 (from_node, to_node) 表示 to_node 依赖 from_node，from_node 位于更低层级。
    检测到环时抛出 422 cycle_detected。
    """
    names = set(node_names)
    dependents: dict[str, set[str]] = defaultdict(set)  # 前置 -> 后置集合
    indegree: dict[str, int] = {n: 0 for n in names}

    for frm, to in edges:
        if frm not in names:
            raise APIError(422, "unknown_node", f"依赖边引用了不存在的节点: {frm}")
        if to not in names:
            raise APIError(422, "unknown_node", f"依赖边引用了不存在的节点: {to}")
        if to in dependents[frm]:  # 去重后的重复边
            continue
        dependents[frm].add(to)
        indegree[to] += 1

    current = sorted(n for n, d in indegree.items() if d == 0)
    levels: list[list[str]] = []
    processed: set[str] = set()

    while current:
        levels.append(current)
        processed.update(current)
        nxt: list[str] = []
        for node in current:
            for dep in dependents[node]:
                indegree[dep] -= 1
                if indegree[dep] == 0:
                    nxt.append(dep)
        current = sorted(nxt)

    if len(processed) != len(names):
        cycle_nodes = sorted(names - processed)
        raise APIError(
            422,
            "cycle_detected",
            f"检测到循环依赖，涉及节点: {', '.join(cycle_nodes)}",
            {"cycle_nodes": cycle_nodes},
        )
    return levels


# --------------------------------------------------------------------------- #
# 关键路径（CPM）：前向 / 后向动态规划
# --------------------------------------------------------------------------- #
def compute_schedule(
    nodes: list[dict[str, Any]],
    edges: list[tuple[str, str]],
    deadline_seconds: int | None = None,
) -> dict[str, Any]:
    """基于依赖 DAG 计算最短工期、最早 / 最晚开始时间、总时差与关键路径。

    - 前向 DP：ES[v] = max(EF[u])，EF[v] = ES[v] + duration[v]
    - 后向 DP：LF[v] = min(LS[w])，LS[v] = LF[v] - duration[v]
    - total_slack = LS - ES；为 0 的节点即关键节点
    - 后向推算锚定在最短总工期 T（保证时差非负、关键节点语义稳定）；
      deadline 仅用于可行性判定，小于 T 时仍返回完整分析
    - critical_path 在所有最长源-汇路径中返回节点名序列字典序最小的一条

    ``nodes`` 已按 (level, name) 排序（拓扑序）；旧批次缺失工期按 0 处理。
    """
    order = [n["name"] for n in nodes]
    duration = {n["name"]: int(n.get("estimated_seconds") or 0) for n in nodes}

    prereqs: dict[str, list[str]] = defaultdict(list)
    succs: dict[str, list[str]] = defaultdict(list)
    for frm, to in dict.fromkeys(edges):
        prereqs[to].append(frm)
        succs[frm].append(to)

    # ---- 前向：最早开始 ES / 最早完成 EF ----
    es: dict[str, int] = {}
    ef: dict[str, int] = {}
    for name in order:
        es[name] = max((ef[p] for p in prereqs[name]), default=0)
        ef[name] = es[name] + duration[name]
    total_duration = max(ef.values(), default=0)

    # ---- 后向：最晚完成 LF / 最晚开始 LS（锚定最短总工期）----
    lf: dict[str, int] = {}
    ls: dict[str, int] = {}
    for name in reversed(order):
        lf[name] = min((ls[s] for s in succs[name]), default=total_duration)
        ls[name] = lf[name] - duration[name]

    slack = {name: ls[name] - es[name] for name in order}
    critical = {name for name in order if slack[name] == 0}

    # ---- 关键子图：两端均关键且无等待的“紧”边 EF[u] == ES[v] ----
    critical_succ: dict[str, list[str]] = defaultdict(list)
    for frm, to in dict.fromkeys(edges):
        if frm in critical and to in critical and ef[frm] == es[to]:
            critical_succ[frm].append(to)

    # 仅保留能抵达关键汇点的边（标准 DAG 下关键节点都能抵达汇点，这里做防御）
    critical_sinks = {n for n in critical if ef[n] == total_duration}
    can_reach_sink: set[str] = set(critical_sinks)
    # 沿关键紧边逆向 BFS：所有能到达关键汇点的关键节点
    stack = list(critical_sinks)
    rev_critical: dict[str, list[str]] = defaultdict(list)
    for frm, nbrs in critical_succ.items():
        for s in nbrs:
            rev_critical[s].append(frm)
    while stack:
        cur = stack.pop()
        for p in rev_critical[cur]:
            if p not in can_reach_sink:
                can_reach_sink.add(p)
                stack.append(p)

    # ---- 贪心选字典序最小最长路径：每一步选名字最小且仍可走到汇点的后继 ----
    critical_path: list[str] = []
    starts = sorted(n for n in order if n in critical and es[n] == 0)
    if starts:
        cur = next((n for n in starts if n in can_reach_sink), None)
        while cur is not None:
            critical_path.append(cur)
            nxt = sorted(s for s in critical_succ[cur] if s in can_reach_sink)
            cur = nxt[0] if nxt else None

    feasible = True
    gap_seconds: int | None = None
    if deadline_seconds is not None:
        feasible = deadline_seconds >= total_duration
        gap_seconds = max(0, total_duration - deadline_seconds)

    node_schedule = {
        name: {
            "estimated_seconds": duration[name],
            "earliest_start": es[name],
            "latest_start": ls[name],
            "total_slack": slack[name],
            "is_critical": name in critical,
        }
        for name in order
    }
    return {
        "shortest_duration_seconds": total_duration,
        "deadline_seconds": deadline_seconds,
        "feasible": feasible,
        "gap_seconds": gap_seconds,
        "critical_nodes": [n for n in order if n in critical],
        "critical_path": critical_path,
        "node_schedule": node_schedule,
    }


# --------------------------------------------------------------------------- #
# 发布批次
# --------------------------------------------------------------------------- #
def create_release(payload: CreateReleaseRequest) -> dict[str, Any]:
    node_names = [n.name for n in payload.nodes]
    if len(set(node_names)) != len(node_names):
        dupes = sorted({n for n in node_names if node_names.count(n) > 1})
        raise APIError(422, "duplicate_node", f"节点名重复: {', '.join(dupes)}", {"nodes": dupes})

    edges = [(e.from_node, e.to_node) for e in payload.dependencies]
    levels = compute_levels(node_names, edges)

    release_id = _new_id()
    now = _now()
    level_of = {name: idx for idx, layer in enumerate(levels) for name in layer}

    with transaction() as conn:
        conn.execute(
            """INSERT INTO releases (id, version, status, levels_json, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (release_id, payload.version, R_PENDING, json.dumps(levels), now, now),
        )
        for node in payload.nodes:
            conn.execute(
                """INSERT INTO nodes
                   (release_id, name, node_type, estimated_seconds,
                    max_attempts, retry_delay_seconds, level,
                    metadata_json, status, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    release_id,
                    node.name,
                    node.node_type,
                    node.estimated_seconds or 0,
                    node.max_attempts,
                    node.retry_delay_seconds,
                    level_of[node.name],
                    json.dumps(node.metadata, ensure_ascii=False),
                    PENDING,
                    now,
                ),
            )
        for frm, to in dict.fromkeys(edges):  # 去重，保持插入顺序
            conn.execute(
                "INSERT INTO edges (release_id, from_node, to_node) VALUES (?, ?, ?)",
                (release_id, frm, to),
            )

    return get_release(release_id)


def _get_release_row(conn, release_id: str):
    row = conn.execute("SELECT * FROM releases WHERE id = ?", (release_id,)).fetchone()
    if row is None:
        raise APIError(404, "release_not_found", f"发布批次不存在: {release_id}")
    return row


def list_releases(limit: int = 50) -> list[dict[str, Any]]:
    with lock():
        conn = get_conn()
        rows = conn.execute(
            "SELECT * FROM releases ORDER BY created_at DESC, id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [_release_summary(dict(r)) for r in rows]


def _release_summary(r: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": r["id"],
        "version": r["version"],
        "status": r["status"],
        "levels": json.loads(r["levels_json"]),
        "failed_node": r["failed_node"],
        "created_at": r["created_at"],
        "updated_at": r["updated_at"],
    }


def get_release_plan(
    release_id: str, deadline_seconds: int | None = None
) -> dict[str, Any]:
    with lock():
        conn = get_conn()
        r = dict(_get_release_row(conn, release_id))
        nodes = [dict(n) for n in conn.execute(
            "SELECT * FROM nodes WHERE release_id = ? ORDER BY level, name", (release_id,)
        )]
        edges = [dict(e) for e in conn.execute(
            "SELECT from_node, to_node FROM edges WHERE release_id = ? ORDER BY from_node, to_node",
            (release_id,),
        )]

    # 纯只读分析：不开启事务、不写入任何状态；旧批次缺失工期按 0 处理
    edge_pairs = [(e["from_node"], e["to_node"]) for e in edges]
    schedule = compute_schedule(nodes, edge_pairs, deadline_seconds)
    node_schedule = schedule.pop("node_schedule")

    levels = json.loads(r["levels_json"])
    return {
        "release_id": r["id"],
        "version": r["version"],
        "status": r["status"],
        "total_levels": len(levels),
        "plan": [
            {"level": idx, "parallel_nodes": layer}
            for idx, layer in enumerate(levels)
        ],
        "nodes": [
            {"name": n["name"], "node_type": n["node_type"], "level": n["level"],
             "metadata": json.loads(n["metadata_json"]),
             **node_schedule[n["name"]]}
            for n in nodes
        ],
        "dependencies": [{"from": e["from_node"], "to": e["to_node"]} for e in edges],
        "schedule": schedule,
        "execution_rule": "按 level 从小到大执行；同一 level 内节点无依赖关系可并行回报；"
                          "节点所有前置为 success 后才允许回报",
        "created_at": r["created_at"],
    }


def _parse_ts(value: str | None) -> datetime | None:
    """解析 _now() 产出的带时区 ISO8601 时间；空值 / 异常值返回 None。"""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _retry_due(n: dict[str, Any], now: datetime) -> bool:
    """等待自动重试的节点是否已到期（next_retry_at 为空视为已到期）。"""
    if not n.get("retry_scheduled"):
        return True
    due_at = _parse_ts(n.get("next_retry_at"))
    return due_at is None or due_at <= now


def _build_view(conn, r) -> dict[str, Any]:
    release_id = r["id"]
    node_rows = [dict(n) for n in conn.execute(
        "SELECT * FROM nodes WHERE release_id = ? ORDER BY level, name", (release_id,)
    )]
    edge_rows = [dict(e) for e in conn.execute(
        "SELECT from_node, to_node FROM edges WHERE release_id = ?", (release_id,))]

    prereqs: dict[str, list[str]] = defaultdict(list)
    for e in edge_rows:
        prereqs[e["to_node"]].append(e["from_node"])

    now = datetime.now(timezone.utc)
    nodes = []
    status_of: dict[str, str] = {}
    for n in node_rows:
        status_of[n["name"]] = n["status"]
        nodes.append({
            "name": n["name"],
            "node_type": n["node_type"],
            "estimated_seconds": int(n["estimated_seconds"] or 0),
            "level": n["level"],
            "status": n["status"],
            "max_attempts": int(n["max_attempts"] if n["max_attempts"] is not None else 1),
            "retry_delay_seconds": int(n["retry_delay_seconds"] or 0),
            "attempt_count": int(n["attempt_count"] or 0),
            "retry_scheduled": bool(n["retry_scheduled"]),
            "next_retry_at": n["next_retry_at"],
            "reported_version": n["reported_version"],
            "error_code": n["error_code"],
            "error_message": n["error_message"],
            "reported_at": n["reported_at"],
            "updated_at": n["updated_at"],
            "metadata": json.loads(n["metadata_json"]),
        })

    def _eligible(n: dict[str, Any]) -> bool:
        if n["status"] != PENDING or not _retry_due(n, now):
            return False
        # 等待自动重试期间节点保持 pending；到期且前置全部成功才重新出现
        return all(status_of.get(p) == SUCCESS for p in prereqs[n["name"]])

    # 当前可执行（可回报）的 pending 节点：未到重试时刻的节点被排除
    next_nodes = sorted(n["name"] for n in node_rows if _eligible(n))
    done = sum(1 for s in status_of.values() if s == SUCCESS)
    total = len(node_rows)

    return {
        "id": r["id"],
        "version": r["version"],
        "status": r["status"],
        "levels": json.loads(r["levels_json"]),
        "failed_node": r["failed_node"],
        "error_code": r["error_code"],
        "error_message": r["error_message"],
        "failed_at": r["failed_at"],
        "created_at": r["created_at"],
        "updated_at": r["updated_at"],
        "progress": {"succeeded": done, "total": total, "completed": done == total and total > 0},
        "next_nodes": next_nodes,
        "nodes": nodes,
        "dependencies": [{"from": e["from_node"], "to": e["to_node"]} for e in edge_rows],
    }


def get_release(release_id: str) -> dict[str, Any]:
    with lock():
        conn = get_conn()
        r = dict(_get_release_row(conn, release_id))
        return _build_view(conn, r)


# --------------------------------------------------------------------------- #
# 节点回报
# --------------------------------------------------------------------------- #
def _record_event(conn, release_id: str, node: str, event_type: str,
                  version: str | None, detail: dict[str, Any]) -> None:
    conn.execute(
        """INSERT INTO node_events (release_id, node_name, event_type, version, detail_json, created_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (release_id, node, event_type, version, json.dumps(detail, ensure_ascii=False), _now()),
    )


def report_node(release_id: str, payload: ReportRequest) -> dict[str, Any]:
    with transaction() as conn:
        r = dict(_get_release_row(conn, release_id))

        # 1) 拒绝跨版本回报
        if payload.version != r["version"]:
            raise APIError(
                409, "version_mismatch",
                f"回报版本 {payload.version} 与批次版本 {r['version']} 不一致，拒绝跨版本回报",
                {"reported_version": payload.version, "release_version": r["version"]},
            )

        # 2) 批次必须处于可回报状态
        if r["status"] in (R_FAILED, R_ROLLED_BACK):
            raise APIError(
                409, "release_aborted",
                "该批次已失败并执行过回滚，请先调用 /retry 从断点续发后再回报",
                {"checkpoint": _build_view(conn, r)["next_nodes"]},
            )
        if r["status"] == R_SUCCEEDED:
            raise APIError(409, "release_finished", "该批次已全部成功，不再接受回报")

        node_row = conn.execute(
            "SELECT * FROM nodes WHERE release_id = ? AND name = ?",
            (release_id, payload.node),
        ).fetchone()
        if node_row is None:
            raise APIError(404, "node_not_found", f"批次中不存在节点: {payload.node}")
        node = dict(node_row)

        # 3) 节点级状态校验
        if node["status"] == SUCCESS:
            raise APIError(409, "already_reported", f"节点 {payload.node} 已回报成功，不可重复回报")
        if node["status"] == FAILED:
            raise APIError(409, "already_reported", f"节点 {payload.node} 已回报失败")
        if node["status"] == ROLLED_BACK:
            raise APIError(
                409, "node_rolled_back",
                f"节点 {payload.node} 已在失败回滚中被回滚，请调用 /retry 续发",
            )

        # 3.5) 自动重试退避未到期：节点保持 pending，拒绝提前回报且状态不变
        if node["retry_scheduled"]:
            due_at = _parse_ts(node["next_retry_at"])
            now_ts = datetime.now(timezone.utc)
            if due_at is not None and due_at > now_ts:
                raise APIError(
                    409, "retry_not_due",
                    f"节点 {payload.node} 的自动重试尚未到期，请在 {node['next_retry_at']} 之后再回报",
                    {"node": payload.node, "attempt_count": int(node["attempt_count"] or 0),
                     "max_attempts": int(node["max_attempts"] or 1),
                     "next_retry_at": node["next_retry_at"]},
                )

        # 4) 乱序回报校验：所有前置必须已成功（拓扑顺序不可跨越）
        prereq_rows = conn.execute(
            "SELECT from_node FROM edges WHERE release_id = ? AND to_node = ?",
            (release_id, payload.node),
        ).fetchall()
        prereqs = [row["from_node"] for row in prereq_rows]
        if prereqs:
            placeholders = ",".join("?" for _ in prereqs)
            blocker_rows = conn.execute(
                f"SELECT name, status FROM nodes WHERE release_id = ? AND name IN ({placeholders})",
                (release_id, *prereqs),
            ).fetchall()
            blockers = [
                {"node": b["name"], "status": b["status"]}
                for b in blocker_rows if b["status"] != SUCCESS
            ]
            if blockers:
                raise APIError(
                    409, "out_of_order",
                    f"节点 {payload.node} 的前置节点尚未全部成功，拒绝乱序回报",
                    {"node": payload.node, "blockers": blockers},
                )

        now = _now()

        if payload.status == SUCCESS:
            # 重试到期后成功：清除重试标记，attempt_count 保留为历史失败次数
            conn.execute(
                """UPDATE nodes SET status = ?, reported_version = ?, error_code = NULL,
                   error_message = NULL, reported_at = ?, updated_at = ?,
                   next_retry_at = NULL, retry_scheduled = 0
                   WHERE id = ?""",
                (SUCCESS, payload.version, now, now, node["id"]),
            )
            _record_event(conn, release_id, payload.node, "report_success", payload.version,
                          {"attempt_count": int(node["attempt_count"] or 0)})

            pending = conn.execute(
                "SELECT COUNT(*) AS c FROM nodes WHERE release_id = ? AND status != ?",
                (release_id, SUCCESS),
            ).fetchone()["c"]
            new_status = R_SUCCEEDED if pending == 0 else R_RUNNING
            conn.execute(
                "UPDATE releases SET status = ?, updated_at = ? WHERE id = ?",
                (new_status, now, release_id),
            )
            r["status"] = new_status
            r["updated_at"] = now
            view = _build_view(conn, r)
            return {"result": "accepted", "release": view}

        # ---- 失败回报：先原子递增 attempt_count，再决定“延迟重试”或“失败回滚” ----
        max_attempts = int(node["max_attempts"] if node["max_attempts"] is not None else 1)
        delay = int(node["retry_delay_seconds"] or 0)
        conn.execute(
            "UPDATE nodes SET attempt_count = attempt_count + 1 WHERE id = ?",
            (node["id"],),
        )
        attempt_count = conn.execute(
            "SELECT attempt_count FROM nodes WHERE id = ?", (node["id"],)
        ).fetchone()["attempt_count"]
        _record_event(
            conn, release_id, payload.node, "report_failure", payload.version,
            {"error_code": payload.error_code, "error_message": payload.error_message,
             "attempt_count": attempt_count, "max_attempts": max_attempts},
        )

        # 尚未达到尝试上限：不触发批次回滚，节点恢复 pending，按延迟安排下一次重试
        if attempt_count < max_attempts:
            next_retry_at = (
                datetime.fromisoformat(now) + timedelta(seconds=delay)
            ).isoformat()
            conn.execute(
                """UPDATE nodes SET status = ?, reported_version = ?, error_code = ?,
                   error_message = ?, reported_at = ?, updated_at = ?,
                   next_retry_at = ?, retry_scheduled = 1 WHERE id = ?""",
                (PENDING, payload.version, payload.error_code or "FAILED",
                 payload.error_message, now, now, next_retry_at, node["id"]),
            )
            _record_event(
                conn, release_id, payload.node, "retry_scheduled", payload.version,
                {"attempt_count": attempt_count, "max_attempts": max_attempts,
                 "retry_delay_seconds": delay, "next_retry_at": next_retry_at},
            )
            # 批次状态保持不变（pending / running），仅刷新更新时间
            conn.execute(
                "UPDATE releases SET updated_at = ? WHERE id = ?", (now, release_id)
            )
            r["updated_at"] = now
            view = _build_view(conn, r)
            return {
                "result": "retry_scheduled",
                "failed_node": payload.node,
                "attempt_count": attempt_count,
                "max_attempts": max_attempts,
                "attempts_remaining": max_attempts - attempt_count,
                "retry_delay_seconds": delay,
                "next_retry_at": next_retry_at,
                "retry_scheduled": True,
                "rollback_triggered": False,
                "release": view,
            }

        # 达到上限：保持既有失败回滚 + checkpoint 语义（失败节点本身保持 failed）
        conn.execute(
            """UPDATE nodes SET status = ?, reported_version = ?, error_code = ?,
               error_message = ?, reported_at = ?, updated_at = ?,
               next_retry_at = NULL, retry_scheduled = 0 WHERE id = ?""",
            (FAILED, payload.version, payload.error_code or "FAILED",
             payload.error_message, now, now, node["id"]),
        )
        _record_event(
            conn, release_id, payload.node, "retry_exhausted", payload.version,
            {"attempt_count": attempt_count, "max_attempts": max_attempts},
        )

        # 逆拓扑顺序回滚：层级高的先回滚；同层按回报时间倒序（后成功的先回滚）
        success_rows = conn.execute(
            """SELECT name, level FROM nodes
               WHERE release_id = ? AND status = ?
               ORDER BY level DESC, reported_at DESC, name DESC""",
            (release_id, SUCCESS),
        ).fetchall()
        rolled_back: list[dict[str, Any]] = []
        for row in success_rows:
            conn.execute(
                "UPDATE nodes SET status = ?, updated_at = ? WHERE release_id = ? AND name = ?",
                (ROLLED_BACK, now, release_id, row["name"]),
            )
            _record_event(conn, release_id, row["name"], "rollback", payload.version,
                          {"level": row["level"], "triggered_by": payload.node})
            rolled_back.append({"node": row["name"], "level": row["level"]})

        new_status = R_ROLLED_BACK if rolled_back else R_FAILED
        conn.execute(
            """UPDATE releases SET status = ?, error_code = ?, error_message = ?,
               failed_node = ?, failed_at = ?, updated_at = ? WHERE id = ?""",
            (new_status, payload.error_code or "NODE_FAILED", payload.error_message,
             payload.node, now, now, release_id),
        )
        r.update(status=new_status, error_code=payload.error_code or "NODE_FAILED",
                 error_message=payload.error_message, failed_node=payload.node,
                 failed_at=now, updated_at=now)

        view = _build_view(conn, r)

        # 断点预览：调用 /retry 后，failed/rolled_back 节点会重置为 pending，
        # 执行从其中最低层级的节点重新开始（等价于重试后的 next_nodes）。
        reset_rows = conn.execute(
            "SELECT name, level FROM nodes WHERE release_id = ? AND status IN (?, ?)",
            (release_id, FAILED, ROLLED_BACK),
        ).fetchall()
        min_level = min((row["level"] for row in reset_rows), default=None)
        restart_nodes = sorted(row["name"] for row in reset_rows if row["level"] == min_level)

        return {
            "result": "failed_and_rolled_back",
            "failed_node": payload.node,
            "rollback_scope": "current_release_only",
            "rolled_back_nodes": rolled_back,
            "resumable": True,
            "checkpoint": {
                "retry_endpoint": f"/api/releases/{release_id}/retry",
                "method": "POST",
                "next_nodes": view["next_nodes"],
                "restart_nodes": restart_nodes,
                "restart_level": min_level,
                "instruction": "已成功节点均被回滚；调用重试接口会把 failed/rolled_back 节点重置为 pending，"
                               "从 restart_nodes（最低待执行层级）继续回报",
            },
            "release": view,
        }


# --------------------------------------------------------------------------- #
# 重试（从断点续发）
# --------------------------------------------------------------------------- #
def retry_release(release_id: str) -> dict[str, Any]:
    with transaction() as conn:
        r = dict(_get_release_row(conn, release_id))
        if r["status"] not in (R_FAILED, R_ROLLED_BACK):
            raise APIError(
                409, "retry_not_allowed",
                f"批次状态为 {r['status']}，仅 failed / partially_rolled_back 状态可重试",
            )

        reset_rows = conn.execute(
            "SELECT name FROM nodes WHERE release_id = ? AND status IN (?, ?)",
            (release_id, FAILED, ROLLED_BACK),
        ).fetchall()
        now = _now()
        conn.execute(
            """UPDATE nodes SET status = ?, reported_version = NULL, error_code = NULL,
               error_message = NULL, reported_at = NULL, updated_at = ?,
               attempt_count = 0, next_retry_at = NULL, retry_scheduled = 0
               WHERE release_id = ? AND status IN (?, ?)""",
            (PENDING, now, release_id, FAILED, ROLLED_BACK),
        )
        for row in reset_rows:
            _record_event(conn, release_id, row["name"], "retry_reset", r["version"], {})

        # 仍在退避等待中的 pending 节点一并解除调度（手动续发不再等待，计数清零）
        waiting_rows = conn.execute(
            "SELECT name FROM nodes WHERE release_id = ? AND status = ? AND retry_scheduled = 1",
            (release_id, PENDING),
        ).fetchall()
        conn.execute(
            """UPDATE nodes SET reported_version = NULL, error_code = NULL, error_message = NULL,
               reported_at = NULL, updated_at = ?, attempt_count = 0,
               next_retry_at = NULL, retry_scheduled = 0
               WHERE release_id = ? AND status = ? AND retry_scheduled = 1""",
            (now, release_id, PENDING),
        )
        for row in waiting_rows:
            _record_event(conn, release_id, row["name"], "retry_reset", r["version"],
                          {"was_waiting_retry": True})

        conn.execute(
            """UPDATE releases SET status = ?, error_code = NULL, error_message = NULL,
               failed_node = NULL, failed_at = NULL, updated_at = ? WHERE id = ?""",
            (R_RUNNING, now, release_id),
        )
        r.update(status=R_RUNNING, error_code=None, error_message=None,
                 failed_node=None, failed_at=None, updated_at=now)
        view = _build_view(conn, r)

        return {
            "result": "retried",
            "reset_nodes": [row["name"] for row in reset_rows],
            "cleared_waiting_nodes": [row["name"] for row in waiting_rows],
            "checkpoint": {
                "next_nodes": view["next_nodes"],
                "instruction": "从 next_nodes 中的节点开始重新按拓扑层级回报",
            },
            "release": view,
        }
