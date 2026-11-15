"""阶段06板块4: 标准 Markdown 阶段报告构建器 report_builder.

输入 run_id, 聚合以下全部证据输出标准 Markdown 阶段报告:
    - T1~T5 结构化判定结果 (task_results / dependency_checks, 只读)
    - 缺口与候选方案 (blockers / plan_results, 只读)
    - 统计摘要 (stats_collect 同口径: 物资缺口频次 / 方案失败原因分布)
    - 辅助证据 (天气 / ML / RAG / 工具调用轨迹 evidence_v2)
    - LLM 解释 (llm_explainer, 先计算再解释; 故障时报告标注异常)

铁律: 报告只聚合已落库的计算结果, 不重新计算任何判定, 不修改任何
业务数据; 组件异常在报告中明确标注 ⚠。

CLI 用法: python analysis/report_builder.py <run_id>
"""

import json
import sys
from datetime import datetime
from pathlib import Path

# 项目根/backend 加入 sys.path, 保证独立运行或被 backend/main.py import
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
for _p in (_PROJECT_ROOT, _PROJECT_ROOT / "backend"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _md_table(headers, rows) -> str:
    """rows: list[list]; 空 rows 返回表头 + 一行「无」占位。"""
    lines = ["| " + " | ".join(headers) + " |",
             "| " + " | ".join(["---"] * len(headers)) + " |"]
    if not rows:
        return "\n".join(lines)
    for r in rows:
        cells = ["" if c is None else str(c).replace("|", "\\|").replace("\n", " ")
                 for c in r]
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def _latest_evidence(conn, evidence_type, limit=1):
    return [dict(r) for r in conn.execute(
        "SELECT evidence_id, evidence_type, source_id, status, value_json, "
        "observed_at, error_message FROM evidence_v2 "
        "WHERE evidence_type=? ORDER BY created_at DESC, rowid DESC LIMIT ?",
        (evidence_type, limit),
    ).fetchall()]


def _load_json(text):
    try:
        return json.loads(text)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


def build_report(run_id: str) -> tuple:
    """聚合全部证据生成 Markdown 报告。

    返回 (ok, http_status, msg, data);
    data = {run_id, generated_at, report_markdown, components:{...}}。
    """
    from db import close_conn, get_conn
    from gap_check import get_blockers
    from plan_store import get_plans_with_results
    from t1_check import get_task_result
    from analysis.llm_explainer import run_llm_explain
    from analysis.stats_collect import (collect_gap_frequency,
                                        collect_plan_fail_reasons)

    if run_id is None or not str(run_id).strip():
        return False, 400, "run_id required", None
    run_id = str(run_id).strip()

    # ---- 0. run 存在性 (404) ----
    conn = get_conn()
    try:
        run = conn.execute(
            "SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
    finally:
        close_conn(conn)
    if run is None:
        return False, 404, "run %s not found" % run_id, None

    # ---- 1. T1~T5 结构化判定结果 (只读) ----
    task_rows, dep_rows = [], []
    for task_id in ("T1", "T2", "T3", "T4", "T5"):
        ok, http_status, msg, data = get_task_result(run_id, task_id)
        tr = (data or {}).get("task_result")
        task_rows.append([
            task_id,
            tr["result_status"] if tr else "(未执行)",
            tr["summary_reason"] if tr else "-",
        ])
        for c in (data or {}).get("dependency_checks") or []:
            dep_rows.append([
                task_id, c["dep_key"], c["status"], c["reason"] or "-",
            ])

    # ---- 2. 缺口与方案 (只读) ----
    ok, _, _, blockers_data = get_blockers(run_id)
    blockers = (blockers_data or {}).get("blockers") or []
    ok, _, _, plans_data = get_plans_with_results(run_id)
    plans = (plans_data or {}).get("plans") or []

    # ---- 3. 统计摘要 (stats_collect 同口径, 只读) ----
    conn = get_conn()
    try:
        _, gap_summary, _ = collect_gap_frequency(conn)
        _, plan_dist, _ = collect_plan_fail_reasons(conn)
        weather_rows = _latest_evidence(conn, "weather", 1)
        ml_rows = _latest_evidence(conn, "ml", 1)
        rag_rows = _latest_evidence(conn, "rag", 3)
        tool_rows = _latest_evidence(conn, "tool", 10)
    finally:
        close_conn(conn)

    # ---- 4. LLM 解释 (先计算再解释; 故障返回 unavailable) ----
    ok_llm, _, msg_llm, llm_data = run_llm_explain(run_id)
    llm_status = llm_data["status"] if ok_llm else "error"
    llm_error = "" if ok_llm else msg_llm

    # ---- 5. 组件健康状态 ----
    components = {"weather": {"status": "ok", "detail": ""},
                  "rag": {"status": "ok", "detail": ""},
                  "ml": {"status": "ok", "detail": ""},
                  "llm": {"status": llm_status, "detail": llm_error},
                  "tool": {"status": "ok", "detail": ""}}
    if weather_rows:
        w_status = weather_rows[0]["status"]
        components["weather"]["status"] = w_status
        if w_status == "unavailable":
            components["weather"]["detail"] = (weather_rows[0].get("error_message")
                                               or "无原因")
        else:
            wj = _load_json(weather_rows[0]["value_json"]) or {}
            wd = (wj.get("weather") or {}) if isinstance(wj, dict) else {}
            components["weather"]["detail"] = "%s %s℃ %s" % (
                wj.get("location", "-"), wd.get("temp_c", "-"),
                wd.get("desc_zh", wd.get("desc", "-")))
    else:
        components["weather"] = {"status": "unavailable",
                                 "detail": "无天气证据记录"}
    if ml_rows:
        mj = _load_json(ml_rows[0]["value_json"]) or {}
        if ml_rows[0]["status"] == "unavailable":
            components["ml"]["status"] = "unavailable"
            components["ml"]["detail"] = ml_rows[0].get("error_message") or "无原因"
        elif (isinstance(mj, dict)
                and mj.get("model_status") == "insufficient_data"):
            components["ml"]["status"] = "unavailable"
            components["ml"]["detail"] = "历史数据不足, 无法训练"
        else:
            pred = (mj.get("prediction") or {}) if isinstance(mj, dict) else {}
            components["ml"]["detail"] = "trend=%s (up 概率 %s)" % (
                pred.get("trend"), pred.get("probability"))
    else:
        components["ml"] = {"status": "unavailable", "detail": "无 ML 证据记录"}
    if rag_rows:
        rj = _load_json(rag_rows[0]["value_json"]) or {}
        if (isinstance(rj, dict) and rj.get("doc_count") == 0):
            components["rag"]["status"] = "unavailable"
            components["rag"]["detail"] = "知识库为空 (rag_docs 无可用文档)"
        else:
            components["rag"]["detail"] = "最近查询「%s」" % (
                rj.get("query") if isinstance(rj, dict) else "-")
    else:
        components["rag"] = {"status": "unavailable", "detail": "无 RAG 证据记录"}
    if tool_rows and any(r["status"] == "error" for r in tool_rows):
        components["tool"]["status"] = "unavailable"
        components["tool"]["detail"] = "存在工具调用失败轨迹"
    else:
        components["tool"]["detail"] = "最近 %d 条轨迹" % len(tool_rows)

    # ---- 6. 组装 Markdown ----
    comp_rows = []
    for name, info in components.items():
        flag = "⚠ " if info["status"] != "ok" else ""
        comp_rows.append([name, flag + info["status"],
                          info["detail"] or "-"])

    blocker_rows = [
        [b.get("service_id"), b.get("material_id"),
         b.get("required_quantity"), b.get("valid_received"), b.get("gap")]
        for b in blockers]
    plan_rows = [
        [p.get("plan_name"), p.get("status") or "(未判定)",
         p.get("reserve_id"), p.get("vehicle_id"), p.get("route_id"),
         p.get("transfer_qty")]
        for p in plans]
    gap_freq_rows = ([
        [r.get("物资"), r.get("缺口记录数"), r.get("涉及run数"),
         r.get("缺口合计"), r.get("平均缺口")]
        for r in (gap_summary.to_dict("records") if gap_summary is not None else [])
    ] or None)
    fail_dist_rows = ([
        [r.get("失败原因"), r.get("次数"), r.get("占比")]
        for r in (plan_dist.to_dict("records") if plan_dist is not None else [])
    ] or None)

    explanation_md = (llm_data["explanation"]
                      if ok_llm and llm_status == "ok" else
                      "⚠ LLM 解释不可用: %s" % (
                          llm_error or (llm_data or {}).get("provider")))

    md = [
        "# 阶段06 保障分析报告",
        "",
        "- run_id: %s" % run_id,
        "- 生成时间: %s" % _now(),
        "- run 状态: %s (scenario %s)" % (run["run_status"], run["scenario_id"]),
        "",
        "## 一、T1~T5 结构化判定结果",
        "",
        _md_table(["任务", "状态", "摘要"], task_rows),
        "",
        "### 依赖检查明细",
        "",
        _md_table(["任务", "依赖", "状态", "理由"], dep_rows),
        "",
        "## 二、缺口与候选方案",
        "",
        "### 缺口 (blockers)",
        "",
        _md_table(["服务点", "物资", "需求", "有效接收", "缺口"], blocker_rows),
        "",
        "### 候选方案与可行性判定",
        "",
        _md_table(["方案", "判定", "储备点", "车辆", "路线", "调拨量"], plan_rows),
        "",
        "## 三、统计摘要 (全局)",
        "",
        "### 物资缺口频次",
        "",
        _md_table(["物资", "缺口记录数", "涉及run数", "缺口合计", "平均缺口"],
                  gap_freq_rows if gap_freq_rows else []),
        "",
        "### 候选方案失败原因分布",
        "",
        _md_table(["失败原因", "次数", "占比"], fail_dist_rows
                  if fail_dist_rows else []),
        "",
        "## 四、辅助证据",
        "",
        "### 天气",
        "",
        ("- 证据行: %s" % weather_rows[0]["evidence_id"]
         if weather_rows else "- 无证据记录"),
        "- 状态: %s %s" % (components["weather"]["status"],
                           components["weather"]["detail"]),
        "",
        "### ML 需求趋势预测",
        "",
        ("- 证据行: %s" % ml_rows[0]["evidence_id"] if ml_rows else "- 无证据记录"),
        "- 状态: %s %s" % (components["ml"]["status"],
                           components["ml"]["detail"]),
        "",
        "### RAG 知识库检索",
        "",
        "- 最近证据行数: %d" % len(rag_rows),
        "- 状态: %s %s" % (components["rag"]["status"],
                           components["rag"]["detail"]),
        "",
        "### 工具调用轨迹 (最近 %d 条)" % len(tool_rows),
        "",
        _md_table(["时间", "工具", "状态"],
                  [[r["observed_at"], r["source_id"], r["status"]]
                   for r in tool_rows]),
        "",
        "## 五、LLM 解释 (先计算, 再解释)",
        "",
        explanation_md,
        "",
        "## 六、组件健康状态",
        "",
        _md_table(["组件", "状态", "说明"], comp_rows),
        "",
        "## 七、数据与判定完整性声明",
        "",
        "- 本报告全部判定结论直接读取 task_results / dependency_checks 落库结果, "
        "未重新计算、未修改任何判定。",
        "- 辅助证据 (天气/RAG/ML/工具/LLM) 仅作为解读输入, 不参与判定逻辑。",
        "- 组件异常已在「六、组件健康状态」标注 ⚠。",
    ]
    report_md = "\n".join(md)
    return True, 200, "report built", {
        "run_id": run_id,
        "generated_at": _now(),
        "report_markdown": report_md,
        "components": components,
        "llm_status": llm_status,
    }


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: python report_builder.py <run_id>", file=sys.stderr)
        sys.exit(2)
    ok, http_status, msg, data = build_report(sys.argv[1])
    if not ok:
        print(json.dumps({"ok": ok, "http_status": http_status, "msg": msg},
                         ensure_ascii=False, indent=2), file=sys.stderr)
        sys.exit(1)
    print(data["report_markdown"])
