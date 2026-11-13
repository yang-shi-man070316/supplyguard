import json
import time
from pathlib import Path

DATA_FILE = Path(__file__).parent / "scenario_data.json"

_last_mtime = None
_last_data = None

def load_scenario():
    """读取场景数据。scenario_data.json 内容变化时自动重新加载（热加载）；
    文件暂时不可读或损坏（如保存写到一半）时沿用上一次成功加载的数据。"""
    global _last_mtime, _last_data
    try:
        mtime = DATA_FILE.stat().st_mtime
    except OSError:
        return _last_data
    if mtime == _last_mtime:
        return _last_data
    try:
        with open(DATA_FILE, encoding="utf-8") as f:
            new_data = json.load(f)
    except (OSError, ValueError):
        return _last_data
    _last_data = new_data
    _last_mtime = mtime
    print(f"[{time.strftime('%H:%M:%S')}] 已热加载 {DATA_FILE.name}")
    return _last_data
