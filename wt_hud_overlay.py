"""
War Thunder 8111 HUD 覆盖层
透明、置顶、鼠标穿透的实时 HUD 覆盖窗口

使用方法：
1. 将 War Thunder 设为「窗口化」或「无边框窗口」模式（全屏独占不行）
2. 运行本脚本：python wt_hud_overlay.py
3. 按 Ctrl+C 或关闭控制台退出

数据来源：localhost:8111
"""
import sys
import json
import math
import urllib.request
import threading
import time
from collections import deque
from datetime import datetime

from PyQt5.QtWidgets import (
    QApplication, QWidget, QLabel, QVBoxLayout, QHBoxLayout,
    QFrame, QGraphicsOpacityEffect
)
from PyQt5.QtCore import Qt, QTimer, QRect, QPoint, pyqtSignal, QObject
from PyQt5.QtGui import QColor, QFont, QPainter, QPen, QBrush, QPolygonF, QFontMetrics
try:
    from PyQt5.QtWinExtras import QtWin  # 可选，用于Windows任务栏
except ImportError:
    pass

BASE = "http://localhost:8111"
MAP_MAX = 4096.0  # 归一化坐标→实际距离换算系数

# ========== 数据采集线程 ==========

class DataFetcher(QObject):
    """后台线程持续抓取 8111 数据"""
    data_ready = pyqtSignal(dict)

    def __init__(self):
        super().__init__()
        self._running = True
        self._last_evt = 0
        self._last_dmg = 0
        self._enemy_history = {}  # id -> deque of (timestamp, x, y) 用于速度估算

    def stop(self):
        self._running = False

    def fetch(self, path):
        try:
            url = BASE + path
            req = urllib.request.Request(url, headers={"User-Agent": "WT-HUD"})
            with urllib.request.urlopen(req, timeout=2) as resp:
                return json.loads(resp.read().decode("utf-8", "replace"))
        except Exception:
            return None

    def run(self):
        poll_count = 0
        while self._running:
            poll_count += 1
            data = {"timestamp": time.time(), "poll": poll_count}

            # indicators（载具状态）
            ind = self.fetch("/indicators")
            data["indicators"] = ind if isinstance(ind, dict) else {}

            # map_obj（地图对象）
            mobj = self.fetch("/map_obj.json")
            data["map_obj"] = mobj if isinstance(mobj, list) else []

            # map_info（坐标系）
            mi = self.fetch("/map_info.json")
            data["map_info"] = mi if isinstance(mi, dict) else {}

            # hudmsg（事件流）
            try:
                url = f"{BASE}/hudmsg?lastEvt={self._last_evt}&lastDmg={self._last_dmg}"
                req = urllib.request.Request(url, headers={"User-Agent": "WT-HUD"})
                with urllib.request.urlopen(req, timeout=2) as resp:
                    hud = json.loads(resp.read().decode("utf-8", "replace"))
                    if isinstance(hud, dict) and "damage" in hud:
                        for d in hud["damage"]:
                            self._last_dmg = max(self._last_dmg, d.get("id", 0))
                    if isinstance(hud, dict) and "events" in hud:
                        for e in hud["events"]:
                            self._last_evt = max(self._last_evt, e.get("id", 0))
                    data["hudmsg"] = hud
            except Exception:
                data["hudmsg"] = {}

            # 解析敌方/友方
            enemies = []
            allies = []
            player = None
            aircraft = []
            capture_zones = []

            for obj in data["map_obj"]:
                if not isinstance(obj, dict):
                    continue
                color = obj.get("color", "")
                icon = obj.get("icon", "")
                otype = obj.get("type", "")
                x = obj.get("x", 0)
                y = obj.get("y", 0)
                dx = obj.get("dx", 0)
                dy = obj.get("dy", 0)

                if icon == "Player":
                    player = obj
                elif otype == "aircraft":
                    aircraft.append(obj)
                elif otype == "capture_zone":
                    capture_zones.append(obj)
                elif otype == "ground_model":
                    if "#fa0C00" in color or "#FA0C00" in color or "#e10B00" in color or "#f01E00" in color:
                        enemies.append(("tank", obj))
                    elif "#174DFF" in color or "#134AFF" in color or "#185AFF" in color:
                        allies.append(("tank", obj))

                if otype == "aircraft":
                    if "#fa0C00" in color or "#FA0C00" in color or "#e10B00" in color or "#f01E00" in color:
                        enemies.append(("air", obj))
                    elif "#174DFF" in color or "#134AFF" in color:
                        allies.append(("air", obj))

            data["player"] = player
            data["enemies"] = enemies
            data["allies"] = allies
            data["aircraft"] = aircraft
            data["capture_zones"] = capture_zones

            # 计算距离和方位
            if player:
                px, py = player.get("x", 0), player.get("y", 0)
                enhanced_enemies = []
                for kind, e in enemies:
                    ex, ey = e.get("x", 0), e.get("y", 0)
                    dx_m = (ex - px) * MAP_MAX
                    dy_m = (ey - py) * MAP_MAX
                    dist = math.sqrt(dx_m**2 + dy_m**2)
                    bearing = math.degrees(math.atan2(dy_m, dx_m))
                    enhanced_enemies.append({
                        "kind": kind,
                        "icon": e.get("icon", "?"),
                        "color": e.get("color", ""),
                        "dist": dist,
                        "bearing": bearing,
                        "x": ex, "y": ey,
                        "dx": e.get("dx", 0),
                        "dy": e.get("dy", 0),
                    })
                enhanced_enemies.sort(key=lambda x: x["dist"])
                data["enemies_enhanced"] = enhanced_enemies
            else:
                data["enemies_enhanced"] = []

            self.data_ready.emit(data)
            time.sleep(0.2)  # 5Hz


# ========== HUD 面板 ==========

class HudPanel(QFrame):
    """单个 HUD 面板"""
    def __init__(self, title, width=240):
        super().__init__()
        self.title = title
        self.fixed_width = width
        self.setFixedWidth(width)
        self.setStyleSheet("""
            QFrame {
                background-color: rgba(13, 13, 18, 200);
                border: 1px solid rgba(58, 58, 72, 180);
                border-radius: 6px;
            }
            QLabel {
                color: #e2e8f0;
                font-size: 11px;
                font-family: Consolas, "Microsoft YaHei", sans-serif;
            }
            QLabel#title {
                font-size: 11px;
                font-weight: bold;
                color: #e2e8f0;
                padding: 2px 0px;
            }
            QLabel#enemy {
                color: #f7c1c1;
                font-size: 10px;
            }
            QLabel#ally {
                color: #85b7eb;
                font-size: 10px;
            }
            QLabel#warn {
                color: #fac775;
                font-size: 10px;
            }
            QLabel#ok {
                color: #97c459;
                font-size: 11px;
            }
            QLabel#muted {
                color: #888880;
                font-size: 9px;
            }
            QLabel#alert {
                color: #f7c1c1;
                font-size: 12px;
                font-weight: bold;
            }
        """)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(3)

        self.title_label = QLabel(title)
        self.title_label.setObjectName("title")
        layout.addWidget(self.title_label)

        # 分隔线
        sep = QFrame()
        sep.setFixedHeight(1)
        sep.setStyleSheet("background-color: rgba(58, 58, 72, 120);")
        layout.addWidget(sep)

        self.content_layout = layout

    def clear_content(self):
        while self.content_layout.count() > 2:
            item = self.content_layout.takeAt(2)
            if item.widget():
                item.widget().deleteLater()

    def add_label(self, text, obj_name=""):
        label = QLabel(text)
        if obj_name:
            label.setObjectName(obj_name)
        self.content_layout.addWidget(label)
        return label


class RadarWidget(QWidget):
    """中央方位雷达"""
    def __init__(self, size=140):
        super().__init__()
        self.setFixedSize(size + 20, size + 20)
        self.size_val = size
        self.enemies = []
        self.allies = []
        self.player_pos = None
        self.capture_zones = []

    def update_data(self, data):
        self.enemies = data.get("enemies_enhanced", [])
        self.allies_raw = []
        player = data.get("player")
        if player:
            px, py = player.get("x", 0), player.get("y", 0)
            for kind, a in data.get("allies", []):
                ax, ay = a.get("x", 0), a.get("y", 0)
                dx_m = (ax - px) * MAP_MAX
                dy_m = (ay - py) * MAP_MAX
                dist = math.sqrt(dx_m**2 + dy_m**2)
                bearing = math.degrees(math.atan2(dy_m, dx_m))
                self.allies_raw.append({"dist": dist, "bearing": bearing})
        self.capture_zones = data.get("capture_zones", [])
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        cx = self.width() // 2
        cy = self.height() // 2
        r = self.size_val // 2

        # 背景圆
        painter.setBrush(QBrush(QColor(13, 13, 18, 180)))
        painter.setPen(QPen(QColor(58, 58, 72, 150), 0.5))
        painter.drawEllipse(QPoint(cx, cy), r, r)

        # 内圈
        painter.setPen(QPen(QColor(42, 42, 48, 100), 0.5))
        painter.drawEllipse(QPoint(cx, cy), r * 2 // 3, r * 2 // 3)
        painter.drawEllipse(QPoint(cx, cy), r // 3, r // 3)

        # 方位文字
        font = QFont("sans-serif", 8)
        painter.setFont(font)
        painter.setPen(QColor(136, 136, 128))
        painter.drawText(cx - 4, cy - r + 12, "N")
        painter.drawText(cx - 4, cy + r - 4, "S")
        painter.drawText(cx + r - 8, cy + 4, "E")
        painter.drawText(cx - r + 2, cy + 4, "W")

        # 玩家（中心）
        painter.setBrush(QBrush(QColor(250, 200, 30)))
        painter.setPen(Qt.NoPen)
        painter.drawEllipse(QPoint(cx, cy), 5, 5)

        # 最大显示距离（归一化到半径）
        max_dist = 2000  # 显示2km内

        # 敌人
        for e in self.enemies[:8]:
            dist = e["dist"]
            if dist > max_dist:
                dist = max_dist  # 限制在边缘
            bearing = math.radians(e["bearing"])
            px = cx + int(dist / max_dist * r * math.cos(bearing))
            py = cy + int(dist / max_dist * r * math.sin(bearing))

            if e["kind"] == "air":
                # 飞机用三角形
                painter.setBrush(QBrush(QColor(239, 159, 39, 230)))
                painter.setPen(Qt.NoPen)
                poly = QPolygonF([
                    QPoint(px, py - 6),
                    QPoint(px + 5, py + 4),
                    QPoint(px - 5, py + 4),
                ])
                painter.drawPolygon(poly)
            else:
                # 坦克用圆点
                painter.setBrush(QBrush(QColor(226, 75, 74, 230)))
                painter.setPen(Qt.NoPen)
                painter.drawEllipse(QPoint(px, py), 5, 5)

            # 距离标注
            painter.setPen(QColor(247, 193, 193))
            painter.setFont(QFont("sans-serif", 7))
            painter.drawText(px + 7, py + 3, f"{int(e['dist'])}m")

        # 友军
        for a in self.allies_raw[:6]:
            dist = a["dist"]
            if dist > max_dist:
                dist = max_dist
            bearing = math.radians(a["bearing"])
            px = cx + int(dist / max_dist * r * math.cos(bearing))
            py = cy + int(dist / max_dist * r * math.sin(bearing))
            painter.setBrush(QBrush(QColor(55, 138, 221, 180)))
            painter.setPen(Qt.NoPen)
            painter.drawEllipse(QPoint(px, py), 4, 4)


class AlertBar(QFrame):
    """底部警报条"""
    def __init__(self):
        super().__init__()
        self.setFixedHeight(28)
        self.setStyleSheet("""
            QFrame {
                background-color: rgba(61, 16, 16, 200);
                border: 1px solid rgba(226, 75, 74, 200);
                border-radius: 4px;
            }
        """)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 2, 10, 2)
        self.label = QLabel("")
        self.label.setStyleSheet("color: #f7c1c1; font-size: 12px; font-weight: bold; font-family: 'Microsoft YaHei', sans-serif;")
        self.label.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.label)

    def set_alert(self, text):
        self.label.setText(text)
        self.setVisible(bool(text))


# ========== 主 HUD 窗口 ==========

class HudOverlay(QWidget):
    """透明覆盖窗口"""
    def __init__(self):
        super().__init__()
        # 透明、无边框、置顶、工具窗口（不在任务栏显示）
        self.setWindowFlags(
            Qt.FramelessWindowHint |
            Qt.WindowStaysOnTopHint |
            Qt.Tool
        )
        # 透明背景
        self.setAttribute(Qt.WA_TranslucentBackground)
        # 鼠标穿透
        self.setAttribute(Qt.WA_TransparentForMouseEvents)

        # 全屏覆盖
        screen = QApplication.desktop().screenGeometry()
        self.setGeometry(screen)

    def showEvent(self, event):
        """窗口显示后，手动设置 Win32 扩展样式确保鼠标穿透"""
        super().showEvent(event)
        try:
            import ctypes
            hwnd = int(self.winId())
            GWL_EXSTYLE = -20
            WS_EX_LAYERED = 0x00080000
            WS_EX_TRANSPARENT = 0x00000020
            WS_EX_TOPMOST = 0x00000008
            WS_EX_NOACTIVATE = 0x08000000
            WS_EX_TOOLWINDOW = 0x00000080
            # 获取当前样式
            style = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            # 添加鼠标穿透 + 不激活
            style |= WS_EX_TRANSPARENT | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW
            ctypes.windll.user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style)
            # 设置为点击穿透的分层窗口
            # 使用 SetLayeredWindowAttributes 确保透明度
            LWA_COLORKEY = 0x00000001
            LWA_ALPHA = 0x00000002
            # 完全不透明（alpha=255），靠 WA_TranslucentBackground 实现局部透明
            ctypes.windll.user32.SetLayeredWindowAttributes(hwnd, 0, 255, LWA_ALPHA)
        except Exception as e:
            print(f"Win32 样式设置失败: {e}")

        # 布局
        from PyQt5.QtWidgets import QGridLayout
        layout = QGridLayout(self)
        layout.setContentsMargins(15, 15, 15, 15)
        layout.setSpacing(8)

        # 左上：战场态势
        self.threat_panel = HudPanel("战场态势", 240)
        layout.addWidget(self.threat_panel, 0, 0, alignment=Qt.AlignTop | Qt.AlignLeft)

        # 右上：载具状态
        self.vehicle_panel = HudPanel("载具状态", 240)
        layout.addWidget(self.vehicle_panel, 0, 2, alignment=Qt.AlignTop | Qt.AlignRight)

        # 中央：雷达
        self.radar = RadarWidget(140)
        layout.addWidget(self.radar, 1, 1, alignment=Qt.AlignCenter)

        # 左下：占领区
        self.zone_panel = HudPanel("占领区", 200)
        layout.addWidget(self.zone_panel, 2, 0, alignment=Qt.AlignBottom | Qt.AlignLeft)

        # 右下：事件流
        self.event_panel = HudPanel("事件流", 200)
        layout.addWidget(self.event_panel, 2, 2, alignment=Qt.AlignBottom | Qt.AlignRight)

        # 中下：警报条
        self.alert_bar = AlertBar()
        self.alert_bar.setVisible(False)
        layout.addWidget(self.alert_bar, 3, 0, 1, 3, alignment=Qt.AlignBottom | Qt.AlignHCenter)

        # 设置列拉伸
        layout.setColumnStretch(0, 1)
        layout.setColumnStretch(1, 3)
        layout.setColumnStretch(2, 1)
        layout.setRowStretch(0, 1)
        layout.setRowStretch(1, 3)
        layout.setRowStretch(2, 1)
        layout.setRowStretch(3, 0)

        self.last_hud_msgs = []

    def update_hud(self, data):
        """更新所有面板"""
        self.update_threat_panel(data)
        self.update_vehicle_panel(data)
        self.update_zone_panel(data)
        self.update_event_panel(data)
        self.radar.update_data(data)
        self.update_alert(data)

    def update_threat_panel(self, data):
        self.threat_panel.clear_content()
        enemies = data.get("enemies_enhanced", [])

        if not enemies:
            self.threat_panel.add_label("附近无敌军", "ok")
        else:
            shown = 0
            for e in enemies[:4]:
                kind_label = "飞机" if e["kind"] == "air" else ""
                icon_map = {
                    "HeavyTank": "重坦", "MediumTank": "中坦",
                    "LightTank": "轻坦", "TankDestroyer": "坦歼",
                    "Fighter": "战斗机", "Bomber": "轰炸机", "Assault": "攻击机",
                }
                label_text = icon_map.get(e["icon"], e["icon"])
                if e["kind"] == "air":
                    label_text += " (空)"

                dist_text = f"{int(e['dist'])}m"
                # 方位转中文
                b = e["bearing"]
                if -22.5 <= b < 22.5:
                    direction = "E"
                elif 22.5 <= b < 67.5:
                    direction = "SE"
                elif 67.5 <= b < 112.5:
                    direction = "S"
                elif 112.5 <= b < 157.5:
                    direction = "SW"
                elif b >= 157.5 or b < -157.5:
                    direction = "W"
                elif -157.5 <= b < -112.5:
                    direction = "NW"
                elif -112.5 <= b < -67.5:
                    direction = "N"
                else:
                    direction = "NE"

                text = f"  {label_text}  {dist_text}  {direction}"
                obj = "warn" if e["kind"] == "air" else "enemy"
                self.threat_panel.add_label(text, obj)
                shown += 1

        # 友军计数
        ally_count = len(data.get("allies", []))
        self.threat_panel.add_label(f"友军: {ally_count} 活跃", "ally")

    def update_vehicle_panel(self, data):
        self.vehicle_panel.clear_content()
        ind = data.get("indicators", {})

        if not ind or not ind.get("valid", False):
            self.vehicle_panel.add_label("未在战斗中", "muted")
            return

        veh_type = ind.get("type", "?")
        if "/" in veh_type:
            veh_type = veh_type.split("/")[-1]
        self.vehicle_panel.add_label(f"载具: {veh_type}", "")

        # 乘员
        crew_cur = ind.get("crew_current", 0)
        crew_total = ind.get("crew_total", 0)
        if crew_total > 0:
            crew_pct = crew_cur / crew_total
            color = "ok" if crew_pct > 0.5 else ("warn" if crew_pct > 0.25 else "alert")
            self.vehicle_panel.add_label(f"乘员: {int(crew_cur)}/{int(crew_total)}", color)
        else:
            self.vehicle_panel.add_label("乘员: -", "muted")

        # 一级弹药
        ammo = ind.get("first_stage_ammo", -1)
        if ammo >= 0:
            self.vehicle_panel.add_label(f"一级弹药: {int(ammo)}", "warn" if ammo < 5 else "")
        else:
            self.vehicle_panel.add_label("一级弹药: -", "muted")

        # 炮手/驾驶员
        gunner = ind.get("gunner_state", 0)
        driver = ind.get("driver_state", 0)
        gunner_text = "OK" if gunner == 0 else "受损"
        driver_text = "OK" if driver == 0 else "受损"
        self.vehicle_panel.add_label(
            f"炮手: {gunner_text}  驾驶员: {driver_text}",
            "ok" if gunner == 0 and driver == 0 else "alert"
        )

        # 稳定器
        stab = ind.get("stabilizer", -1)
        if stab >= 0:
            self.vehicle_panel.add_label(f"稳定器: {'ON' if stab > 0 else 'OFF'}", "ok" if stab > 0 else "muted")

        # 激光告警
        lws = ind.get("lws", -1)
        if lws >= 0:
            lws_text = "被锁定!" if lws > 0 else "安全"
            self.vehicle_panel.add_label(f"激光告警: {lws_text}", "alert" if lws > 0 else "muted")

        # 速度
        speed = ind.get("speed", 0)
        gear = ind.get("gear", 0)
        self.vehicle_panel.add_label(f"速度: {abs(speed):.0f} km/h  档: {int(gear)}", "")

    def update_zone_panel(self, data):
        self.zone_panel.clear_content()
        zones = data.get("capture_zones", [])

        if not zones:
            self.zone_panel.add_label("无占领区数据", "muted")
            return

        for zone in zones:
            label = zone.get("zone_label", "?")
            color = zone.get("color", "")
            if "#174DFF" in color or "#134AFF" in color:
                status = "我方"
                obj = "ok"
            elif "#fa0C00" in color or "#FA0C00" in color:
                status = "敌方"
                obj = "enemy"
            else:
                status = "中立"
                obj = "warn"
            self.zone_panel.add_label(f"  {label}区: {status}", obj)

    def update_event_panel(self, data):
        self.event_panel.clear_content()
        hud = data.get("hudmsg", {})
        damages = hud.get("damage", []) if isinstance(hud, dict) else []

        # 只保留最近5条
        recent = damages[-5:] if damages else []
        if not recent:
            self.event_panel.add_label("暂无事件", "muted")
            return

        for d in recent:
            msg = d.get("msg", "")
            enemy = d.get("enemy", False)
            obj = "enemy" if enemy else "ok"
            # 截断长消息
            if len(msg) > 28:
                msg = msg[:25] + "..."
            self.event_panel.add_label(msg, obj)

    def update_alert(self, data):
        enemies = data.get("enemies_enhanced", [])
        ind = data.get("indicators", {})

        # 优先级1：激光告警
        lws = ind.get("lws", -1)
        if lws is not None and lws > 0:
            self.alert_bar.set_alert("⚠ 被激光锁定！")
            return

        # 优先级2：敌机接近
        for e in enemies:
            if e["kind"] == "air" and e["dist"] < 1000:
                icon_map = {"Fighter": "战斗机", "Bomber": "轰炸机", "Assault": "攻击机"}
                name = icon_map.get(e["icon"], "敌机")
                b = e["bearing"]
                if -45 <= b < 45:
                    d = "东"
                elif 45 <= b < 135:
                    d = "南"
                elif b >= 135 or b < -135:
                    d = "西"
                else:
                    d = "北"
                self.alert_bar.set_alert(f"⚠ {name}接近 {int(e['dist'])}m · 方位{d}")
                return

        # 优先级3：近距敌人
        for e in enemies:
            if e["kind"] == "tank" and e["dist"] < 200:
                icon_map = {"HeavyTank": "重坦", "MediumTank": "中坦", "LightTank": "轻坦", "TankDestroyer": "坦歼"}
                name = icon_map.get(e["icon"], "敌人")
                self.alert_bar.set_alert(f"⚠ {name}接近 {int(e['dist'])}m！")
                return

        # 优先级4：乘员危急
        crew_cur = ind.get("crew_current", 0)
        crew_total = ind.get("crew_total", 0)
        if crew_total > 0 and crew_cur / crew_total <= 0.34:
            self.alert_bar.set_alert(f"⚠ 乘员危急 {int(crew_cur)}/{int(crew_total)}")
            return

        self.alert_bar.set_alert("")


# ========== 主程序 ==========

def main():
    app = QApplication(sys.argv)

    # 创建 HUD
    hud = HudOverlay()
    hud.show()
    print(f"HUD窗口已创建, winId={hud.winId()}, visible={hud.isVisible()}", flush=True)

    # 创建数据采集线程
    fetcher = DataFetcher()

    # 信号连接（跨线程安全更新 UI）
    fetcher.data_ready.connect(hud.update_hud, Qt.QueuedConnection)

    # 启动采集线程
    thread = threading.Thread(target=fetcher.run, daemon=True)
    thread.start()
    print("数据采集线程已启动", flush=True)

    print("=" * 50, flush=True)
    print("  War Thunder HUD 覆盖层已启动", flush=True)
    print("  数据来源: localhost:8111", flush=True)
    print("  按 Ctrl+C 退出", flush=True)
    print("=" * 50, flush=True)
    print()
    print("注意:")
    print("  1. 游戏需设为「窗口化」或「无边框窗口」模式")
    print("  2. HUD 窗口透明、置顶、鼠标穿透")
    print("  3. 不会修改游戏文件，不会被封号")
    print()

    # 定时器保持运行
    timer = QTimer()
    timer.timeout.connect(lambda: None)
    timer.start(1000)

    try:
        app.exec_()
    except KeyboardInterrupt:
        pass
    finally:
        fetcher.stop()


if __name__ == "__main__":
    main()
