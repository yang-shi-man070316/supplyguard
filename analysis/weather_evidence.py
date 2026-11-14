"""阶段06板块2: 天气外部辅助证据 weather_evidence.

封装天气 Web API 调用 (wttr.in, 无需 API key), 作为运输/需求的辅助证据。
核心约束:
    - 异常降级: 网络超时 / HTTP 错误 / 返回结构异常一律标记 status=unavailable,
      绝不抛异常上抛, 绝不修改 T1~T4 任何判定结果 (本模块不触碰
      task_results / dependency_checks / runs)。
    - 证据落库: run_weather_evidence() 把结果写入 evidence_v2
      (evidence_type='weather', 新增类型); 调用失败时 evidence status 记为
      unavailable 而非 error, 表达「外部证据暂不可用」的降级语义。
    - 环境变量 (便于测试与部署):
        SUPPLYGUARD_WEATHER_LOCATION  地点, 默认 Beijing
        SUPPLYGUARD_WEATHER_URL       完整接口 URL 覆盖 (测试 API 宕机场景用)

CLI 用法 (抓取 + 落库 + 打印):
    python analysis/weather_evidence.py

纯抓取函数 fetch_weather() 不依赖数据库, 可独立调用。
"""

import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

# 项目根加入 sys.path, 保证独立运行 (python analysis/weather_evidence.py)
# 或被 backend/main.py import 时都能解析 backend 包与 db 模块
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
for _p in (_PROJECT_ROOT, _PROJECT_ROOT / "backend"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

WEATHER_PROVIDER = "wttr.in"
WEATHER_LOCATION = os.environ.get("SUPPLYGUARD_WEATHER_LOCATION", "Beijing")
WEATHER_URL = os.environ.get(
    "SUPPLYGUARD_WEATHER_URL",
    "https://wttr.in/%s?format=j1" % WEATHER_LOCATION,
)
_TIMEOUT_SECONDS = 8

# wttr.in 英文天气描述 -> 中文 (常见取值; 未命中保留原文)
_DESC_ZH = {
    "Clear": "晴", "Sunny": "晴", "Partly cloudy": "多云",
    "Cloudy": "多云", "Overcast": "阴", "Mist": "雾", "Fog": "雾",
    "Smoke": "霾", "Smoky haze": "霾", "Haze": "霾",
    "Light rain": "小雨", "Moderate rain": "中雨", "Heavy rain": "大雨",
    "Light snow": "小雪", "Moderate snow": "中雪", "Heavy snow": "大雪",
    "Thundery outbreaks possible": "可能有雷雨",
}


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _to_int(value):
    """字符串数值转 int, 失败返回 None (防御返回结构异常)。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def fetch_weather() -> dict:
    """调用天气 Web API 抓取当前天气 (纯网络函数, 不依赖数据库, 不抛异常)。

    返回:
        {
          "status": "ok" | "unavailable",
          "provider": "wttr.in",
          "location": "<地点>",
          "observed_at": "<本次调用时间 ISO>",
          "weather": {observation_time, temp_c, wind_kmh, humidity,
                      desc, desc_zh}   # status=ok 时存在, 否则 None
          "error_message": ""          # status=unavailable 时非空
        }
    任何异常 (超时/连接失败/HTTP 非 200/JSON 损坏/字段缺失) 都降级为
    status=unavailable, 不向上抛异常。
    """
    result = {
        "status": "unavailable",
        "provider": WEATHER_PROVIDER,
        "location": WEATHER_LOCATION,
        "observed_at": _now(),
        "weather": None,
        "error_message": "",
    }
    try:
        req = urllib.request.Request(
            WEATHER_URL,
            headers={"User-Agent": "curl/8.0", "Accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=_TIMEOUT_SECONDS) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        data = json.loads(raw)
        if not isinstance(data, dict):  # 防御: 返回体不是对象 (数组/标量等)
            raise ValueError("response is not a JSON object")
        cond = data.get("current_condition") or [{}]
        if not isinstance(cond, list) or not cond:
            raise ValueError("current_condition missing or empty")
        cc = cond[0]
        desc = (cc.get("weatherDesc") or [{}])
        desc = desc[0].get("value", "") if isinstance(desc, list) and desc else ""
        weather = {
            "observation_time": cc.get("observation_time", ""),
            "temp_c": _to_int(cc.get("temp_C")),
            "wind_kmh": _to_int(cc.get("windspeedKmph")),
            "humidity": _to_int(cc.get("humidity")),
            "desc": desc,
            "desc_zh": _DESC_ZH.get(desc, desc),
        }
        # 关键字段 (温度/描述) 缺失视为证据不可用, 不做半截返回
        if weather["temp_c"] is None or not desc:
            raise ValueError("temperature or weather desc missing in response")
        result["status"] = "ok"
        result["weather"] = weather
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError,
            ConnectionError, OSError, ValueError, json.JSONDecodeError,
            KeyError, TypeError, AttributeError, IndexError) as exc:
        result["error_message"] = "%s: %s" % (type(exc).__name__, exc)
    return result


def run_weather_evidence() -> tuple:
    """抓取天气证据并写入 evidence_v2 (辅助证据, 不触碰任何判定结果)。

    - fetch 成功 → evidence status='ok', value_json 为完整天气载荷
    - fetch 失败 → evidence status='unavailable', error_message 记录原因
      (降级语义: 天气数据暂不可用, 不影响 T1~T4 判定)
    - 数据库错误 → (False, 500, ...)
    返回 (ok, http_status, msg, data); data = fetch 结果 + evidence_id。
    """
    from backend.run_store import save_evidence  # 懒加载, 避免纯抓取依赖 DB

    result = fetch_weather()
    evidence_id = None
    try:
        ok, evidence_id = save_evidence(
            evidence_type="weather",
            source_id="%s:%s" % (WEATHER_PROVIDER, WEATHER_LOCATION),
            status=result["status"],  # ok | unavailable
            value_json=json.dumps(result, ensure_ascii=False),
            observed_at=result["observed_at"],
            error_message=result["error_message"],
        )
    except ValueError as exc:
        return False, 500, "weather evidence save failed: %s" % exc, None
    if not ok:
        return False, 500, "weather evidence save failed: %s" % evidence_id, None
    data = dict(result)
    data["evidence_id"] = evidence_id
    return True, 200, "weather evidence collected (status=%s)" % result["status"], data


if __name__ == "__main__":
    ok, http_status, msg, data = run_weather_evidence()
    print(json.dumps({"ok": ok, "http_status": http_status, "msg": msg,
                      "data": data}, ensure_ascii=False, indent=2))
    sys.exit(0 if ok else 1)
