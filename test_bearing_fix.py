"""
敌人指向（边缘箭头）方位换算的离线验证 —— 2026-09-25 方向全错修复的回归测试。

背景：8111 map_obj 坐标 +x=东、+y=南（贴在北朝上地图上）。
atan2(dy,dx) 是「地图角」(0=东,90=南,顺时针)，不是罗盘角(0=北)。
旧代码：空战当罗盘角用 → 全体偏 90°；陆战当逆时针数学角又取负 → 左右镜像。

运行：python test_bearing_fix.py  （离屏，无需游戏/无需显示）
"""
import math
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from wt_common import (bearing_compass, heading_compass, norm180,
                       TargetCluster, layout_arrows)

FAILURES = []


def check(name, got, want, tol=0.5):
    ok = abs(got - want) <= tol
    print(f"  [{'OK' if ok else 'FAIL'}] {name}: got={got:.1f} want={want:.1f}")
    if not ok:
        FAILURES.append(name)


# ---------------------------------------------------------------- 1. 换算助手
print("== bearing_compass / heading_compass（8111 地图坐标 +x=东 +y=南）==")
check("东 (dx=+1,dy=0) 方位", bearing_compass(1, 0), 90)
check("北 (dx=0,dy=-1) 方位", bearing_compass(0, -1), 0)
check("南 (dx=0,dy=+1) 方位", bearing_compass(0, 1), 180)
check("西 (dx=-1,dy=0) 方位", bearing_compass(-1, 0), 270)
check("东北 (dx=+1,dy=-1)", bearing_compass(1, -1), 45)
check("朝北航向 (0,-1)", heading_compass(0, -1), 0)
check("朝东航向 (1,0)", heading_compass(1, 0), 90)
check("朝南航向 (0,1)", heading_compass(0, 1), 180)
check("零向量航向", heading_compass(0, 0), 0)

# ------------------------------------------------- 2. 空战链路：bearing-compass
# 玩家机头朝北（/indicators compass=0），敌人在正东方 → rel=+90 → 箭头必须在右侧
print("== 空战链路（p_hdg 来自 indicators compass，真罗盘角）==")
p_hdg = 0.0
b_east = bearing_compass(1, 0)
check("敌在正东 rel_bearing(机头朝北)", norm180(b_east - p_hdg), 90)
check("敌在正西 rel_bearing(机头朝北)", norm180(bearing_compass(-1, 0) - p_hdg), -90)
check("敌在正北 rel_bearing(机头朝北)", norm180(bearing_compass(0, -1) - p_hdg), 0)
# 机头朝东（compass=90），敌人在正北 → 敌在左 → rel=-90
check("敌在正北 rel_bearing(机头朝东)", norm180(bearing_compass(0, -1) - 90.0), -90)


def arrow_side(rel, sw=1280, sh=800):
    """复现 HUD 绘制路径：TargetCluster → layout_arrows → 判断箭头落在哪侧"""
    t = TargetCluster(1, "air", 0.5, 0.5, "Test", bearing=0, dist=3000, ts=0.0)
    t.rel_bearing = rel
    t.dist = 3000
    a = layout_arrows([t], sw, sh, margin=40)[0]
    cx, cy = sw / 2.0, sh / 2.0
    if abs(a["x"] - cx) <= abs(a["y"] - cy):
        return "TOP" if a["y"] < cy else "BOTTOM"
    return "RIGHT" if a["x"] > cx else "LEFT"


print("== layout_arrows 屏幕位置（rel: 0=前 → 上, +90 → 右）==")
check("rel=+90 箭头在右(位置字符序)", 0, 0, 0)  # 占位，实际断言见下
assert arrow_side(90) == "RIGHT", f"rel=+90 应在右侧, got {arrow_side(90)}"
assert arrow_side(-90) == "LEFT", f"rel=-90 应在左侧, got {arrow_side(-90)}"
assert arrow_side(0) == "TOP", f"rel=0 应在上方, got {arrow_side(0)}"
assert arrow_side(180) == "BOTTOM", f"rel=180 应在下方, got {arrow_side(180)}"
print("  [OK] rel=+90 → 右 / -90 → 左 / 0 → 上 / 180 → 下")

# ------------------------------------------------- 3. 陆战链路（修复后：直接相减）
# 陆战 rel_bearing = norm180(bearing_compass(dx,dy) - heading_compass(pdx,pdy))
print("== 陆战链路（玩家坦克头朝北 dx=0,dy=-1）==")
pdx, pdy = 0, -1
p_head = heading_compass(pdx, pdy)
check("陆战 玩家航向=北", p_head, 0)
check("陆战 敌在右(东) rel", norm180(bearing_compass(1, 0) - p_head), 90)
check("陆战 敌在左(西) rel", norm180(bearing_compass(-1, 0) - p_head), -90)
check("陆战 敌在前(北) rel", norm180(bearing_compass(0, -1) - p_head), 0)
check("陆战 敌在后(南) rel", abs(norm180(bearing_compass(0, 1) - p_head)), 180)
# 玩家朝东，敌在南（右侧）
p_head2 = heading_compass(1, 0)
check("陆战 朝东+敌在南 rel", norm180(bearing_compass(0, 1) - p_head2), 90)

# ------------------------------------------------- 4. 战场态势方位字母（罗盘序）
print("== 战场态势面板方位字母（标准罗盘序 N/NE/E/...）==")
dirs = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
idx = lambda b: int((b + 360 + 22.5) / 45) % 8
assert dirs[idx(0)] == "N" and dirs[idx(90)] == "E"
assert dirs[idx(180)] == "S" and dirs[idx(270)] == "W"
assert dirs[idx(45)] == "NE" and dirs[idx(135)] == "SE"
print("  [OK] 0=N, 90=E, 180=S, 270=W, 45=NE, 135=SE")

# ------------------------------------------------- 5. Track.predict（罗盘角外推）
print("== Track.predict 罗盘角外推方向 ==")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "wt_air_hud"))
import data_fetcher as af

t = af.Track(1, "Fighter", "#f00C00", 0.5, 0.5, heading=0, ts=0.0)
t.map_span = 65536.0     # 空战大地图
t.speed_kmh = 360.0      # 100 m/s
t.last_update = 0.0
x1, y1 = t.predict(10.0)  # 10s → 1000m 正北 → y 必须减小
check("朝北外推 dy(应<0, 1000m/65536)", y1 - 0.5, -1000.0 / 65536.0, tol=1e-6)
check("朝北外推 dx(应为0)", x1 - 0.5, 0.0, tol=1e-9)
t.heading = 90.0          # 朝东 → x 增大
x2, y2 = t.predict(10.0)
check("朝东外推 dx(应>0)", x2 - 0.5, 1000.0 / 65536.0, tol=1e-6)
check("朝东外推 dy(应为0)", y2 - 0.5, 0.0, tol=1e-9)

# -------------------------------------------------
print()
if FAILURES:
    print(f"✗ {len(FAILURES)} 项失败: {FAILURES}")
    sys.exit(1)
print("✓ 全部通过：方位换算 / 屏幕位置 / 陆空两链路 / 预测外推 均符合「右为正、0=正前」约定")
