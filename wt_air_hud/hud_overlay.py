"""
War Thunder Air Battle HUD - Overlay Module
Transparent, topmost, click-through overlay for air combat

Features:
- Flight instruments (altitude, speed, Mach, AoA, G-force, heading)
- Engine status panel
- Tactical radar with heading vectors
- Threat list with distance, bearing, aspect angle, speed
- Combat metrics (energy state, stall warning, enemy on six)
- Alert bar (stall, low fuel, low altitude, over-G, enemy behind)

Requirements:
- Game must be in Windowed or Borderless Windowed mode
- Python 3.10+ with PyQt5
"""
import sys
import os
import json
import math
import ctypes

from PyQt5.QtWidgets import QApplication, QWidget
from PyQt5.QtCore import Qt, QTimer, QRect
from PyQt5.QtGui import QColor, QFont, QPainter, QPen, QPolygonF, QBrush
from PyQt5.QtCore import QPoint

from data_fetcher import AirDataFetcher
from wt_common import layout_arrows
from wt_foreground import is_war_thunder_foreground


class AirHudOverlay(QWidget):
    """Pure paintEvent transparent overlay for air combat"""

    # Colors
    BG = QColor(8, 12, 20, 220)
    BORDER = QColor(50, 60, 80, 180)
    RED = QColor(231, 76, 60)
    GREEN = QColor(46, 204, 113)
    BLUE = QColor(52, 152, 219)
    YELLOW = QColor(241, 196, 15)
    ORANGE = QColor(230, 126, 34)
    CYAN = QColor(26, 188, 156)
    WHITE = QColor(236, 240, 241)
    MUTED = QColor(128, 139, 150)
    DARK_RED = QColor(192, 57, 43)

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

        self.fetcher = AirDataFetcher()
        self.fetcher.start()

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.update)
        self.timer.start(100)  # 10fps

        # 失去焦点（切到别的窗口）时是否自动隐藏 HUD。
        # 默认关闭 —— HUD 常驻显示，不做隐藏/恢复的来回切换。
        # 需要时设 WT_HUD_HIDE_ON_BLUR=1 开启（也可在启动器窗口里勾选）。
        self._hide_on_blur = os.environ.get("WT_HUD_HIDE_ON_BLUR", "") == "1"
        self._fg_visible = True
        self.fg_timer = QTimer(self)
        self.fg_timer.timeout.connect(self._check_foreground)
        self.fg_timer.start(400)

        self.layout, self.offsets, self.ui = self._load_layout()
        self.FONT_NAME = self.ui.get("font", "Consolas")

        # For flicker-free radar trail
        self._radar_sweep = 0

    @staticmethod
    def _load_layout():
        """
        读取面板位置配置，返回 (角落配置, 偏移配置)。

        优先级：环境变量 > hud_layout.json > 默认值。
        角落代号：TL 左上 / TR 右上 / BL 左下 / BR 右下 / OFF 不显示。
        偏移：{"面板名": [dx, dy]}，负 dx = 左移，负 dy = 上移。
        """
        layout = dict(AirHudOverlay.DEFAULT_LAYOUT)
        offsets = dict(AirHudOverlay.DEFAULT_OFFSETS)
        ui = dict(AirHudOverlay.DEFAULT_UI)

        try:
            cfg = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "hud_layout.json")
            if os.path.exists(cfg):
                with open(cfg, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    for k, v in data.items():
                        if k == "ui" and isinstance(v, dict):
                            # {"ui": {"scale": 1.3, "font": "Consolas"}}
                            if "scale" in v:
                                try:
                                    ui["scale"] = max(0.5, min(3.0, float(v["scale"])))
                                except Exception:
                                    pass
                            f = v.get("font")
                            if isinstance(f, str) and f.strip():
                                ui["font"] = f.strip()
                        elif k == "offset" and isinstance(v, dict):
                            # {"offset": {"target": [-300, 0]}}
                            for pk, pv in v.items():
                                if (isinstance(pv, list) and len(pv) >= 2):
                                    offsets[pk] = (int(pv[0]), int(pv[1]))
                        elif k in layout and isinstance(v, str):
                            layout[k] = v.strip().upper()
        except Exception:
            pass

        for env_key, key in (("WT_HUD_FLIGHT", "flight"),
                             ("WT_HUD_TARGET", "target"),
                             ("WT_HUD_COMBAT", "combat"),
                             ("WT_HUD_ENGINE", "engine")):
            v = os.environ.get(env_key, "").strip().upper()
            if v:
                layout[key] = v

        # 环境变量覆盖 UI 缩放/字体
        try:
            s = os.environ.get("WT_HUD_SCALE", "").strip()
            if s:
                ui["scale"] = max(0.5, min(3.0, float(s)))
        except Exception:
            pass
        f = os.environ.get("WT_HUD_FONT", "").strip()
        if f:
            ui["font"] = f
        return layout, offsets, ui

    def reset_stack(self):
        """每帧开始清空堆叠记录（面板在同一角落时依次排开）"""
        self._stack_used = {}   # 角落 -> 已占用的高度总和（含间距）

    def panel_pos(self, key, w, h):
        """
        返回面板左上角坐标 (x, y)；OFF 或未知返回 None。

        同一角落放多个面板时自动堆叠：
        上方角落（TL/TR）从上往下排，下方角落（BL/BR）从下往上排。
        面板在 specs 列表里越靠前，越靠近屏幕边缘那一端。

        注意：偏移必须用「之前所有面板的累计高度」，
        不能用 n*h（各面板高度不同，那样会互相重叠）。
        """
        pos = (self.layout.get(key) or "OFF").upper()
        if pos not in ("TL", "TR", "BL", "BR"):
            return None

        m, gap = self.PANEL_MARGIN, 8
        used = self._stack_used.get(pos, 0)
        self._stack_used[pos] = used + h + gap

        if pos == "TL":
            x, y = m, m + used
        elif pos == "TR":
            x, y = self.sw - w - m, m + used
        elif pos == "BL":
            x, y = m, self.sh - h - m - used
        else:
            x, y = self.sw - w - m, self.sh - h - m - used

        # 附加微调偏移（负 dx = 左移，负 dy = 上移）
        dx, dy = self.offsets.get(key, (0, 0))
        # ⚠ 必须转 int：PyQt5 的 drawText(int,int,str) 不接受 float，
        # 传浮点会抛 TypeError（还被绘制循环的 except 吞掉，看起来像"没渲染"）。
        # 缩放后 w*s/h*s 是浮点，这里统一取整。
        return (int(x + dx), int(y + dy))

    def showEvent(self, event):
        super().showEvent(event)
        try:
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
        # 旧一帧的绘制会残留形成"残影"。这里强制整屏清为透明。
        p.setCompositionMode(QPainter.CompositionMode_Clear)
        p.fillRect(self.rect(), Qt.transparent)
        p.setCompositionMode(QPainter.CompositionMode_SourceOver)

        data = self.fetcher.get_data()

        # 不在战斗中（机库/菜单/未开局）时整屏留空 —— 不画任何提示框。
        # HUD 现在默认常驻显示，待机提示会一直挂在屏幕上很碍事。
        # 需要提示时用 WT_HUD_STANDBY=1 恢复。
        if not data or not data.get("in_battle", False):
            if os.environ.get("WT_HUD_STANDBY", "") == "1":
                self._draw_standby(p)
            return

        # Ensure all values are safe (not None)
        data["flight"] = data.get("flight") or {}
        data["metrics"] = data.get("metrics") or {}
        data["enemies"] = data.get("enemies") or []
        data["allies"] = data.get("allies") or []
        data["zones"] = data.get("zones") or []

        # Wrap each panel in try/except so one failure doesn't kill all
        # 屏幕中央不显示内容（雷达/姿态仪），只保留四角面板。
        #
        # 空战不显示威胁列表（THREAT LIST）：玩家盯的是屏幕内的敌机、边缘方向箭头
        # 和飞行数据，左上角再挂一个文字列表意义不大，还挤占视野。
        # RB 的丢失目标追踪仍会在左下 COMBAT 面板与底部警报条提示，不会丢功能。
        # 需要列表时用环境变量 WT_HUD_THREAT=1 恢复。
        #
        # 右下角同理不再显示引擎面板（油门/RPM/油温看游戏内仪表更直接），
        # 腾出来的位置改放「选中目标」信息（距离/速度/接近率）。
        # 需要引擎面板时用 WT_HUD_ENGINE=1 恢复（挂在目标面板上方）。
        #
        # 面板位置可配置（角落代号 TL / TR / BL / BR / OFF）：
        #   1) 编辑 wt_air_hud/hud_layout.json（推荐，改完重启 HUD）
        #   2) 或设环境变量 WT_HUD_FLIGHT / WT_HUD_TARGET / WT_HUD_COMBAT
        # 默认飞行数据放左上，右上角整个空出来留给游戏小地图。
        panels = []
        self.reset_stack()   # 同一角落的面板依次堆叠

        # COMBAT 已并入 FLIGHT（一个窗口），combat 配置不再单独绘制。
        # 默认极简版（只有发动机功率 + 爬升率）；
        # WT_HUD_FULL=1 切换到完整版（G力/AoA/VS/罗盘/燃油 + E-alt/TURN/NEAR/LOST）。
        full = os.environ.get("WT_HUD_FULL", "") == "1"
        flight_fn = self._draw_flight_panel_full if full else self._draw_flight_panel
        flight_h = 215 if full else 150

        specs = [
            ("flight", flight_fn, 240, flight_h),
            ("target", self._draw_target_panel, 240, 155),
        ]
        # UI 缩放：用 QPainter 变换实现，绘制代码仍用原始坐标，
        # 尺寸/字号/内部间距一起缩放，无需改动各绘制方法。
        # 定位(panel_pos)用缩放后尺寸，避免溢出或互相重叠。
        s = float(self.ui.get("scale", 1.0))

        for key, fn, w, h in specs:
            pos = self.panel_pos(key, w * s, h * s)
            if pos is None:
                continue
            panels.append((fn, (p, pos[0], pos[1], data, s)))

        panels.append((self._draw_alert_bar, (p, data)))
        panels.append((self._draw_edge_arrows, (p, data)))

        if os.environ.get("WT_HUD_THREAT", "") == "1":
            # 威胁列表默认关。开启时若配置里没指定角落，放到右上（避开左上的飞行数据）
            pos = self.panel_pos("threat", 240, 200) or (self.sw - 255, 15)
            panels.insert(0, (self._draw_threat_panel, (p, pos[0], pos[1], data)))
        if os.environ.get("WT_HUD_ENGINE", "") == "1":
            eng = self.panel_pos("engine", 240, 185) or (self.sw - 255, self.sh - 375)
            panels.append((self._draw_engine_panel, (p, eng[0], eng[1], data)))

        for draw_fn, args in panels:
            try:
                # args = (p, x, y, data[, scale])
                if len(args) >= 5:
                    pp, px, py, dd, sc = args
                    if sc and abs(sc - 1.0) > 1e-3:
                        pp.save()
                        pp.translate(px, py)      # 以面板左上角为原点
                        pp.scale(sc, sc)
                        pp.translate(-px, -py)
                        try:
                            draw_fn(pp, px, py, dd)
                        finally:
                            pp.restore()
                        continue
                    draw_fn(pp, px, py, dd)
                else:
                    draw_fn(*args)
            except Exception:
                pass  # Skip failed panel, continue rendering others

    def _draw_panel(self, p, x, y, w, h, title=""):
        p.setBrush(self.BG)
        p.setPen(Qt.NoPen)
        p.drawRoundedRect(x, y, w, h, 6, 6)
        p.setPen(self.BORDER)
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(x, y, w, h, 6, 6)
        if title:
            p.setPen(self.WHITE)
            p.setFont(QFont(self.FONT_NAME, 10, QFont.Bold))
            p.drawText(x + 10, y + 16, title)
            p.setPen(QColor(50, 60, 80, 120))
            p.drawLine(x + 10, y + 22, x + w - 10, y + 22)

    # 面板默认角落。飞行数据放左上，把右上角整块留给游戏小地图
    DEFAULT_LAYOUT = {
        "flight": "TL",
        "target": "BR",
        "combat": "BL",
        "engine": "OFF",
        "threat": "OFF",
    }
    # 面板微调偏移：{"面板名": (dx, dy)}，负 dx = 左移、负 dy = 上移
    DEFAULT_OFFSETS = {}
    PANEL_MARGIN = 15

    # UI 缩放与字体（可在启动器窗口里调，存 hud_layout.json 的 "ui"）
    # scale 用 QPainter 变换实现，一处生效即尺寸+字号+内部间距一起缩放，
    # 不必改每个绘制方法的坐标。
    DEFAULT_UI = {
        "scale": 1.0,
        "font": "Consolas",
    }

    # 距离滞回（米）：近于 HIDE_IN 隐藏，需退到 HIDE_OUT 之外才重新显示
    # 调试开关：设置环境变量 WT_ARROW_DEBUG=1 可忽略距离阈值，强制显示所有箭头
    _ARROW_DEBUG = os.environ.get("WT_ARROW_DEBUG", "") == "1"
    ARROW_HIDE_IN = 0.0 if _ARROW_DEBUG else 2000.0
    ARROW_HIDE_OUT = 0.0 if _ARROW_DEBUG else 2500.0

    def _draw_edge_arrows(self, p, data):
        """
        屏幕边缘敌人方向指示箭头（off-screen indicators）

        保证「一个敌人 = 一个箭头」：
        1. 同一目标的重复 map_obj 记录（一个防空阵地 = 3门炮 + 1辆SPAA，
           坐标只差几米）已在 TargetManager 中按位置聚类合并；
        2. 屏幕边缘的箭头再做去重叠推开，避免多个目标叠成一团；
        3. 方位角/距离经 EMA 平滑，消除坐标抖动导致的拖影；
        4. 距离滞回，避免在阈值边界反复闪现。
        """
        flight = data.get("flight", {})
        # 用 visible_targets（已按 show_ground_targets 过滤地面目标），
        # 保证箭头与 TARGET 面板显示的是同一批目标。
        targets = data.get("visible_targets")
        if targets is None:
            targets = data.get("targets", [])
        if not flight or not targets:
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

        show = show[:8]   # 最多 8 个最近目标

        arrows = layout_arrows(show, self.sw, self.sh, margin=40, min_gap=46.0)

        for a in arrows:
            t = a["cluster"]
            dist = t.dist
            aspect = getattr(t, "aspect", 0)

            # 颜色：地面目标=橙，空中近距正面威胁=红，其余橙
            if t.kind == "air" and dist < 4000 and abs(aspect) < 60:
                color = self.RED
            elif t.kind == "air":
                color = self.ORANGE
            else:
                color = QColor(239, 130, 40)   # 地面目标

            p.save()
            p.translate(a["x"], a["y"])
            p.rotate(a["angle"])      # 屏幕约定：右为正 → 顺时针旋转
            p.setBrush(color)
            p.setPen(Qt.NoPen)
            tri = QPolygonF([
                QPoint(0, -10),
                QPoint(6, 7),
                QPoint(0, 3),
                QPoint(-6, 7),
            ])
            p.drawPolygon(tri)
            p.restore()

            # 距离标签：放在箭头**内侧**（朝屏幕中心），
            # 若放外侧会被屏幕边缘裁掉。
            lx = int(a["x"] - a["dx"] * 28)
            ly = int(a["y"] - a["dy"] * 28)
            label = f"{dist/1000:.1f}km"
            if t.count > 1:
                label += f" x{t.count}"
            p.setFont(QFont(self.FONT_NAME, 8, QFont.Bold))
            fm = p.fontMetrics()
            tw = fm.horizontalAdvance(label)
            p.setBrush(QColor(0, 0, 0, 190))
            p.setPen(Qt.NoPen)
            p.drawRect(lx - tw // 2 - 3, ly - 8, tw + 6, 15)
            p.setPen(color)
            p.drawText(lx - tw // 2, ly + 3, label)

    def _draw_standby(self, p):
        """Show standby message when not in battle"""
        self._draw_panel(p, self.sw // 2 - 120, self.sh // 2 - 20, 240, 40)
        p.setPen(self.MUTED)
        p.setFont(QFont(self.FONT_NAME, 11))
        p.drawText(self.sw // 2 - 120, self.sh // 2 - 20, 240, 40, Qt.AlignCenter, "等待进入战斗...")

    def _draw_threat_panel(self, p, x, y, data):
        """Left top: enemy threat list"""
        w, h = 240, 200
        # Add mode label to panel title
        mode = data.get("game_mode", "?")
        self._draw_panel(p, x, y, w, h, f"THREAT LIST [{mode}]")

        enemies = data.get("enemies", [])
        lost_tracks = data.get("tracks_lost", [])

        row_y = y + 38
        p.setFont(QFont(self.FONT_NAME, 9))

        if not enemies and not lost_tracks:
            p.setPen(self.GREEN)
            p.setFont(QFont(self.FONT_NAME, 10))
            p.drawText(x + 10, row_y, "No enemies detected")
            return

        icon_map = {
            "Fighter": "FTR", "Bomber": "BMR", "Assault": "ATK",
            "HeavyTank": "HT", "MediumTank": "MT",
        }

        for i, e in enumerate(enemies[:6]):
            ey = row_y + i * 26

            # Color based on threat level
            dist = e.get("dist", 99999)
            aspect = e.get("aspect", 0)

            # Threat color: red if close and heading toward us
            if dist < 1000 and abs(aspect) < 45:
                color = self.RED
                threat = "!!"
            elif dist < 2000:
                color = self.ORANGE
                threat = "!"
            else:
                color = self.YELLOW
                threat = ""

            # Row background for high threat
            if dist < 1000:
                p.setBrush(QColor(40, 10, 10, 100))
                p.setPen(Qt.NoPen)
                p.drawRoundedRect(x + 5, ey - 4, w - 10, 24, 3, 3)

            label = icon_map.get(e["icon"], e["icon"][:3].upper())
            p.setPen(color)
            p.drawText(x + 10, ey + 10, f"{label} {dist:>5.0f}m")

            # Aspect indicator
            if abs(aspect) < 30:
                aspect_str = ">>IN<<"
                p.setPen(self.RED)
            elif abs(aspect) > 150:
                aspect_str = "<<OUT>>"
                p.setPen(self.GREEN)
            else:
                aspect_str = f"ASP {aspect:+.0f}"
                p.setPen(self.MUTED)
            p.drawText(x + 100, ey + 10, aspect_str)

            # Speed
            spd = e.get("speed_kmh", 0)
            if spd > 0:
                p.setPen(self.CYAN)
                p.drawText(x + 170, ey + 10, f"{spd:.0f}")

            # Threat marker
            if threat:
                p.setPen(self.RED)
                p.setFont(QFont(self.FONT_NAME, 11, QFont.Bold))
                p.drawText(x + w - 20, ey + 10, threat)
                p.setFont(QFont(self.FONT_NAME, 9))

        # Show lost tracks (RB mode) below the enemy list
        # Lost tracks are targets that dropped off the minimap but are
        # still tracked via prediction
        if lost_tracks:
            lost_y = row_y + min(len(enemies), 6) * 26
            if lost_y + len(lost_tracks) * 16 > y + h - 10:
                lost_y = y + h - 10 - len(lost_tracks) * 16

            p.setPen(self.MUTED)
            p.setFont(QFont(self.FONT_NAME, 8))
            p.drawText(x + 10, lost_y, f"--- LOST ({len(lost_tracks)}) ---")
            lost_y += 14

            for t in lost_tracks[:4]:
                pred_dist = getattr(t, "pred_dist", 0)
                age = t.age()
                if age > 20:
                    continue  # too stale
                label = icon_map.get(t.icon, t.icon[:3].upper())
                p.setPen(QColor(120, 120, 130, 200))
                p.setFont(QFont(self.FONT_NAME, 8))
                p.drawText(x + 10, lost_y, f"~{label} {pred_dist:.0f}m {age:.0f}s")
                lost_y += 14

    def _draw_flight_panel(self, p, x, y, data):
        """
        极简面板（默认）：只显示发动机功率与爬升率。

        发动机功率：
          喷气机取 /state 的 "thrust N, kgs"（推力），多发送加
          螺旋桨机取 "power N, hp"（轴马力）
        爬升率：/state 的 "Vy, m/s"，正=爬升(绿)、负=下降(红)
        Ps：比能量变化率 = Vy + (V/g)·(dV/dt)，单位 m/s。
            正=在攒能量(绿)、负=在烧能量(红)。8111 给不出阻力值，
            Ps 是推力与阻力之差的综合结果，格斗时最好用。
        """
        w, h = 240, 150
        self._draw_panel(p, x, y, w, h, "ENGINE / CLIMB")

        flight = data.get("flight")
        if not flight:
            p.setPen(self.MUTED)
            p.setFont(QFont(self.FONT_NAME, 10))
            p.drawText(x + 10, y + 42, "No data")
            return

        engines = flight.get("engines", []) or []
        thrust = sum(e.get("thrust", 0) or 0 for e in engines)
        power = sum(e.get("power", 0) or 0 for e in engines)

        if thrust > 0:
            label, value, unit = "THR", f"{thrust:.0f}", "kg"
        elif power > 0:
            label, value, unit = "PWR", f"{power:.0f}", "hp"
        else:
            label, value, unit = "THR", "--", ""

        yy = y + 36

        # 发动机功率
        p.setFont(QFont(self.FONT_NAME, 9))
        p.setPen(self.MUTED)
        p.drawText(x + 10, yy + 8, label)
        p.setFont(QFont(self.FONT_NAME, 17, QFont.Bold))
        p.setPen(self.WHITE)
        p.drawText(x + 52, yy + 14, value)
        p.setFont(QFont(self.FONT_NAME, 9))
        p.setPen(self.MUTED)
        p.drawText(x + 150, yy + 14, unit)
        yy += 34

        # 爬升率
        vy = flight.get("v_speed", 0)
        p.setFont(QFont(self.FONT_NAME, 9))
        p.setPen(self.MUTED)
        p.drawText(x + 10, yy + 8, "CLIMB")
        vy_color = self.GREEN if vy > 2 else (self.RED if vy < -2 else self.WHITE)
        p.setFont(QFont(self.FONT_NAME, 17, QFont.Bold))
        p.setPen(vy_color)
        p.drawText(x + 52, yy + 14, f"{vy:+.1f}")
        p.setFont(QFont(self.FONT_NAME, 9))
        p.setPen(self.MUTED)
        p.drawText(x + 150, yy + 14, "m/s")
        yy += 34

        # Ps 比能量变化率（绿=攒能量 / 红=烧能量）
        ps = flight.get("ps", 0)
        p.setFont(QFont(self.FONT_NAME, 9))
        p.setPen(self.MUTED)
        p.drawText(x + 10, yy + 8, "Ps")
        ps_color = self.GREEN if ps > 1 else (self.RED if ps < -1 else self.WHITE)
        p.setFont(QFont(self.FONT_NAME, 17, QFont.Bold))
        p.setPen(ps_color)
        p.drawText(x + 52, yy + 14, f"{ps:+.1f}")
        p.setFont(QFont(self.FONT_NAME, 9))
        p.setPen(self.MUTED)
        p.drawText(x + 150, yy + 14, "m/s")

    def _draw_flight_panel_full(self, p, x, y, data):
        """
        完整版合并面板：飞行仪表 + 战斗指标（一个窗口，占一个角落）。
        默认不启用；需要详细信息时用 WT_HUD_FULL=1 切到这个版本。

        **不显示高度 / 空速 / 马赫**：游戏内 HUD 已有大号读数，重复显示
        纯属占地方（马赫也是速度的另一种表示，一并去掉）。

        飞行区：G力 / 攻角 / 垂直速度 / 罗盘 / 燃油
        战斗区：能量高度 / 转弯率 / 最近敌机 / 丢失目标 / 状态告警
        """
        w, h = 240, 215
        self._draw_panel(p, x, y, w, h, "FLIGHT / COMBAT")

        flight = data.get("flight")
        metrics = data.get("metrics") or {}
        if not flight:
            p.setPen(self.MUTED)
            p.setFont(QFont(self.FONT_NAME, 10))
            p.drawText(x + 10, y + 42, "No flight data")
            return

        fuel_pct = metrics.get("fuel_pct", 100)
        fuel_color = self.RED if fuel_pct < 20 else (self.ORANGE if fuel_pct < 40 else self.GREEN)

        yy = y + 36

        # ===== 飞行区 =====
        # G 力（大字）+ 燃油%（同行右侧）
        g = flight.get("g_force", 1.0)
        g_color = self.RED if g > 7 else (self.ORANGE if g > 5 else self.GREEN)
        p.setPen(g_color)
        p.setFont(QFont(self.FONT_NAME, 16, QFont.Bold))
        p.drawText(x + 10, yy + 14, f"{g:.1f}G")
        if flight.get("g_max", 0) > 5:
            p.setFont(QFont(self.FONT_NAME, 8))
            p.setPen(self.MUTED)
            p.drawText(x + 82, yy + 14, f"max {flight['g_max']:.1f}")
        p.setPen(fuel_color)
        p.setFont(QFont(self.FONT_NAME, 10, QFont.Bold))
        p.drawText(x + 132, yy + 14, f"Fuel {fuel_pct:.0f}%")
        yy += 28

        # 攻角 + 垂直速度（同行两列）
        aoa = flight["aoa"]
        aoa_color = self.RED if aoa > 12 else (self.ORANGE if aoa > 8 else self.WHITE)
        p.setPen(aoa_color)
        p.setFont(QFont(self.FONT_NAME, 11))
        p.drawText(x + 10, yy + 10, f"AoA {aoa:+.1f}d")
        vs = flight["v_speed"]
        vs_color = self.ORANGE if abs(vs) > 20 else self.WHITE
        p.setPen(vs_color)
        p.drawText(x + 124, yy + 10, f"VS {vs:+.1f}m/s")
        yy += 20

        # 罗盘
        compass = flight.get("compass", 0)
        p.setPen(self.CYAN)
        p.setFont(QFont(self.FONT_NAME, 11, QFont.Bold))
        p.drawText(x + 10, yy + 10, f"HDG {compass:.0f}")
        yy += 20

        # 燃油条
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(30, 30, 40))
        p.drawRoundedRect(x + 10, yy, w - 20, 5, 2, 2)
        p.setBrush(fuel_color)
        p.drawRoundedRect(x + 10, yy, int((w - 20) * fuel_pct / 100), 5, 2, 2)
        yy += 14

        # 分隔线
        p.setPen(QColor(50, 60, 80, 120))
        p.drawLine(x + 10, yy, x + w - 10, yy)
        yy += 12

        # ===== 战斗区 =====
        # 能量高度 + 转弯率（同行两列）
        p.setFont(QFont(self.FONT_NAME, 9))
        p.setPen(self.MUTED)
        p.drawText(x + 10, yy + 8, "E-alt")
        p.setPen(self.WHITE)
        p.setFont(QFont(self.FONT_NAME, 11, QFont.Bold))
        p.drawText(x + 44, yy + 8, f"{metrics.get('energy_altitude', 0):.0f}m")

        v_ms = flight.get("tas", 0) / 3.6
        if v_ms > 10 and g > 1.5:
            turn_rate = math.degrees(g * 9.81 / v_ms)
            p.setPen(self.CYAN if turn_rate > 15 else self.WHITE)
            p.setFont(QFont(self.FONT_NAME, 9))
            p.drawText(x + 124, yy + 8, f"TURN {turn_rate:.1f}")
        else:
            p.setPen(self.MUTED)
            p.setFont(QFont(self.FONT_NAME, 9))
            p.drawText(x + 124, yy + 8, "TURN --")
        yy += 20

        # 最近敌机 + 丢失目标（同行两列）
        nearest = metrics.get("nearest_enemy_dist", 99999)
        if nearest < 99999:
            p.setPen(self.YELLOW)
            p.setFont(QFont(self.FONT_NAME, 10, QFont.Bold))
            p.drawText(x + 10, yy + 8, f"NEAR {nearest:.0f}m")
        lost_count = metrics.get("lost_targets", 0)
        if lost_count > 0:
            p.setPen(self.ORANGE)
            p.setFont(QFont(self.FONT_NAME, 9, QFont.Bold))
            p.drawText(x + 124, yy + 8, f"LOST {lost_count}")
        yy += 20

        # 状态告警
        status_parts = []
        if metrics.get("stall_risk"):
            status_parts.append(("STALL", self.RED))
        if metrics.get("low_fuel"):
            status_parts.append(("LOW FUEL", self.RED))
        if metrics.get("low_altitude"):
            status_parts.append(("TERRAIN", self.RED))
        if metrics.get("over_g"):
            status_parts.append(("OVER-G", self.RED))
        if metrics.get("enemy_on_six"):
            status_parts.append(("ON SIX", self.RED))
        if metrics.get("lost_threat"):
            status_parts.append(("LOST-TRK", self.ORANGE))

        if status_parts:
            p.setFont(QFont(self.FONT_NAME, 9, QFont.Bold))
            for text, color in status_parts[:4]:
                p.setPen(color)
                p.drawText(x + 10, yy + 10, text)
                yy += 14
        else:
            p.setPen(self.GREEN)
            p.setFont(QFont(self.FONT_NAME, 9))
            p.drawText(x + 10, yy + 10, "Nominal")

    def _draw_target_panel(self, p, x, y, data):
        """
        右下角：选中目标信息。

        显示距离 / 地速 / 接近率 / 进入角。
        速度由位置差分估算（8111 不给目标速度），接近率是距离变化率。

        ⚠ 目标高度拿不到：map_obj 只有二维小地图坐标，不含 altitude，
          也没有目标相对本机的俯仰角；/state 只有本机高度，
          /indicators 的 71 个字段里也没有任何目标相关项。
          所以这里如实标注 ALT n/a，不编造数字。
        """
        w, h = 240, 155
        self._draw_panel(p, x, y, w, h, "TARGET")

        t = data.get("selected")
        if t is None:
            p.setPen(self.MUTED)
            p.setFont(QFont(self.FONT_NAME, 10))
            p.drawText(x + 10, y + 48, "无目标")
            p.setFont(QFont(self.FONT_NAME, 8))
            p.drawText(x + 10, y + 70, "机头对准敌机即自动选中")
            p.drawText(x + 10, y + 86, "(锥角 ±45°)")

            p.setPen(self.MUTED)
            p.setFont(QFont(self.FONT_NAME, 8))
            p.drawText(x + 10, y + h - 12, "ALT 数据 8111 不提供")
            return

        # 第一行：机型 + 相对方位
        p.setFont(QFont(self.FONT_NAME, 10, QFont.Bold))
        p.setPen(self.WHITE)
        p.drawText(x + 10, y + 42, f"{t.icon}")
        rel = getattr(t, "rel_bearing", 0)
        p.setPen(self.CYAN)
        p.drawText(x + 150, y + 42, f"{rel:+4.0f}d")

        yy = y + 62

        # 距离
        p.setFont(QFont(self.FONT_NAME, 8))
        p.setPen(self.MUTED)
        p.drawText(x + 10, yy, "DIST")
        p.setFont(QFont(self.FONT_NAME, 15, QFont.Bold))
        p.setPen(self.YELLOW)
        p.drawText(x + 52, yy + 2, f"{t.dist:.0f} m")

        # 地速（估算）
        yy += 26
        p.setFont(QFont(self.FONT_NAME, 8))
        p.setPen(self.MUTED)
        p.drawText(x + 10, yy, "SPD")
        p.setFont(QFont(self.FONT_NAME, 15, QFont.Bold))
        p.setPen(self.CYAN)
        p.drawText(x + 52, yy + 2, f"{t.speed_kmh:.0f} km/h")

        # 接近率：绿=正在接近，红=正在拉开
        yy += 26
        p.setFont(QFont(self.FONT_NAME, 8))
        p.setPen(self.MUTED)
        p.drawText(x + 10, yy, "CLOS")
        cl = getattr(t, "closure_ms", 0)
        col = self.GREEN if cl > 2 else (self.RED if cl < -2 else self.MUTED)
        arrow = ">>" if cl > 2 else ("<<" if cl < -2 else "--")
        p.setFont(QFont(self.FONT_NAME, 12, QFont.Bold))
        p.setPen(col)
        p.drawText(x + 52, yy + 1, f"{cl:+.0f} m/s {arrow}")

        # 底部一行：进入角 + 高度不可得
        aspect = getattr(t, "aspect", 0)
        p.setFont(QFont(self.FONT_NAME, 8))
        p.setPen(self.MUTED)
        p.drawText(x + 10, y + h - 12, f"ASP {aspect:+.0f}d")

        p.setPen(QColor(120, 120, 130))
        p.drawText(x + 100, y + h - 12, "ALT n/a")

    def _draw_engine_panel(self, p, x, y, data):
        """Right bottom: engine status"""
        w, h = 240, 185
        self._draw_panel(p, x, y, w, h, "ENGINES")

        flight = data.get("flight")
        if not flight or not flight.get("engines"):
            p.setPen(self.MUTED)
            p.setFont(QFont(self.FONT_NAME, 10))
            p.drawText(x + 10, y + 42, "No engine data")
            return

        p.setFont(QFont(self.FONT_NAME, 9))
        yy = y + 36

        for i, eng in enumerate(flight["engines"]):
            row_y = yy + i * 48

            # Engine number
            p.setPen(self.WHITE)
            p.setFont(QFont(self.FONT_NAME, 10, QFont.Bold))
            p.drawText(x + 10, row_y + 10, f"E{i+1}")

            # Throttle
            p.setFont(QFont(self.FONT_NAME, 9))
            p.setPen(self.GREEN if eng["throttle"] > 50 else self.MUTED)
            p.drawText(x + 30, row_y + 10, f"THR {eng['throttle']:.0f}%")

            # RPM
            p.setPen(self.WHITE)
            p.drawText(x + 110, row_y + 10, f"RPM {eng['rpm']:.0f}")

            # Oil temp
            oil = eng["oil_temp"]
            oil_color = self.RED if oil > 110 else (self.ORANGE if oil > 90 else self.GREEN)
            p.setPen(oil_color)
            p.drawText(x + 10, row_y + 22, f"Oil {oil:.0f}C")

            # Thrust
            if eng["thrust"] > 0:
                p.setPen(self.CYAN)
                p.drawText(x + 70, row_y + 22, f"Thr {eng['thrust']:.0f}kg")

            # Efficiency
            eff = eng["efficiency"]
            eff_color = self.RED if eff < 50 else (self.ORANGE if eff < 80 else self.GREEN)
            p.setPen(eff_color)
            p.drawText(x + 140, row_y + 22, f"Eff {eff:.0f}%")

            # Throttle bar
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(30, 30, 40))
            p.drawRoundedRect(x + 10, row_y + 28, w - 20, 4, 2, 2)
            p.setBrush(self.GREEN)
            p.drawRoundedRect(x + 10, row_y + 28, int((w - 20) * eng["throttle"] / 100), 4, 2, 2)

    def _draw_radar(self, p, x, y, data):
        """Center: tactical radar"""
        cx = x + 90
        cy = y + 90
        r = 85

        self._radar_sweep = (self._radar_sweep + 6) % 360

        # Background
        p.setBrush(QColor(5, 10, 18, 200))
        p.setPen(QColor(50, 60, 80, 150))
        p.drawEllipse(QPoint(cx, cy), r, r)
        p.setPen(QColor(30, 40, 55, 80))
        p.drawEllipse(QPoint(cx, cy), r * 2 // 3, r * 2 // 3)
        p.drawEllipse(QPoint(cx, cy), r // 3, r // 3)

        # Crosshair
        p.setPen(QColor(30, 40, 55, 60))
        p.drawLine(cx - r, cy, cx + r, cy)
        p.drawLine(cx, cy - r, cx, cy + r)

        # Compass markings
        p.setPen(self.MUTED)
        p.setFont(QFont("sans-serif", 7))
        p.drawText(cx - 3, cy - r + 8, "N")
        p.drawText(cx - 3, cy + r - 2, "S")
        p.drawText(cx + r - 5, cy + 3, "E")
        p.drawText(cx - r, cy + 3, "W")

        # Sweep line
        sweep_rad = math.radians(self._radar_sweep)
        p.setPen(QPen(QColor(46, 204, 113, 80), 1.5))
        p.drawLine(cx, cy, int(cx + r * math.cos(sweep_rad)), int(cy + r * math.sin(sweep_rad)))

        # Own aircraft (center, pointing up)
        flight = data.get("flight", {})
        compass = flight.get("compass", 0) if flight else 0
        p.setBrush(self.YELLOW)
        p.setPen(Qt.NoPen)
        # Draw as triangle pointing up (nose = N)
        p.drawPolygon(QPolygonF([
            QPoint(cx, cy - 6),
            QPoint(cx + 4, cy + 4),
            QPoint(cx - 4, cy + 4),
        ]))

        # Enemies
        enemies = data.get("enemies", [])
        game_mode = data.get("game_mode", "RB")
        # RB mode radar range is larger (targets appear further on minimap)
        max_d = 15000 if game_mode == "RB" else 5000
        if game_mode == "SB":
            max_d = 8000

        for e in enemies[:10]:
            dist = min(e.get("dist", 0), max_d)
            bearing_deg = e.get("bearing", 0)
            # Rotate by own compass so nose is up
            rel_bearing = bearing_deg - compass
            br = math.radians(rel_bearing)
            px = int(cx + dist / max_d * r * math.sin(br))
            py = int(cy - dist / max_d * r * math.cos(br))

            if e.get("dist", 9999) < 1000:
                color = self.RED
                size = 6
            elif e.get("dist", 9999) < 3000:
                color = self.ORANGE
                size = 5
            else:
                color = self.YELLOW
                size = 4

            # Draw enemy as inverted triangle (pointing toward us = threat)
            p.setBrush(color)
            p.setPen(Qt.NoPen)
            p.drawPolygon(QPolygonF([
                QPoint(px, py + size),
                QPoint(px + size, py - size // 2),
                QPoint(px - size, py - size // 2),
            ]))

            # Heading vector
            heading = e.get("heading", 0)
            rel_hdg = heading - compass
            hr = math.radians(rel_hdg)
            hl = 10
            p.setPen(QPen(color, 1.5))
            p.drawLine(px, py, int(px + hl * math.sin(hr)), int(py - hl * math.cos(hr)))

            # Distance label
            p.setPen(color)
            p.setFont(QFont("sans-serif", 7))
            p.drawText(px + 7, py + 3, f"{int(dist)}")

        # Allies
        for a in data.get("allies", [])[:8]:
            dist = min(a.get("dist", 0), max_d)
            bearing_deg = a.get("bearing", 0)
            rel_bearing = bearing_deg - compass
            br = math.radians(rel_bearing)
            px = int(cx + dist / max_d * r * math.sin(br))
            py = int(cy - dist / max_d * r * math.cos(br))
            p.setBrush(self.BLUE)
            p.setPen(Qt.NoPen)
            p.drawEllipse(QPoint(px, py), 3, 3)

        # Lost tracks (RB mode) - dashed circle at predicted position
        lost_tracks = data.get("tracks_lost", [])
        for t in lost_tracks[:8]:
            if t.age() > 20:
                continue
            pred_dist = getattr(t, "pred_dist", 0)
            pred_bearing = getattr(t, "pred_bearing", 0)

            if pred_dist > max_d:
                pred_dist = max_d
            rel_bearing = pred_bearing - compass
            br = math.radians(rel_bearing)
            lx = int(cx + pred_dist / max_d * r * math.sin(br))
            ly = int(cy - pred_dist / max_d * r * math.cos(br))

            # Dashed circle marker
            p.setPen(QPen(QColor(128, 128, 140, 180), 1, Qt.DashLine))
            p.setBrush(Qt.NoBrush)
            p.drawEllipse(QPoint(lx, ly), 5, 5)
            # Blinking "?" for recent lost targets
            p.setPen(QColor(180, 180, 190, 220))
            p.setFont(QFont("sans-serif", 7))
            p.drawText(lx + 6, ly - 3, f"?{int(pred_dist)}")

    def _draw_metrics_panel(self, p, x, y, data):
        """Left bottom: combat metrics"""
        w, h = 180, 130
        self._draw_panel(p, x, y, w, h, "COMBAT")

        metrics = data.get("metrics", {})
        flight = data.get("flight", {})

        if not metrics or not flight:
            p.setPen(self.MUTED)
            p.setFont(QFont(self.FONT_NAME, 10))
            p.drawText(x + 10, y + 42, "No metrics")
            return

        yy = y + 36
        p.setFont(QFont(self.FONT_NAME, 9))

        # Energy altitude
        p.setPen(self.WHITE)
        p.drawText(x + 10, yy + 10, f"Energy alt: {metrics['energy_altitude']:.0f}m")
        yy += 16

        # Turn rate (from roll rate and G-force)
        g = flight.get("g_force", 1.0)
        v_ms = flight.get("tas", 0) / 3.6
        if v_ms > 10 and g > 1.5:
            turn_rate = math.degrees(g * 9.81 / v_ms)
            p.setPen(self.CYAN if turn_rate > 15 else self.WHITE)
            p.drawText(x + 10, yy + 10, f"Turn rate: {turn_rate:.1f}d/s")
        else:
            p.setPen(self.MUTED)
            p.drawText(x + 10, yy + 10, "Turn rate: --")
        yy += 16

        # Nearest enemy
        if metrics.get("nearest_enemy_dist", 99999) < 99999:
            p.setPen(self.YELLOW)
            p.drawText(x + 10, yy + 10, f"Nearest: {metrics['nearest_enemy_dist']:.0f}m")
        else:
            p.setPen(self.MUTED)
            p.drawText(x + 10, yy + 10, "Nearest: --")
        yy += 16

        # Lost targets count (RB mode)
        lost_count = metrics.get("lost_targets", 0)
        if lost_count > 0:
            p.setPen(self.ORANGE)
            p.setFont(QFont(self.FONT_NAME, 9, QFont.Bold))
            p.drawText(x + 10, yy + 10, f"LOST: {lost_count} targets")
            yy += 16

        # Status indicators
        status_parts = []
        if metrics.get("stall_risk"):
            status_parts.append(("STALL", self.RED))
        if metrics.get("low_fuel"):
            status_parts.append(("LOW FUEL", self.RED))
        if metrics.get("low_altitude"):
            status_parts.append(("TERRAIN", self.RED))
        if metrics.get("over_g"):
            status_parts.append(("OVER-G", self.RED))
        if metrics.get("enemy_on_six"):
            status_parts.append(("ON SIX!", self.RED))

        if status_parts:
            for text, color in status_parts:
                p.setPen(color)
                p.setFont(QFont(self.FONT_NAME, 9, QFont.Bold))
                p.drawText(x + 10, yy + 10, text)
                yy += 14
        else:
            p.setPen(self.GREEN)
            p.setFont(QFont(self.FONT_NAME, 9))
            p.drawText(x + 10, yy + 10, "All systems nominal")

    def _draw_alert_bar(self, p, data):
        """Bottom center: alert bar"""
        metrics = data.get("metrics", {})
        flight = data.get("flight", {})

        alert = ""
        color = self.RED

        if metrics.get("stall_risk"):
            alert = "STALL WARNING - LOW SPEED HIGH AoA"
            color = self.RED
        elif metrics.get("lost_threat"):
            alert = metrics.get("lost_threat_desc", "LOST TARGET - TRACKING")
            color = self.ORANGE
        elif metrics.get("enemy_on_six"):
            alert = "ENEMY ON YOUR SIX - EVASIVE MANEUVERS"
            color = self.RED
        elif metrics.get("over_g"):
            alert = f"OVER-G {flight.get('g_force', 0):.1f} - REDUCE LOAD"
            color = self.RED
        elif metrics.get("low_altitude"):
            alert = f"LOW ALTITUDE {flight.get('altitude', 0):.0f}m - PULL UP"
            color = self.ORANGE
        elif metrics.get("low_fuel"):
            alert = f"LOW FUEL {metrics.get('fuel_pct', 0):.0f}% - RTB"
            color = self.ORANGE

        if alert:
            bar_w = 400
            bar_h = 28
            bx = self.sw // 2 - bar_w // 2
            by = self.sh - 45

            p.setBrush(QColor(40, 10, 10, 220))
            p.setPen(color)
            p.drawRoundedRect(bx, by, bar_w, bar_h, 4, 4)
            p.setPen(color)
            p.setFont(QFont(self.FONT_NAME, 11, QFont.Bold))
            p.drawText(bx, by, bar_w, bar_h, Qt.AlignCenter, alert)

    def _draw_attitude(self, p, x, y, data):
        """Simple attitude indicator"""
        flight = data.get("flight", {})
        if not flight:
            return

        roll = flight.get("roll", 0)
        pitch = flight.get("pitch", 0)
        r = 50

        # Background circle
        p.setBrush(QColor(5, 10, 18, 200))
        p.setPen(QColor(50, 60, 80, 150))
        p.drawEllipse(QPoint(x + r, y + r), r, r)

        # Clip to circle
        p.save()
        p.setClipRegion(self._circle_region(x + r, y + r, r))

        # Rotate for roll
        p.translate(x + r, y + r)
        p.rotate(-roll)

        # Sky (blue) and ground (brown) split by pitch
        p.setPen(Qt.NoPen)
        pitch_offset = int(pitch * 2)
        p.setBrush(QColor(40, 80, 140))
        p.drawRect(-r * 2, -r * 2 + pitch_offset, r * 4, r * 2)
        p.setBrush(QColor(80, 50, 30))
        p.drawRect(-r * 2, pitch_offset, r * 4, r * 2)

        # Horizon line
        p.setPen(QPen(QColor(255, 255, 255, 150), 1.5))
        p.drawLine(-r, pitch_offset, r, pitch_offset)

        # Pitch ladder
        for offset in [-30, -20, -10, 10, 20, 30]:
            oy = pitch_offset - offset * 2
            if abs(oy) < r:
                p.setPen(QPen(QColor(255, 255, 255, 100), 0.8))
                lw = 15 if offset % 10 == 0 else 8
                p.drawLine(-lw, oy, lw, oy)

        p.restore()

        # Fixed aircraft symbol
        p.setPen(QPen(self.YELLOW, 2))
        p.drawLine(x + r - 15, y + r, x + r - 5, y + r)
        p.drawLine(x + r + 5, y + r, x + r + 15, y + r)
        p.setBrush(self.YELLOW)
        p.setPen(Qt.NoPen)
        p.drawEllipse(QPoint(x + r, y + r), 2, 2)

        # Roll indicator
        p.setPen(QPen(self.WHITE, 1))
        p.drawArc(x + r - r, y + r - r, r * 2, r * 2, 30 * 16, 120 * 16)
        # Roll pointer
        roll_rad = math.radians(-roll)
        px = int(x + r + (r - 3) * math.sin(roll_rad))
        py = int(y + r - (r - 3) * math.cos(roll_rad))
        p.setBrush(self.WHITE)
        p.drawPolygon(QPolygonF([
            QPoint(px, py - 3),
            QPoint(px + 3, py + 2),
            QPoint(px - 3, py + 2),
        ]))

    def _circle_region(self, cx, cy, r):
        from PyQt5.QtGui import QRegion
        return QRegion(cx - r, cy - r, r * 2, r * 2, QRegion.Ellipse)


def main():
    import traceback

    def excepthook(exc_type, exc_value, exc_tb):
        with open("wt_air_hud_crash.log", "a", encoding="utf-8") as f:
            f.write(f"\n{'='*50}\n")
            traceback.print_exception(exc_type, exc_value, exc_tb, file=f)
        print(f"CRASH: {exc_value}", flush=True)
    sys.excepthook = excepthook

    app = QApplication(sys.argv)
    hud = AirHudOverlay()
    hud.show()
    print("Air Battle HUD started", flush=True)
    print(f"Window: {hud.winId()} visible: {hud.isVisible()}", flush=True)
    print("Press Ctrl+C to exit", flush=True)
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
