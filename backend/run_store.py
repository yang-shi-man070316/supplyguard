"""B-B03 独立任务: run 记录保护.

run 存在状态字段 (pending / completed)。核心规则:
    - 查询: 任意状态的 run 均可读
    - 更新: 仅 pending 允许; completed 的 run 拒绝任何修改, 数据库不改动
    - 新增: 不受限制 (默认 pending)

防竞态要点: 更新/完成语句自带 status='pending' 条件, 状态校验与写库在
同一原子 UPDATE 内完成——即使"先查"与"写"之间 run 被 complete, rowcount=0
同样拒绝, 不存在"查了 pending 却覆盖 completed"的窗口。
"""

import json
import sqlite3
import uuid
from datetime import datetime, timedelta

from db import close_conn, get_conn


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _row_to_dict(row) -> dict:
    d = dict(row)
    if d.get("payload"):
        try:
            d["payload"] = json.loads(d["payload"])
        except json.JSONDecodeError:
            pass  # 保留原文
    return d


def create_run(run_id: str, payload: dict = None) -> tuple:
    """新增 run (status=pending)。run_id 已存在则拒绝, 不覆盖。"""
    try:
        conn = get_conn()
        try:
            conn.execute(
                "INSERT INTO runs (run_id, status, payload, created_at) VALUES (?,?,?,?)",
                (run_id, "pending",
                 json.dumps(payload, ensure_ascii=False) if payload is not None else None,
                 _now()),
            )
            conn.commit()
        finally:
            close_conn(conn)
    except sqlite3.IntegrityError:
        return False, "run %s already exists" % run_id
    except sqlite3.Error as exc:
        return False, "db error: %s" % exc
    return True, "created"


def get_run(run_id: str):
    """查询单个 run (任意状态均可读)。不存在返回 None。"""
    conn = get_conn()
    try:
        row = conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
    finally:
        close_conn(conn)
    return _row_to_dict(row) if row is not None else None


def list_runs() -> list:
    """全部 run 列表 (任意状态均可读), 按 created_at 倒序 (新在前)。

    同秒创建的 run 用 rowid 倒序兜底, 保证结果稳定。
    """
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM runs ORDER BY created_at DESC, rowid DESC"
        ).fetchall()
    finally:
        close_conn(conn)
    return [_row_to_dict(r) for r in rows]


def update_run(run_id: str, payload: dict) -> tuple:
    """更新 run 内容: 仅 pending 允许; completed 拒绝且不改动数据库。"""
    try:
        conn = get_conn()
        try:
            row = conn.execute("SELECT status FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if row is None:
                return False, "run %s not found" % run_id
            if row["status"] == "completed":
                return False, "run %s is completed, not modifiable" % run_id
            cur = conn.execute(
                "UPDATE runs SET payload=?, updated_at=? WHERE run_id=? AND status='pending'",
                (json.dumps(payload, ensure_ascii=False), _now(), run_id),
            )
            if cur.rowcount == 0:  # 查与写之间被 complete 的竞态兜底
                return False, "run %s is completed, not modifiable" % run_id
            conn.commit()
        finally:
            close_conn(conn)
    except sqlite3.Error as exc:
        return False, "db error: %s" % exc
    return True, "updated"


def complete_run(run_id: str) -> tuple:
    """pending → completed 状态转换; 已 completed 或不存在均拒绝。"""
    try:
        conn = get_conn()
        try:
            cur = conn.execute(
                "UPDATE runs SET status='completed', updated_at=? "
                "WHERE run_id=? AND status='pending'",
                (_now(), run_id),
            )
            conn.commit()
        finally:
            close_conn(conn)
    except sqlite3.Error as exc:
        return False, "db error: %s" % exc
    if cur.rowcount == 0:
        if get_run(run_id) is None:
            return False, "run %s not found" % run_id
        return False, "run %s is completed, not modifiable" % run_id
    return True, "completed"


# ============================================================
# B-B03 阶段03板块3: 通用依赖校验框架 + T1 (仅 T1, 禁 T2~T5)
# 框架: seed 元数据 → 读证据快照 → 逐依赖判断 → 写 dependency_checks
#       → 汇总写 task_results。依赖判断函数只实现 T1-D1。
# ============================================================

T1_TASK_ID = "T1"
T1_TASK_NAME = "关键服务点最低需求保障"
T1_TASK_DESC = "全部关键服务点当期最低需求均可被当前库存保障"
T1_DEPS = (
    {
        "dep_id": "T1-D1",
        "dep_name": "关键节点库存≥当期最低需求",
        "dep_desc": "每个服务点每种物资的当期最低需求 ≤ 当前全网该物资库存观测总量",
    },
)


def seed_t1_meta() -> tuple:
    """插入 T1 任务与依赖定义。幂等: 重复执行不重复插入、不覆盖已有行。"""
    try:
        conn = get_conn()
        try:
            conn.execute(
                "INSERT OR IGNORE INTO task_definitions "
                "(task_id, task_name, task_desc, created_at) VALUES (?,?,?,?)",
                (T1_TASK_ID, T1_TASK_NAME, T1_TASK_DESC, _now()),
            )
            for dep in T1_DEPS:
                conn.execute(
                    "INSERT OR IGNORE INTO dependency_definitions "
                    "(dep_id, task_id, dep_name, dep_desc, created_at) VALUES (?,?,?,?,?)",
                    (dep["dep_id"], T1_TASK_ID, dep["dep_name"], dep["dep_desc"], _now()),
                )
            conn.commit()
        finally:
            close_conn(conn)
    except sqlite3.Error as exc:
        return False, "db error: %s" % exc
    return True, "seeded"


def _latest_demands(conn) -> list:
    """每个 (service_id, material_id) 的最新一条需求观测 (按最大 demand_id)。"""
    rows = conn.execute(
        "SELECT service_id, material_id, required_quantity, observed_at "
        "FROM demand_records d "
        "WHERE d.demand_id = ("
        "  SELECT MAX(d2.demand_id) FROM demand_records d2 "
        "  WHERE d2.service_id = d.service_id AND d2.material_id = d.material_id)"
    ).fetchall()
    return [dict(r) for r in rows]


def _latest_stock_totals(conn) -> dict:
    """每个 (reserve_id, material_id) 最新库存观测, 按物资全网求和。"""
    rows = conn.execute(
        "SELECT material_id, quantity FROM stock_observations s "
        "WHERE s.stock_id = ("
        "  SELECT MAX(s2.stock_id) FROM stock_observations s2 "
        "  WHERE s2.reserve_id = s.reserve_id AND s2.material_id = s.material_id)"
    ).fetchall()
    totals = {}
    for r in rows:
        totals[r["material_id"]] = totals.get(r["material_id"], 0) + r["quantity"]
    return totals


def _check_dep_t1d1(src) -> tuple:
    """T1-D1 判断: 每个服务点每种物资的当期需求 ≤ 全网该物资库存。

    src 为证据源: sqlite3.Connection (reality, 读证据表, 行为不变) 或
    _ViewEvidence (阶段04板块2 what-if, 读合成视图)。
    返回 (status, msg), status ∈ satisfied / unsatisfied / unknown。
    无任何需求观测判 unknown; 某物资无库存观测按库存 0 计 (与阶段02
    缺口语义一致: 缺口 = 需求 − 全网该物资库存)。
    """
    demands = _src_latest_demands(src)
    if not demands:
        return "unknown", "无服务点需求观测记录, 无法判断"
    stocks = _src_latest_stock_totals(src)
    shortfalls = []
    ok_count = 0
    for d in demands:
        total = stocks.get(d["material_id"], 0)
        if total >= d["required_quantity"]:
            ok_count += 1
        else:
            shortfalls.append(
                "%s %s 缺口%d (需求%d, 库存%d)"
                % (d["service_id"], d["material_id"],
                   d["required_quantity"] - total, d["required_quantity"], total)
            )
    if shortfalls:
        return "unsatisfied", "; ".join(shortfalls)
    return "satisfied", "全部%d项需求均满足 (关键节点库存≥当期最低需求)" % ok_count


# 依赖校验分发表: dep_id → 判断函数。T2~T5 的依赖不在此实现。
_T1_DEP_CHECKERS = {
    "T1-D1": _check_dep_t1d1,
}


def run_t1_check(run_id: str) -> tuple:
    """执行 T1 全部依赖校验, 结果落库 dependency_checks + task_results。

    - 仅 status=pending 的 run 允许执行; completed / 不存在直接拒绝。
    - 落库用 INSERT...SELECT FROM runs WHERE status='pending' 原子控制:
      预检与写库之间 run 被 complete 时 rowcount=0, 整体 rollback, 不写任何数据。
    - 同一 run 重复执行时覆盖旧结果 (每 run 每依赖/每任务只保留最新一次)。

    返回 (ok, msg, data); data = {task_id, task_status, deps:[{dep_id,status,check_msg}]}
    """
    seed_t1_meta()  # 幂等保底: 元数据缺失时补种
    try:
        conn = get_conn()
        try:
            row = conn.execute(
                "SELECT status FROM runs WHERE run_id=?", (run_id,)
            ).fetchone()
            if row is None:
                return False, "run %s not found" % run_id, None
            if row["status"] != "pending":
                return False, "run %s is completed, not modifiable" % run_id, None

            deps = conn.execute(
                "SELECT dep_id FROM dependency_definitions WHERE task_id=? ORDER BY dep_id",
                (T1_TASK_ID,),
            ).fetchall()
            if not deps:
                return False, "task %s metadata missing" % T1_TASK_ID, None

            dep_results = []
            for d in deps:
                dep_id = d["dep_id"]
                checker = _T1_DEP_CHECKERS.get(dep_id)
                if checker is None:
                    dep_results.append({
                        "dep_id": dep_id, "status": "unknown",
                        "check_msg": "依赖 %s 的判断逻辑未实现" % dep_id,
                    })
                else:
                    status, check_msg = checker(conn)
                    dep_results.append(
                        {"dep_id": dep_id, "status": status, "check_msg": check_msg}
                    )

            if any(r["status"] == "unsatisfied" for r in dep_results):
                task_status = "blocked"
            elif all(r["status"] == "satisfied" for r in dep_results):
                task_status = "established"
            else:
                task_status = "unknown"
            task_msg = "; ".join(
                "%s:%s" % (r["dep_id"], r["status"]) for r in dep_results
            )

            # ---- 原子落库: 写库瞬间要求 run 仍为 pending, 否则整体回滚 ----
            new_check_ids = []  # (dep_id, 新行 check_id), 用于覆盖旧结果
            for r in dep_results:
                cur = conn.execute(
                    "INSERT INTO dependency_checks "
                    "(run_id, dep_id, status, check_msg, created_at) "
                    "SELECT ?,?,?,?,? FROM runs WHERE run_id=? AND status='pending'",
                    (run_id, r["dep_id"], r["status"], r["check_msg"], _now(), run_id),
                )
                if cur.rowcount == 0:
                    conn.rollback()
                    return False, "run %s is completed, not modifiable" % run_id, None
                new_check_ids.append((r["dep_id"], cur.lastrowid))
            cur_results = conn.execute(
                "INSERT INTO task_results (run_id, task_id, status, result_msg, created_at) "
                "SELECT ?,?,?,?,? FROM runs WHERE run_id=? AND status='pending'",
                (run_id, T1_TASK_ID, task_status, task_msg, _now(), run_id),
            )
            if cur_results.rowcount == 0:
                conn.rollback()
                return False, "run %s is completed, not modifiable" % run_id, None
            # 覆盖旧结果: 同一 run 的同一依赖/任务只保留本次新写入的行
            for dep_id, check_id in new_check_ids:
                conn.execute(
                    "DELETE FROM dependency_checks "
                    "WHERE run_id=? AND dep_id=? AND check_id<?",
                    (run_id, dep_id, check_id),
                )
            conn.execute(
                "DELETE FROM task_results WHERE run_id=? AND task_id=? AND result_id<?",
                (run_id, T1_TASK_ID, cur_results.lastrowid),
            )
            conn.commit()
        finally:
            close_conn(conn)
    except sqlite3.Error as exc:
        return False, "db error: %s" % exc, None
    return True, "checked", {
        "run_id": run_id,
        "task_id": T1_TASK_ID,
        "task_status": task_status,
        "deps": dep_results,
    }


# ============================================================
# B-B03 阶段03板块4: T2~T5 依赖判断 + 全套校验执行器
# (板块3的 T1 代码一律不动; 本块复用 _check_dep_t1d1/_latest_demands
#  与板块3的原子落库模式。禁止 what-if / plan 推演逻辑。)
# ============================================================

T2T5_TASKS = (
    {
        "task_id": "T2",
        "task_name": "运输链路可达保障",
        "task_desc": "关键物资存在可用运输通道",
        "deps": (
            {
                "dep_id": "T2-D1",
                "dep_name": "物资存在可用运输通道",
                "dep_desc": "至少存在一个最新状态为 AVAILABLE/IN_TRANSIT 的运输资源",
            },
        ),
    },
    {
        "task_id": "T3",
        "task_name": "调度响应时效保障",
        "task_desc": "调度计划可以在需求时限前抵达",
        "deps": (
            {
                "dep_id": "T3-D1",
                "dep_name": "调度可在需求时限前送达",
                "dep_desc": "每项最新需求提出后存在运输/签收活动响应 "
                            "(库中无调度计划证据, 以现有观测做响应性代理判断)",
            },
        ),
    },
    {
        "task_id": "T4",
        "task_name": "入库签收合规保障",
        "task_desc": "在途物资签收记录可闭环核验",
        "deps": (
            {
                "dep_id": "T4-D1",
                "dep_name": "在途物资签收记录可闭环核验",
                "dep_desc": "每条签收记录可对应到同储备点同物资的库存观测; "
                            "有在途运输时必须有签收记录",
            },
        ),
    },
    {
        "task_id": "T5",
        "task_name": "证据一致性校验",
        "task_desc": "库存、运输、签收、需求观测之间数据不自相矛盾",
        "deps": (
            {
                "dep_id": "T5-D1",
                "dep_name": "多源观测数据无冲突",
                "dep_desc": "无负值矛盾、无非法状态值、观测时间不晚于落库时间(容差内)",
            },
        ),
    },
)


def seed_t2t5_meta() -> tuple:
    """插入 T2~T5 任务与依赖定义。幂等: 重复执行不重复插入、不覆盖已有行。"""
    try:
        conn = get_conn()
        try:
            for task in T2T5_TASKS:
                conn.execute(
                    "INSERT OR IGNORE INTO task_definitions "
                    "(task_id, task_name, task_desc, created_at) VALUES (?,?,?,?)",
                    (task["task_id"], task["task_name"], task["task_desc"], _now()),
                )
                for dep in task["deps"]:
                    conn.execute(
                        "INSERT OR IGNORE INTO dependency_definitions "
                        "(dep_id, task_id, dep_name, dep_desc, created_at) VALUES (?,?,?,?,?)",
                        (dep["dep_id"], task["task_id"], dep["dep_name"],
                         dep["dep_desc"], _now()),
                    )
            conn.commit()
        finally:
            close_conn(conn)
    except sqlite3.Error as exc:
        return False, "db error: %s" % exc
    return True, "seeded"


def _parse_ts(value):
    """解析观测时间字符串 (兼容 ISO 'T' 与空格分隔), 失败返回 None。"""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def _check_dep_t2d1(src) -> tuple:
    """T2-D1: 至少存在一个最新状态 AVAILABLE/IN_TRANSIT 的运输资源。

    src 同 _check_dep_t1d1 (连接或合成视图)。
    全部 FAULT → unsatisfied; 无运输观测 → unknown。
    """
    rows = _src_latest_transports(src)
    if not rows:
        return "unknown", "无运输资源观测记录, 无法判断"
    usable = [r["transport_id"] for r in rows
              if r["availability"] in ("AVAILABLE", "IN_TRANSIT")]
    faulty = [r["transport_id"] for r in rows if r["availability"] == "FAULT"]
    others = [r["transport_id"] for r in rows
              if r["availability"] not in ("AVAILABLE", "IN_TRANSIT", "FAULT")]
    if usable:
        msg = "存在可用运输通道: %s" % ", ".join(usable)
        if faulty:
            msg += "; 故障: %s" % ", ".join(faulty)
        return "satisfied", msg
    if faulty:
        return "unsatisfied", "全部运输资源均 FAULT, 无可用运输通道: %s" % ", ".join(faulty)
    return "unknown", "运输资源状态值无法识别: %s" % ", ".join(others)


def _check_dep_t3d1(src) -> tuple:
    """T3-D1: 每项最新需求提出后, 存在运输观测/签收活动响应。

    src 同 _check_dep_t1d1 (连接或合成视图)。库中无调度计划(dispatch)
    证据表且约束禁止新增表, 以"需求观测时间之后存在运输或签收活动"作为
    调度响应时效的代理判断; 需求时间不可解析判 unknown。
    """
    demands = _src_latest_demands(src)
    if not demands:
        return "unknown", "无服务点需求观测记录, 无法判断"
    activity_ts = []
    for ts in _src_transport_times(src):
        t = _parse_ts(ts)
        if t is not None:
            activity_ts.append(t)
    for ts in _src_receipt_times(src):
        t = _parse_ts(ts)
        if t is not None:
            activity_ts.append(t)
    unverified, lagging = [], []
    for d in demands:
        t = _parse_ts(d["observed_at"])
        if t is None:
            unverified.append("%s %s" % (d["service_id"], d["material_id"]))
            continue
        if not any(a >= t for a in activity_ts):
            lagging.append(
                "%s %s (需求 %s)" % (d["service_id"], d["material_id"], d["observed_at"])
            )
    if lagging:
        return "unsatisfied", ("以下需求提出后无任何运输/签收活动响应: "
                               + "; ".join(lagging))
    if unverified:
        return "unknown", "以下需求时间不可解析, 无法核验时效: " + "; ".join(unverified)
    return "satisfied", "全部%d项需求提出后均有运输/签收活动响应" % len(demands)


def _check_dep_t4d1(src) -> tuple:
    """T4-D1: 每条签收记录可对应到同储备点同物资的库存观测 (闭环核验);
    存在在途运输时必须有签收记录支撑。

    src 同 _check_dep_t1d1 (连接或合成视图); 合成视图不含签收证据,
    receipts 恒为空。
    """
    receipts = _src_receipts(src)
    in_transit = _src_in_transit_count(src)
    if not receipts:
        if in_transit:
            return "unsatisfied", ("存在%d个在途运输资源但无任何签收记录, "
                                   "无法闭环核验" % in_transit)
        return "unknown", "无签收记录且无在途运输, 依赖不适用"
    broken = [
        "%s (%s/%s)" % b
        for b in _src_receipt_stock_missing(src, receipts)
    ]
    if broken:
        return "unsatisfied", "签收记录无法与库存观测闭环核验: " + "; ".join(broken)
    return "satisfied", "全部%d条签收记录均可与库存观测闭环核验" % len(receipts)


_TIME_TOLERANCE = timedelta(seconds=60)  # 观测时间晚于落库时间的允许容差


def _check_dep_t5d1(src) -> tuple:
    """T5-D1: 多源观测一致性 — 负值矛盾 / 运输状态非法值 / 时间线倒挂。

    src 同 _check_dep_t1d1 (连接或合成视图); 合成视图无落库时间,
    时间线倒挂恒为 0。
    """
    conflicts = []
    total = 0
    for label, bad, count in _src_t5_negative_counts(src):
        total += count
        if bad:
            conflicts.append("%s观测存在%d条负值记录" % (label, bad))
    bad_states = _src_t5_bad_states(src)
    if bad_states:
        conflicts.append("运输状态非法值: %s" % ", ".join(bad_states))
    inverted = _src_t5_inverted(src)
    if inverted:
        conflicts.append("观测/签收时间晚于落库时间%d条 (超过容差)" % inverted)
    if total == 0 and not conflicts:
        return "unknown", "无任何观测证据, 无法校验一致性"
    if conflicts:
        return "unsatisfied", "; ".join(conflicts)
    return "satisfied", "库存/需求/运输/签收观测之间无矛盾"


_ALL_TASK_IDS = ("T1", "T2", "T3", "T4", "T5")

# 全任务依赖分发表: task_id -> {dep_id: 判断函数}。T1 复用板块3函数, 不改其代码。
_ALL_TASK_CHECKERS = {
    "T1": {"T1-D1": _check_dep_t1d1},
    "T2": {"T2-D1": _check_dep_t2d1},
    "T3": {"T3-D1": _check_dep_t3d1},
    "T4": {"T4-D1": _check_dep_t4d1},
    "T5": {"T5-D1": _check_dep_t5d1},
}


def run_all_tasks_check(run_id: str) -> tuple:
    """串行执行 T1~T5 全套依赖校验, 结果一次性落库 dependency_checks + task_results。

    - 仅 status=pending 的 run 允许执行; completed / 不存在直接拒绝。
    - 全部 checks/results 在同一事务内用 INSERT...SELECT FROM runs
      WHERE status='pending' 原子写入, 任一写入 rowcount=0 整体回滚, 不写任何数据。
    - 同 run 重复执行覆盖旧结果 (每 run 每依赖/每任务只保留最新一次)。

    返回 (ok, msg, data);
    data = {run_id, tasks:[{task_id, task_status, task_msg, deps:[...]}]}
    """
    seed_t1_meta()   # 幂等保底 (板块3函数, 不改动)
    seed_t2t5_meta()
    try:
        conn = get_conn()
        try:
            row = conn.execute(
                "SELECT status FROM runs WHERE run_id=?", (run_id,)
            ).fetchone()
            if row is None:
                return False, "run %s not found" % run_id, None
            if row["status"] != "pending":
                return False, "run %s is completed, not modifiable" % run_id, None

            # 汇总循环提取到阶段04板块2 _collect_tasks (reality/what-if 共用, 行为不变)
            tasks_out = _collect_tasks(conn, conn)

            # ---- 原子落库 (同一事务): 全部 dependency_checks + task_results ----
            new_check_ids, new_result_ids = [], []
            for t in tasks_out:
                for r in t["deps"]:
                    if r["dep_id"] == "-":  # 无依赖定义的占位行不落库
                        continue
                    cur = conn.execute(
                        "INSERT INTO dependency_checks "
                        "(run_id, dep_id, status, check_msg, created_at) "
                        "SELECT ?,?,?,?,? FROM runs WHERE run_id=? AND status='pending'",
                        (run_id, r["dep_id"], r["status"], r["check_msg"], _now(), run_id),
                    )
                    if cur.rowcount == 0:
                        conn.rollback()
                        return False, "run %s is completed, not modifiable" % run_id, None
                    new_check_ids.append((r["dep_id"], cur.lastrowid))
                cur = conn.execute(
                    "INSERT INTO task_results "
                    "(run_id, task_id, status, result_msg, created_at) "
                    "SELECT ?,?,?,?,? FROM runs WHERE run_id=? AND status='pending'",
                    (run_id, t["task_id"], t["task_status"], t["task_msg"], _now(), run_id),
                )
                if cur.rowcount == 0:
                    conn.rollback()
                    return False, "run %s is completed, not modifiable" % run_id, None
                new_result_ids.append((t["task_id"], cur.lastrowid))
            # 覆盖旧结果: 同一 run 的同一依赖/任务只保留本次新写入的行
            for dep_id, check_id in new_check_ids:
                conn.execute(
                    "DELETE FROM dependency_checks "
                    "WHERE run_id=? AND dep_id=? AND check_id<?",
                    (run_id, dep_id, check_id),
                )
            for task_id, result_id in new_result_ids:
                conn.execute(
                    "DELETE FROM task_results WHERE run_id=? AND task_id=? AND result_id<?",
                    (run_id, task_id, result_id),
                )
            conn.commit()
        finally:
            close_conn(conn)
    except sqlite3.Error as exc:
        return False, "db error: %s" % exc, None
    return True, "checked", {
        "run_id": run_id,
        "tasks": [
            {k: t[k] for k in ("task_id", "task_status", "task_msg", "deps")}
            for t in tasks_out
        ],
    }


# ============================================================
# B-B03 阶段04板块1: 假设变更叠加 + 内存临时证据视图合成
# 只读最新证据 → 内存叠加假设变更 → 返回合成视图。全程不写库、
# 不新增表、不调用 T1~T5 校验函数、不产生 task_results/dependency_checks。
# 取数口径与阶段03一致: 每个 (键) 取最大自增id的一条最新观测。
# ============================================================

_HYPO_TYPES = ("stock", "demand", "transport")
_TRANSPORT_STATES = ("AVAILABLE", "IN_TRANSIT", "FAULT")


def _latest_stocks_by_reserve(conn) -> list:
    """每个 (reserve_id, material_id) 的最新一条库存观测 (按最大 stock_id)。

    与板块3 _latest_stock_totals 的取数口径一致 (同键取最新一条),
    本函数保留储备点维度, 供假设按 (储备点, 物资) 叠加。
    """
    rows = conn.execute(
        "SELECT reserve_id, material_id, quantity, observed_at "
        "FROM stock_observations s "
        "WHERE s.stock_id = ("
        "  SELECT MAX(s2.stock_id) FROM stock_observations s2 "
        "  WHERE s2.reserve_id = s.reserve_id AND s2.material_id = s.material_id)"
    ).fetchall()
    return [dict(r) for r in rows]


def _latest_transports(conn) -> list:
    """每个 transport_id 的最新一条运输观测 (按最大 observation_id)。

    与板块4 T2-D1 的取数口径一致 (同键取最新一条状态)。
    """
    rows = conn.execute(
        "SELECT transport_id, availability, capacity, observed_at "
        "FROM transport_observations t "
        "WHERE t.observation_id = ("
        "  SELECT MAX(t2.observation_id) FROM transport_observations t2 "
        "  WHERE t2.transport_id = t.transport_id)"
    ).fetchall()
    return [dict(r) for r in rows]


def _parse_hypotheses(hypothesis_list: list) -> dict:
    """校验并解析假设列表, 得到各目标最终生效的变更 (后覆盖前, 不累计)。

    结构/取值非法抛 ValueError; 目标无观测基线不在此判错, 由
    build_hypothesis_view 以 0 基线合成并标记 hypothesized=true。
    返回:
        {"stock": {(reserve, material): delta},
         "demand": {(service, material): delta},
         "transport": {channel: status}}
    """
    if not isinstance(hypothesis_list, list):
        raise ValueError("hypothesis_list must be a list")
    effective = {t: {} for t in _HYPO_TYPES}
    for i, hypo in enumerate(hypothesis_list):
        if not isinstance(hypo, dict):
            raise ValueError("hypothesis #%d must be an object" % (i + 1))
        htype = hypo.get("type")
        if htype not in effective:
            raise ValueError(
                "hypothesis #%d: unknown type %r (expected stock/demand/transport)"
                % (i + 1, htype)
            )
        if htype == "transport":
            channel = hypo.get("channel")
            status = hypo.get("status")
            if channel is None or not str(channel).strip():
                raise ValueError("hypothesis #%d: transport requires channel" % (i + 1))
            if status not in _TRANSPORT_STATES:
                raise ValueError(
                    "hypothesis #%d: illegal status %r "
                    "(expected AVAILABLE/IN_TRANSIT/FAULT)" % (i + 1, status)
                )
            effective["transport"][str(channel)] = status
            continue
        key_field = "reserve" if htype == "stock" else "service"
        key = hypo.get(key_field)
        material = hypo.get("material")
        delta = hypo.get("delta")
        if key is None or not str(key).strip():
            raise ValueError(
                "hypothesis #%d: %s requires %s" % (i + 1, htype, key_field)
            )
        if material is None or not str(material).strip():
            raise ValueError("hypothesis #%d: %s requires material" % (i + 1, htype))
        if isinstance(delta, bool):
            raise ValueError("hypothesis #%d: delta must be an integer" % (i + 1))
        if isinstance(delta, float):
            if not delta.is_integer():
                raise ValueError("hypothesis #%d: delta must be an integer" % (i + 1))
            delta = int(delta)
        if not isinstance(delta, int):
            raise ValueError("hypothesis #%d: delta must be an integer" % (i + 1))
        # 同目标后出现者覆盖先出现者: 直接覆盖写, 不累计
        effective[htype][(str(key), str(material))] = delta
    return effective


def build_hypothesis_view(hypothesis_list: list) -> dict:
    """读取最新证据, 内存叠加假设变更, 返回合成视图。全程不写数据库。

    hypothesis_list 每条假设结构:
        {"type": "stock",     "reserve": .., "material": .., "delta": ±n}
            储备点+物资库存增量叠加
        {"type": "demand",    "service": .., "material": .., "delta": ±n}
            服务点+物资需求增量叠加
        {"type": "transport", "channel": .., "status": "AVAILABLE|IN_TRANSIT|FAULT"}
            通道状态覆盖
    规则:
        - 同目标 (stock: reserve+material / demand: service+material /
          transport: channel) 后出现的假设覆盖先出现的, 不累计
        - delta 支持正负; 结果为纯算术叠加, 不做下限截断 (负值如实呈现)
        - 目标无观测基线时以 0 基线合成新条目, 标记 hypothesized=true
    返回 {"stock_view": [...], "demand_view": [...], "transport_view": [...]},
    非法输入抛 ValueError (不产生任何数据变更), 库错误向上抛 sqlite3.Error。
    """
    effective = _parse_hypotheses(hypothesis_list)

    conn = get_conn()
    try:
        stock_rows = _latest_stocks_by_reserve(conn)
        demand_rows = _latest_demands(conn)     # 复用板块3函数, 口径一致
        transport_rows = _latest_transports(conn)
    finally:
        close_conn(conn)

    # ---- stock_view: 基线 + 各目标生效 delta ----
    stock_baseline = {(r["reserve_id"], r["material_id"]): r for r in stock_rows}
    overridden = set()
    stock_view = []
    for key, delta in effective["stock"].items():
        base = stock_baseline.get(key)
        overridden.add(key)
        stock_view.append({
            "reserve_id": key[0],
            "material_id": key[1],
            "quantity": (base["quantity"] + delta) if base else delta,
            "delta": delta,
            "observed_at": base["observed_at"] if base else None,
            "hypothesized": base is None,
        })
    for key, base in stock_baseline.items():
        if key not in overridden:
            stock_view.append({
                "reserve_id": key[0],
                "material_id": key[1],
                "quantity": base["quantity"],
                "delta": 0,
                "observed_at": base["observed_at"],
                "hypothesized": False,
            })
    stock_view.sort(key=lambda e: (e["reserve_id"], e["material_id"]))

    # ---- demand_view: 基线 + 各目标生效 delta ----
    demand_baseline = {(r["service_id"], r["material_id"]): r for r in demand_rows}
    overridden = set()
    demand_view = []
    for key, delta in effective["demand"].items():
        base = demand_baseline.get(key)
        overridden.add(key)
        demand_view.append({
            "service_id": key[0],
            "material_id": key[1],
            "required_quantity": (base["required_quantity"] + delta) if base else delta,
            "delta": delta,
            "observed_at": base["observed_at"] if base else None,
            "hypothesized": base is None,
        })
    for key, base in demand_baseline.items():
        if key not in overridden:
            demand_view.append({
                "service_id": key[0],
                "material_id": key[1],
                "required_quantity": base["required_quantity"],
                "delta": 0,
                "observed_at": base["observed_at"],
                "hypothesized": False,
            })
    demand_view.sort(key=lambda e: (e["service_id"], e["material_id"]))

    # ---- transport_view: 基线 + 各通道生效状态覆盖 ----
    transport_baseline = {r["transport_id"]: r for r in transport_rows}
    transport_view = []
    for channel, status in effective["transport"].items():
        base = transport_baseline.get(channel)
        if base is None:
            transport_view.append({
                "transport_id": channel,
                "availability": status,
                "baseline_availability": None,
                "capacity": None,
                "observed_at": None,
                "hypothesized": True,
            })
        else:
            transport_view.append({
                "transport_id": channel,
                "availability": status,
                "baseline_availability": base["availability"],
                "capacity": base["capacity"],
                "observed_at": base["observed_at"],
                "hypothesized": False,
            })
    for key, base in transport_baseline.items():
        if key not in effective["transport"]:
            transport_view.append({
                "transport_id": key,
                "availability": base["availability"],
                "baseline_availability": base["availability"],
                "capacity": base["capacity"],
                "observed_at": base["observed_at"],
                "hypothesized": False,
            })
    transport_view.sort(key=lambda e: e["transport_id"])

    return {
        "stock_view": stock_view,
        "demand_view": demand_view,
        "transport_view": transport_view,
    }


# ============================================================
# B-B03 阶段04板块2: 一次性 what-if 推演 (纯内存, 结果不落库)
#
# 复用方式 (硬性约束: 不复制 T1~T5 判断代码):
#   - 板块3/4 的 _check_dep_* 判断函数入参由 conn 泛化为 src:
#     证据源可以是 sqlite3.Connection (reality, 原行为不变) 或
#     _ViewEvidence (what-if, 读板块1合成视图), 取数经下方 _src_* 适配。
#   - run_all_tasks_check 的汇总循环提取为 _collect_tasks(src, conn),
#     reality 与 what-if 共用同一份任务/依赖汇总与状态聚合逻辑。
#   - 本板块不写任何表: 不落 dependency_checks/task_results, 不 seed 元数据。
# ============================================================

class _ViewEvidence:
    """合成视图证据源: 把 build_hypothesis_view 的视图包装成
    T1~T5 判断函数可读的统一 src (另一个合法 src 是 sqlite3.Connection)。
    """

    def __init__(self, view: dict):
        self.stock_view = view["stock_view"]
        self.demand_view = view["demand_view"]
        self.transport_view = view["transport_view"]


def _src_latest_demands(src) -> list:
    """每个 (service_id, material_id) 最新需求观测。连接走原查询, 视图直接给。"""
    if isinstance(src, _ViewEvidence):
        return src.demand_view
    return _latest_demands(src)


def _src_latest_stock_totals(src) -> dict:
    """每个物资全网库存合计 (最新观测)。连接走原查询; 视图按物资求和。"""
    if isinstance(src, _ViewEvidence):
        totals = {}
        for e in src.stock_view:
            totals[e["material_id"]] = totals.get(e["material_id"], 0) + e["quantity"]
        return totals
    return _latest_stock_totals(src)


def _src_latest_transports(src) -> list:
    """每个 transport_id 最新运输观测。连接复用板块1 _latest_transports, 视图直接给。"""
    if isinstance(src, _ViewEvidence):
        return src.transport_view
    return _latest_transports(src)


def _src_transport_times(src) -> list:
    """全部运输观测时间 (reality 含历史; 视图仅合成条目)。"""
    if isinstance(src, _ViewEvidence):
        return [e["observed_at"] for e in src.transport_view]
    return [r["observed_at"]
            for r in src.execute("SELECT observed_at FROM transport_observations")]


def _src_receipt_times(src) -> list:
    """全部签收时间。合成视图不含签收证据 → 恒空。"""
    if isinstance(src, _ViewEvidence):
        return []
    return [r["received_at"]
            for r in src.execute("SELECT received_at FROM receipt_records")]


def _src_receipts(src) -> list:
    """全部签收记录 (receipt_id/reserve_id/material_id)。视图不含签收 → 恒空。"""
    if isinstance(src, _ViewEvidence):
        return []
    rows = src.execute(
        "SELECT receipt_id, reserve_id, material_id FROM receipt_records"
    ).fetchall()
    return [dict(r) for r in rows]


def _src_in_transit_count(src) -> int:
    """最新状态为 IN_TRANSIT 的运输资源数 (原 T4-D1 口径)。"""
    if isinstance(src, _ViewEvidence):
        return sum(1 for e in src.transport_view
                   if e["availability"] == "IN_TRANSIT")
    return src.execute(
        "SELECT COUNT(*) c FROM transport_observations t "
        "WHERE t.observation_id = ("
        "  SELECT MAX(t2.observation_id) FROM transport_observations t2 "
        "  WHERE t2.transport_id = t.transport_id) AND t.availability='IN_TRANSIT'"
    ).fetchone()["c"]


def _src_receipt_stock_missing(src, receipts) -> list:
    """无法与库存观测闭环的签收记录 [(receipt_id, reserve_id, material_id)]。

    连接: 逐条查 stock_observations 存在性 (原 T4-D1 查询);
    视图: 以合成 stock_view 的 (reserve, material) 键集合核验
    (视图 receipts 恒空, 该分支为完备性兜底)。
    """
    if isinstance(src, _ViewEvidence):
        keys = {(e["reserve_id"], e["material_id"]) for e in src.stock_view}
        return [
            (r["receipt_id"], r["reserve_id"], r["material_id"])
            for r in receipts
            if (r["reserve_id"], r["material_id"]) not in keys
        ]
    out = []
    for r in receipts:
        hit = src.execute(
            "SELECT 1 FROM stock_observations "
            "WHERE reserve_id=? AND material_id=? LIMIT 1",
            (r["reserve_id"], r["material_id"]),
        ).fetchone()
        if hit is None:
            out.append((r["receipt_id"], r["reserve_id"], r["material_id"]))
    return out


_T5_TABLES = (
    ("stock_observations", "quantity", "库存"),
    ("demand_records", "required_quantity", "需求"),
    ("transport_observations", "capacity", "运输容量"),
)


def _src_t5_negative_counts(src) -> list:
    """三表负值统计 [(label, bad, total), ...] (原 T5-D1 计数查询口径)。"""
    if isinstance(src, _ViewEvidence):
        series = (
            ("库存", [e["quantity"] for e in src.stock_view]),
            ("需求", [e["required_quantity"] for e in src.demand_view]),
            ("运输容量", [e["capacity"] for e in src.transport_view]),
        )
        return [
            (label, sum(1 for v in vals if v is not None and v < 0), len(vals))
            for label, vals in series
        ]
    out = []
    for table, col, label in _T5_TABLES:
        bad = src.execute(
            "SELECT COUNT(*) c FROM %s WHERE %s IS NOT NULL AND %s < 0"
            % (table, col, col)
        ).fetchone()["c"]
        count = src.execute("SELECT COUNT(*) c FROM %s" % table).fetchone()["c"]
        out.append((label, bad, count))
    return out


def _src_t5_bad_states(src) -> list:
    """非法运输状态值列表 (原 T5-D1 DISTINCT 口径)。"""
    if isinstance(src, _ViewEvidence):
        return [e["availability"] for e in src.transport_view
                if e["availability"] not in _TRANSPORT_STATES]
    rows = src.execute(
        "SELECT DISTINCT availability FROM transport_observations "
        "WHERE availability NOT IN ('AVAILABLE','IN_TRANSIT','FAULT')"
    ).fetchall()
    return [r["availability"] for r in rows]


def _src_t5_inverted(src) -> int:
    """观测/签收时间晚于落库时间的行数。合成视图无落库时间 → 恒为 0。"""
    if isinstance(src, _ViewEvidence):
        return 0
    inverted = 0
    for table, obs_col in (
        ("stock_observations", "observed_at"),
        ("demand_records", "observed_at"),
        ("transport_observations", "observed_at"),
    ):
        for row in src.execute(
            "SELECT %s AS o, created_at AS c FROM %s" % (obs_col, table)
        ):
            o, c = _parse_ts(row["o"]), _parse_ts(row["c"])
            if o is not None and c is not None and o > c + _TIME_TOLERANCE:
                inverted += 1
    for row in src.execute(
        "SELECT received_at AS o, created_at AS c FROM receipt_records"
    ):
        o, c = _parse_ts(row["o"]), _parse_ts(row["c"])
        if o is not None and c is not None and o > c + _TIME_TOLERANCE:
            inverted += 1
    return inverted


def _collect_tasks(src, conn) -> list:
    """以 src 为证据源执行全套 T1~T5 判断并汇总 (原 run_all_tasks_check 逻辑提取)。

    conn 仅用于读取依赖元数据定义 (dependency_definitions), 不写任何表。
    状态聚合沿用约定: 依赖 unsatisfied/unknown/satisfied 按任一 unsatisfied
    → blocked / 全部 satisfied → established / 其余 unknown。
    返回 [{"task_id","task_status","task_msg","deps":[{"dep_id","status","check_msg"}]}]。
    """
    tasks_out = []
    for task_id in _ALL_TASK_IDS:
        dep_rows = conn.execute(
            "SELECT dep_id FROM dependency_definitions "
            "WHERE task_id=? ORDER BY dep_id",
            (task_id,),
        ).fetchall()
        dep_results = []
        for d in dep_rows:
            dep_id = d["dep_id"]
            checker = _ALL_TASK_CHECKERS.get(task_id, {}).get(dep_id)
            if checker is None:
                dep_results.append({
                    "dep_id": dep_id, "status": "unknown",
                    "check_msg": "依赖 %s 的判断逻辑未实现" % dep_id,
                })
            else:
                status, check_msg = checker(src)
                dep_results.append(
                    {"dep_id": dep_id, "status": status, "check_msg": check_msg}
                )
        if not dep_results:
            dep_results.append({
                "dep_id": "-", "status": "unknown",
                "check_msg": "任务 %s 无依赖定义" % task_id,
            })
        if any(r["status"] == "unsatisfied" for r in dep_results):
            task_status = "blocked"
        elif all(r["status"] == "satisfied" for r in dep_results):
            task_status = "established"
        else:
            task_status = "unknown"
        task_msg = "; ".join(
            "%s:%s" % (r["dep_id"], r["status"]) for r in dep_results
        )
        tasks_out.append({
            "task_id": task_id, "task_status": task_status,
            "task_msg": task_msg, "deps": dep_results,
        })
    return tasks_out


def run_whatif_once(run_id: str, hypothesis_list: list) -> tuple:
    """一次性 what-if 推演: 合成视图上跑 T1~T5 全套校验, 纯内存不落库。

    流程:
      ① run 状态校验: 不存在 → (False, "... not found") 404 语义;
         非 pending → (False, "... completed ...") 409 语义
      ② build_hypothesis_view 合成视图 (非法假设抛 ValueError, 接口层转 400)
      ③ 同一套 _check_dep_* 判断函数 + _collect_tasks, 证据源换成合成视图
      ④ 组装返回; 不写 dependency_checks/task_results, 不写任何表

    返回 (ok, msg, data); data 结构对齐 run_all_tasks_check:
        {run_id, tasks:[{task_id, task_status, task_msg, deps:[{dep_id,status,check_msg}]}]}
    """
    try:
        conn = get_conn()
        try:
            row = conn.execute(
                "SELECT status FROM runs WHERE run_id=?", (run_id,)
            ).fetchone()
            if row is None:
                return False, "run %s not found" % run_id, None
            if row["status"] != "pending":
                return False, "run %s is completed, not modifiable" % run_id, None

            view = build_hypothesis_view(hypothesis_list)
            tasks_out = _collect_tasks(_ViewEvidence(view), conn)
        finally:
            close_conn(conn)
    except sqlite3.Error as exc:
        return False, "db error: %s" % exc, None
    return True, "whatif evaluated", {
        "run_id": run_id,
        "tasks": [
            {k: t[k] for k in ("task_id", "task_status", "task_msg", "deps")}
            for t in tasks_out
        ],
    }


# ============================================================
# B-B03 阶段04板块3: 可复用 what-if 假设方案存储管理
# 只存配置 (hypothesis_list), 不存推演结果; 推演每次实时计算 (板块2)。
# 幂等规则:
#   - 同 scenario_id 重复保存 → 更新 (run_id/name/description/hypothesis_list)
#   - 同 run_id 下 name 已存在 (不同 scenario_id) → 同名覆盖 (旧行替换)
#   - 更新改名撞到同 run 其他方案名 → 拒绝 (409 语义)
# 本板块不校验 run 状态 (run 校验在 whatif 触发时执行, 见板块2)。
# ============================================================

def _scenario_row_to_dict(row) -> dict:
    d = dict(row)
    if d.get("hypothesis_list"):
        try:
            d["hypothesis_list"] = json.loads(d["hypothesis_list"])
        except json.JSONDecodeError:
            pass  # 保留原文 (防御脏数据)
    return d


def create_scenario(scenario_id, run_id, name, description=None,
                    hypothesis_list=None) -> tuple:
    """新增/更新假设方案 (幂等, 同名覆盖)。

    - scenario_id/run_id/name 必填非空, hypothesis_list 必须为对象列表,
      否则抛 ValueError (接口层转 400);
    - scenario_id 已存在 → 更新该行 (含 run_id/name);
    - 同 run_id 下 name 已被其他方案占用:
        新增路径 → 覆盖旧方案 (旧行删除, 新行插入);
        更新改名路径 → 拒绝, 返回 (False, "... already exists ...") 409 语义。
    created_at 记录最近一次保存时间。返回 (ok, msg), msg ∈ created/updated。
    """
    if scenario_id is None or not str(scenario_id).strip():
        raise ValueError("scenario_id required")
    if run_id is None or not str(run_id).strip():
        raise ValueError("run_id required")
    if name is None or not str(name).strip():
        raise ValueError("name required")
    if not isinstance(hypothesis_list, list) or any(
        not isinstance(h, dict) for h in hypothesis_list
    ):
        raise ValueError("hypothesis_list must be a list of objects")
    scenario_id, run_id, name = str(scenario_id), str(run_id), str(name)
    payload = json.dumps(hypothesis_list, ensure_ascii=False)
    try:
        conn = get_conn()
        try:
            existing = conn.execute(
                "SELECT 1 FROM hypothesis_scenario WHERE scenario_id=?",
                (scenario_id,),
            ).fetchone()
            if existing is not None:
                conn.execute(
                    "UPDATE hypothesis_scenario "
                    "SET run_id=?, name=?, description=?, hypothesis_list=?, created_at=? "
                    "WHERE scenario_id=?",
                    (run_id, name, description, payload, _now(), scenario_id),
                )
                conn.commit()
                return True, "updated"
            # 同名覆盖: 删除同 (run_id, name) 旧行后插入新行 (同一事务)
            conn.execute(
                "DELETE FROM hypothesis_scenario WHERE run_id=? AND name=?",
                (run_id, name),
            )
            conn.execute(
                "INSERT INTO hypothesis_scenario "
                "(scenario_id, run_id, name, description, hypothesis_list, created_at) "
                "VALUES (?,?,?,?,?,?)",
                (scenario_id, run_id, name, description, payload, _now()),
            )
            conn.commit()
        finally:
            close_conn(conn)
    except sqlite3.IntegrityError:
        return False, ("scenario name %s already exists under run %s"
                       % (name, run_id))
    except sqlite3.Error as exc:
        return False, "db error: %s" % exc
    return True, "created"


def list_scenarios(run_id) -> list:
    """列出该 run 下全部方案, created_at 倒序 (rowid 兜底, 结果稳定)。"""
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM hypothesis_scenario WHERE run_id=? "
            "ORDER BY created_at DESC, rowid DESC",
            (run_id,),
        ).fetchall()
    finally:
        close_conn(conn)
    return [_scenario_row_to_dict(r) for r in rows]


def get_scenario(scenario_id):
    """查询单条方案 (hypothesis_list 解析为列表)。不存在返回 None。"""
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM hypothesis_scenario WHERE scenario_id=?", (scenario_id,)
        ).fetchone()
    finally:
        close_conn(conn)
    return _scenario_row_to_dict(row) if row is not None else None


def delete_scenario(scenario_id) -> tuple:
    """删除方案。不存在返回 (False, 'scenario %s not found') 404 语义。"""
    try:
        conn = get_conn()
        try:
            cur = conn.execute(
                "DELETE FROM hypothesis_scenario WHERE scenario_id=?", (scenario_id,)
            )
            conn.commit()
        finally:
            close_conn(conn)
    except sqlite3.Error as exc:
        return False, "db error: %s" % exc
    if cur.rowcount == 0:
        return False, "scenario %s not found" % scenario_id
    return True, "deleted"


# ============================================================
# B-B03 阶段04板块4: what-if 推演快照归档 + 真实研判差异对比
# 快照只归档查阅: 保存推演当时的 hypothesis_list + result_json;
# 后续推演依然实时计算 (板块2), 绝不把快照 result_json 当输入。
# 对比只读 task_results / dependency_checks (最近一次 check-all)。
# ============================================================

def _snapshot_row_to_dict(row) -> dict:
    d = dict(row)
    for key in ("hypothesis_list", "result_json"):
        if d.get(key):
            try:
                d[key] = json.loads(d[key])
            except json.JSONDecodeError:
                pass  # 保留原文 (防御脏数据)
    return d


def save_whatif_snapshot(snapshot_id, run_id, scenario_id, name, description=None,
                         hypothesis_list=None, result_json=None) -> tuple:
    """保存 what-if 推演快照 (幂等, 同名覆盖)。

    - snapshot_id/run_id/name 必填非空; hypothesis_list 须为对象列表,
      result_json 须为对象 (全套 T1~T5 结果), 否则抛 ValueError (接口层 400);
    - 同 snapshot_id 再存 → 更新该行 (含 run_id/scenario_id/name);
    - 同 run_id 下 name 已被其他快照占用: 新增路径 → 覆盖旧快照;
      更新改名路径 → 拒绝 (409 语义)。
    created_at 为最近一次保存时间。返回 (ok, msg), msg ∈ created/updated。
    """
    if snapshot_id is None or not str(snapshot_id).strip():
        raise ValueError("snapshot_id required")
    if run_id is None or not str(run_id).strip():
        raise ValueError("run_id required")
    if name is None or not str(name).strip():
        raise ValueError("name required")
    if not isinstance(hypothesis_list, list) or any(
        not isinstance(h, dict) for h in hypothesis_list
    ):
        raise ValueError("hypothesis_list must be a list of objects")
    if not isinstance(result_json, dict):
        raise ValueError("result_json must be an object")
    snapshot_id, run_id, name = str(snapshot_id), str(run_id), str(name)
    hpayload = json.dumps(hypothesis_list, ensure_ascii=False)
    rpayload = json.dumps(result_json, ensure_ascii=False)
    try:
        conn = get_conn()
        try:
            existing = conn.execute(
                "SELECT 1 FROM hypothesis_result_snapshot WHERE snapshot_id=?",
                (snapshot_id,),
            ).fetchone()
            if existing is not None:
                conn.execute(
                    "UPDATE hypothesis_result_snapshot "
                    "SET run_id=?, scenario_id=?, name=?, description=?, "
                    "hypothesis_list=?, result_json=?, created_at=? "
                    "WHERE snapshot_id=?",
                    (run_id, scenario_id, name, description, hpayload, rpayload,
                     _now(), snapshot_id),
                )
                conn.commit()
                return True, "updated"
            # 同名覆盖: 删除同 (run_id, name) 旧行后插入新行 (同一事务)
            conn.execute(
                "DELETE FROM hypothesis_result_snapshot WHERE run_id=? AND name=?",
                (run_id, name),
            )
            conn.execute(
                "INSERT INTO hypothesis_result_snapshot "
                "(snapshot_id, run_id, scenario_id, name, description, "
                "hypothesis_list, result_json, created_at) VALUES (?,?,?,?,?,?,?,?)",
                (snapshot_id, run_id, scenario_id, name, description,
                 hpayload, rpayload, _now()),
            )
            conn.commit()
        finally:
            close_conn(conn)
    except sqlite3.IntegrityError:
        return False, "snapshot name %s already exists under run %s" % (name, run_id)
    except sqlite3.Error as exc:
        return False, "db error: %s" % exc
    return True, "created"


def list_whatif_snapshots(run_id) -> list:
    """列出该 run 下全部推演快照, created_at 倒序 (rowid 兜底, 结果稳定)。"""
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM hypothesis_result_snapshot WHERE run_id=? "
            "ORDER BY created_at DESC, rowid DESC",
            (run_id,),
        ).fetchall()
    finally:
        close_conn(conn)
    return [_snapshot_row_to_dict(r) for r in rows]


def get_whatif_snapshot(snapshot_id):
    """查询单条快照 (hypothesis_list/result_json 解析为对象)。不存在返回 None。"""
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM hypothesis_result_snapshot WHERE snapshot_id=?",
            (snapshot_id,),
        ).fetchone()
    finally:
        close_conn(conn)
    return _snapshot_row_to_dict(row) if row is not None else None


def delete_whatif_snapshot(snapshot_id) -> tuple:
    """删除快照。不存在返回 (False, 'snapshot %s not found') 404 语义。"""
    try:
        conn = get_conn()
        try:
            cur = conn.execute(
                "DELETE FROM hypothesis_result_snapshot WHERE snapshot_id=?",
                (snapshot_id,),
            )
            conn.commit()
        finally:
            close_conn(conn)
    except sqlite3.Error as exc:
        return False, "db error: %s" % exc
    if cur.rowcount == 0:
        return False, "snapshot %s not found" % snapshot_id
    return True, "deleted"


def _latest_reality_results(run_id) -> tuple:
    """该 run 最近一次 check-all 的 task_results / dependency_checks (只读)。

    与 run_all_tasks_check 覆盖语义一致 (每 task/dep 只保留最新一条),
    取 result_id / check_id 最大者。
    返回 ({task_id: {"status","result_msg"}}, {dep_id: {"status","check_msg"}},
          最新 task_results created_at 或 None)。
    """
    conn = get_conn()
    try:
        trows = conn.execute(
            "SELECT task_id, status, result_msg, created_at FROM task_results t "
            "WHERE t.run_id=? AND t.result_id = ("
            "  SELECT MAX(t2.result_id) FROM task_results t2 "
            "  WHERE t2.run_id = t.run_id AND t2.task_id = t.task_id)",
            (run_id,),
        ).fetchall()
        drows = conn.execute(
            "SELECT dep_id, status, check_msg FROM dependency_checks c "
            "WHERE c.run_id=? AND c.check_id = ("
            "  SELECT MAX(c2.check_id) FROM dependency_checks c2 "
            "  WHERE c2.run_id = c.run_id AND c2.dep_id = c.dep_id)",
            (run_id,),
        ).fetchall()
    finally:
        close_conn(conn)
    tasks = {r["task_id"]: {"status": r["status"], "result_msg": r["result_msg"]}
             for r in trows}
    deps = {r["dep_id"]: {"status": r["status"], "check_msg": r["check_msg"]}
            for r in drows}
    latest_ts = max((r["created_at"] for r in trows), default=None)
    return tasks, deps, latest_ts


def _diff_changed(reality_status, whatif_status):
    """状态对比: 两侧都有值才可比; reality 缺失返回 None (无基线可比)。"""
    if reality_status is None or whatif_status is None:
        return None
    return reality_status != whatif_status


def compare_reality_vs_whatif(run_id: str, snapshot_id: str) -> tuple:
    """快照(what-if 推演结果) vs 该 run 最近一次 check-all 真实研判, 差异报告。

    只读 task_results / dependency_checks (SELECT); 快照仅归档, 不参与
    后续推演计算。快照不存在或不属于该 run → (False, "... not found") 404 语义。

    返回 (ok, msg, data), data = {
        run_id, snapshot_id, snapshot_name, reality_available, reality_checked_at,
        comparison: [{task_id, reality_status, whatif_status, changed,
                      deps: [{dep_id, reality_status, whatif_status, changed}]}]
    }
    reality 无记录的 task/dep → reality_status=None, changed=None (无基线可比)。
    """
    snap = get_whatif_snapshot(snapshot_id)
    if snap is None:
        return False, "snapshot %s not found" % snapshot_id, None
    if snap["run_id"] != run_id:
        return False, "snapshot %s not found for run %s" % (snapshot_id, run_id), None

    reality_tasks, reality_deps, latest_ts = _latest_reality_results(run_id)

    # 快照中的 what-if 任务/依赖 (防御脏数据, 结构异常按缺省处理)
    result = snap["result_json"] or {}
    whatif_tasks = {}
    if isinstance(result, dict):
        for t in result.get("tasks") or []:
            if isinstance(t, dict):
                whatif_tasks[t.get("task_id")] = t

    # 依赖元数据: dep_id -> task_id (只读 dependency_definitions)
    conn = get_conn()
    try:
        meta_rows = conn.execute(
            "SELECT task_id, dep_id FROM dependency_definitions"
        ).fetchall()
    finally:
        close_conn(conn)
    task_deps = {}
    for r in meta_rows:
        task_deps.setdefault(r["task_id"], []).append(r["dep_id"])

    comparison = []
    for task_id in _ALL_TASK_IDS:
        rt = reality_tasks.get(task_id)
        wt = whatif_tasks.get(task_id)
        r_status = rt["status"] if rt else None
        w_status = wt.get("task_status") if wt else None

        dep_ids = set(task_deps.get(task_id, []))
        if wt:
            for d in wt.get("deps") or []:
                if isinstance(d, dict) and d.get("dep_id"):
                    dep_ids.add(d["dep_id"])
        dep_rows = []
        for dep_id in sorted(dep_ids):
            if dep_id == "-":  # 无依赖定义占位行不参与对比
                continue
            rd = reality_deps.get(dep_id)
            wd = next(
                (d for d in (wt.get("deps") or [])
                 if isinstance(d, dict) and d.get("dep_id") == dep_id),
                None,
            ) if wt else None
            r_dep_status = rd["status"] if rd else None
            w_dep_status = wd.get("status") if wd else None
            dep_rows.append({
                "dep_id": dep_id,
                "reality_status": r_dep_status,
                "whatif_status": w_dep_status,
                "changed": _diff_changed(r_dep_status, w_dep_status),
            })
        comparison.append({
            "task_id": task_id,
            "reality_status": r_status,
            "whatif_status": w_status,
            "changed": _diff_changed(r_status, w_status),
            "deps": dep_rows,
        })

    return True, "compared", {
        "run_id": run_id,
        "snapshot_id": snapshot_id,
        "snapshot_name": snap["name"],
        "reality_available": bool(reality_tasks),
        "reality_checked_at": latest_ts,
        "comparison": comparison,
    }


# ============================================================
# 阶段02板块4: 统一证据表 evidence_v2 (只新增, 不改动任何已有函数)
# 阶段06板块2: 新增 weather / rag 两种辅助证据类型 (只扩展枚举, 不改动
# 判定逻辑; T1~T5 校验不读 evidence_v2, 新增类型不影响任何判定结果)
# ============================================================
_ALLOWED_EVIDENCE_TYPES = {"stock", "demand", "dispatch", "receipt",
                           "transport", "weather", "rag"}
_ALLOWED_EVIDENCE_STATUS = {"ok", "unavailable", "error"}


def save_evidence(evidence_type, source_id, status, value_json, observed_at,
                  error_message):
    """写入一条统一证据到 evidence_v2 (只增不改, 禁止覆盖历史)。

    入参校验 (代码层, 不合法直接抛 ValueError, 不落库):
        evidence_type  ∈ {stock, demand, dispatch, receipt, transport,
                         weather, rag}   (weather/rag 为阶段06辅助证据)
        status         ∈ {ok, unavailable, error}
        source_id      非空 str (关联外部业务记录ID, 如 demand_id/dispatch_id/receipt_id)
        value_json     None 或 str (原始观测/原始报文 JSON 字符串; 获取失败传 None 存 NULL)
        observed_at    非空 str (业务事件发生的观测时间)
        error_message  status=ok 时固定存空字符串 ""; unavailable/error 必须为非空 str

    evidence_id 由 UUID 生成 (主键); created_at 由数据库默认 NOW() 填充。
    成功返回 (True, evidence_id); 数据库异常返回 (False, 错误信息), 不抛出。
    """
    if evidence_type not in _ALLOWED_EVIDENCE_TYPES:
        raise ValueError("invalid evidence_type: %r" % evidence_type)
    if status not in _ALLOWED_EVIDENCE_STATUS:
        raise ValueError("invalid status: %r" % status)
    if not isinstance(source_id, str) or not source_id:
        raise ValueError("source_id must be a non-empty str")
    if value_json is not None and not isinstance(value_json, str):
        raise ValueError("value_json must be str or None")
    if not isinstance(observed_at, str) or not observed_at:
        raise ValueError("observed_at must be a non-empty str")
    if status == "ok":
        error_message = ""  # 规则: status=ok 时固定为空字符串
    else:
        if not isinstance(error_message, str) or not error_message:
            raise ValueError(
                "error_message must be a non-empty str when status=%r" % status)

    evidence_id = str(uuid.uuid4())
    try:
        conn = get_conn()
        try:
            conn.execute(
                "INSERT INTO evidence_v2 "
                "(evidence_id, evidence_type, source_id, status, value_json, "
                "observed_at, error_message) VALUES (?,?,?,?,?,?,?)",
                (evidence_id, evidence_type, source_id, status,
                 value_json, observed_at, error_message),
            )
            conn.commit()
        finally:
            close_conn(conn)
    except sqlite3.Error as exc:
        return False, "db error: %s" % exc
    return True, evidence_id
