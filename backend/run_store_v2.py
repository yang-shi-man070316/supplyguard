"""阶段03板块2: run 生命周期数据层 (新 runs 表, 板块1文档字段).

职责:
    - create_run: 自动生成 BB03-RUN-xxx, 抓取当前时刻快照并冻结入库
    - list_runs / get_run: run 查询
不做任何 T1~T5 验算逻辑 (板块2 禁止)。

快照冻结规则:
    - input_snapshot_json 在 create 时一次性写入「当前时刻」数据:
      服务点 / 物资 / 需求(每个服务点×物资最新一条) / 接收(全部签收记录)
      / 库存(每个储备点×物资最新一条) / 运输(每个运输资源最新一条)
      (板块4 起扩展库存/运输与各条目 created_at, 供 T2~T5 只读校验)
    - assumptions_json 初始为空 JSON 对象 {}
    - run_status=pending; created_at 由表默认填充
    - completed 之后快照禁止修改、禁止覆盖: 由板块1触发器
      trg_runs_completed_snapshot_immutable 强制, 本层不提供任何更新接口。
"""

import json
import sqlite3
from datetime import datetime

from constants import RUN_TYPE
from db import close_conn, get_conn, next_run_id

_RUN_ID_RETRY = 3  # run_id 撞主键 (并发创建) 时的重试次数


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _row_to_dict(row) -> dict:
    """run 行转 dict; input_snapshot_json / assumptions_json 解析为对象。"""
    d = dict(row)
    for key in ("input_snapshot_json", "assumptions_json"):
        if d.get(key):
            try:
                d[key] = json.loads(d[key])
            except json.JSONDecodeError:
                pass  # 防御脏数据: 保留原文
    return d


def _capture_snapshot(conn) -> dict:
    """抓取当前时刻快照 (与创建 run 同连接同事务, 保证口径一致)。

    内容 (板块4 起扩展为全量观测, 供 T1~T5 只读快照校验):
        services    当前出现过的服务点列表 (需求/接收记录 distinct)
        materials   当前出现过的物资列表 (需求/接收/库存 distinct)
        demands    每个 (服务点, 物资) 的最新一条需求观测 (含 created_at)
        receipts   全部接收(签收)记录 (追加型数据, 全量; 含 created_at)
        stocks     每个 (储备点, 物资) 的最新一条库存观测 (含 created_at)
        transports 每个运输资源的最新一条观测 (含 created_at)
    各条目均带 created_at (落库时间), 供 T5 时间线倒挂校验。
    """
    services = [r[0] for r in conn.execute(
        "SELECT DISTINCT service_id FROM demand_records "
        "UNION SELECT DISTINCT service_id FROM receipt_records_v2 ORDER BY 1"
    ).fetchall()]
    materials = [r[0] for r in conn.execute(
        "SELECT DISTINCT material_id FROM demand_records "
        "UNION SELECT DISTINCT material_id FROM receipt_records_v2 "
        "UNION SELECT DISTINCT material_id FROM stock_observations ORDER BY 1"
    ).fetchall()]
    demands = [dict(r) for r in conn.execute(
        "SELECT service_id, material_id, required_quantity, observed_at, created_at "
        "FROM demand_records d "
        "WHERE d.demand_id = ("
        "  SELECT MAX(d2.demand_id) FROM demand_records d2 "
        "  WHERE d2.service_id = d.service_id AND d2.material_id = d.material_id)"
        "ORDER BY service_id, material_id"
    ).fetchall()]
    receipts = [dict(r) for r in conn.execute(
        "SELECT receipt_id, dispatch_id, service_id, material_id, "
        "received_quantity, observed_at, created_at "
        "FROM receipt_records_v2 ORDER BY observed_at, id"
    ).fetchall()]
    stocks = [dict(r) for r in conn.execute(
        "SELECT reserve_id, material_id, quantity, observed_at, created_at "
        "FROM stock_observations s "
        "WHERE s.stock_id = ("
        "  SELECT MAX(s2.stock_id) FROM stock_observations s2 "
        "  WHERE s2.reserve_id = s.reserve_id AND s2.material_id = s.material_id)"
        "ORDER BY reserve_id, material_id"
    ).fetchall()]
    transports = [dict(r) for r in conn.execute(
        "SELECT transport_id, availability, capacity, observed_at, created_at "
        "FROM transport_observations t "
        "WHERE t.observation_id = ("
        "  SELECT MAX(t2.observation_id) FROM transport_observations t2 "
        "  WHERE t2.transport_id = t.transport_id)"
        "ORDER BY transport_id"
    ).fetchall()]
    return {
        "snapshot_at": _now(),
        "services": services,
        "materials": materials,
        "demands": demands,
        "receipts": receipts,
        "stocks": stocks,
        "transports": transports,
    }


def create_run(scenario_id: str, parent_run_id: str = None,
               run_type: str = RUN_TYPE) -> tuple:
    """新建 reality run 并冻结当前快照。

    - run_id 自动生成 (BB03-RUN-001/002/... 自增, next_run_id);
      并发创建撞主键时重试 (_RUN_ID_RETRY 次)。
    - input_snapshot_json = 当前时刻快照 (见 _capture_snapshot);
      assumptions_json = {} (空 JSON 对象, 非 NULL)。
    - run_status 默认 pending; parent_run_id 默认 NULL; created_at 表默认填充。
    - 入参非法抛 ValueError (接口层转 400)。

    返回 (ok, msg, data); 成功时 data 为完整 run 记录 (JSON 列已解析)。
    """
    if scenario_id is None or not str(scenario_id).strip():
        raise ValueError("scenario_id required")
    if run_type != RUN_TYPE:
        raise ValueError("only run_type=%r supported" % RUN_TYPE)
    if parent_run_id is not None and not str(parent_run_id).strip():
        parent_run_id = None  # 空串按未传处理 (默认 null)
    scenario_id = str(scenario_id).strip()

    last_err = None
    for _ in range(_RUN_ID_RETRY):
        conn = get_conn()
        try:
            run_id = next_run_id(conn)
            snapshot = _capture_snapshot(conn)
            conn.execute(
                "INSERT INTO runs "
                "(run_id, scenario_id, run_type, parent_run_id, run_status, "
                "input_snapshot_json, assumptions_json) VALUES (?,?,?,?,?,?,?)",
                (run_id, scenario_id, run_type, parent_run_id, "pending",
                 json.dumps(snapshot, ensure_ascii=False), "{}"),
            )
            row = conn.execute(
                "SELECT * FROM runs WHERE run_id=?", (run_id,)
            ).fetchone()
            conn.commit()
            return True, "created", _row_to_dict(row)
        except sqlite3.IntegrityError as exc:
            last_err = exc
            conn.rollback()  # 撞主键: 换下一个 run_id 重试
        finally:
            close_conn(conn)
    return False, "run_id generation failed after %d retries: %s" % (
        _RUN_ID_RETRY, last_err), None


def list_runs() -> list:
    """run 列表 (只含轻量字段, 不含快照), 按 created_at 倒序 (新在前)。

    同秒创建用 run_id 倒序兜底, 保证结果稳定。
    """
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT run_id, scenario_id, run_type, parent_run_id, run_status, "
            "created_at, completed_at FROM runs "
            "ORDER BY created_at DESC, run_id DESC"
        ).fetchall()
    finally:
        close_conn(conn)
    return [dict(r) for r in rows]


def get_run(run_id) -> dict:
    """单 run 完整详情 (含 input_snapshot_json / assumptions_json, 已解析)。

    任意状态均可读; 不存在返回 None。
    """
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM runs WHERE run_id=?", (run_id,)
        ).fetchone()
    finally:
        close_conn(conn)
    return _row_to_dict(row) if row is not None else None
