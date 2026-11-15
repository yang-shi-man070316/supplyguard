"""阶段08板块1: 场景冻结加载器 load_scenario.py.

职责:
    - 加载 scenarios/s1.json ~ s5.json 冻结基线场景 (禁止直接修改场景文件)
    - 现场变量调整统一走 input_overrides (reality) / assumptions (what-if)
    - 把冻结基线种子写入观测表 (source_type='scenario_<id>', 幂等),
      使 /api/state、/api/history/* 与新建 run 快照反映该场景
    - 重放 (replay) 全链路: 种基线 → 建 reality run → 快照按冻结基线
      校准 (仅 pending 期, 与 inject_s3/inject_s5 同口径) → T1~T5 执行
      → 缺口识别 → 候选方案 → 方案判定
    - what-if 推演: 复用 run_whatif_pipeline (父基线零写入), 支持
      S4 标准假设 (whatif-A/B/C) 或自定义 assumptions JSON
    - 历史一致性自检 (--check-history): 冻结基线 vs 历史 run 快照比对

约束 (阶段08板块1):
    - 不新增任何 T1~T5 判定逻辑, 不修改后端核心判定模块;
    - 本脚本只调用既有模块 (run_store_v2 / task_execute / gap_check /
      plan_store / plan_judge / whatif_run) 组合编排;
    - 对 pending run 快照的校准写入与 inject_s3_snapshot.py /
      inject_s5_snapshot.py 的既有实践一致 (completed 后触发器拒改)。

用法 (在 scenarios/ 或任意目录):
    python load_scenario.py S1                          # 种基线 + 打印摘要
    python load_scenario.py S1 --overrides '{"stocks":[{"reserve_id":"R1",
        "material_id":"W","quantity":5}]}'               # reality 现场变量调整
    python load_scenario.py S1 --full                   # 完整重放 (run+判定全链路)
    python load_scenario.py S5 --variant refill --full  # 证据回填复测变体
    python load_scenario.py S4 --whatif A [--parent BB03-RUN-011]
    python load_scenario.py S4 --whatif '{"remaining_minutes":50}'
    python load_scenario.py S1 --check-history          # 基线 vs 历史 run 一致性
    python load_scenario.py --check-all                 # S1~S5 全部一致性自检

overrides 语法 (reality input_overrides):
    可选 key: stocks / demands / transports / receipts / routes /
    remaining_time。列表条目按自然键 upsert 覆盖整条
    (stocks: reserve_id+material_id; demands: service_id+material_id;
    transports: transport_id; receipts: receipt_id; routes: route_id),
    自然键不存在则追加; remaining_time 为标量整体替换。
"""

import argparse
import copy
import json
import sys
from pathlib import Path

SCENARIO_DIR = Path(__file__).resolve().parent
BACKEND_DIR = SCENARIO_DIR.parent / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import db  # noqa: E402  import 即 init_db (幂等建表, 与 main.py 一致)
from constants import RUN_TYPE  # noqa: E402
from run_store_v2 import apply_assumptions, create_run  # noqa: E402
from task_execute import run_execute_all  # noqa: E402
from gap_check import run_gap_check  # noqa: E402
from plan_store import save_candidate_plans  # noqa: E402
from plan_judge import run_plan_judge  # noqa: E402
from whatif_run import (  # noqa: E402
    capture_fingerprint,
    run_whatif_pipeline,
    verify_fingerprint,
)

SCENARIO_IDS = ("S1", "S2", "S3", "S4", "S5")

# 快照五段 + 可选 routes/remaining_time
_SECTIONS = ("stocks", "demands", "transports", "receipts")
_OPTIONAL_SECTIONS = ("routes", "remaining_time")

# 各段自然键 (upsert 覆盖依据)
_NATURAL_KEYS = {
    "stocks": ("reserve_id", "material_id"),
    "demands": ("service_id", "material_id"),
    "transports": ("transport_id",),
    "receipts": ("receipt_id",),
    "routes": ("route_id",),
}


def _fmt(data) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


def load_scenario(scenario_id: str) -> dict:
    """加载指定场景冻结文件, 校验基本结构; 失败抛 ValueError。"""
    scenario_id = str(scenario_id).strip().upper()
    path = SCENARIO_DIR / ("%s.json" % scenario_id.lower())
    if not path.exists():
        raise ValueError("scenario %s not found (%s)" % (scenario_id, path))
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if data.get("scenario_id") != scenario_id:
        raise ValueError("scenario file %s declares scenario_id=%r"
                         % (path.name, data.get("scenario_id")))
    baseline = data.get("baseline")
    if not isinstance(baseline, dict):
        raise ValueError("scenario %s missing baseline object" % scenario_id)
    for sec in _SECTIONS:
        if not isinstance(baseline.get(sec), list):
            raise ValueError("scenario %s baseline.%s must be a list"
                             % (scenario_id, sec))
    return data


def apply_input_overrides(baseline: dict, overrides: dict) -> dict:
    """reality 现场变量调整: 把 input_overrides 叠加到基线副本上 (纯函数)。

    入参 baseline 不被修改; 未知 key → ValueError (不静默忽略)。
    """
    if overrides is None:
        return copy.deepcopy(baseline)
    if not isinstance(overrides, dict):
        raise ValueError("input_overrides must be an object")
    out = copy.deepcopy(baseline)
    for key, value in overrides.items():
        if key == "remaining_time":
            if isinstance(value, bool) or not isinstance(value, int) \
                    or value < 0:
                raise ValueError(
                    "override remaining_time must be a non-negative integer")
            out[key] = value
            continue
        if key not in _NATURAL_KEYS:
            raise ValueError(
                "unsupported override key %r (expected %s / remaining_time)"
                % (key, "/".join(sorted(_NATURAL_KEYS))))
        if not isinstance(value, list):
            raise ValueError("override %s must be a list" % key)
        keys = _NATURAL_KEYS[key]
        section = out.setdefault(key, [])
        for entry in value:
            if not isinstance(entry, dict):
                raise ValueError("override %s entries must be objects" % key)
            idx = next((i for i, item in enumerate(section)
                        if all(item.get(k) == entry.get(k) for k in keys)),
                       None)
            if idx is None:
                section.append(copy.deepcopy(entry))
            else:
                section[idx] = copy.deepcopy(entry)
    return out


def resolve_variant(scenario: dict, variant: str = None) -> dict:
    """返回 (baseline, variant_meta); S5 的 refill 变体替换 receipts。"""
    if not variant:
        return copy.deepcopy(scenario.get("baseline") or {}), None
    variant = str(variant).strip()
    variants = scenario.get("variants") or {}
    meta = variants.get(variant)
    if meta is None:
        raise ValueError(
            "scenario %s has no variant %r (available: %s)"
            % (scenario.get("scenario_id"), variant,
               ", ".join(sorted(variants)) or "none"))
    baseline = copy.deepcopy(scenario.get("baseline") or {})
    detail = meta.get("receipts_detail")
    if detail is not None:
        baseline["receipts"] = copy.deepcopy(detail)
    return baseline, meta


def derive_services_materials(baseline: dict):
    """从基线条目派生 services / materials (与快照口径一致的排序列表)。"""
    services = sorted({d.get("service_id") for d in baseline.get("demands") or []
                       if d.get("service_id")}
                      | {r.get("service_id") for r in baseline.get("receipts") or []
                         if r.get("service_id")})
    materials = sorted({d.get("material_id") for d in baseline.get("demands") or []
                        if d.get("material_id")}
                       | {r.get("material_id") for r in baseline.get("receipts") or []
                          if r.get("material_id")}
                       | {s.get("material_id") for s in baseline.get("stocks") or []
                          if s.get("material_id")})
    return services, materials


def seed_observations(scenario_id: str, baseline: dict) -> dict:
    """把冻结基线写入观测表 (幂等, source_type='scenario_<id>' 行先清后种)。

    - demands / stocks / transports / receipts(可入库部分) 均以冻结
      observed_at 落库, 最新观测口径自然指向本场景;
    - 签收条目带 status (unavailable/ok) 或 received_quantity=null 的
      取证确认无法入 receipt_records_v2 (表无 status 列且数量非空),
      跳过表写入 (它们经 run 快照校准进入判定, 与 inject_s5 同口径);
    - receipt_id 已存在且业务值一致 → 视为已种跳过; 不一致 → 中止报错
      (禁止覆盖非本场景来源的历史签收)。
    返回 {seeded: {...}, skipped: {...}} 摘要。
    """
    source = "scenario_%s" % scenario_id.lower()
    conn = db.get_conn()
    seeded, skipped = {"demands": 0, "stocks": 0, "transports": 0,
                       "receipts": 0}, {"receipts": []}
    try:
        conn.execute("DELETE FROM demand_records WHERE source_type=?", (source,))
        conn.execute("DELETE FROM stock_observations WHERE source_type=?", (source,))
        conn.execute("DELETE FROM transport_observations WHERE source_type=?", (source,))
        conn.execute("DELETE FROM receipt_records_v2 WHERE source_type=?", (source,))

        for d in baseline.get("demands") or []:
            conn.execute(
                "INSERT INTO demand_records (service_id, material_id, "
                "required_quantity, observed_at, source_type) "
                "VALUES (?,?,?,?,?)",
                (d["service_id"], d["material_id"], d["required_quantity"],
                 d["observed_at"], source))
            seeded["demands"] += 1
        for s in baseline.get("stocks") or []:
            conn.execute(
                "INSERT INTO stock_observations (reserve_id, material_id, "
                "quantity, observed_at, source_type) VALUES (?,?,?,?,?)",
                (s["reserve_id"], s["material_id"], s["quantity"],
                 s["observed_at"], source))
            seeded["stocks"] += 1
        for t in baseline.get("transports") or []:
            conn.execute(
                "INSERT INTO transport_observations (transport_id, "
                "availability, capacity, observed_at, source_type) "
                "VALUES (?,?,?,?,?)",
                (t["transport_id"], t["availability"], t["capacity"],
                 t["observed_at"], source))
            seeded["transports"] += 1
        for r in baseline.get("receipts") or []:
            qty = r.get("received_quantity")
            if qty is None or r.get("status") == "unavailable":
                skipped["receipts"].append(r.get("receipt_id"))
                continue  # 取证确认/异常回执不入观测表 (快照校准注入)
            dup = conn.execute(
                "SELECT service_id, material_id, received_quantity, "
                "observed_at FROM receipt_records_v2 WHERE receipt_id=?",
                (r["receipt_id"],)).fetchone()
            if dup is not None:
                if (dup["service_id"] == r.get("service_id")
                        and dup["material_id"] == r.get("material_id")
                        and dup["received_quantity"] == qty):
                    skipped["receipts"].append(r.get("receipt_id"))
                    continue  # 历史同值签收已存在, 视为已种
                raise ValueError(
                    "receipt %s already exists with different values "
                    "(%s/%s/%s), refusing to overwrite non-scenario history"
                    % (r["receipt_id"], dup["service_id"], dup["material_id"],
                       dup["received_quantity"]))
            conn.execute(
                "INSERT INTO receipt_records_v2 (receipt_id, dispatch_id, "
                "service_id, material_id, received_quantity, observed_at, "
                "source_type) VALUES (?,?,?,?,?,?,?)",
                (r["receipt_id"], r.get("dispatch_id"), r["service_id"],
                 r["material_id"], qty, r["observed_at"], source))
            seeded["receipts"] += 1
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        db.close_conn(conn)
    return {"source_type": source, "seeded": seeded, "skipped": skipped}


def _replace_run_snapshot(run_id: str, baseline: dict) -> tuple:
    """校准 pending run 快照为冻结基线 (与 inject_s3/inject_s5 同口径)。

    只替换五段业务数据 + 可选 routes/remaining_time; services/materials/
    snapshot_at 保留 _capture_snapshot 的既有口径。run 非 pending → 409。
    """
    conn = db.get_conn()
    try:
        row = conn.execute(
            "SELECT input_snapshot_json FROM runs WHERE run_id=?",
            (run_id,)).fetchone()
        if row is None:
            return False, 404, "run %s not found" % run_id
        try:
            snap = json.loads(row["input_snapshot_json"])
        except (TypeError, ValueError):
            return False, 500, "run %s input_snapshot_json invalid" % run_id
        for key in list(_SECTIONS) + list(_OPTIONAL_SECTIONS):
            snap.pop(key, None)
        for sec in _SECTIONS:
            snap[sec] = copy.deepcopy(baseline.get(sec) or [])
        for sec in _OPTIONAL_SECTIONS:
            if sec in baseline:
                snap[sec] = copy.deepcopy(baseline[sec])
        cur = conn.execute(
            "UPDATE runs SET input_snapshot_json=? "
            "WHERE run_id=? AND run_status='pending'",
            (json.dumps(snap, ensure_ascii=False), run_id))
        conn.commit()
        if cur.rowcount == 0:
            return False, 409, (
                "run %s is no longer pending, snapshot immutable" % run_id)
    finally:
        db.close_conn(conn)
    return True, 200, "snapshot aligned to frozen baseline"


def create_baseline_run(scenario_id: str, overrides: dict = None,
                        variant: str = None) -> tuple:
    """种基线 + 建 reality run + 快照校准为冻结基线 (run 仍 pending)。

    返回 (ok, http_status, msg, data); data = {run, scenario_id}。
    """
    scenario_id = str(scenario_id).strip().upper()
    scenario = load_scenario(scenario_id)
    baseline, vmeta = resolve_variant(scenario, variant)
    baseline = apply_input_overrides(baseline, overrides)
    seed = seed_observations(scenario_id, baseline)
    ok, st, msg, run = create_run(scenario_id, None, RUN_TYPE)
    if not ok:
        return False, st, msg, None
    ok2, st2, msg2 = _replace_run_snapshot(run["run_id"], baseline)
    if not ok2:
        return False, st2, msg2, {"run_id": run["run_id"], "seed": seed}
    return True, 201, "baseline run created (pending)", {
        "scenario_id": scenario_id,
        "variant": (vmeta or {}).get("label"),
        "run_id": run["run_id"],
        "seed": seed,
        "baseline_summary": {
            "demands": len(baseline.get("demands") or []),
            "stocks": len(baseline.get("stocks") or []),
            "transports": len(baseline.get("transports") or []),
            "receipts": len(baseline.get("receipts") or []),
            "routes": len(baseline.get("routes") or []),
            "remaining_time": baseline.get("remaining_time"),
        },
    }


def replay_scenario(scenario_id: str, overrides: dict = None,
                    variant: str = None) -> tuple:
    """完整重放: 种基线 → 建 run → T1~T5 → 缺口 → 方案 → 方案判定。

    返回 (ok, http_status, msg, data); 每步失败时 data 携带已完成的步骤。
    """
    scenario_id = str(scenario_id).strip().upper()
    ok_c, st_c, msg_c, created = create_baseline_run(scenario_id, overrides,
                                                     variant)
    if not ok_c:
        return False, st_c, msg_c, None
    run_id = created["run_id"]
    steps = {"created": created}

    ok_e, st_e, msg_e, exec_data = run_execute_all(run_id)
    if not ok_e:
        return False, st_e, msg_e, {"steps": steps, "execute_error": msg_e}
    steps["execute"] = {"run_id": run_id,
                        "tasks": [(t["task_id"], t["result_status"])
                                  for t in exec_data["tasks"]]}
    ok_g, st_g, msg_g, gaps = run_gap_check(run_id)
    if not ok_g:
        return False, st_g, msg_g, {"steps": steps, "gaps_error": msg_g}
    steps["gaps"] = {"blocker_count": gaps["blocker_count"],
                     "blockers": [(b["service_id"], b["material_id"], b["gap"])
                                  for b in gaps["blockers"]]}
    ok_p, st_p, msg_p, plans = save_candidate_plans(run_id)
    if not ok_p:
        return False, st_p, msg_p, {"steps": steps, "plans_error": msg_p}
    steps["plans"] = {"plan_count": plans["plan_count"],
                      "transfer_qty": plans["transfer_qty"]}
    ok_j, st_j, msg_j, judge = run_plan_judge(run_id)
    if not ok_j:
        return False, st_j, msg_j, {"steps": steps, "judge_error": msg_j}
    steps["plan_results"] = {
        r.get("plan_name"): r.get("status")
        for r in (judge.get("results") or [])}
    return True, 200, "scenario %s replay done (run %s)" % (scenario_id,
                                                            run_id), {
        "scenario_id": scenario_id,
        "run_id": run_id,
        "steps": steps,
    }


def run_scenario_whatif(scenario_id: str, assumptions: dict = None,
                        assumption_key: str = None,
                        parent_run_id: str = None) -> tuple:
    """what-if 推演: 复用 run_whatif_pipeline + 基线指纹无污染验证。

    - S4 可用 --whatif A/B/C 取 standard_assumptions 标准假设, 或直接
      传 assumptions JSON;
    - parent_run_id 缺省取场景文件 baseline_run_refs.reality 指向的
      历史基线 (不存在则报错要求显式 --parent)。
    """
    scenario_id = str(scenario_id).strip().upper()
    scenario = load_scenario(scenario_id)
    if assumption_key:
        std = scenario.get("standard_assumptions") or {}
        if assumption_key not in std:
            raise ValueError(
                "scenario %s has no standard assumption %r (available: %s)"
                % (scenario_id, assumption_key, ", ".join(sorted(std))))
        assumptions = std[assumption_key]["assumptions"]
    if not assumptions:
        raise ValueError("what-if requires --whatif <key|json> assumptions")
    if parent_run_id is None:
        refs = scenario.get("baseline_run_refs") or {}
        parent_run_id = refs.get("reality")
        if not parent_run_id:
            raise ValueError(
                "scenario %s has no baseline_run_refs.reality, "
                "please pass --parent BB03-RUN-xxx" % scenario_id)
    before = capture_fingerprint(parent_run_id)
    ok, st, msg, data = run_whatif_pipeline(parent_run_id, assumptions,
                                            scenario_id)
    after = capture_fingerprint(parent_run_id)
    intact, changed = verify_fingerprint(before, after)
    return ok, st, msg, {
        "scenario_id": scenario_id,
        "parent_run_id": parent_run_id,
        "assumptions": assumptions,
        "pipeline": data,
        "baseline_intact": intact,
        "changed_tables": changed,
    }


# ============================================================
# 历史一致性自检: 冻结基线 vs 历史 run 快照
# ============================================================

def _natural_key(sec, entry):
    return tuple(entry.get(k) for k in _NATURAL_KEYS[sec])


def _entries_equal(frozen, run_entry):
    """run 快照条目必须包含冻结条目的全部字段且值一致 (run 可多字段)。"""
    for k, v in frozen.items():
        if run_entry.get(k) != v:
            return False, "field %s: frozen=%r run=%r" % (k, v, run_entry.get(k))
    return True, ""


def compare_baseline_to_snapshot(baseline: dict, snapshot: dict) -> list:
    """冻结基线 ⊆ run 快照 的逐条比对, 返回差异列表 (空 = 一致)。"""
    if not isinstance(snapshot, dict):
        return ["run snapshot missing or invalid"]
    diffs = []
    for sec in _SECTIONS:
        run_entries = snapshot.get(sec) or []
        index = {}
        for e in run_entries:
            index.setdefault(_natural_key(sec, e), []).append(e)
        for frozen in baseline.get(sec) or []:
            matches = index.get(_natural_key(sec, frozen))
            if not matches:
                diffs.append("%s %s missing in run snapshot"
                             % (sec, dict(zip(_NATURAL_KEYS[sec],
                                              _natural_key(sec, frozen)))))
                continue
            ok, detail = _entries_equal(frozen, matches[-1])
            if not ok:
                diffs.append("%s %s mismatch: %s"
                             % (sec, _natural_key(sec, frozen), detail))
    for sec in _OPTIONAL_SECTIONS:
        if sec in baseline and snapshot.get(sec) != baseline[sec]:
            diffs.append("%s: frozen=%r run=%r"
                         % (sec, baseline[sec], snapshot.get(sec)))
    return diffs


def _load_run_snapshot(run_id: str):
    conn = db.get_conn()
    try:
        row = conn.execute(
            "SELECT input_snapshot_json FROM runs WHERE run_id=?",
            (run_id,)).fetchone()
    finally:
        db.close_conn(conn)
    if row is None:
        return None
    try:
        return json.loads(row["input_snapshot_json"])
    except (TypeError, ValueError):
        return None


def check_history(scenario_id: str) -> tuple:
    """冻结基线 vs 历史 run 记录一致性自检 (只读, 不写库)。

    - reality 基线: baseline ⊆ 历史 reality run 快照 逐条一致;
    - S4 what-if: 每组标准假设 = apply_assumptions(基线, 假设) 与
      历史 what-if run 快照逐条一致;
    - S5 refill 变体: 回填 receipts 与历史 refill run 一致。
    返回 (ok, report_dict)。
    """
    scenario_id = str(scenario_id).strip().upper()
    scenario = load_scenario(scenario_id)
    baseline = scenario.get("baseline") or {}
    refs = scenario.get("baseline_run_refs") or {}
    report = {"scenario_id": scenario_id, "checks": [], "ok": True}

    def _record(name, ref_run, diffs):
        ok = ref_run is not None and not diffs
        report["checks"].append({
            "name": name,
            "baseline_run_ref": ref_run,
            "consistent": ok,
            "diffs": list(diffs),
        })
        report["ok"] = report["ok"] and ok

    baseline_ref = refs.get("reality") or refs.get("degrade")
    if baseline_ref is not None:
        _record("baseline", baseline_ref,
                compare_baseline_to_snapshot(baseline,
                                             _load_run_snapshot(baseline_ref)))
    for key, meta in (scenario.get("standard_assumptions") or {}).items():
        ref = ((refs.get("whatif") or {}).get(key))
        if ref is None:
            _record("whatif %s" % key, None, [])
            continue
        expected = apply_assumptions(baseline, meta["assumptions"])
        _record("whatif %s" % key, ref,
                compare_baseline_to_snapshot(expected, _load_run_snapshot(ref)))
    for key, meta in (scenario.get("variants") or {}).items():
        ref = refs.get(key)
        if ref is None:
            continue
        vbaseline, _ = resolve_variant(scenario, key)
        if not meta.get("receipts_detail"):
            continue  # 变体即基线本身, 基线检查已覆盖
        _record("variant %s" % key, ref,
                compare_baseline_to_snapshot(vbaseline, _load_run_snapshot(ref)))
    return report["ok"], report


def check_all() -> tuple:
    reports = []
    for sid in SCENARIO_IDS:
        ok, report = check_history(sid)
        reports.append(report)
    return all(r["ok"] for r in reports), {"scenarios": reports}


# ============================================================
# CLI
# ============================================================

def _parse_overrides(raw: str) -> dict:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("--overrides must be valid JSON: %s" % exc)
    return data


def _parse_assumptions(raw: str):
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None  # 视为标准假设名 (whatif-A/B/C)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="SupplyGuard 阶段08板块1 场景冻结加载器")
    parser.add_argument("scenario_id", nargs="?",
                        help="场景 id (S1~S5); --check-all 时省略")
    parser.add_argument("--overrides", metavar="JSON",
                        help="reality 现场变量调整 (input_overrides)")
    parser.add_argument("--variant", metavar="NAME",
                        help="场景变体 (如 S5 的 degrade/refill)")
    parser.add_argument("--full", action="store_true",
                        help="完整重放: 种基线→建run→T1~T5→缺口→方案→判定")
    parser.add_argument("--dry-run", action="store_true",
                        help="只解析场景+overrides, 打印基线摘要, 不写库")
    parser.add_argument("--whatif", metavar="KEY|JSON",
                        help="what-if 推演: 标准假设名 (S4: whatif-A/B/C) "
                             "或 assumptions JSON")
    parser.add_argument("--parent", metavar="RUN_ID",
                        help="what-if 父基线 run_id (缺省取场景 ref)")
    parser.add_argument("--check-history", action="store_true",
                        help="冻结基线 vs 历史 run 记录一致性自检")
    parser.add_argument("--check-all", action="store_true",
                        help="S1~S5 全部场景一致性自检")
    args = parser.parse_args(argv)

    try:
        if args.check_all:
            ok, report = check_all()
            print(_fmt(report))
            return 0 if ok else 1
        if not args.scenario_id:
            parser.error("scenario_id required (or use --check-all)")
        scenario_id = args.scenario_id.strip().upper()
        if args.check_history:
            ok, report = check_history(scenario_id)
            print(_fmt(report))
            return 0 if ok else 1

        scenario = load_scenario(scenario_id)
        if args.whatif:
            assumptions = _parse_assumptions(args.whatif)
            key = None if isinstance(assumptions, dict) else args.whatif
            ok, st, msg, data = run_scenario_whatif(
                scenario_id, assumptions, key, args.parent)
            print(_fmt({"ok": ok, "http_status": st, "msg": msg,
                        "data": data}))
            return 0 if ok else 1

        overrides = _parse_overrides(args.overrides) if args.overrides else None
        baseline, vmeta = resolve_variant(scenario, args.variant)
        baseline = apply_input_overrides(baseline, overrides)
        if args.dry_run:
            services, materials = derive_services_materials(baseline)
            print(_fmt({
                "scenario_id": scenario_id,
                "name": scenario.get("name"),
                "variant": (vmeta or {}).get("label"),
                "dry_run": True,
                "services": services,
                "materials": materials,
                "baseline": baseline,
            }))
            return 0
        if args.full:
            ok, st, msg, data = replay_scenario(
                scenario_id, overrides, args.variant)
            print(_fmt({"ok": ok, "http_status": st, "msg": msg,
                        "data": data}))
            return 0 if ok else 1
        ok, st, msg, data = create_baseline_run(scenario_id, overrides,
                                                args.variant)
        print(_fmt({"ok": ok, "http_status": st, "msg": msg, "data": data}))
        return 0 if ok else 1
    except (ValueError, OSError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
