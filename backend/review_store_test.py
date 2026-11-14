"""阶段05板块1: review_store 存储层自测脚本.

覆盖用例:
    1. 建表幂等 (init_review_tables 重复执行安全)
    2. create_review 创建 pending 复核单 (字段绑定/默认值校验)
    3. get_pending_reviews 查询全部 pending 单
    4. get_review_by_id 单条查询 + 不存在 ID → 404
    5. mark_review_completed 完成流转 + completed_at 回填
       + 重复标记幂等 (不覆盖原完成时间)
    6. 幂等拦截: 同 (dispatch_id, service_id, material_id) 重复创建
       → created=False 返回原单; 换 trigger_run_id 同样拦截
    7. 复核完成后同三元组可再次生成 (再次待复核语义)
    8. create_review 必填缺失 → 400; trigger_run_id 不存在 → 404
    9. mark_review_completed 不存在 ID → 404
   10. is_review_completed 工具函数三种取值
   11. 数据层兜底: 部分唯一索引拒绝重复 pending 直插;
       status CHECK 拒绝非法枚举

隔离说明: 将 db.DB_PATH 指向临时库, 全部测试数据只落在临时库,
不读写真实 bb03.db (import db 时对真实库的 init_db 为启动既有行为,
IF NOT EXISTS 幂等, 不产生/污染任何测试数据)。

用法 (backend 目录下):
    python review_store_test.py
退出码: 0 = 全部通过; 1 = 存在失败用例。
"""

import os
import shutil
import sqlite3
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import db  # noqa: E402  导入即执行 init_db() (对真实库幂等建表, 启动既有行为)
import review_store as rs  # noqa: E402

_PASS = 0
_FAIL = 0


def check(name, cond, detail=""):
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print("[PASS] %s" % name)
    else:
        _FAIL += 1
        print("[FAIL] %s %s" % (name, detail))


def expect(result, want_ok, want_status, name):
    ok, http_status, msg, data = result
    check(name, ok is want_ok and http_status == want_status,
          "got ok=%r http_status=%r msg=%r" % (ok, http_status, msg))
    return data


def main():
    # ---- 临时库隔离 ----
    tmp_dir = tempfile.mkdtemp(prefix="review_store_test_")
    db.DB_PATH = os.path.join(tmp_dir, "test.db")
    db.init_db()          # 临时库全量建表 (含 runs, 供 trigger_run_id 校验)
    rs.init_review_tables()
    print("test db: %s" % db.DB_PATH)

    # 种子: 两个测试 run
    conn = db.get_conn()
    conn.execute(
        "INSERT INTO runs (run_id, scenario_id) VALUES (?, 'S1')",
        ("BB03-RUN-901",))
    conn.execute(
        "INSERT INTO runs (run_id, scenario_id) VALUES (?, 'S1')",
        ("BB03-RUN-902",))
    conn.commit()
    db.close_conn(conn)

    try:
        # ---- 1. 建表幂等 ----
        rs.init_review_tables()
        conn = db.get_conn()
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='review_requests'").fetchone()
        db.close_conn(conn)
        check("建表幂等: review_requests 存在且重复执行安全", row is not None)

        # ---- 2. create_review 基础用例 ----
        data = expect(rs.create_review("DSP-100", "BB03-RUN-901", "P1", "W"),
                      True, 200, "create_review 创建成功")
        check("create_review created=True", data and data["created"] is True)
        rev1 = data["review"]["review_id"] if data else None
        check("review_id 规则 BB03-REV-*",
              isinstance(rev1, str) and rev1.startswith("BB03-REV-"),
              "got %r" % rev1)
        r = data["review"] if data else {}
        check("字段绑定: dispatch/trigger_run/service/material 与入参一致",
              r.get("dispatch_id") == "DSP-100"
              and r.get("trigger_run_id") == "BB03-RUN-901"
              and r.get("service_id") == "P1"
              and r.get("material_id") == "W",
              "got %r" % r)
        check("默认状态 pending, created_at 已填, completed_at 为 NULL",
              r.get("status") == "pending"
              and r.get("created_at")
              and r.get("completed_at") is None,
              "got status=%r created_at=%r completed_at=%r"
              % (r.get("status"), r.get("created_at"), r.get("completed_at")))

        # 第二条: 不同三元组
        data2 = expect(rs.create_review("DSP-200", "BB03-RUN-901", "P1", "W"),
                       True, 200, "create_review 不同 dispatch 创建成功")
        rev2 = data2["review"]["review_id"] if data2 else None
        check("第二个 review_id 自增且不同",
              rev2 and rev2.startswith("BB03-REV-") and rev2 != rev1,
              "rev1=%r rev2=%r" % (rev1, rev2))

        # ---- 3. get_pending_reviews ----
        data = expect(rs.get_pending_reviews(), True, 200,
                      "get_pending_reviews 查询成功")
        pending_ids = [x["review_id"] for x in (data or {}).get("reviews", [])]
        check("pending 列表含两条新单且全部 pending",
              data and data["review_count"] == 2
              and rev1 in pending_ids and rev2 in pending_ids
              and all(x["status"] == "pending"
                      for x in data["reviews"]),
              "got %r" % data)

        # ---- 4. get_review_by_id ----
        data = expect(rs.get_review_by_id(rev1), True, 200,
                      "get_review_by_id 命中")
        check("命中单与创建单一致",
              data and data["review"]["review_id"] == rev1
              and data["review"]["dispatch_id"] == "DSP-100",
              "got %r" % data)
        data = expect(rs.get_review_by_id("BB03-REV-999"), False, 404,
                      "get_review_by_id 不存在 → 404")
        check("404 时 data 为 None", data is None)

        # ---- 5. mark_review_completed + 幂等 ----
        data = expect(rs.mark_review_completed(rev1), True, 200,
                      "mark_review_completed 完成流转")
        check("完成后 status=completed 且 completed_at 回填",
              data and data["review"]["status"] == "completed"
              and data["completed_at"]
              and data["completed_at"] == data["review"]["completed_at"],
              "got %r" % data)
        first_done_at = data["completed_at"]
        check("completed_at 不早于 created_at",
              first_done_at[:19].replace("T", " ")
              >= data["review"]["created_at"][:19],
              "done=%r created=%r"
              % (first_done_at, data["review"]["created_at"]))
        time.sleep(1.1)
        data = expect(rs.mark_review_completed(rev1), True, 200,
                      "重复标记完成 → 幂等成功")
        check("重复标记不覆盖原 completed_at",
              data and data["completed_at"] == first_done_at,
              "first=%r second=%r" % (first_done_at, data["completed_at"]))
        data = expect(rs.get_pending_reviews(), True, 200,
                      "完成后 pending 列表减少")
        check("pending 列表只剩未完成的单",
              data and data["review_count"] == 1
              and rev1 not in [x["review_id"] for x in data["reviews"]],
              "got %r" % data)

        # ---- 6. 幂等拦截 ----
        data = expect(rs.create_review("DSP-200", "BB03-RUN-901", "P1", "W"),
                      True, 200, "重复创建同三元组 → 幂等跳过")
        check("created=False 且返回原 pending 单",
              data and data["created"] is False
              and data["review_id"] == rev2,
              "got %r" % data)
        data = expect(rs.create_review("DSP-200", "BB03-RUN-902", "P1", "W"),
                      True, 200, "同三元组换 trigger_run_id → 仍拦截")
        check("换 trigger_run_id 依然返回原单 (幂等键为三元组)",
              data and data["created"] is False
              and data["review_id"] == rev2,
              "got %r" % data)
        data = expect(rs.get_pending_reviews(), True, 200,
                      "拦截后 pending 数量不变")
        check("pending 数量仍为 1", data and data["review_count"] == 1,
              "got %r" % data)

        # ---- 7. 完成后同三元组可再次生成 ----
        expect(rs.mark_review_completed(rev2), True, 200,
               "先完成 rev2 (为再次生成用例清空 pending)")
        data = expect(rs.create_review("DSP-200", "BB03-RUN-901", "P1", "W"),
                      True, 200, "完成后同三元组再次创建")
        rev3 = data["review"]["review_id"] if data else None
        check("再次创建成功且为新 review_id",
              data and data["created"] is True
              and rev3 and rev3 != rev2,
              "got %r" % data)
        data = expect(rs.get_pending_reviews(), True, 200,
                      "再次创建后 pending 列表")
        check("新单重新进入 pending 列表",
              data and data["review_count"] == 1
              and rev3 in [x["review_id"] for x in data["reviews"]],
              "got %r" % data)

        # ---- 8. 参数校验 ----
        expect(rs.create_review("", "BB03-RUN-901", "P1", "W"),
               False, 400, "dispatch_id 为空 → 400")
        expect(rs.create_review("DSP-300", "BB03-RUN-404", "P1", "W"),
               False, 404, "trigger_run_id 不存在 → 404")

        # ---- 9. mark 不存在 → 404 ----
        expect(rs.mark_review_completed("BB03-REV-999"), False, 404,
               "mark_review_completed 不存在 → 404")

        # ---- 10. is_review_completed ----
        check("is_review_completed: pending → False",
              rs.is_review_completed(rev3) is False)
        rs.mark_review_completed(rev3)
        check("is_review_completed: completed → True",
              rs.is_review_completed(rev3) is True)
        check("is_review_completed: 不存在 → False",
              rs.is_review_completed("BB03-REV-999") is False)

        # ---- 11. 数据层兜底 ----
        conn = db.get_conn()
        try:
            conn.execute(
                "INSERT INTO review_requests "
                "(review_id, service_id, material_id, dispatch_id, "
                "trigger_run_id, status) "
                "VALUES ('X-1','P9','W','DSP-9','BB03-RUN-901','pending')")
            conn.execute(
                "INSERT INTO review_requests "
                "(review_id, service_id, material_id, dispatch_id, "
                "trigger_run_id, status) "
                "VALUES ('X-2','P9','W','DSP-9','BB03-RUN-901','pending')")
            conn.commit()
            check("部分唯一索引: 同三元组直插第二条 pending 被拒", False)
        except sqlite3.IntegrityError:
            conn.rollback()
            check("部分唯一索引: 同三元组直插第二条 pending 被拒", True)
        try:
            conn.execute(
                "INSERT INTO review_requests "
                "(review_id, service_id, material_id, dispatch_id, "
                "trigger_run_id, status) "
                "VALUES ('X-3','P9','W','DSP-9','BB03-RUN-901','bogus')")
            conn.commit()
            check("status CHECK: 非法枚举 bogus 被拒", False)
        except sqlite3.IntegrityError:
            conn.rollback()
            check("status CHECK: 非法枚举 bogus 被拒", True)
        db.close_conn(conn)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        print("temp db cleaned: %s" % tmp_dir)

    print("-" * 60)
    print("result: %d passed, %d failed" % (_PASS, _FAIL))
    if _FAIL:
        print("FAILED")
        sys.exit(1)
    print("ALL PASSED")
    sys.exit(0)


if __name__ == "__main__":
    main()
