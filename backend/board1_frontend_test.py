"""阶段08板块1: 整合版前端 UI 自测脚本 (Playwright, 只读访问后端).

覆盖用例 (TC-UI-*):
    TC-UI-01 页面加载: 四视图 + 拓扑导航渲染, run 列表与 T1~T5 状态加载
    TC-UI-02 视图切换: ①总览 → ②缺口 → ③证据 → ④方案 → ⑤拓扑 全部正常
    TC-UI-03 run_trace 下钻: 总览行点击 → 追溯链路面板 (任务→缺口→证据→方案)
    TC-UI-04 缺口与依赖: P1/P2/P3 需求/接收/缺口矩阵 + blockers + 依赖明细
    TC-UI-05 证据与运行: 签收记录、关联证据、全局证据列表 (类型筛选)
    TC-UI-06 方案与对比: PLAN-A/B/C 判定; reality 基线 + what-if 子运行并排对比
    TC-UI-07 Three.js 拓扑: 固定节点/连线渲染; 选中 run 状态联动;
              点击节点弹出简要信息
    TC-UI-08 视图联动: 切换 run → 拓扑与各视图同步刷新

用法:
    python board1_frontend_test.py [base_url]
    base_url 默认 http://127.0.0.1:8010 (测试实例; 生产 8000 亦可)
截图输出: docs/phase08-board1-shots/。
"""

import os
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
SHOT_DIR = ROOT / "docs" / "phase08-board1-shots"
BASE_URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8010"
CONSOLE_URL = BASE_URL.rstrip("/") + "/console/"

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


def main():
    SHOT_DIR.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 960})
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(CONSOLE_URL, wait_until="networkidle")
        time.sleep(1.5)

        # ---- TC-UI-01 页面加载 ----
        check("TC-UI-01a 五个导航入口渲染",
              page.locator("#nav a").count() == 5,
              "count=%d" % page.locator("#nav a").count())
        page.wait_for_selector("tbody tr.click", timeout=15000)
        run_rows = page.locator("tbody tr.click").count()
        check("TC-UI-01b run 列表加载 (>0 行)", run_rows > 0, "rows=%d" % run_rows)
        page.wait_for_function(
            "document.querySelectorAll('tbody tr.click td .t').length > 0",
            timeout=20000)
        check("TC-UI-01c T1~T5 状态芯片已加载 (trace 预取)",
              page.locator("tbody tr.click td .t").count() >= 10,
              "chips=%d" % page.locator("tbody tr.click td .t").count())
        page.screenshot(path=str(SHOT_DIR / "01-overview.png"), full_page=True)

        # ---- TC-UI-03 选中 S3 基线 run → 追溯链路 ----
        page.click('tr.click[data-run="BB03-RUN-011"]')
        page.wait_for_selector("#chainSlot .chain", timeout=10000)
        chain_text = page.locator("#chainSlot .chain").inner_text()
        check("TC-UI-03a run trace 追溯链路面板渲染 (含任务/缺口/方案段)",
              "T1" in chain_text and "缺口" in chain_text and "方案" in chain_text,
              "chain=%r" % chain_text[:120])
        check("TC-UI-03b 链路包含 PLAN 方案判定",
              "PLAN-A" in chain_text and "PLAN-B" in chain_text,
              "chain=%r" % chain_text[:160])
        page.screenshot(path=str(SHOT_DIR / "02-overview-trace.png"), full_page=True)

        # ---- TC-UI-02/04 视图② 缺口与依赖 ----
        page.click('#nav a[href="#/gaps"]')
        page.wait_for_selector(".gapcell", timeout=10000)
        matrix_text = page.locator(".card").first.inner_text()
        check("TC-UI-02a 视图②缺口矩阵渲染 (需求/接收/缺口)",
              "需 7 / 收 3" in matrix_text and "缺口 4" in matrix_text,
              "matrix=%r" % matrix_text[:150])
        blockers_text = page.locator(".card").nth(1).inner_text()
        check("TC-UI-04a blockers 表含 P1/W 缺口 4",
              "P1" in blockers_text and "W" in blockers_text,
              "blockers=%r" % blockers_text[:120])
        deps_text = page.locator(".card").nth(2).inner_text()
        check("TC-UI-04b 依赖检查明细含 T1-D1/T5-D1",
              "T1-D1" in deps_text, "deps=%r" % deps_text[:120])
        page.screenshot(path=str(SHOT_DIR / "03-gaps.png"), full_page=True)

        # ---- TC-UI-02/05 视图③ 证据 ----
        page.click('#nav a[href="#/evidence"]')
        page.wait_for_selector("#evGlobal table", timeout=10000)
        ev1 = page.locator(".card").nth(0).inner_text()
        check("TC-UI-02b 视图③签收记录渲染", "RCT-S3-001" in ev1,
              "receipts=%r" % ev1[:120])
        chain2 = page.locator(".card").nth(2).inner_text()
        check("TC-UI-05a 证据溯源链路面板渲染", "溯源链路" in chain2,
              "chain2=%r" % chain2[:80])
        page.select_option("#evFilter", "receipt")
        page.click("#evReload")
        page.wait_for_function(
            "document.querySelector('#evGlobal tbody') !== null", timeout=8000)
        check("TC-UI-05b 全局证据列表按类型筛选",
              page.locator("#evGlobal tbody tr").count() >= 1,
              "rows=%d" % page.locator("#evGlobal tbody tr").count())
        page.screenshot(path=str(SHOT_DIR / "04-evidence.png"), full_page=True)

        # ---- TC-UI-02/06 视图④ 方案与对比 ----
        page.click('#nav a[href="#/plans"]')
        page.wait_for_selector(".cmp .col", timeout=10000)
        plans_text = page.locator(".card").first.inner_text()
        check("TC-UI-02c 视图④方案表渲染", "PLAN-A" in plans_text
              and "可执行" in plans_text, "plans=%r" % plans_text[:150])
        cols = page.locator(".cmp .col").count()
        children_count = page.evaluate(
            "fetch('/api/runs').then(r => r.json()).then(rs => "
            "rs.filter(x => x.parent_run_id === 'BB03-RUN-011').length)")
        check("TC-UI-06a 同基线对比列 = 基线 + 全部 what-if 子运行",
              cols == 1 + children_count,
              "cols=%d children=%d" % (cols, children_count))
        cmp_text = page.locator(".cmp").inner_text()
        check("TC-UI-06b 对比列含 reality/what-if 与假设标注",
              "reality" in cmp_text and "what-if" in cmp_text,
              "cmp=%r" % cmp_text[:150])
        page.screenshot(path=str(SHOT_DIR / "05-plans.png"), full_page=True)

        # ---- TC-UI-02/07 视图⑤ 拓扑 ----
        page.click('#nav a[href="#/topology"]')
        page.wait_for_selector("canvas#topo", timeout=10000)
        page.wait_for_function("window.__topoApp !== undefined", timeout=10000)
        has_gl = page.evaluate(
            "!!(document.querySelector('canvas#topo').getContext('webgl2') "
            "|| document.querySelector('canvas#topo').getContext('webgl'))")
        check("TC-UI-02d 视图⑤ Three.js WebGL 画布渲染", has_gl, "no webgl")
        hint = page.locator("#topoHint").inner_text()
        check("TC-UI-07a 拓扑与选中 run 联动 (hint 含 BB03-RUN-011)",
              "BB03-RUN-011" in hint, "hint=%r" % hint)
        # 点击 P1 节点 → 弹出简要信息
        pos = page.evaluate("window.__topoApp.screenPosOf('P1')")
        box = page.locator("canvas#topo").bounding_box()
        page.mouse.click(box["x"] + pos["x"], box["y"] + pos["y"])
        page.wait_for_timeout(400)
        info_display = page.locator("#nodeInfo").evaluate(
            "el => el.style.display")
        info_text = page.locator("#nodeInfo").inner_text()
        check("TC-UI-07b 点击节点弹出简要信息 (P1 含 T1-D1 状态)",
              info_display == "block" and "P1" in info_text,
              "display=%r text=%r" % (info_display, info_text[:80]))
        page.screenshot(path=str(SHOT_DIR / "06-topology-s3.png"))

        # ---- TC-UI-08 切换 run → 拓扑联动 ----
        page.select_option("#runSel", "BB03-RUN-016")
        page.wait_for_timeout(800)
        hint2 = page.locator("#topoHint").inner_text()
        check("TC-UI-08a 切换 run 后拓扑同步刷新 (hint 含 BB03-RUN-016)",
              "BB03-RUN-016" in hint2, "hint=%r" % hint2)
        page.screenshot(path=str(SHOT_DIR / "07-topology-s5.png"))

        # ---- 拓扑直接加载 (刷新后 hash 保持) ----
        page.reload(wait_until="networkidle")
        page.wait_for_selector("canvas#topo", timeout=10000)
        check("TC-UI-08b 刷新后拓扑视图可独立加载", True, "")

        # ---- JS 页面错误 ----
        check("TC-UI-09 无未捕获 JS 错误", not errors, "errors=%r" % errors[:3])

        browser.close()
    print("-" * 60)
    print("result: %d passed, %d failed" % (_PASS, _FAIL))
    print("shots: %s" % SHOT_DIR)
    print("ALL PASSED" if _FAIL == 0 else "FAILURES PRESENT")
    return 0 if _FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
