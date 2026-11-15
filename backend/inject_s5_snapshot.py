"""阶段07板块2/3 测试辅助: 构造 S5 取证失败降级 / 证据回填复测场景快照.

用法:
    python inject_s5_snapshot.py <run_id>          # 取证失败降级场景 (板块2)
    python inject_s5_snapshot.py <run_id> refill   # 证据回填复测场景 (板块3)

S5 场景 (基于 S1 基线构造):
    - 快照 demands 只保留 P3/L 当期需求 (S5 聚焦「P3/L 接收确认」);
    - 快照 receipts 原样保留 (S1 历史回执一律不动, 数据只增不改);

inject_s5() (板块2): 追加 P3/L 接收确认取证失败条目 (CF-S5-001):
    status='unavailable', received_quantity=null, error_message 记录原因
    —— 不使用 received:false, 取证失败 ≠ 确认未收到 (禁止 NOT_RECEIVED);
    同步写入 evidence_v2 异常证据 (status='unavailable', value_json=null)。

inject_s5_refill() (板块3): 证据回填复测 —— 不修改旧 run, 基于 S5 条件
    新建 reality run, P3/L 证据改为 status='ok' 的合法签收凭证
    (RCT-S5-002, 签收 8 件 ≥ 需求 8): T1 恢复 established;
    同步写入 evidence_v2 回填证据 (status='ok', value_json 为合法 JSON)。

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

REFILL_RECEIPT_ID = "RCT-S5-002"
REFILL_DISPATCH_ID = "DSP-S5-002"


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _now_sqlite() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _default_p3l_demand() -> dict:
    """快照无 P3/L 需求观测时的默认条件 (S1 基线口径)。"""
    return {"service_id": "P3", "material_id": "L", "required_quantity": 8,
            "observed_at": "2026-11-15T01:56:22",
            "created_at": "2026-11-15 01:56:22"}


def _load_pending_snapshot(run_id: str):
    """读 pending run 快照 dict; 失败返回 (None, http_status, msg)。"""
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT run_id, run_status, input_snapshot_json "
            "FROM runs WHERE run_id=?", (run_id,),
        ).fetchone()
        if row is None:
            return None, 404, "run %s not found" % run_id
        if row["run_status"] == "completed":
            return None, 409, (
                "run %s is completed, snapshot immutable" % run_id)
        try:
            snap = json.loads(row["input_snapshot_json"])
        except (TypeError, ValueError):
            return None, 500, "run %s input_snapshot_json invalid" % run_id
        if not isinstance(snap, dict):
            return None, 500, "run %s input_snapshot_json invalid" % run_id
    finally:
        close_conn(conn)
    return snap, 200, "ok"


def _save_snapshot(run_id: str, snap: dict) -> tuple:
    """写回 pending run 快照 (只写自身 run, 不触碰其他 run)。

    带 run_status='pending' 条件原子写: 若注入期间 run 已被 execute
    completed (快照冻结), rowcount=0 拒绝写入, 返回 (False, 409, msg)。
    """
    conn = get_conn()
    try:
        cur = conn.execute(
            "UPDATE runs SET input_snapshot_json=? "
            "WHERE run_id=? AND run_status='pending'",
            (json.dumps(snap, ensure_ascii=False), run_id),
        )
        conn.commit()
        if cur.rowcount == 0:
            return False, 409, (
                "run %s is no longer pending, snapshot immutable" % run_id)
    finally:
        close_conn(conn)
    return True, 200, "saved"


def inject_s5(run_id: str) -> tuple:
    """注入 S5 取证失败场景快照 + 异常证据, 返回 (ok, http_status, msg, data)。"""
    snap, st, msg = _load_pending_snapshot(run_id)
    if snap is None:
        return False, st, msg, None

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
    ok_sv, st_sv, msg_sv = _save_snapshot(run_id, snap)
    if not ok_sv:
        return False, st_sv, msg_sv, None

    # ---- 3) 异常证据写入 evidence_v2 辅助证据 (value_json=null) ----
    ok_ev, ev_id = save_evidence(
        "receipt", CONFIRM_ID, "unavailable", None, observed_at,
        FAIL_MESSAGE)
    if not ok_ev:
        return False, 500, (
            "S5 snapshot injected but evidence_v2 write failed: %s"
            % ev_id), None
    return True, 200, "S5 degrade snapshot injected (P3/L confirmation failed)", {
        "run_id": run_id,
        "confirmation_id": CONFIRM_ID,
        "confirmation_status": "unavailable",
        "value_json": None,
        "evidence_id": ev_id,
        "observed_at": observed_at,
        "error_message": FAIL_MESSAGE,
    }


def inject_s5_refill(run_id: str) -> tuple:
    """证据回填复测注入 (阶段07板块3): P3/L 证据回填为 status='ok' 的
    合法签收凭证 (RCT-S5-002, 签收 8 件 ≥ 需求 8) → T1 恢复 established。

    - 不修改任何旧 run: 只改写本 run 自身 pending 快照 + 新增 evidence_v2
      回填证据 (旧 S5 run 的字段与判定结果完全不动);
    - 快照 demands 仍只保留 P3/L (与 S5 同条件), receipts 保留 S1 历史
      回执 + 追加合法回填凭证 (无 unavailable 确认);
    - evidence_v2 写入 status='ok' 且 value_json 为合法 JSON (回填证据)。
    """
    snap, st, msg = _load_pending_snapshot(run_id)
    if snap is None:
        return False, st, msg, None

    # ---- 快照改造 (只写本 run 自身 pending 快照) ----
    # 1) demands 只保留 P3/L (与 S5 同条件)
    p3l = [d for d in (snap.get("demands") or [])
           if d.get("service_id") == "P3" and d.get("material_id") == "L"]
    snap["demands"] = p3l or [_default_p3l_demand()]

    # 2) receipts 原样保留 (S1 历史回执不动) + 追加合法回填凭证
    receipts = snap.get("receipts") or []
    observed_at = _now_iso()
    created_at = _now_sqlite()
    receipts.append({
        "receipt_id": REFILL_RECEIPT_ID,
        "dispatch_id": REFILL_DISPATCH_ID,
        "service_id": "P3",
        "material_id": "L",
        "received_quantity": 8,          # 回填合法凭证: 签收 8 件 ≥ 需求 8
        "status": "ok",                  # 证据状态: ok (取证成功)
        "observed_at": observed_at,
        "created_at": created_at,
    })
    snap["receipts"] = receipts
    ok_sv, st_sv, msg_sv = _save_snapshot(run_id, snap)
    if not ok_sv:
        return False, st_sv, msg_sv, None

    # ---- 3) 回填证据写入 evidence_v2 (status='ok', value_json 合法 JSON) ----
    value_json = json.dumps({
        "receipt_id": REFILL_RECEIPT_ID,
        "service_id": "P3", "material_id": "L",
        "received_quantity": 8, "refill": True,
    }, ensure_ascii=False)
    ok_ev, ev_id = save_evidence(
        "receipt", REFILL_RECEIPT_ID, "ok", value_json, observed_at, "")
    if not ok_ev:
        return False, 500, (
            "S5 refill snapshot injected but evidence_v2 write failed: %s"
            % ev_id), None
    return True, 200, "S5 refill snapshot injected (P3/L receipt RCT-S5-002 ok)", {
        "run_id": run_id,
        "receipt_id": REFILL_RECEIPT_ID,
        "receipt_status": "ok",
        "received_quantity": 8,
        "evidence_id": ev_id,
        "value_json": value_json,
        "observed_at": observed_at,
    }


if __name__ == "__main__":
    if len(sys.argv) not in (2, 3):
        print("usage: python inject_s5_snapshot.py <run_id> [refill]",
              file=sys.stderr)
        sys.exit(2)
    if len(sys.argv) == 3 and sys.argv[2] == "refill":
        ok, http_status, msg, data = inject_s5_refill(sys.argv[1])
    else:
        ok, http_status, msg, data = inject_s5(sys.argv[1])
    print(json.dumps({"ok": ok, "http_status": http_status, "msg": msg,
                      "data": data}, ensure_ascii=False, indent=2))
    sys.exit(0 if ok else 1)
