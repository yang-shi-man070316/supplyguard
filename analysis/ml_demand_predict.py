"""阶段06板块3: 轻量 ML 需求趋势预测 ml_demand_predict.

基于历史 run 演练数据训练轻量分类模型 (numpy 手写逻辑回归, 零新增依赖),
预测「下一窗口需求量趋势」高低分类。核心约束 (板块红线):
    - ML 输出仅作辅助证据: 只写 evidence_v2 (evidence_type='ml'),
      绝不读写 task_results / dependency_checks / runs 任何字段,
      绝不参与 T1~T5 判定 (判定只读观测表, 本模块产出无法影响判定)。
    - 输出格式约束 (测试文档核验): prediction.trend 仅 ∈ {"up","down"},
      probability ∈ [0,1], auxiliary_only 恒为 true。

建模口径:
    - 样本: 按快照时间排序的历史 run 相邻窗口对 (窗口i, 窗口i+1)
    - 特征 (窗口i): 总需求 / P1 P2 P3 各服务点需求 / W L R 各物资需求 /
      需求记录数 / 快照时刻(小时) / 距上一窗口小时数
    - 标签: 下一窗口总需求 > 当前窗口 → "up"(上升/高), 否则 "down"(下降/低)
    - 模型: 逻辑回归 (特征标准化 + 梯度下降, 确定性强, 轻量可解释)
    - 不足 2 个可解析 run → model_status='insufficient_data',
      trend 为 null (不臆造预测)
    注: 当前历史数据各窗口总需求恒定, 标签全为 "down", 模型退化为基准
    预测 (总是 down) —— 这是数据现状的诚实反映, 非错误。

CLI 用法: python analysis/ml_demand_predict.py
"""

import json
import sys
import sqlite3
from datetime import datetime
from pathlib import Path

import numpy as np

# 项目根加入 sys.path, 保证独立运行或被 backend/main.py import 时
# 都能解析 backend 包与 db 模块
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
for _p in (_PROJECT_ROOT, _PROJECT_ROOT / "backend"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

MODEL_NAME = "logistic_regression"
TREND_CLASSES = {"up": "上升(高)", "down": "下降(低)"}
EPOCHS = 800
LEARNING_RATE = 0.1

FEATURE_NAMES = ("total_demand", "svc_P1", "svc_P2", "svc_P3",
                 "mat_W", "mat_L", "mat_R", "record_count",
                 "hour_of_day", "gap_hours")


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _parse_ts(value):
    """解析时间字符串, 失败返回 None。"""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", ""))
    except (TypeError, ValueError):
        return None


def _read_run_windows(conn) -> tuple:
    """读取全部 run 并按快照时间排序, 返回 (windows, skipped)。

    windows = [{"run_id", "ts", "total_demand", "features"(dict)}...]
    快照缺失/损坏或无 demands 的 run 计入 skipped, 不参与建模。
    """
    rows = conn.execute(
        "SELECT run_id, created_at, input_snapshot_json FROM runs "
        "ORDER BY rowid").fetchall()
    windows, skipped = [], []
    for r in rows:
        try:
            snap = json.loads(r["input_snapshot_json"] or "")
        except (TypeError, ValueError):
            skipped.append(r["run_id"])
            continue
        demands = snap.get("demands") if isinstance(snap, dict) else None
        if not isinstance(demands, list):
            skipped.append(r["run_id"])
            continue
        ts = _parse_ts(snap.get("snapshot_at")) or _parse_ts(r["created_at"])
        svc, mat, total, n_records = {}, {}, 0, 0
        for d in demands:
            if not isinstance(d, dict):
                continue
            sid, mid = str(d.get("service_id") or ""), str(d.get("material_id") or "")
            q = d.get("required_quantity")
            if isinstance(q, bool) or not isinstance(q, (int, float)):
                continue
            q = int(q)
            svc[sid] = svc.get(sid, 0) + q
            mat[mid] = mat.get(mid, 0) + q
            total += q
            n_records += 1
        windows.append({
            "run_id": r["run_id"],
            "ts": ts,
            "total_demand": total,
            "svc": svc,
            "mat": mat,
            "n_records": n_records,
        })
    # 按时间排序; ts 为 None 的排最后 (防御)
    windows.sort(key=lambda w: (w["ts"] is None, w["ts"]))
    return windows, skipped


def _build_dataset(windows: list) -> tuple:
    """相邻窗口对 → (X, y)。返回 None 表示样本不足。

    X 行 = 当前窗口特征; y = 下一窗口总需求 > 当前窗口 → 1(up) 否则 0(down)。
    """
    if len(windows) < 2:
        return None
    X, y = [], []
    prev_ts = None
    for i in range(len(windows) - 1):
        w = windows[i]
        nxt = windows[i + 1]
        gap = 0.0
        if w["ts"] is not None and prev_ts is not None:
            gap = max(0.0, (w["ts"] - prev_ts).total_seconds() / 3600.0)
        hour = w["ts"].hour + w["ts"].minute / 60.0 if w["ts"] else 0.0
        features = [
            float(w["total_demand"]),
            float(w["svc"].get("P1", 0)), float(w["svc"].get("P2", 0)),
            float(w["svc"].get("P3", 0)),
            float(w["mat"].get("W", 0)), float(w["mat"].get("L", 0)),
            float(w["mat"].get("R", 0)),
            float(w["n_records"]),
            float(hour),
            float(gap),
        ]
        X.append(features)
        y.append(1 if nxt["total_demand"] > w["total_demand"] else 0)
        prev_ts = w["ts"]
    return np.asarray(X, dtype=float), np.asarray(y, dtype=float)


def _standardize(X: np.ndarray) -> np.ndarray:
    """列标准化 (std=0 的列归 0, 保证常量特征下模型可训练)。"""
    mean = X.mean(axis=0)
    std = X.std(axis=0)
    std[std == 0] = 1.0
    return (X - mean) / std


def _sigmoid(z: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-np.clip(z, -500.0, 500.0)))


def _train_logreg(X: np.ndarray, y: np.ndarray) -> tuple:
    """手写逻辑回归 (梯度下降, 确定性)。返回 (w, b)。"""
    n, d = X.shape
    w = np.zeros(d)
    b = 0.0
    for _ in range(EPOCHS):
        p = _sigmoid(X @ w + b)
        grad_w = X.T @ (p - y) / n
        grad_b = float(np.mean(p - y))
        w -= LEARNING_RATE * grad_w
        b -= LEARNING_RATE * grad_b
    return w, b


def predict_next_window(conn=None) -> dict:
    """读历史 run → 训练 → 预测下一窗口需求趋势 (纯计算, 不落库)。

    返回 dict (输出格式约束见模块 docstring 与测试文档)。
    """
    own = conn is None
    if own:
        from backend.db import close_conn, get_conn  # 懒加载 (同 weather/rag 模式)
        conn = get_conn()
    try:
        windows, skipped = _read_run_windows(conn)
    finally:
        if own:
            close_conn(conn)

    base = {
        "model": MODEL_NAME,
        "classes": TREND_CLASSES,
        "auxiliary_only": True,
        "observed_at": _now(),
        "skipped_runs": skipped,
    }
    dataset = _build_dataset(windows)
    if dataset is None:
        base["model_status"] = "insufficient_data"
        base["prediction"] = {
            "trend": None,
            "probability": None,
            "window": "下一窗口",
            "latest_run_id": windows[-1]["run_id"] if windows else None,
            "note": "历史可解析 run 不足 2 个, 无法训练, 不臆造预测",
        }
        return base

    X, y = dataset
    w, b = _train_logreg(_standardize(X), y)
    proba = _sigmoid(_standardize(X) @ w + b)
    pred_binary = (proba >= 0.5).astype(int)
    accuracy = float(np.mean(pred_binary == y))

    # 下一窗口特征 = 最新窗口特征 (常数假设: 最近观测是最佳预测基线)
    last = X[-1:]
    next_proba = float(_sigmoid(_standardize(last) @ w + b)[0])
    latest_run = windows[-1]["run_id"]
    base.update({
        "model_status": "trained",
        "training": {
            "samples": int(len(y)),
            "class_counts": {"up": int(np.sum(y == 1)),
                             "down": int(np.sum(y == 0))},
            "accuracy": round(accuracy, 4),
            "features": list(FEATURE_NAMES),
            "degenerate_features": bool(
                np.all(X.std(axis=0) == 0)),  # 全部特征无变化时为 true
        },
        "prediction": {
            "trend": "up" if next_proba >= 0.5 else "down",
            "probability": round(next_proba, 4),
            "window": "%s → 下一窗口" % latest_run,
            "latest_run_id": latest_run,
        },
    })
    return base


def run_ml_predict() -> tuple:
    """训练 + 预测 + 结果写入 evidence_v2 (辅助证据, 不触碰判定)。

    - 预测结果 (含模型状态) 写入 evidence_v2: evidence_type='ml',
      status='ok', value_json 为完整预测载荷
    - 数据库错误 → (False, 500, ...)
    返回 (ok, http_status, msg, data); data = 预测载荷 + evidence_id。
    """
    from backend.run_store import save_evidence  # 懒加载, 纯预测不依赖 DB

    try:
        result = predict_next_window()
    except sqlite3.Error as exc:
        return False, 500, "db error: %s" % exc, None
    evidence_id = None
    try:
        ok, evidence_id = save_evidence(
            evidence_type="ml",
            source_id="ml:demand-trend",
            status="ok",
            value_json=json.dumps(result, ensure_ascii=False),
            observed_at=result["observed_at"],
            error_message="",
        )
    except ValueError as exc:
        return False, 500, "ml evidence save failed: %s" % exc, None
    if not ok:
        return False, 500, "ml evidence save failed: %s" % evidence_id, None
    data = dict(result)
    data["evidence_id"] = evidence_id
    return True, 200, ("ml demand trend predicted (trend=%s)"
                       % data["prediction"]["trend"]), data


if __name__ == "__main__":
    ok, http_status, msg, data = run_ml_predict()
    print(json.dumps({"ok": ok, "http_status": http_status, "msg": msg,
                      "data": data}, ensure_ascii=False, indent=2))
    sys.exit(0 if ok else 1)
