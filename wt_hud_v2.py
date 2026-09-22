"""
War Thunder 8111 HUD 覆盖层 v2
纯 paintEvent 绘制，无子控件，更可靠

使用方法：
1. 将 War Thunder 设为「窗口化」或「无边框窗口」模式
2. 运行：python wt_hud_v2.py
3. 按 Ctrl+C 退出
"""
import sys
import os
import json
import math
import urllib.request
import threading
import time
from collections import deque

from PyQt5.QtWidgets import QApplication, QWidget
from PyQt5.QtCore import Qt, QTimer, QRect, QPointF
from PyQt5.QtGui import QColor, QFont, QPainter, QPolygonF, QPixmap

from wt_common import (is_enemy_color, is_friend_color, norm180,
                       TargetManager, layout_arrows)
from wt_foreground import is_war_thunder_foreground

BASE = "http://localhost:8111"
MAP_MAX = 4096.0


class DataThread(threading.Thread):
    """后台数据采集线程"""
    def __init__(self):
        super().__init__(daemon=True)
        self.data = {}
        self.lock = threading.Lock()
        self.running = True
        self._map_image = None       # QImage 缓存
        self._last_map_gen = None    # 地图版本，变化时重新下载
        # 目标聚类管理器：把 map_obj 中同一目标的重复记录合并成一个
        # （例如一个防空阵地 = 3门炮 + 1辆SPAA，坐标只差几米）
        self.target_mgr = TargetManager(cluster_radius_m=120.0,
                                        match_radius_m=400.0,
                                        smooth_alpha=0.35,
                                        ttl_s=3.0)

    @staticmethod
    def map_span(mi):
        """
        归一化坐标(0~1) → 米 的换算系数。

        ⚠ 不能直接用 map_max[0] 当跨度。空战实测：
            map_max=[32768,32768] / map_min=[-32768,-32768] / grid_size=[65536,65536]
        归一化 0~1 覆盖的是 map_min~map_max 这一整段，
        真实跨度 = map_max - map_min = grid_size = 65536 米。
        （用本机 TAS 标定过：按 65536 算出的水平地速与实测吻合，误差 0.6%；
          按 4096 会低估 16 倍，敌机 780km/h 被算成 36km/h。）
        陆战 map_max 通常为 [4096,4096]，此时若 map_min=[0,0] 则跨度仍为 4096。
        """
        try:
            mmax = mi.get("map_max")
            mmin = mi.get("map_min")
            if (isinstance(mmax, list) and isinstance(mmin, list)
                    and len(mmax) >= 1 and len(mmin) >= 1):
                span = float(mmax[0]) - float(mmin[0])
                if span > 0:
                    return span
            gs = mi.get("grid_size")
            if isinstance(gs, list) and len(gs) >= 1 and float(gs[0]) > 0:
                return float(gs[0])
        except Exception:
            pass
        return MAP_MAX

    def fetch(self, path):
        try:
            url = BASE + path
            req = urllib.request.Request(url, headers={"User-Agent": "WT-HUD"})
            with urllib.request.urlopen(req, timeout=2) as resp:
                return json.loads(resp.read().decode("utf-8", "replace"))
        except Exception:
            return None

    def fetch_map_image(self):
        """下载 /map.img 并缓存为 QImage"""
        try:
            from PyQt5.QtGui import QImage
            import io
            req = urllib.request.Request(BASE + "/map.img", headers={"User-Agent": "WT-HUD"})
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = resp.read()
            img = QImage()
            img.loadFromData(data)
            if not img.isNull():
                self._map_image = img
                print(f"[HUD] 地图图片已加载: {img.width()}x{img.height()}", flush=True)
        except Exception as e:
            print(f"[HUD] 地图图片下载失败: {e}", flush=True)

    def run(self):
        while self.running:
            try:
                data = {"ts": time.time()}
                ind = self.fetch("/indicators")
                data["ind"] = ind if isinstance(ind, dict) else {}
                mobj = self.fetch("/map_obj.json")
                data["map_obj"] = mobj if isinstance(mobj, list) else []
                mi = self.fetch("/map_info.json")
                data["map_info"] = mi if isinstance(mi, dict) else {}

                # ⚠ 必须在解析坐标之前算好：下面 dx_m/dy_m 与 target_mgr.map_span
                # 都依赖它。漏掉这行会抛 NameError: name 'span' is not defined，
                # 数据线程直接死掉 → HUD 一直空白（2026-09-22 修）。
                span = self.map_span(data["map_info"])

                # 地图版本变化时重新下载底图
                map_gen = mi.get("map_generation") if isinstance(mi, dict) else None
                if map_gen is not None and map_gen != self._last_map_gen:
                    self._last_map_gen = map_gen
                    self.fetch_map_image()
                data["map_image"] = self._map_image

                # 解析
                player = None
                enemies = []
                allies = []
                zones = []
                for obj in data["map_obj"]:
                    if not isinstance(obj, dict):
                        continue
                    color = obj.get("color", "")
                    icon = obj.get("icon", "")
                    otype = obj.get("type", "")
                    if icon == "Player":
                        player = obj
                    elif otype == "capture_zone":
                        zones.append(obj)
                    elif otype in ("ground_model", "aircraft"):
                        # 用 RGB 色系判定，覆盖 WT 的所有红/蓝变体
                        # （实测敌方地面单位用 #f00C00，旧硬编码列表会漏判）
                        is_enemy = is_enemy_color(color)
                        is_friend = is_friend_color(color)
                        kind = "air" if otype == "aircraft" else "tank"
                        entry = {"kind": kind, "icon": icon, "color": color,
                                 "x": obj.get("x", 0), "y": obj.get("y", 0),
                                 "dx": obj.get("dx", 0), "dy": obj.get("dy", 0)}
                        if is_enemy:
                            enemies.append(entry)
                        elif is_friend:
                            allies.append(entry)

                # 计算距离方位
                if player:
                    px, py = player["x"], player["y"]
                    # 玩家朝向（地图方向向量 dx/dy → 航向角，0=东 90=南）
                    p_dx, p_dy = player.get("dx", 0), player.get("dy", 0)
                    if p_dx != 0 or p_dy != 0:
                        player["heading"] = (math.degrees(math.atan2(p_dy, p_dx)) + 360) % 360
                    else:
                        player["heading"] = 0
                    for e in enemies:
                        dx_m = (e["x"] - px) * span
                        dy_m = (e["y"] - py) * span
                        e["dist"] = math.sqrt(dx_m**2 + dy_m**2)
                        e["bearing"] = math.degrees(math.atan2(dy_m, dx_m))
                    enemies.sort(key=lambda x: x.get("dist", 9999))
                    for a in allies:
                        dx_m = (a["x"] - px) * span
                        dy_m = (a["y"] - py) * span
                        a["dist"] = math.sqrt(dx_m**2 + dy_m**2)
                        a["bearing"] = math.degrees(math.atan2(dy_m, dx_m))

                    # ---- 目标聚类去重：一个逻辑目标只产生一个箭头 ----
                    # 8111 会把一个防空阵地拆成多条记录（坐标只差几米），
                    # 逐条画箭头会重叠成"残影"。这里先按位置合并。
                    units = []
                    for e in enemies:
                        units.append({
                            "x": e["x"], "y": e["y"],
                            "kind": e["kind"], "icon": e["icon"],
                            "bearing": e.get("bearing", 0.0),
                            "dist": e.get("dist", 0.0),
                        })
                    self.target_mgr.map_span = span
                    targets = self.target_mgr.update(units)
                    p_heading = player.get("heading", 0)
                    for t in targets:
                        # 陆战使用数学角（0=东，逆时针为正 → 左侧为正）
                        # 统一转换为屏幕约定（0=正前，右侧为正）→ 取负
                        t.rel_bearing = -norm180(t.bearing - p_heading)
                        t.rel_dist = t.dist
                    player["heading_smoothed"] = p_heading

                else:
                    targets = []

                data["player"] = player
                data["enemies"] = enemies
                data["targets"] = targets
                data["allies"] = allies
                data["zones"] = zones

                with self.lock:
                    self.data = data

            except Exception as e:
                # 单帧异常不能让数据线程整个死掉 —— 否则 HUD 会永久空白且毫无提示
                print(f"[HUD] 数据线程异常: {type(e).__name__}: {e}", flush=True)
                import traceback; traceback.print_exc()

            time.sleep(0.2)

    def get_data(self):
        with self.lock:
            return self.data.copy()


class HudOverlay(QWidget):
    """纯 paintEvent 的透明覆盖层"""

    PANEL_BG = QColor(13, 13, 18, 220)
    PANEL_BORDER = QColor(58, 58, 72, 180)
    RED = QColor(226, 75, 74)
    GREEN = QColor(151, 196, 89)
    BLUE = QColor(85, 183, 235)
    YELLOW = QColor(250, 200, 30)
    ORANGE = QColor(239, 159, 39)
    WHITE = QColor(226, 232, 240)
    MUTED = QColor(136, 136, 128)

    def __init__(self):
        super().__init__()
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        # 隐藏窗口内鼠标指针（防止挡住游戏画面）
        self.setCursor(Qt.BlankCursor)

        screen = QApplication.desktop().screenGeometry()
        self.setGeometry(screen)
        self.sw = screen.width()
        self.sh = screen.height()

        self.data_thread = DataThread()
        self.data_thread.start()

        # 定时刷新
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.update)
        self.timer.start(200)  # 5fps 刷新

        # 失去焦点时是否自动隐藏 HUD。默认关闭（常驻显示）。
        # 需要时设 WT_HUD_HIDE_ON_BLUR=1 开启（也可在启动器窗口里勾选）。
        self._hide_on_blur = os.environ.get("WT_HUD_HIDE_ON_BLUR", "") == "1"
        self._fg_visible = True
        self.fg_timer = QTimer(self)
        self.fg_timer.timeout.connect(self._check_foreground)
        self.fg_timer.start(400)

    def showEvent(self, event):
        super().showEvent(event)
        try:
            import ctypes
            hwnd = int(self.winId())
            GWL_EXSTYLE = -20
            # 只设置鼠标穿透 + 不激活，让 Qt 自己管理透明度
            # 不要调用 SetLayeredWindowAttributes，否则与 Qt 的
            # UpdateLayeredWindowIndirect 冲突导致重绘报错
            WS_EX_TRANSPARENT = 0x00000020
            WS_EX_NOACTIVATE = 0x08000000
            style = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            style |= WS_EX_TRANSPARENT | WS_EX_NOACTIVATE
            ctypes.windll.user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style)
        except Exception:
            pass

    def _check_foreground(self):
        """失去焦点时隐藏、恢复焦点时显示（需 WT_HUD_HIDE_ON_BLUR=1 开启）"""
        if not self._hide_on_blur:
            return
        try:
            fg = is_war_thunder_foreground()
        except Exception:
            fg = True          # 检测异常时保持显示，不要永久藏起来
        if fg == self._fg_visible:
            return
        self._fg_visible = fg
        if fg:
            self.show()
            print("[HUD] War Thunder 前台 -> HUD 显示", flush=True)
        else:
            self.hide()
            print("[HUD] War Thunder 失去焦点 -> HUD 隐藏", flush=True)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)

        # 显式清空上一帧。
        # 透明分层窗口（UpdateLayeredWindowIndirect）若依赖 Qt 自动清屏，
        # 旧一帧的绘制会残留下来形成"残影"。这里强制整屏清为透明。
        p.setCompositionMode(QPainter.CompositionMode_Clear)
        p.fillRect(self.rect(), Qt.transparent)
        p.setCompositionMode(QPainter.CompositionMode_SourceOver)

        data = self.data_thread.get_data()

        # 左上：战场态势
        self.draw_threat_panel(p, 15, 15, data)
        # 右上：载具状态
        self.draw_vehicle_panel(p, self.sw - 255, 15, data)
        # 底部中央：警报条
        self.draw_alert_bar(p, data)
        # 屏幕边缘：敌人方向指示箭头
        self.draw_edge_arrows(p, data)

    def draw_panel_bg(self, p, x, y, w, h):
        p.setBrush(self.PANEL_BG)
        p.setPen(Qt.NoPen)
        p.drawRoundedRect(x, y, w, h, 6, 6)
        p.setPen(self.PANEL_BORDER)
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(x, y, w, h, 6, 6)

    # 距离滞回（米）：近于 HIDE_IN 隐藏，需退到 HIDE_OUT 之外才重新显示。
    # 防止目标恰好卡在阈值上时箭头反复闪现。
    # 调试开关：WT_ARROW_DEBUG=1 时忽略距离阈值，强制显示所有箭头
    _ARROW_DEBUG = os.environ.get("WT_ARROW_DEBUG", "") == "1"
    ARROW_HIDE_IN = 0.0 if _ARROW_DEBUG else 1000.0
    ARROW_HIDE_OUT = 0.0 if _ARROW_DEBUG else 1300.0

    def draw_edge_arrows(self, p, data):
        """
        屏幕边缘敌人方向指示箭头（off-screen indicators）

        保证「一个敌人 = 一个箭头」：
        1. 同一个目标的重复 map_obj 记录（如一个防空阵地 = 3门炮 + 1辆SPAA，
           坐标只差几米）已在 TargetManager 中按位置聚类合并；
        2. 屏幕边缘的箭头再做去重叠推开，避免多个目标叠成一团；
        3. 方位角/距离经 EMA 平滑，消除坐标抖动导致的拖影；
        4. 距离滞回，避免在阈值边界反复闪现。
        """
        player = data.get("player")
        targets = data.get("targets", [])
        if not player or not targets:
            return

        # 滞回筛选：只显示"游戏视野外"的目标
        show = []
        for t in targets:
            d = t.dist
            if t.hide_latched:
                if d > self.ARROW_HIDE_OUT:
                    t.hide_latched = False
                else:
                    continue
            else:
                if d < self.ARROW_HIDE_IN:
                    t.hide_latched = True
                    continue
            show.append(t)

        if not show:
            return

        # 最多显示 8 个最近的目标
        show = show[:8]

        arrows = layout_arrows(show, self.sw, self.sh, margin=38, min_gap=44.0)

        for a in arrows:
            t = a["cluster"]
            dist = t.dist
            # 箭头颜色：飞机=橙色，坦克=红色
            color = QColor(239, 159, 39) if t.kind == "air" else QColor(226, 75, 74)

            # 箭头本体（指向目标方向）
            p.save()
            p.translate(a["x"], a["y"])
            p.rotate(a["angle"])      # 屏幕约定：右为正 → 顺时针旋转
            p.setBrush(color)
            p.setPen(Qt.NoPen)
            tri = QPolygonF([
                QPointF(0, -10),
                QPointF(6, 7),
                QPointF(0, 3),
                QPointF(-6, 7),
            ])
            p.drawPolygon(tri)
            p.restore()

            # 距离标签：放在箭头**内侧**（朝屏幕中心），
            # 若放外侧会被屏幕边缘裁掉。
            lx = int(a["x"] - a["dx"] * 26)
            ly = int(a["y"] - a["dy"] * 26)
            label = f"{dist/1000:.1f}km"
            if t.count > 1:
                label += f" x{t.count}"
            p.setFont(QFont("Consolas", 8, QFont.Bold))
            fm = p.fontMetrics()
            tw = fm.horizontalAdvance(label)
            p.setBrush(QColor(0, 0, 0, 190))
            p.setPen(Qt.NoPen)
            p.drawRect(lx - tw // 2 - 3, ly - 8, tw + 6, 15)
            p.setPen(color)
            p.drawText(lx - tw // 2, ly + 3, label)

    def draw_minimap(self, p, data):
        """
        敌方迷你地图：只显示敌方
        - 游戏小地图视野内（距离 < GAME_VIEW_RADIUS）的敌人：游戏自己会显示，不画
        - 视野外的敌人：HUD 补充标记
        位置：左上角下方（战场态势面板下面），大小 260x260
        注：避免与游戏内小地图（左下/右下角）重叠
        """
        player = data.get("player")
        enemies = data.get("enemies", [])
        map_image = data.get("map_image")

        if not player:
            return

        size = 260
        # 移到左上角下方，避开游戏小地图区域
        x = 15
        y = 190

        # 游戏小地图视野半径（米）：此范围内的敌人游戏会显示，HUD 跳过
        GAME_VIEW_RADIUS = 1000.0

        # 底图（map.img 缩放到面板）
        has_map = map_image is not None and not map_image.isNull()
        if has_map:
            p.drawPixmap(x, y, size, size, QPixmap.fromImage(map_image))
        else:
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(20, 25, 35, 200))
            p.drawRect(x, y, size, size)

        # 暗化覆盖层：让敌人标记更醒目，底图仍然可见
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(0, 0, 0, 130))
        p.drawRect(x, y, size, size)

        # 玩家居中
        cx = x + size // 2
        cy = y + size // 2
        px, py = player["x"], player["y"]
        scale = size  # 归一化坐标 1.0 = 面板大小（全图）

        # 只画视野外的敌方（游戏小地图上没有的）
        drawn = 0
        for e in enemies:
            dist = e.get("dist", 0)
            if dist < GAME_VIEW_RADIUS:
                continue  # 游戏小地图会显示，不重复

            ex = cx + (e["x"] - px) * scale
            ey = cy + (e["y"] - py) * scale
            if ex < x - 8 or ex > x + size + 8 or ey < y - 8 or ey > y + size + 8:
                continue  # 超出面板范围

            # 敌方标记：飞机=橙色圆点，坦克=红色圆点
            color = QColor(239, 159, 39) if e.get("kind") == "air" else QColor(226, 75, 74)
            p.setBrush(color)
            p.setPen(QColor(255, 255, 255))
            r = 5 if e.get("kind") == "air" else 6
            p.drawEllipse(QPointF(ex, ey), r, r)

            # 距离标签（黑底白字确保可读）
            label = f"{dist/1000:.1f}km"
            fm = p.fontMetrics()
            tw = fm.horizontalAdvance(label)
            p.setBrush(QColor(0, 0, 0, 200))
            p.setPen(Qt.NoPen)
            p.drawRect(int(ex + 6), int(ey - 9), tw + 6, 14)
            p.setPen(color)
            p.setFont(QFont("Consolas", 8, QFont.Bold))
            p.drawText(QPointF(ex + 9, ey + 3), label)
            drawn += 1

        # 玩家标记（黄色描白边，醒目）
        p.setBrush(QColor(250, 200, 30))
        p.setPen(QColor(255, 255, 255))
        p.drawEllipse(QPointF(cx, cy), 5, 5)

        # 玩家朝向指示（北字）
        p.setPen(QColor(250, 200, 30))
        p.setFont(QFont("Consolas", 8, QFont.Bold))
        p.drawText(QPointF(cx - 4, cy - 10), "N")

        # 面板边框 + 标题
        p.setBrush(Qt.NoBrush)
        p.setPen(QColor(58, 58, 72, 180))
        p.drawRect(x, y, size, size)
        p.setPen(QColor(226, 232, 240))
        p.setFont(QFont("Consolas", 8, QFont.Bold))
        map_status = "MAP" if has_map else "NO MAP"
        p.drawText(x + 6, y + 12, f"ENEMY MAP ({drawn}) {map_status}")

    def draw_threat_panel(self, p, x, y, data):
        w, h = 240, 160
        self.draw_panel_bg(p, x, y, w, h)

        p.setPen(self.WHITE)
        p.setFont(QFont("Microsoft YaHei", 10, QFont.Bold))
        p.drawText(x + 10, y + 18, "战场态势")

        # 优先用聚类去重后的目标，避免同一阵地重复占多行
        targets = data.get("targets") or []
        if targets:
            items = [{"kind": t.kind, "icon": t.icon,
                      "dist": t.dist, "bearing": t.bearing,
                      "count": t.count} for t in targets[:5]]
        else:
            items = [{"kind": e.get("kind"), "icon": e.get("icon"),
                      "dist": e.get("dist", 0), "bearing": e.get("bearing", 0),
                      "count": 1} for e in (data.get("enemies") or [])[:5]]

        if not items:
            p.setPen(self.GREEN)
            p.setFont(QFont("Microsoft YaHei", 10))
            p.drawText(x + 10, y + 42, "附近无敌军")
        else:
            icon_map = {"HeavyTank": "重坦", "MediumTank": "中坦", "LightTank": "轻坦",
                        "TankDestroyer": "坦歼", "Fighter": "战斗机", "Bomber": "轰炸机",
                        "Airdefence": "防空炮", "SPAA": "自行高炮"}
            p.setFont(QFont("Microsoft YaHei", 9))
            for i, e in enumerate(items):
                ey = y + 38 + i * 22
                name = icon_map.get(e["icon"], e["icon"])
                if e["kind"] == "air":
                    name += "(空)"
                if e.get("count", 1) > 1:
                    name += f"x{e['count']}"
                # 方位：陆战 bearing 是数学角（0=东，逆时针为正）
                # 故 45°=东北、90°=北、135°=西北
                b = e.get("bearing", 0)
                dirs = ["E", "NE", "N", "NW", "W", "SW", "S", "SE"]
                idx = int((b + 360 + 22.5) / 45) % 8
                dir_str = dirs[idx]

                color = self.ORANGE if e["kind"] == "air" else self.RED
                p.setPen(color)
                p.drawText(x + 10, ey, f"  {name}  {int(e['dist'])}m  {dir_str}")

        allies = data.get("allies", [])
        p.setPen(self.BLUE)
        p.setFont(QFont("Microsoft YaHei", 8))
        p.drawText(x + 10, y + h - 12, f"友军: {len(allies)} 活跃")

    def draw_vehicle_panel(self, p, x, y, data):
        w, h = 240, 160
        self.draw_panel_bg(p, x, y, w, h)

        p.setPen(self.WHITE)
        p.setFont(QFont("Microsoft YaHei", 10, QFont.Bold))
        p.drawText(x + 10, y + 18, "载具状态")

        ind = data.get("ind", {})
        if not ind or not ind.get("valid", False):
            p.setPen(self.MUTED)
            p.setFont(QFont("Microsoft YaHei", 9))
            p.drawText(x + 10, y + 40, "未在战斗中")
            return

        p.setFont(QFont("Microsoft YaHei", 9))
        yy = y + 38

        # 载具类型
        vtype = ind.get("type", "?")
        if "/" in vtype:
            vtype = vtype.split("/")[-1]
        p.setPen(self.WHITE)
        p.drawText(x + 10, yy, f"载具: {vtype}")
        yy += 20

        # 乘员
        crew_c = ind.get("crew_current", 0)
        crew_t = ind.get("crew_total", 0)
        if crew_t > 0:
            pct = crew_c / crew_t
            color = self.GREEN if pct > 0.5 else (self.ORANGE if pct > 0.25 else self.RED)
            p.setPen(color)
            p.drawText(x + 10, yy, f"乘员: {int(crew_c)}/{int(crew_t)}")
        yy += 18

        # 一级弹药
        ammo = ind.get("first_stage_ammo", -1)
        if ammo >= 0:
            p.setPen(self.ORANGE if ammo < 5 else self.WHITE)
            p.drawText(x + 10, yy, f"一级弹药: {int(ammo)}")
        else:
            p.setPen(self.MUTED)
            p.drawText(x + 10, yy, "一级弹药: -")
        yy += 18

        # 炮手/驾驶员
        gs = ind.get("gunner_state", 0)
        ds = ind.get("driver_state", 0)
        p.setPen(self.GREEN if gs == 0 and ds == 0 else self.RED)
        p.drawText(x + 10, yy, f"炮手:{'OK' if gs==0 else '受损'} 驾驶员:{'OK' if ds==0 else '受损'}")
        yy += 18

        # 稳定器
        stab = ind.get("stabilizer", -1)
        if stab >= 0:
            p.setPen(self.GREEN if stab > 0 else self.MUTED)
            p.drawText(x + 10, yy, f"稳定器: {'ON' if stab > 0 else 'OFF'}")
        yy += 18

        # 激光告警
        lws = ind.get("lws", -1)
        if lws >= 0:
            p.setPen(self.RED if lws > 0 else self.MUTED)
            p.drawText(x + 10, yy, f"激光告警: {'被锁定!' if lws > 0 else '安全'}")
        yy += 18

        # 速度
        speed = ind.get("speed", 0)
        p.setPen(self.WHITE)
        p.drawText(x + 10, yy, f"速度: {abs(speed):.0f} km/h")

    def draw_radar(self, p, x, y, data):
        cx, cy = x + 80, y + 80
        r = 75

        # 背景圆
        p.setBrush(QColor(13, 13, 18, 180))
        p.setPen(QColor(58, 58, 72, 150))
        p.drawEllipse(cx, cy, r, r)
        p.setPen(QColor(42, 42, 48, 100))
        p.drawEllipse(cx, cy, r * 2 // 3, r * 2 // 3)
        p.drawEllipse(cx, cy, r // 3, r // 3)

        # 方位
        p.setPen(self.MUTED)
        p.setFont(QFont("sans-serif", 7))
        p.drawText(cx - 3, cy - r + 10, "N")
        p.drawText(cx - 3, cy + r - 2, "S")
        p.drawText(cx + r - 6, cy + 3, "E")
        p.drawText(cx - r, cy + 3, "W")

        # 玩家（中心）
        p.setBrush(self.YELLOW)
        p.setPen(Qt.NoPen)
        p.drawEllipse(cx, cy, 5, 5)

        # 敌人
        max_d = 2000
        for e in data.get("enemies", [])[:8]:
            dist = min(e.get("dist", 0), max_d)
            bearing = math.radians(e.get("bearing", 0))
            px = int(cx + dist / max_d * r * math.cos(bearing))
            py = int(cy + dist / max_d * r * math.sin(bearing))
            if e["kind"] == "air":
                p.setBrush(self.ORANGE)
                p.setPen(Qt.NoPen)
                from PyQt5.QtGui import QPolygonF
                from PyQt5.QtCore import QPoint
                poly = QPolygonF([QPoint(px, py-5), QPoint(px+4, py+3), QPoint(px-4, py+3)])
                p.drawPolygon(poly)
            else:
                p.setBrush(self.RED)
                p.setPen(Qt.NoPen)
                p.drawEllipse(px, py, 5, 5)
            p.setPen(QColor(247, 193, 193))
            p.setFont(QFont("sans-serif", 7))
            p.drawText(px + 6, py + 3, f"{int(dist)}m")

        # 友军
        for a in data.get("allies", [])[:6]:
            dist = min(a.get("dist", 0), max_d)
            bearing = math.radians(a.get("bearing", 0))
            px = int(cx + dist / max_d * r * math.cos(bearing))
            py = int(cy + dist / max_d * r * math.sin(bearing))
            p.setBrush(self.BLUE)
            p.setPen(Qt.NoPen)
            p.drawEllipse(px, py, 4, 4)

    def draw_zone_panel(self, p, x, y, data):
        w, h = 180, 135
        self.draw_panel_bg(p, x, y, w, h)

        p.setPen(self.WHITE)
        p.setFont(QFont("Microsoft YaHei", 10, QFont.Bold))
        p.drawText(x + 10, y + 18, "占领区")

        zones = data.get("zones", [])
        p.setFont(QFont("Microsoft YaHei", 9))
        for i, z in enumerate(zones[:3]):
            zy = y + 40 + i * 26
            label = z.get("zone_label", "?")
            color = z.get("color", "")
            if "#174DFF" in color:
                status, col = "我方", self.GREEN
            elif "#fa0C00" in color:
                status, col = "敌方", self.RED
            else:
                status, col = "中立", self.ORANGE
            p.setPen(col)
            p.drawText(x + 10, zy, f"  {label}区: {status}")

    def draw_alert_bar(self, p, data):
        enemies = data.get("enemies", [])
        ind = data.get("ind", {})

        alert_text = ""
        alert_color = self.RED

        # 激光告警
        lws = ind.get("lws", -1)
        if lws is not None and lws > 0:
            alert_text = "被激光锁定!"
            alert_color = self.RED
        else:
            for e in enemies:
                if e["kind"] == "air" and e.get("dist", 9999) < 1000:
                    icon_map = {"Fighter": "战斗机", "Bomber": "轰炸机"}
                    name = icon_map.get(e["icon"], "敌机")
                    alert_text = f"  {name}接近 {int(e['dist'])}m"
                    alert_color = self.ORANGE
                    break
                elif e["kind"] == "tank" and e.get("dist", 9999) < 200:
                    icon_map = {"HeavyTank": "重坦", "MediumTank": "中坦", "LightTank": "轻坦"}
                    name = icon_map.get(e["icon"], "敌人")
                    alert_text = f"  {name}接近 {int(e['dist'])}m!"
                    alert_color = self.RED
                    break

        if not alert_text:
            crew_c = ind.get("crew_current", 0)
            crew_t = ind.get("crew_total", 0)
            if crew_t > 0 and crew_c / crew_t <= 0.34:
                alert_text = f"  乘员危急 {int(crew_c)}/{int(crew_t)}"
                alert_color = self.RED

        if alert_text:
            bar_w = 300
            bar_h = 26
            bx = self.sw // 2 - bar_w // 2
            by = self.sh - 50

            p.setBrush(QColor(61, 16, 16, 220))
            p.setPen(alert_color)
            p.drawRoundedRect(bx, by, bar_w, bar_h, 4, 4)
            p.setPen(alert_color)
            p.setFont(QFont("Microsoft YaHei", 11, QFont.Bold))
            p.drawText(bx, by, bar_w, bar_h, Qt.AlignCenter, alert_text)


def main():
    import traceback

    # 全局异常捕获，防止静默崩溃
    def excepthook(exc_type, exc_value, exc_tb):
        with open("J:/Quant/wt_hud_crash.log", "a", encoding="utf-8") as f:
            f.write(f"\n{'='*50}\n{time.strftime('%Y-%m-%d %H:%M:%S')}\n")
            traceback.print_exception(exc_type, exc_value, exc_tb, file=f)
        print(f"崩溃: {exc_value}", flush=True)
    sys.excepthook = excepthook

    app = QApplication(sys.argv)
    hud = HudOverlay()
    hud.show()
    print("HUD 已启动 - 透明覆盖层工作中", flush=True)
    print("按 Ctrl+C 退出", flush=True)
    print(f"窗口: {hud.winId()} 可见: {hud.isVisible()}", flush=True)
    ret = app.exec_()
    print(f"事件循环退出, code={ret}", flush=True)
    sys.exit(ret)

if __name__ == "__main__":
    main()
