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
from datetime import datetime

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
