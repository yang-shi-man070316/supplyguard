"""阶段06板块4: LLM 解释模块 llm_explainer (严格执行「先计算, 再解释」).

流程 (两条铁律):
    1. 先计算: T1~T5 判定结果由任务执行链 (task_execute) 事先计算并落库
       (task_results / dependency_checks)。本模块只读这些结构化结果 +
       统计摘要 + 全部辅助证据, 组装解释上下文 —— 绝不重新计算判定,
       绝不修改任何业务判定结论。
    2. 再解释: 大模型仅把结构化结果翻译成自然语言解读。解释文本写入
       evidence_v2 (evidence_type='llm') 作为辅助证据。

解释提供方 (按环境变量选择, 失败一律降级 status='unavailable'):
    - SUPPLYGUARD_LLM_DISABLED=1   强制故障注入 (失效降级专项测试用)
    - SUPPLYGUARD_LLM_URL 已设置   远程 OpenAI 兼容接口
                                   (另需 SUPPLYGUARD_LLM_API_KEY,
                                    SUPPLYGUARD_LLM_MODEL 默认 gpt-4o-mini)
                                   超时 15s, 任何异常 → unavailable
    - 均未设置                     本地确定性模板引擎 (无需外网/密钥,
                                   严格按结构化状态逐条生成解读)

CLI 用法: python analysis/llm_explainer.py <run_id>
"""

import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

# 项目根/backend 加入 sys.path, 保证独立运行或被 backend/main.py import
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
for _p in (_PROJECT_ROOT, _PROJECT_ROOT / "backend"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

LLM_URL = os.environ.get("SUPPLYGUARD_LLM_URL", "")
LLM_API_KEY = os.environ.get("SUPPLYGUARD_LLM_API_KEY", "")
LLM_MODEL = os.environ.get("SUPPLYGUARD_LLM_MODEL", "gpt-4o-mini")
LLM_DISABLED = os.environ.get("SUPPLYGUARD_LLM_DISABLED", "") == "1"
_LLM_TIMEOUT_SECONDS = 15

_TASK_STATUS_ZH = {
    "established": "成立(全部依赖满足)",
    "blocked": "受阻(存在不满足依赖)",
    "pending_verification": "待验证(存在无法判断依赖)",
    "unknown": "无法判断",
}
_DEP_STATUS_ZH = {
    "satisfied": "满足", "unsatisfied": "不满足", "unknown": "无法判断",
}

_SYSTEM_PROMPT = (
    "你是应急物资保障系统的判定解读助手。铁律: 只解读给定结构化判定结果, "
    "不修改、不补充、不推翻任何判定结论; 所有状态以输入的结构化数据为准; "
    "缺失的数据如实标注「无结果」; 用简体中文输出 Markdown 解读文本。"
)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


# ============================================================
# 步骤1 (先计算): 组装解释上下文 —— 全部只读, 零计算判定
# ============================================================

def _latest_evidence_rows(conn, evidence_type, limit=1):
    """evidence_v2 中某类型最新 N 行 (只读)。"""
    return [dict(r) for r in conn.execute(
        "SELECT evidence_id, evidence_type, source_id, status, value_json, "
        "observed_at, error_message FROM evidence_v2 "
        "WHERE evidence_type=? ORDER BY created_at DESC, rowid DESC LIMIT ?",
        (evidence_type, limit),
    ).fetchall()]


def gather_context(run_id: str) -> dict:
    """组装 LLM 解释上下文 (只读数据库与统计函数, 不做任何判定计算)。

    返回:
      {run_id, run_status, tasks:{task_id:{status,reason}}, deps:{task_id:[...]},
       blockers, plans, stats:{gap_frequency, plan_fail_dist},
       evidence:{weather, ml, rag, tools}, computed_first: True}
    """
    from db import close_conn, get_conn
    from gap_check import get_blockers
    from plan_store import get_plans_with_results
    from t1_check import get_task_result

    context = {
        "run_id": run_id,
        "run_status": None,
        "tasks": {},
        "deps": {},
        "blockers": None,
        "plans": None,
        "stats": {},
        "evidence": {"weather": [], "ml": [], "rag": [], "tools": []},
        "computed_first": True,  # 先计算标志: 上下文全部来自已落库计算结果
    }

    # ---- T1~T5 结构化判定结果 (已落库, 只读) ----
    for task_id in ("T1", "T2", "T3", "T4", "T5"):
        ok, http_status, msg, data = get_task_result(run_id, task_id)
        if not ok and http_status == 404:
            return {"run_id": run_id, "not_found": True}
        if not ok:
            # 数据库错误 (500): 上下文组装失败, 不臆造解释, 显式透传错误
            return {"run_id": run_id, "context_error": msg}
        tr = data["task_result"] if data.get("task_result") else None
        context["run_status"] = data.get("run_status")
        context["tasks"][task_id] = {
            "status": tr["result_status"] if tr else None,
            "reason": tr["summary_reason"] if tr else None,
        }
        context["deps"][task_id] = [
            {"dep_key": c["dep_key"], "status": c["status"],
             "reason": c["reason"]}
            for c in (data.get("dependency_checks") or [])
        ]

    # ---- 缺口与方案 (已落库, 只读) ----
    ok, _, _, data = get_blockers(run_id)
    context["blockers"] = data if ok else None
    ok, _, _, data = get_plans_with_results(run_id)
    context["plans"] = data if ok else None

    # ---- 统计摘要 (stats_collect 同口径) ----
    from analysis.stats_collect import (collect_gap_frequency,
                                        collect_plan_fail_reasons)
    conn = get_conn()
    try:
        _, gap_summary, _ = collect_gap_frequency(conn)
        _, plan_dist, _ = collect_plan_fail_reasons(conn)
        context["stats"]["gap_frequency"] = (
            gap_summary.to_dict("records") if gap_summary is not None else [])
        context["stats"]["plan_fail_dist"] = (
            plan_dist.to_dict("records") if plan_dist is not None else [])
        context["evidence"]["weather"] = _latest_evidence_rows(conn, "weather", 1)
        context["evidence"]["ml"] = _latest_evidence_rows(conn, "ml", 1)
        context["evidence"]["rag"] = _latest_evidence_rows(conn, "rag", 3)
        context["evidence"]["tools"] = _latest_evidence_rows(conn, "tool", 10)
    finally:
        close_conn(conn)
    return context


# ============================================================
# 步骤2 (再解释): 解释生成 —— 模板引擎 / 远程 LLM, 均只解读
# ============================================================

def _fmt_task_line(task_id, name, task) -> str:
    if not task.get("status"):
        return "- **%s** %s: 无结果 (该任务尚未执行或未落库)" % (task_id, name)
    zh = _TASK_STATUS_ZH.get(task["status"], task["status"])
    return ("- **%s** %s: %s — %s"
            % (task_id, name, zh, task.get("reason") or "无摘要"))


def _local_explain(context: dict) -> str:
    """本地确定性模板引擎: 严格按结构化状态逐条生成解读 (无外网依赖)。"""
    task_names = {
        "T1": "关键服务点最低需求保障", "T2": "运输链路可达保障",
        "T3": "调度响应时效保障", "T4": "入库签收合规保障",
        "T5": "证据一致性校验",
    }
    lines = [
        "> 以下解读严格基于系统已计算的 T1~T5 结构化判定结果与落库证据, "
        "不修改、不补充任何判定结论。",
        "",
        "## 一、任务判定解读",
    ]
    for task_id in ("T1", "T2", "T3", "T4", "T5"):
        lines.append(_fmt_task_line(task_id, task_names[task_id],
                                    context["tasks"].get(task_id, {})))

    lines += ["", "## 二、缺口与候选方案解读"]
    blockers = context.get("blockers") or {}
    b_list = blockers.get("blockers") or []
    if b_list:
        lines.append("- 本 run 存在 %d 项物资缺口:" % len(b_list))
        for b in b_list:
            lines.append("  - %s / %s: 需求 %s, 有效接收 %s, 缺口 %s"
                         % (b.get("service_id"), b.get("material_id"),
                            b.get("required_quantity"),
                            b.get("valid_received"), b.get("gap")))
    else:
        lines.append("- 本 run 无落库缺口记录。")
    plans = context.get("plans") or {}
    p_list = plans.get("plans") or []
    if p_list:
        exe = sum(1 for p in p_list if p.get("status") == "executable")
        non = sum(1 for p in p_list if p.get("status") == "non_executable")
        lines.append("- 候选方案 %d 套: %d 可执行, %d 不可执行。"
                     % (len(p_list), exe, non))

    lines += ["", "## 三、辅助证据解读"]
    weather_rows = context["evidence"].get("weather") or []
    if weather_rows and weather_rows[0].get("status") == "ok":
        try:
            w = json.loads(weather_rows[0]["value_json"])
            wd = w.get("weather") or {}
            lines.append("- 天气: 可用 — %s %s℃, %s, 风速 %s km/h"
                         % (w.get("location"), wd.get("temp_c"),
                            wd.get("desc_zh") or wd.get("desc"),
                            wd.get("wind_kmh")))
        except (TypeError, ValueError, json.JSONDecodeError):
            lines.append("- 天气: 证据解析异常")
    elif weather_rows:
        lines.append("- 天气: ⚠ 不可用 — %s"
                     % (weather_rows[0].get("error_message") or "无原因"))
    else:
        lines.append("- 天气: 无证据记录")
    ml_rows = context["evidence"].get("ml") or []
    if ml_rows:
        try:
            m = json.loads(ml_rows[0]["value_json"])
            pred = m.get("prediction") or {}
            lines.append("- ML 需求趋势: %s — 下一窗口 trend=%s (up 概率 %s)"
                         % (m.get("model_status"), pred.get("trend"),
                            pred.get("probability")))
        except (TypeError, ValueError, json.JSONDecodeError):
            lines.append("- ML 需求趋势: 证据解析异常")
    else:
        lines.append("- ML 需求趋势: 无证据记录")
    rag_rows = context["evidence"].get("rag") or []
    if rag_rows:
        try:
            r = json.loads(rag_rows[0]["value_json"])
            lines.append("- RAG: 最近查询「%s」→ %s"
                         % (r.get("query"), "命中(有来源引用)" if r.get("matched")
                            else "无可靠依据"))
        except (TypeError, ValueError, json.JSONDecodeError):
            lines.append("- RAG: 证据解析异常")
    else:
        lines.append("- RAG: 无证据记录")
    tool_rows = context["evidence"].get("tools") or []
    lines.append("- 工具调用轨迹: 最近 %d 条" % len(tool_rows))

    lines += ["", "## 四、解释边界",
              "本解读为自然语言辅助解读; 判定结论以 task_results / "
              "dependency_checks 落库结果为准, 本模块不改变任何判定。"]
    return "\n".join(lines)


def _remote_explain(context: dict) -> dict:
    """远程 OpenAI 兼容接口调用 (只解读, 系统提示词已约束不改变判定)。

    返回契约 (与 explain() 全部分支统一, 四键齐全):
        {"status": "ok"|"unavailable",
         "explanation": text|None,
         "error_message": str}
    (provider 键由 explain() 统一补齐)。
    """
    user_content = json.dumps(context, ensure_ascii=False, default=str)
    payload = json.dumps({
        "model": LLM_MODEL,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content":
                "请解读以下结构化判定上下文, 输出 Markdown 解读文本:\n"
                + user_content},
        ],
        "temperature": 0.2,
    }, ensure_ascii=False).encode("utf-8")
    try:
        req = urllib.request.Request(
            LLM_URL,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer %s" % LLM_API_KEY,
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=_LLM_TIMEOUT_SECONDS) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        data = json.loads(raw)
        text = data["choices"][0]["message"]["content"]
        if not text or not str(text).strip():
            raise ValueError("empty LLM response content")
        return {"status": "ok", "explanation": str(text).strip()}
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError,
            ConnectionError, OSError, ValueError, json.JSONDecodeError,
            KeyError, TypeError, AttributeError, IndexError) as exc:
        # 降级契约: 与 explain() 其他分支返回结构完全统一
        # (status/provider/explanation/error_message 四键齐全),
        # explanation 恒为 None —— 下游 run_llm_explain 直接索引不崩溃
        return {"status": "unavailable",
                "explanation": None,
                "error_message": "%s: %s" % (type(exc).__name__, exc)}


def explain(context: dict) -> dict:
    """按配置选择解释提供方 (纯函数, 不落库)。

    返回 {"status": "ok"|"unavailable", "provider": str,
          "explanation": str|None, "error_message": str}。
    """
    if LLM_DISABLED:
        return {"status": "unavailable", "provider": "disabled",
                "explanation": None,
                "error_message": "LLM component disabled by "
                                 "SUPPLYGUARD_LLM_DISABLED=1 (故障注入)"}
    if LLM_URL:
        result = _remote_explain(context)
        result["provider"] = "remote:%s" % LLM_MODEL
        return result
    return {"status": "ok", "provider": "local-template",
            "explanation": _local_explain(context), "error_message": ""}


# ============================================================
# 编排: 组装上下文 → 生成解释 → 写入 evidence_v2
# ============================================================

def run_llm_explain(run_id: str) -> tuple:
    """LLM 解释编排 (先计算→再解释→解释文本落 evidence_v2)。

    - run 不存在 → (False, 404, ...)
    - 上下文组装数据库错误 → (False, 500, ...), 不写解释证据
    - LLM 故障/禁用 → 解释 status='unavailable', 证据同标 unavailable
      (降级语义: 判定结论不受影响)
    - 解释文本写入 evidence_v2: evidence_type='llm'
    返回 (ok, http_status, msg, data);
    data = {run_id, status, provider, explanation, error_message,
            context_summary, evidence_id} (降级分支 error_message 必非空)。
    """
    from backend.run_store import save_evidence  # 懒加载

    if run_id is None or not str(run_id).strip():
        return False, 400, "run_id required", None
    run_id = str(run_id).strip()
    context = gather_context(run_id)
    if context.get("not_found"):
        return False, 404, "run %s not found" % run_id, None
    if context.get("context_error"):
        return False, 500, ("context gather failed: %s"
                            % context["context_error"]), None
    result = explain(context)
    evidence_id = None
    try:
        ok, evidence_id = save_evidence(
            evidence_type="llm",
            source_id="llm:explain:%s" % run_id,
            status=result["status"],
            value_json=json.dumps({
                "run_id": run_id,
                "status": result["status"],
                "provider": result["provider"],
                "explanation": result["explanation"],
                "error_message": result.get("error_message", ""),
                "context_summary": {
                    "tasks": context["tasks"],
                    "computed_first": context["computed_first"],
                },
            }, ensure_ascii=False, default=str),
            observed_at=_now(),
            error_message=result.get("error_message", ""),
        )
    except ValueError as exc:
        return False, 500, "llm evidence save failed: %s" % exc, None
    if not ok:
        return False, 500, "llm evidence save failed: %s" % evidence_id, None
    data = {
        "run_id": run_id,
        "status": result["status"],
        "provider": result["provider"],
        "explanation": result["explanation"],
        "error_message": result.get("error_message", ""),
        "context_summary": {"tasks": context["tasks"],
                            "computed_first": context["computed_first"]},
        "evidence_id": evidence_id,
    }
    return True, 200, ("llm explanation %s (provider=%s)"
                       % (result["status"], result["provider"])), data


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: python llm_explainer.py <run_id>", file=sys.stderr)
        sys.exit(2)
    ok, http_status, msg, data = run_llm_explain(sys.argv[1])
    print(json.dumps({"ok": ok, "http_status": http_status, "msg": msg,
                      "data": data}, ensure_ascii=False, indent=2))
    sys.exit(0 if ok else 1)
