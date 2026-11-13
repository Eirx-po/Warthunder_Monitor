"""回归测试：目标数据不可达时隐藏 TARGET / BOMB ZONE 面板。

背景：8111 会出现 /state 正常、但 /map_obj.json 返回 HTTP 200 + 空 body、
/map_info.json 返回 {"valid": false} 的情况。此时目标列表根本算不出来，
HUD 却仍画出 "无目标" / "无轰炸目标" 的空壳面板。本测试固化修复。

运行：
    J:\\Quant\\.venv_wt\\Scripts\\python.exe test_map_unavailable.py
"""
import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "wt_air_hud"))

from wt_common import map_data_available
import wt_hud_v2
from data_fetcher import AirDataFetcher

_passed = 0


def check(name, cond):
    global _passed
    assert cond, "FAIL: " + name
    _passed += 1
    print("  OK  " + name)


# ---- 1. 公共可达性判据 ----
check("无 map_ok 字段的旧数据 -> 可达（向后兼容）", map_data_available({}) is True)
check("map_ok=False -> 不可达", map_data_available({"map_ok": False}) is False)
check("map_ok=True + 新鲜 ts -> 可达",
      map_data_available({"map_ok": True, "ts": time.time()}) is True)
check("整帧时间戳陈旧 -> 不可达",
      map_data_available({"map_ok": True, "ts": time.time() - 10}) is False)
check("ts=0 -> 跳过陈旧检查（离线数据）",
      map_data_available({"map_ok": True, "ts": 0}) is True)
check("非 dict -> 不可达", map_data_available(None) is False)

# ---- 2. 两个抓取线程的可达性标记 ----
for cls, label in ((AirDataFetcher, "空战"), (wt_hud_v2.DataThread, "陆战")):
    f = cls()
    d = {}
    f._mark_map_ok(d, [])                     # HTTP 200 + 空 body
    check(f"{label}: 空 map_obj -> 不可达", d["map_ok"] is False)
    d = {}
    f._mark_map_ok(d, [{"icon": "Player"}])
    check(f"{label}: 有 map_obj -> 可达", d["map_ok"] is True)
    d = {}
    f._mark_map_ok(d, [])                     # 单帧失败，宽限内不隐藏
    check(f"{label}: 单帧失败仍在宽限内 -> 可达（防闪烁）", d["map_ok"] is True)
    f._last_map_ok_ts = time.time() - 5
    d = {}
    f._mark_map_ok(d, [])
    check(f"{label}: 持续不可达 -> 不可达", d["map_ok"] is False)

# ---- 3. 离屏渲染：TARGET / BOMB ZONE 面板显隐 ----
from PyQt5.QtWidgets import QApplication
from PyQt5.QtGui import QImage
from PyQt5.QtCore import Qt
from hud_overlay import AirHudOverlay

app = QApplication.instance() or QApplication(sys.argv)


class Stub(object):
    def __init__(self, data):
        self._d = data

    def get_data(self):
        return self._d

    def bomb_zone_enabled(self):
        return True


def make_data(map_ok):
    return {"ts": time.time(), "in_battle": True, "map_ok": map_ok,
            "state": {"valid": True}, "indicators": {}, "flight": {},
            "metrics": {}, "enemies": [], "allies": [], "targets": [],
            "zones": [], "bomb_targets": [], "selected": None, "player": None}


def alpha_count(img, x, y, w, h):
    n = 0
    for yy in range(y, y + h):
        for xx in range(x, x + w):
            if img.pixelColor(xx, yy).alpha() > 8:
                n += 1
    return n


w = AirHudOverlay()
for t in (w.timer, w.fg_timer, w.wd_timer):
    t.stop()
w.fetcher.running = False
# 固定布局到屏幕内（真实配置里的绝对坐标是按 1920x1080 填的）
w.ui = {"scale": 1.0, "font": "Consolas"}
w.FONT_NAME = "Consolas"
w.pscales = {}
w.offsets = {}
w.poses = {"flight": (15, 15), "target": (400, 300)}
w.layout = {"flight": "TL", "target": "BR", "bomb": "BL",
            "engine": "OFF", "threat": "OFF", "combat": "OFF"}

TARGET_RECT = (400, 300, 200, 120)
BOMB_RECT = (15, w.sh - 150 - 15, 200, 120)   # BL 角落

for map_ok, expect in ((True, True), (False, False)):
    w.fetcher = Stub(make_data(map_ok))
    img = QImage(w.size(), QImage.Format_ARGB32)
    img.fill(Qt.transparent)
    w.render(img)
    tn = alpha_count(img, *TARGET_RECT)
    bn = alpha_count(img, *BOMB_RECT)
    blank = alpha_count(img, 300, 100, 100, 100)
    check("map_ok=%s: 空白区域保持透明（像素确实来自面板）" % map_ok, blank < 100)
    check("map_ok=%s: TARGET 面板%s" % (map_ok, "显示" if expect else "隐藏"),
          (tn > 500) == expect)
    check("map_ok=%s: BOMB ZONE 面板%s" % (map_ok, "显示" if expect else "隐藏"),
          (bn > 500) == expect)
    print("       pixels: target=%d bomb=%d blank=%d" % (tn, bn, blank))

# ---- 4. 陆战 HUD：战场态势(TARGET)面板同样按可达性隐藏 ----
gw = wt_hud_v2.HudOverlay()
for name in ("timer", "fg_timer", "wd_timer"):
    t = getattr(gw, name, None)
    if t is not None:
        t.stop()
gw.data_thread.running = False
gw.ui = {"scale": 1.0, "font": "Microsoft YaHei"}
gw.pscales = {}
gw.offsets = {}
gw.poses = {"flight": (15, 15), "target": (400, 300)}
gw.layout = {"flight": "TL", "target": "BR"}
GROUND_RECT = (400, 300, 200, 120)

for map_ok, expect in ((True, True), (False, False)):
    gw.data_thread = Stub(make_data(map_ok))
    img = QImage(gw.size(), QImage.Format_ARGB32)
    img.fill(Qt.transparent)
    gw.render(img)
    n = alpha_count(img, *GROUND_RECT)
    check("陆战 map_ok=%s: 战场态势面板%s" % (map_ok, "显示" if expect else "隐藏"),
          (n > 500) == expect)
    print("       pixels: ground_target=%d" % n)

print("\nALL %d CHECKS PASSED" % _passed)
