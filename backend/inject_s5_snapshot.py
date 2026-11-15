"""阶段07板块2 测试辅助: 构造 S5 取证失败降级场景快照.

用法:
    python inject_s5_snapshot.py <run_id>

S5 场景 (基于 S1 基线构造):
    - 快照 demands 只保留 P3/L 当期需求 (S5 聚焦「P3/L 接收确认取证失败」);
    - 快照 receipts 原样保留 (S1 历史回执一律不动, 数据只增不改);
    - 追加一条 P3/L 接收确认取证失败条目 (CF-S5-001):
        status='unavailable', received_quantity=null, error_message 记录原因
        —— 不使用 received:false, 取证失败 ≠ 确认未收到 (禁止 NOT_RECEIVED);
    - 同步写入 evidence_v2 异常证据 (evidence_type='receipt',
      status='unavailable', value_json=null) 作为辅助证据。

T1~T5 预期 (阶段07板块2):
    T1 unknown                    (P3/L 取证失败 → 依赖 unknown, 全 unknown → unknown)
    T2 established                (V1/V2 AVAILABLE)
    T3 established                (需求后存在确认/运输/签收活动)
    T4 pending_verification       (历史回执 satisfied + 取证失败 unknown 混合)
    T5 established (degraded)     (无冲突, 但标注降级置信结论)

要求 run 仍为 pending (completed 快照被触发器冻结, 注入会失败 409)。
"""

import json
import sys
from datetime import datetime

from db import close_conn, get_conn
from run_store import save_evidence

CONFIRM_ID = "CF-S5-001"
DISPATCH_ID = "DSP-S5-001"
FAIL_MESSAGE = ("接收确认取证失败: 现场无法提供有效签收凭证 (签收人/照片缺失), "
                "送达与否未知")


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _now_sqlite() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _default_p3l_demand() -> dict:
    """快照无 P3/L 需求观测时的默认条件 (S1 基线口径)。"""
    return {"service_id": "P3", "material_id": "L", "required_quantity": 8,
            "observed_at": "2026-11-15T01:56:22",
            "created_at": "2026-11-15 01:56:22"}


def inject_s5(run_id: str) -> tuple:
    """注入 S5 取证失败场景快照 + 异常证据, 返回 (ok, http_status, msg, data)。"""
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT run_id, run_status, input_snapshot_json "
            "FROM runs WHERE run_id=?", (run_id,),
        ).fetchone()
        if row is None:
            return False, 404, "run %s not found" % run_id, None
        if row["run_status"] == "completed":
            return False, 409, (
                "run %s is completed, snapshot immutable" % run_id), None
        try:
            snap = json.loads(row["input_snapshot_json"])
        except (TypeError, ValueError):
            return False, 500, (
                "run %s input_snapshot_json invalid" % run_id), None
        if not isinstance(snap, dict):
            return False, 500, (
                "run %s input_snapshot_json invalid" % run_id), None

        # ---- 快照改造 (只写本 run 自身 pending 快照, 不触碰其他 run) ----
        # 1) demands 只保留 P3/L (S5 聚焦取证失败场景; S1 基线口径兜底)
        p3l = [d for d in (snap.get("demands") or [])
               if d.get("service_id") == "P3" and d.get("material_id") == "L"]
        snap["demands"] = p3l or [_default_p3l_demand()]

        # 2) receipts 原样保留 (S1 历史回执不动) + 追加取证失败确认
        receipts = snap.get("receipts") or []
        observed_at = _now_iso()
        created_at = _now_sqlite()
        receipts.append({
            "receipt_id": CONFIRM_ID,
            "dispatch_id": DISPATCH_ID,
            "service_id": "P3",
            "material_id": "L",
            "received_quantity": None,      # 取证失败: 无有效数量 (不使用 received:false)
            "status": "unavailable",        # 证据状态: unavailable (降级语义)
            "observed_at": observed_at,
            "created_at": created_at,
            "error_message": FAIL_MESSAGE,
        })
        snap["receipts"] = receipts
        conn.execute(
            "UPDATE runs SET input_snapshot_json=? WHERE run_id=?",
            (json.dumps(snap, ensure_ascii=False), run_id),
        )
        conn.commit()

        # ---- 3) 异常证据写入 evidence_v2 辅助证据 (value_json=null) ----
        ok_ev, ev_id = save_evidence(
            "receipt", CONFIRM_ID, "unavailable", None, observed_at,
            FAIL_MESSAGE)
        if not ok_ev:
            return False, 500, (
                "S5 snapshot injected but evidence_v2 write failed: %s"
                % ev_id), None
    finally:
        close_conn(conn)
    return True, 200, "S5 degrade snapshot injected (P3/L confirmation failed)", {
        "run_id": run_id,
        "confirmation_id": CONFIRM_ID,
        "confirmation_status": "unavailable",
        "value_json": None,
        "evidence_id": ev_id,
        "observed_at": observed_at,
        "error_message": FAIL_MESSAGE,
    }


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: python inject_s5_snapshot.py <run_id>", file=sys.stderr)
        sys.exit(2)
    ok, http_status, msg, data = inject_s5(sys.argv[1])
    print(json.dumps({"ok": ok, "http_status": http_status, "msg": msg,
                      "data": data}, ensure_ascii=False, indent=2))
    sys.exit(0 if ok else 1)
