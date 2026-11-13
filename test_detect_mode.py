# -*- coding: utf-8 -*-
"""
载具识别 + 四路在局判据回归测试（离线，不依赖游戏在跑）。

背景：
  2026-11-12 空战实测 `/state` 完整但 `/indicators` 恒 {"valid": false}；
  2026-11-13 陆战实测正好反过来（indicators 完整含 type/army，state 无效）。
  即端点是**按空战/陆战互补可用**，不是被永久掏空。只看任一路都会把
  在局误判成机库 → 守护反复停 HUD、面板写「未在战斗中」。

判据：
  在局 = /indicators.valid | /state.valid |
         /mission.json status=running | /map_info.json valid
  空地 = indicators 的 type/army（陆战精确）→ /state 气动仪表（空战兜底）

跑法：J:\\Quant\\.venv_wt\\Scripts\\python.exe test_detect_mode.py
"""
import sys

sys.path.insert(0, r"J:\Quant")
import wt_hud_launcher as L
from wt_common import battle_signals

# ---- 典型 /state ----
AIR_STATE = {
    "valid": True,
    "H, m": 1400, "TAS, km/h": 311, "IAS, km/h": 285, "M": 0.25,
    "AoA, deg": -0.2, "AoS, deg": 0.0, "Ny": 1.0, "Vy, m/s": 1.2,
    "RPM 1": 2550, "thrust 1, kgs": 0, "pitch 1, deg": 22.0,
}
TANK_STATE = {"valid": True, "gear, %": 0, "steering, %": 0,
              "throttle, %": 0, "H, m": 120, "speed, km/h": 0}
HELI_STATE = {"valid": True, "RPM 1": 320, "TAS, km/h": 200,
              "H, m": 300, "Ny": 1.0}
HANGAR_STATE = {"valid": False}

DEAD_IND = {"valid": False}          # 空战里 indicators 的常态
GROUND_IND = {"valid": True, "type": "tankModels/germ_kv_2_754r",
              "army": "tank", "crew_total": 6}
OLD_AIR_IND = {"valid": True, "type": "aircraftModels/f_16c", "army": "air"}
OLD_SHIP_IND = {"valid": True, "type": "shipModels/us_pt6", "army": "ship"}

RUNNING = {"status": "running", "objectives": [{"status": "in_progress"}]}
FINISHED = {"status": "finished", "objectives": []}
MAP_OK = {"valid": True, "grid_size": [1700.0, 1700.0],
          "map_min": [0, 0], "map_max": [2048, 2048]}
MAP_BAD = {"valid": False}

# (名称, indicators, state, mission, map_info, 期望模式)
CASES = [
    # ---- 原有：两级空地判据 ----
    ("空战: ind 失效 + state 完整", DEAD_IND, AIR_STATE, None, None, L.MODE_AIR),
    ("陆战: ind 失效 + state 地面", DEAD_IND, TANK_STATE, None, None, L.MODE_GROUND),
    ("直升机: state 有 RPM/TAS", DEAD_IND, HELI_STATE, None, None, L.MODE_AIR),
    ("机库: ind/state 全 false", DEAD_IND, HANGAR_STATE, None, None, L.MODE_NONE),
    ("8111 完全不可达", None, None, None, None, L.MODE_NONE),
    ("老版 tank indicators", GROUND_IND, TANK_STATE, None, None, L.MODE_GROUND),
    ("老版 aircraft indicators", OLD_AIR_IND, AIR_STATE, None, None, L.MODE_AIR),
    ("老版 ship indicators", OLD_SHIP_IND, TANK_STATE, None, None, L.MODE_GROUND),
    ("type='?' 回落 state", {"valid": True, "type": "?", "army": ""},
     TANK_STATE, None, None, L.MODE_GROUND),
    ("state 只有 valid（载入中）", DEAD_IND, {"valid": True},
     None, None, L.MODE_UNKNOWN),
    # ---- 新增：四路信号并集 ----
    ("真实陆战快照: ind 完整 + state 失效", GROUND_IND, HANGAR_STATE,
     RUNNING, MAP_OK, L.MODE_GROUND),
    ("真实空战快照: state 完整 + ind 失效", DEAD_IND, AIR_STATE,
     RUNNING, MAP_OK, L.MODE_AIR),
    ("只有 mission=running → 保持在局", DEAD_IND, HANGAR_STATE,
     RUNNING, None, L.MODE_UNKNOWN),
    ("只有 map_info.valid → 保持在局", DEAD_IND, HANGAR_STATE,
     None, MAP_OK, L.MODE_UNKNOWN),
    ("mission=RUNNING 大写也认", DEAD_IND, HANGAR_STATE,
     {"status": "RUNNING"}, None, L.MODE_UNKNOWN),
    ("mission=finished + 全 false → 机库", DEAD_IND, HANGAR_STATE,
     FINISHED, MAP_BAD, L.MODE_NONE),
    ("机库残留上一局 type，四路全 false", {"valid": False,
     "type": "tankModels/germ_kv_2_754r", "army": "tank"}, HANGAR_STATE,
     FINISHED, MAP_BAD, L.MODE_NONE),
    ("四路全 false（含空 dict）→ 机库", {}, {}, {}, {}, L.MODE_NONE),
]


def main():
    fails = 0

    # ---- 公共判据单独验证：命中哪些信号 ----
    ok, src = battle_signals(GROUND_IND, AIR_STATE, RUNNING, MAP_OK)
    checks = [
        ("battle_signals 四路全命中", ok and len(src) == 4 and
         src == ["indicators", "state", "mission", "map_info"]),
        ("battle_signals 全 false", battle_signals(
            DEAD_IND, HANGAR_STATE, FINISHED, MAP_BAD)[0] is False),
        ("battle_signals 全部 None", battle_signals()[0] is False),
        ("battle_signals 非 dict 不炸", battle_signals(
            "bad", 123, [], None)[0] is False),
        ("battle_signals 仅 mission", battle_signals(
            None, None, RUNNING, None)[1] == ["mission"]),
    ]
    for name, okk in checks:
        fails += 0 if okk else 1
        print(f"[{'OK ' if okk else 'FAIL'}] {name}")

    print()
    for name, ind, st, ms, mi, want in CASES:
        got, desc = L.detect_mode(ind, st, ms, mi)
        okk = got == want
        fails += 0 if okk else 1
        print(f"[{'OK ' if okk else 'FAIL'}] {name:38s} -> {got:7s} ({desc})")

    print()
    print("FAILS =", fails)
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
