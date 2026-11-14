"""阶段06板块1: 历史数据统计采集脚本 stats_collect (pandas).

使用 pandas 读取数据库历史数据, 输出三类统计:
    1. 服务点需求趋势       数据源 runs.input_snapshot_json.demands
                            (每个 run 一个时间点, 按服务点聚合需求总量)
    2. 物资缺口频次         数据源 blockers 表
                            (按物资汇总缺口记录数/涉及run数/缺口合计等)
    3. 候选方案失败原因分布  数据源 plan_results 表 (status='non_executable')
                            (按 reason_json.checks 中未通过规则的类别归类)

约束:
    - 只读数据库 (仅 SELECT), 不写任何表、不改任何状态
    - 口径与 gap_check / plan_judge 一致: 只读各 run 冻结快照, 不读实时观测表
    - 脏数据防御: 快照/reason_json 解析失败不中断, 跳过并计数报告

CLI 用法:
    python analysis/stats_collect.py                 # 打印三类统计到控制台
    python analysis/stats_collect.py --db <路径>     # 指定数据库文件
    python analysis/stats_collect.py --csv <目录>    # 额外导出 UTF-8 CSV (带 BOM)
"""

import argparse
import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path

import pandas as pd

# 默认数据库: 项目根 backend/bb03.db (与 db.DB_PATH 一致)
DEFAULT_DB = Path(__file__).resolve().parents[1] / "backend" / "bb03.db"

# 候选方案判定规则 -> 失败原因类别 (与 plan_judge.py checks[].rule 对齐)
RULE_LABELS = {
    "reserve_stock": "仓库库存不足",
    "vehicle": "车辆不可用/运力不足",
    "route": "路线不可用/超时",
}


def _safe_json(text):
    """解析 JSON 字符串, 失败返回 None (防御脏数据, 不抛异常)。"""
    if not text:
        return None
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return None


def _parse_time(value):
    """解析时间字符串 -> pandas Timestamp; 无法解析返回 NaT。"""
    if not value:
        return pd.NaT
    try:
        return pd.to_datetime(str(value), errors="coerce")
    except (TypeError, ValueError):
        return pd.NaT


def _run_sort_key(run_id):
    """按 run_id 尾部数字排序 (避免 BB03-RUN-010 < BB03-RUN-002 的字典序问题)。"""
    try:
        return int(str(run_id).rsplit("-", 1)[-1])
    except (TypeError, ValueError):
        return 0


# ============================================================
# 统计1: 服务点需求趋势
# ============================================================

def collect_demand_trend(conn):
    """逐 run 解析冻结快照中的需求, 输出服务点需求趋势两张表。

    返回 (overview, agg_df, detail_pivot, long_df):
        agg_df        各服务点需求总量趋势 (run x 服务点, 含合计列)
        detail_pivot  各服务点分物资需求趋势 (run x "服务点·物资")
        long_df       明细长表 (run_id/快照时间/服务点/物资/需求量)
    无可用数据时返回 (overview, None, None, None)。
    """
    runs = pd.read_sql_query(
        "SELECT run_id, run_status, created_at, input_snapshot_json "
        "FROM runs ORDER BY rowid", conn)

    rows, skipped = [], []
    for r in runs.itertuples():
        snap = _safe_json(r.input_snapshot_json)
        demands = snap.get("demands") if isinstance(snap, dict) else None
        if not isinstance(demands, list):
            skipped.append(str(r.run_id))
            continue
        ts_raw = snap.get("snapshot_at") or r.created_at
        ts = _parse_time(ts_raw)
        for d in demands:
            if not isinstance(d, dict):
                continue
            q = d.get("required_quantity")
            if isinstance(q, bool) or not isinstance(q, (int, float)):
                continue  # 非法需求量跳过 (防御脏数据)
            rows.append({
                "run_id": str(r.run_id), "ts_raw": ts_raw, "ts": ts,
                "service_id": str(d.get("service_id") or ""),
                "material_id": str(d.get("material_id") or ""),
                "required_quantity": int(q),
            })

    overview = {
        "run_total": int(len(runs)),
        "run_parsed": int(len(runs) - len(skipped)),
        "run_skipped": skipped,
        "demand_rows": len(rows),
    }
    if not rows:
        return overview, None, None, None

    df = pd.DataFrame(rows)

    # -- 表1a: 各服务点需求总量趋势 (run x 服务点) --
    agg = (df.pivot_table(index=["run_id", "ts_raw", "ts"],
                          columns="service_id",
                          values="required_quantity",
                          aggfunc="sum", fill_value=0)
             .reset_index())
    service_cols = sorted(c for c in agg.columns
                          if c not in ("run_id", "ts_raw", "ts"))
    agg["合计"] = agg[service_cols].sum(axis=1)
    agg = (agg[["run_id", "ts_raw", "ts"] + service_cols + ["合计"]]
           .sort_values("ts", na_position="last", kind="mergesort")
           .reset_index(drop=True))
    agg["快照时间"] = agg["ts"].dt.strftime("%Y-%m-%d %H:%M:%S").fillna(agg["ts_raw"])
    agg = agg[["run_id", "快照时间"] + service_cols + ["合计"]]

    # -- 表1b: 各服务点分物资需求趋势 (run x "服务点·物资") --
    detail = (df.pivot_table(index=["run_id", "ts_raw", "ts"],
                             columns=["service_id", "material_id"],
                             values="required_quantity",
                             aggfunc="sum", fill_value=0)
                .reset_index())
    # pivot_table(columns=[...]) 经 reset_index 后所有列均为 tuple 形式
    # (如 ('ts', ''), ('P1', 'L')), 过滤空层级后拼平
    detail.columns = ["·".join(filter(None, map(str, c)))
                      if isinstance(c, tuple) else str(c)
                      for c in detail.columns]
    detail = (detail.sort_values("ts", na_position="last", kind="mergesort")
              .reset_index(drop=True))
    detail["快照时间"] = detail["ts"].dt.strftime("%Y-%m-%d %H:%M:%S").fillna(detail["ts_raw"])
    pair_cols = sorted(c for c in detail.columns
                       if c not in ("run_id", "ts_raw", "ts", "快照时间"))
    detail = detail[["run_id", "快照时间"] + pair_cols]

    long_df = df.copy()
    long_df["快照时间"] = (long_df["ts"].dt.strftime("%Y-%m-%d %H:%M:%S")
                           .fillna(long_df["ts_raw"]))
    long_df = long_df[["run_id", "快照时间", "service_id", "material_id",
                       "required_quantity"]]
    return overview, agg, detail, long_df


# ============================================================
# 统计2: 物资缺口频次
# ============================================================

def collect_gap_frequency(conn):
    """blockers 表按物资汇总缺口频次。

    返回 (overview, summary_df, by_run_pivot):
        summary_df     按物资汇总 (缺口记录数/涉及run数/涉及服务点数/缺口合计/
                       平均缺口/最大缺口)
        by_run_pivot   各 run 分物资缺口合计 (run x 物资, 含合计列)
    无缺口数据时返回 (overview, None, None)。
    """
    gaps = pd.read_sql_query(
        "SELECT blocker_id, run_id, service_id, material_id, "
        "required_quantity, valid_received, gap, created_at "
        "FROM blockers ORDER BY blocker_id", conn)

    overview = {
        "blocker_total": int(len(gaps)),
        "run_count": int(gaps["run_id"].nunique()) if not gaps.empty else 0,
        "service_count": int(gaps["service_id"].nunique()) if not gaps.empty else 0,
    }
    if gaps.empty:
        return overview, None, None

    summary = (gaps.groupby("material_id")
               .agg(缺口记录数=("blocker_id", "count"),
                    涉及run数=("run_id", "nunique"),
                    涉及服务点数=("service_id", "nunique"),
                    缺口合计=("gap", "sum"),
                    平均缺口=("gap", lambda s: round(float(s.mean()), 2)),
                    最大缺口=("gap", "max"))
               .reset_index()
               .rename(columns={"material_id": "物资"}))
    summary = (summary.sort_values(["缺口记录数", "缺口合计"],
                                    ascending=[False, False])
               .reset_index(drop=True))

    by_run = (gaps.pivot_table(index="run_id", columns="material_id",
                               values="gap", aggfunc="sum", fill_value=0)
              .reset_index())
    material_cols = sorted(c for c in by_run.columns if c != "run_id")
    by_run["缺口合计"] = by_run[material_cols].sum(axis=1)
    by_run["_k"] = by_run["run_id"].map(_run_sort_key)
    by_run = (by_run.sort_values("_k", kind="mergesort")
              .drop(columns="_k")
              .reset_index(drop=True))
    by_run = by_run[["run_id"] + material_cols + ["缺口合计"]]
    return overview, summary, by_run


# ============================================================
# 统计3: 候选方案失败原因分布
# ============================================================

def collect_plan_fail_reasons(conn):
    """plan_results 非可执行记录按失败规则归类。

    返回 (overview, dist_df, detail_df):
        dist_df    失败原因类别分布 (类别/次数/占比)
        detail_df  失败方案明细 (run_id/方案名/失败规则/原因摘要)
    无判定数据时返回 (overview, None, None)。
    """
    results = pd.read_sql_query(
        "SELECT pr.run_id, pr.plan_id, pr.status, pr.reason_json, pr.created_at, "
        "       pd.plan_name "
        "FROM plan_results pr "
        "LEFT JOIN plan_definitions pd "
        "  ON pd.run_id = pr.run_id AND pd.plan_id = pr.plan_id "
        "ORDER BY pr.plan_result_id", conn)

    overview = {
        "plan_result_total": int(len(results)),
        "executable": int((results["status"] == "executable").sum()),
        "non_executable": int((results["status"] == "non_executable").sum()),
    }
    non_exec = results[results["status"] == "non_executable"]
    if non_exec.empty:
        return overview, None, None

    counter = Counter()
    detail_rows = []
    for r in non_exec.itertuples():
        reason = _safe_json(r.reason_json)
        failed_labels, summary = [], ""
        if isinstance(reason, dict):
            summary = reason.get("summary") or ""
            checks = reason.get("checks")
            if isinstance(checks, list):
                for c in checks:
                    if isinstance(c, dict) and not c.get("passed"):
                        rule = str(c.get("rule") or "unknown")
                        failed_labels.append(
                            RULE_LABELS.get(rule, "其他(%s)" % rule))
        else:
            failed_labels.append("原因解析失败")
            summary = "reason_json 无法解析"
        if not failed_labels:
            failed_labels.append("无结构化原因")
            counter["无结构化原因"] += 1
        else:
            for label in failed_labels:
                counter[label] += 1
        detail_rows.append({
            "run_id": str(r.run_id),
            "plan_name": str(r.plan_name or ("plan_id=%s" % r.plan_id)),
            "失败规则": "、".join(failed_labels),
            "原因摘要": summary,
        })

    total = sum(counter.values())
    dist = pd.DataFrame([
        {"失败原因": label, "次数": count,
         "占比": "%.1f%%" % (100.0 * count / total)}
        for label, count in counter.most_common()
    ])

    detail = pd.DataFrame(detail_rows,
                          columns=["run_id", "plan_name", "失败规则", "原因摘要"])
    detail["_k"] = detail["run_id"].map(_run_sort_key)
    detail = (detail.sort_values(["_k", "plan_name"], kind="mergesort")
              .drop(columns="_k")
              .reset_index(drop=True))
    return overview, dist, detail


# ============================================================
# 输出与入口
# ============================================================

def _fmt_df(df):
    """DataFrame -> 控制台表格文本 (无索引)。"""
    return df.to_string(index=False)


def _print_section(title, text):
    print()
    print("=" * 72)
    print(title)
    print("=" * 72)
    print(text)


def main():
    parser = argparse.ArgumentParser(
        description="SupplyGuard 历史数据统计采集 (服务点需求趋势 / "
                    "物资缺口频次 / 候选方案失败原因分布)")
    parser.add_argument("--db", default=str(DEFAULT_DB),
                        help="数据库文件路径 (默认 backend/bb03.db)")
    parser.add_argument("--csv", default=None, metavar="DIR",
                        help="额外导出 CSV 到指定目录 (UTF-8 带 BOM, Excel 可直接打开)")
    args = parser.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        print("[错误] 数据库不存在: %s" % db_path, file=sys.stderr)
        sys.exit(2)

    conn = sqlite3.connect(str(db_path))
    try:
        trend_overview, trend_agg, trend_detail, trend_long = \
            collect_demand_trend(conn)
        gap_overview, gap_summary, gap_by_run = collect_gap_frequency(conn)
        plan_overview, plan_dist, plan_detail = collect_plan_fail_reasons(conn)
    except sqlite3.Error as exc:
        print("[错误] 数据库读取失败: %s" % exc, file=sys.stderr)
        sys.exit(1)
    finally:
        conn.close()

    # ---- 数据源概要 ----
    print("=" * 72)
    print("SupplyGuard 历史统计 (数据库: %s)" % db_path)
    print("=" * 72)
    print("数据源概要:")
    print("  run 总数: %d | 快照可解析: %d | 跳过(快照缺失/损坏): %d%s"
          % (trend_overview["run_total"], trend_overview["run_parsed"],
             len(trend_overview["run_skipped"]),
             (" (%s)" % ", ".join(trend_overview["run_skipped"])
              if trend_overview["run_skipped"] else "")))
    print("  需求明细行数: %d" % trend_overview["demand_rows"])
    print("  blocker 缺口记录总数: %d (涉及 run %d 个, 服务点 %d 个)"
          % (gap_overview["blocker_total"], gap_overview["run_count"],
             gap_overview["service_count"]))
    print("  plan_results 判定总数: %d (可执行 %d, 不可执行 %d)"
          % (plan_overview["plan_result_total"],
             plan_overview["executable"], plan_overview["non_executable"]))

    # ---- 统计1: 服务点需求趋势 ----
    if trend_agg is None:
        _print_section("[统计1] 服务点需求趋势",
                       "(无可用需求快照数据: 所有 run 的 input_snapshot_json "
                       "缺失或无 demands 字段)")
    else:
        _print_section("[统计1] 服务点需求趋势"
                       "\n口径: 逐 run 解析 input_snapshot_json.demands, "
                       "快照时间取 snapshot_at (缺失用 run created_at); "
                       "数值为该服务点当次快照全部物资需求之和。",
                       "各服务点需求总量趋势 (run x 服务点):\n%s"
                       % _fmt_df(trend_agg))
        _print_section("[统计1 明细] 各服务点分物资需求趋势 (run x 服务点·物资)",
                       _fmt_df(trend_detail))

    # ---- 统计2: 物资缺口频次 ----
    if gap_summary is None:
        _print_section("[统计2] 物资缺口频次",
                       "(blockers 表无缺口记录)")
    else:
        _print_section("[统计2] 物资缺口频次"
                       "\n口径: blockers 只存 gap>0 的行 (gap_check 板块1), "
                       "频次 = 缺口记录数, 另附涉及 run/服务点数与缺口量汇总。",
                       "按物资汇总:\n%s" % _fmt_df(gap_summary))
        _print_section("[统计2 明细] 各 run 分物资缺口合计",
                       _fmt_df(gap_by_run))

    # ---- 统计3: 候选方案失败原因分布 ----
    if plan_dist is None:
        _print_section("[统计3] 候选方案失败原因分布",
                       "(plan_results 表无非可执行记录)")
    else:
        _print_section("[统计3] 候选方案失败原因分布"
                       "\n口径: status='non_executable' 的 plan_results, "
                       "按 reason_json.checks 中 passed=false 的 rule 归类; "
                       "占比 = 该类失败次数 / 全部失败规则次数。",
                       "失败原因类别分布:\n%s" % _fmt_df(plan_dist))
        _print_section("[统计3 明细] 非可执行方案明细",
                       _fmt_df(plan_detail))

    # ---- CSV 导出 (可选) ----
    if args.csv:
        out_dir = Path(args.csv)
        out_dir.mkdir(parents=True, exist_ok=True)
        exports = []
        if trend_agg is not None:
            exports.append(("stats1_demand_trend_by_service.csv", trend_agg))
            exports.append(("stats1_demand_trend_by_material.csv", trend_detail))
            exports.append(("stats1_demand_trend_long.csv", trend_long))
        if gap_summary is not None:
            exports.append(("stats2_gap_frequency.csv", gap_summary))
            exports.append(("stats2_gap_by_run.csv", gap_by_run))
        if plan_dist is not None:
            exports.append(("stats3_plan_fail_reason_dist.csv", plan_dist))
            exports.append(("stats3_plan_fail_detail.csv", plan_detail))
        for name, df in exports:
            path = out_dir / name
            df.to_csv(path, index=False, encoding="utf-8-sig")
        print()
        print("[CSV] 已导出 %d 个文件到 %s (UTF-8 带 BOM):"
              % (len(exports), out_dir))
        for name, _ in exports:
            print("  - %s" % name)


if __name__ == "__main__":
    main()
