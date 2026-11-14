"""阶段04板块1: 缺口识别 gap_check — 只读 run 快照, 不读实时观测表.

业务规则 (严格按阶段04板块1规格):
    1. 硬性规则: 一个 (service_id, material_id) 组合必须同时具备
       「有效需求证据」与「有效接收证据」才生成 blocker;
       任意一个缺失 → 不生成 blocker (签收取证失败 ≠ 未送达, 不武断算缺口)。
    2. 有效需求: 该组合最新需求观测 (快照 demands 本身即每组合最新一条)
       的 required_quantity 为有效非负整数。
       有效接收: 该组合全部签收记录中 received_quantity 有效的行求和
       → valid_received (当前有效接收总量, 累计口径)。
    3. gap = required_quantity - valid_received;
       仅 gap > 0 (真实短缺) 生成 blocker, gap <= 0 不构成缺口。
    4. blocker 描述格式:
       "{service} / {material}：需求{required}，当前有效接收{received}，明确缺口{gap}"
       (存 reason_json.summary)。
    5. 同一 run 同一组合只保留一条 blocker:
       表 UNIQUE(run_id, service_id, material_id) + 写库 UPSERT,
       重复执行幂等刷新 (快照自创建起冻结, 重复计算结果恒一致, 无需清理旧行)。
    6. 数据来源: 只读 runs.input_snapshot_json (创建 run 时已冻结),
       不读 demand_records / receipt_records_v2 等实时观测表。

与 T1 校验的关系: 本模块完全独立于 t1_check (未改动 T1 任何代码)。
T1 判断「签收量 ≥ 需求」, 本模块在双证据齐全时量化缺口, 供后续板块
生成调配计划 (plan_definitions) 使用。

CLI 用法: python gap_check.py <run_id>
"""

import json
import sqlite3
from datetime import datetime

from db import close_conn, get_conn

BLOCKER_SUMMARY_TEMPLATE = "%s / %s：需求%d，当前有效接收%d，明确缺口%d"

# UPSERT: 同一 run 同一 (service_id, material_id) 只保留一条, 重复执行刷新数值
_UPSERT_BLOCKER_SQL = """
INSERT INTO blockers (run_id, service_id, material_id, required_quantity,
                      valid_received, gap, reason_json)
VALUES (:run_id, :service_id, :material_id, :required_quantity,
        :valid_received, :gap, :reason_json)
ON CONFLICT(run_id, service_id, material_id)
DO UPDATE SET required_quantity = excluded.required_quantity,
              valid_received = excluded.valid_received,
              gap = excluded.gap,
              reason_json = excluded.reason_json
"""


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _is_valid_quantity(value) -> bool:
    """有效数量: 非 bool 的 int 且 >= 0 (与 t1_check 同口径)。"""
    return (isinstance(value, int) and not isinstance(value, bool)
            and value >= 0)


def _latest_demand_for(demands, service_id, material_id):
    """该组合的最新一条需求。快照 demands 本身即每组合最新一条,
    此处按 observed_at 兜底取最后匹配项; 无匹配返回 None。"""
    latest = None
    for d in demands:
        if (d.get("service_id") == service_id
                and d.get("material_id") == material_id):
            if (latest is None or (d.get("observed_at") or "")
                    >= (latest.get("observed_at") or "")):
                latest = d
    return latest


def _valid_receipts_for(receipts, service_id, material_id):
    """该组合的全部有效签收记录 (received_quantity 为有效数量)。"""
    return [
        r for r in receipts
        if r.get("service_id") == service_id
        and r.get("material_id") == material_id
        and _is_valid_quantity(r.get("received_quantity"))
    ]


def _compute_blocker(service_id, material_id, demand, valid_receipts,
                     computed_at):
    """按硬性规则计算单组合 blocker; 不满足生成条件返回 None。"""
    required = demand.get("required_quantity")
    # 需求证据可靠性: 组合键与需求量必须有效
    if not service_id or not material_id or not _is_valid_quantity(required):
        return None
    # 接收证据可靠性: 至少一条有效签收 (硬性规则)
    if not valid_receipts:
        return None
    valid_received = sum(r["received_quantity"] for r in valid_receipts)
    gap = required - valid_received
    if gap <= 0:
        return None  # 签收已满足, 无缺口
    return {
        "service_id": service_id,
        "material_id": material_id,
        "required_quantity": required,
        "valid_received": valid_received,
        "gap": gap,
        "reason_json": {
            "summary": BLOCKER_SUMMARY_TEMPLATE % (
                service_id, material_id, required, valid_received, gap),
            "demand_observed_at": demand.get("observed_at"),
            "receipt_count": len(valid_receipts),
            "receipt_ids": [r.get("receipt_id") for r in valid_receipts],
            "latest_receipt_observed_at": max(
                (r.get("observed_at") or "") for r in valid_receipts),
            "computed_at": computed_at,
        },
    }


def check_gaps(snapshot: dict) -> list:
    """缺口识别纯计算 (只读快照 dict, 不访问数据库)。

    遍历快照 demands ∪ receipts 中出现的每个 (service_id, material_id)
    组合, 按硬性规则 (双证据齐全 + gap > 0) 生成 blocker 列表,
    按 (service_id, material_id) 排序。
    """
    demands = snapshot.get("demands") or []
    receipts = snapshot.get("receipts") or []
    pairs = sorted({(d.get("service_id"), d.get("material_id"))
                    for d in demands
                    if d.get("service_id") and d.get("material_id")}
                   | {(r.get("service_id"), r.get("material_id"))
                      for r in receipts
                      if r.get("service_id") and r.get("material_id")})
    computed_at = _now()
    blockers = []
    for service_id, material_id in pairs:
        demand = _latest_demand_for(demands, service_id, material_id)
        if demand is None:
            continue  # 无需求证据 → 不生成 blocker (硬性规则)
        valid_receipts = _valid_receipts_for(receipts, service_id, material_id)
        blocker = _compute_blocker(service_id, material_id, demand,
                                   valid_receipts, computed_at)
        if blocker is not None:
            blockers.append(blocker)
    return blockers


def _blocker_to_dict(row) -> dict:
    d = dict(row)
    if d.get("reason_json"):
        try:
            d["reason_json"] = json.loads(d["reason_json"])
        except json.JSONDecodeError:
            pass  # 防御脏数据: 保留原文
    return d


def run_gap_check(run_id: str) -> tuple:
    """对指定 run 执行缺口识别并落库 blockers (同一事务, 幂等)。

    - 只读 runs.input_snapshot_json, 不读任何实时观测表
    - run 不存在 → (False, 404); 快照缺失/损坏 → (False, 500)
    - 结果 UPSERT: 同一 run 同一 (service_id, material_id) 只保留一条
      (表 UNIQUE 约束兜底); run 状态不限 (pending/completed 均可,
      快照自创建起已冻结, 结果恒一致)
    返回 (ok, http_status, msg, data); data =
    {run_id, run_status, blocker_count, blockers: [...]}。
    """
    try:
        conn = get_conn()
        try:
            row = conn.execute(
                "SELECT * FROM runs WHERE run_id=?", (run_id,)
            ).fetchone()
            if row is None:
                return False, 404, "run %s not found" % run_id, None
            try:
                snapshot = json.loads(row["input_snapshot_json"])
            except (TypeError, ValueError):
                return False, 500, (
                    "run %s input_snapshot_json invalid" % run_id), None
            if not isinstance(snapshot, dict):
                return False, 500, (
                    "run %s input_snapshot_json invalid" % run_id), None

            blockers = check_gaps(snapshot)
            for b in blockers:
                conn.execute(_UPSERT_BLOCKER_SQL, {
                    "run_id": run_id,
                    "service_id": b["service_id"],
                    "material_id": b["material_id"],
                    "required_quantity": b["required_quantity"],
                    "valid_received": b["valid_received"],
                    "gap": b["gap"],
                    "reason_json": json.dumps(b["reason_json"],
                                              ensure_ascii=False),
                })
            conn.commit()

            rows = conn.execute(
                "SELECT * FROM blockers WHERE run_id=? ORDER BY blocker_id",
                (run_id,),
            ).fetchall()
        finally:
            close_conn(conn)
    except sqlite3.Error as exc:
        return False, 500, "db error: %s" % exc, None
    return True, 200, "gap check done", {
        "run_id": run_id,
        "run_status": row["run_status"],
        "blocker_count": len(rows),
        "blockers": [_blocker_to_dict(r) for r in rows],
    }


def get_blockers(run_id: str) -> tuple:
    """查询某 run 的全部 blocker 记录 (只读)。

    返回 (ok, http_status, msg, data); run 不存在 → 404。
    """
    try:
        conn = get_conn()
        try:
            row = conn.execute(
                "SELECT * FROM runs WHERE run_id=?", (run_id,)
            ).fetchone()
            if row is None:
                return False, 404, "run %s not found" % run_id, None
            rows = conn.execute(
                "SELECT * FROM blockers WHERE run_id=? ORDER BY blocker_id",
                (run_id,),
            ).fetchall()
        finally:
            close_conn(conn)
    except sqlite3.Error as exc:
        return False, 500, "db error: %s" % exc, None
    return True, 200, "ok", {
        "run_id": run_id,
        "run_status": row["run_status"],
        "blocker_count": len(rows),
        "blockers": [_blocker_to_dict(r) for r in rows],
    }


if __name__ == "__main__":
    import sys

    if len(sys.argv) != 2:
        print("usage: python gap_check.py <run_id>", file=sys.stderr)
        sys.exit(2)
    ok, http_status, msg, data = run_gap_check(sys.argv[1])
    print(json.dumps({"ok": ok, "http_status": http_status, "msg": msg,
                      "data": data}, ensure_ascii=False, indent=2))
    sys.exit(0 if ok else 1)
