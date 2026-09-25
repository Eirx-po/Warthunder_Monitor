"""
War Thunder Air Battle HUD - Data Fetcher Module
8111 port data acquisition for air combat

Key difference from ground battles:
- /state IS available (returns lat/lon, altitude, airspeed, AoA, G-force)
- map_obj coordinates are lat/lon (not normalized 0-1)
- Use Haversine formula for distance calculation
"""
import json
import math
import os
import sys
import time
import threading
import urllib.request
from collections import deque

# 允许直接运行本文件时也能 import 上级目录的共享模块
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from wt_common import (is_enemy_color, is_friend_color, norm180,
                       bearing_compass, heading_compass,
                       TargetManager)

BASE = "http://localhost:8111"
EARTH_RADIUS_M = 6371000.0  # Earth radius in meters


def haversine(lat1, lon1, lat2, lon2):
    """Calculate distance between two lat/lon points in meters"""
    rlat1 = math.radians(lat1)
    rlat2 = math.radians(lat2)
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat/2)**2 + math.cos(rlat1) * math.cos(rlat2) * math.sin(dlon/2)**2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(a))


def bearing(lat1, lon1, lat2, lon2):
    """Calculate bearing from point 1 to point 2 in degrees (0-360)"""
    rlat1 = math.radians(lat1)
    rlat2 = math.radians(lat2)
    dlon = math.radians(lon2 - lon1)
    y = math.sin(dlon) * math.cos(rlat2)
    x = math.cos(rlat1) * math.sin(rlat2) - math.sin(rlat1) * math.cos(rlat2) * math.cos(dlon)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def angle_diff(a1, a2):
    """Smallest angular difference between two bearings (-180 to 180)"""
    d = (a1 - a2 + 540) % 360 - 180
    return d


class Track:
    """Aircraft target track with history and prediction"""
    def __init__(self, track_id, icon, color, x, y, heading=0, ts=None):
        self.id = track_id
        self.icon = icon
        self.color = color
        self.x = x
        self.y = y
        self.heading = heading
        self.speed_kmh = 0
        # 归一化坐标 1.0 对应的米数（map_max-map_min）。由 TrackManager 每帧同步，
        # 空战大地图是 65536，写死 4096 会差 16 倍（2026-09-25 修）
        self.map_span = 4096.0
        self.history = deque(maxlen=20)  # (ts, x, y)
        self.last_update = ts if ts else time.time()
        self.lost = False
        self.lost_since = None
        self.alive_count = 0
        self.history.append((self.last_update, x, y))

    def update(self, x, y, heading, ts=None):
        now = ts if ts else time.time()
        # Compute speed from history
        if self.history:
            t0, x0, y0 = self.history[-1]
            dt = now - t0
            if dt > 0:
                # Use haversine for lat/lon, fallback for normalized
                if abs(x) > 1.0:
                    dist = haversine(x0, y0, x, y)
                else:
                    dist = math.sqrt((x - x0)**2 * self.map_span**2
                                     + (y - y0)**2 * self.map_span**2)
                self.speed_kmh = (dist / dt) * 3.6
        self.x, self.y, self.heading = x, y, heading
        self.last_update = now
        self.lost = False
        self.lost_since = None
        self.alive_count += 1
        self.history.append((now, x, y))

    def mark_lost(self, ts=None):
        if not self.lost:
            self.lost = True
            self.lost_since = ts if ts else time.time()

    def predict(self, dt_sec):
        """Predict position dt_sec into future based on last heading/speed"""
        if self.speed_kmh <= 0:
            return self.x, self.y
        dist_m = (self.speed_kmh / 3.6) * dt_sec
        brg = math.radians(self.heading)
        # For lat/lon: approx move (small distances)
        if abs(self.x) > 1.0:
            # Approximate: 1 deg lat ~ 111320m, 1 deg lon ~ 111320*cos(lat)
            d_lat = (dist_m * math.cos(brg)) / 111320.0
            d_lon = (dist_m * math.sin(brg)) / (111320.0 * max(math.cos(math.radians(self.x)), 0.1))
            return self.x + d_lat, self.y + d_lon
        else:
            # Normalized coords
            # heading 是罗盘角（0=北，顺时针），地图坐标 +y=南：
            # 东向分量 = dist*sin(brg)，南向分量 = -dist*cos(brg)。
            # （旧版把罗盘角直接当 atan2(dy,dx) 用 → 预测点方向错 90°）
            dx_m = dist_m * math.sin(brg)
            dy_m = -dist_m * math.cos(brg)
            return (self.x + dx_m / self.map_span,
                    self.y + dy_m / self.map_span)

    def age(self, ts=None):
        now = ts if ts else time.time()
        return now - self.last_update


class TrackManager:
    """Manages aircraft tracks, handles lost target prediction (RB mode)"""

    def __init__(self, match_threshold_m=3000, lost_timeout_s=6.0, max_lost_age_s=25.0):
        self.tracks = {}  # id -> Track
        self.match_threshold = match_threshold_m
        self.lost_timeout = lost_timeout_s
        self.max_lost_age = max_lost_age_s
        self._next_id = 1
        # 归一化坐标对应的真实米数，由 AirDataFetcher 每帧同步
        self.map_span = 4096.0

    def update(self, units):
        """
        Update tracks with current detected units.
        units: list of dicts with x, y, heading, icon, color, is_enemy
        Returns: (alive_tracks, lost_tracks)
        """
        now = time.time()
        matched = set()

        for u in units:
            # Find nearest existing alive track
            best_id = None
            best_dist = self.match_threshold
            for tid, t in self.tracks.items():
                if t.lost:
                    continue
                if abs(u["x"]) > 1.0:
                    d = haversine(t.x, t.y, u["x"], u["y"])
                else:
                    d = math.sqrt((t.x - u["x"])**2 * 4096**2 + (t.y - u["y"])**2 * 4096**2)
                if d < best_dist:
                    best_dist = d
                    best_id = tid

            if best_id is not None:
                t = self.tracks[best_id]
                t.map_span = self.map_span
                t.update(u["x"], u["y"], u.get("heading", t.heading), now)
                t.icon = u["icon"]  # update type
                matched.add(best_id)
            else:
                # New track
                tid = self._next_id
                self._next_id += 1
                t = Track(tid, u["icon"], u["color"], u["x"], u["y"],
                          u.get("heading", 0), now)
                t.map_span = self.map_span
                self.tracks[tid] = t
                matched.add(tid)

        # Mark unmatched alive tracks as lost
        for tid, t in self.tracks.items():
            if tid not in matched and not t.lost:
                t.mark_lost(now)

        # Remove very old lost tracks
        expired = [tid for tid, t in self.tracks.items()
                   if t.lost and t.age(now) > self.max_lost_age]
        for tid in expired:
            del self.tracks[tid]

        alive = [t for t in self.tracks.values() if not t.lost]
        lost = [t for t in self.tracks.values() if t.lost]
        return alive, lost


class AirDataFetcher(threading.Thread):
    """Background thread fetching 8111 air battle data at 10Hz"""

    def __init__(self):
        super().__init__(daemon=True)
        self.data = {}
        self.lock = threading.Lock()
        self.running = True
        self._last_evt = 0
        self._last_dmg = 0
        self._enemy_history = {}  # track_id -> [(timestamp, lat, lon)] for speed calc
        self._track_id_counter = 0
        self._tas_hist = deque(maxlen=40)   # (ts, tas_m/s) 供 Ps 计算
        self._ps_last = None
        # Track manager for RB mode (target tracking with lost prediction)
        self.track_manager = TrackManager()
        # 目标聚类管理器：合并 8111 中同一目标的重复记录，保证一个敌人一个箭头
        self.target_mgr = TargetManager(cluster_radius_m=150.0,
                                        match_radius_m=600.0,
                                        smooth_alpha=0.35,
                                        ttl_s=3.0)
        # Game mode: "AB" (arcade) / "RB" (realistic) / "SB" (sim) / "unknown"
        self.game_mode = "unknown"
        self._mode_samples = []  # count of visible enemies per sample

    # Ps 计算用：TAS(ms) 的采样历史
    # 注意 (V/g) 会放大 dV/dt 的噪声（V=360m/s 时放大约 37 倍），
    # 所以时间窗不能太短、平滑不能太弱，否则数字会疯狂跳动。
    # 实机实测：0.8s 窗 + EMA 0.25 时抖动约 ±5，读数够稳且不至于太迟钝。
    PS_WINDOW_S = 0.8      # 差分时间窗，太短会抖、太长会迟钝
    PS_SMOOTH = 0.25       # EMA 平滑系数（越小越稳、也越迟钝）

    def _compute_ps(self, tas_kmh, vy_ms):
        """
        比能量变化率 Ps = Vy + (V/g)·(dV/dt)，单位 m/s。

        正 = 飞机在攒能量（能爬升或加速），负 = 在烧能量。
        这是推力与阻力之差的综合体现 —— 8111 给不出阻力值，
        （缺机翼面积/Cd/全重），但 Ps 不需要这些，直接可算。
        """
        try:
            v_ms = float(tas_kmh) / 3.6
            now = time.time()
            hist = self._tas_hist
            hist.append((now, v_ms))

            if len(hist) < 2:
                return vy_ms

            # 取时间窗内的最早样本做差分
            t0, v0 = hist[0]
            while len(hist) > 2 and (now - hist[1][0]) > self.PS_WINDOW_S:
                hist.popleft()
                t0, v0 = hist[0]

            dt = now - t0
            if dt < 0.15:                    # 窗口太短，差分噪声太大
                return self._ps_last

            dvdt = (v_ms - v0) / dt
            ps = vy_ms + (v_ms / 9.81) * dvdt
            # EMA 平滑
            if self._ps_last is None:
                self._ps_last = ps
            else:
                self._ps_last = (self._ps_last * (1 - self.PS_SMOOTH)
                                 + ps * self.PS_SMOOTH)
            return self._ps_last
        except Exception:
            return vy_ms

    def skip_nose_targets(self):
        """
        是否跳过正前方（12点）的目标。默认开启。

        用自由视角(C键)环视时**建议关掉**：8111 只提供 compass（机头航向），
        拿不到摄像机/视角朝向，此时"12点"是相对机头算的，
        与你实际看到的方向并不一致 —— 跳过逻辑就可能把你在看的
        目标给跳过去。

        WT_HUD_NOSE_SKIP=0 关闭（改为始终显示最近的目标）。
        """
        return os.environ.get("WT_HUD_NOSE_SKIP", "1") != "0"

    def show_ground_targets(self):
        """
        是否在 HUD 上**显示**地面目标（TARGET 面板 + 边缘箭头）。

        与 track_ground_targets() 的区别：那个管的是"采不采集"，
        这个管的是"显不显示"。

        默认关闭：空战里地面单位（防空炮/地堡/火炮/船只）数量远多于敌机
        （实测一局 45~78 个目标，绝大多数是地面），会把面板和箭头刷屏，
        而参考价值远低于敌机。

        需要时用 WT_HUD_GROUND_TARGETS=1 开启。
        """
        return os.environ.get("WT_HUD_GROUND_TARGETS", "") == "1"

    def track_ground_targets(self):
        """
        是否监测地面目标（防空炮 / SPAA / 坦克）。

        街机(AB)：保留 —— 地面目标可刷分，也有战术价值。
        历史(RB) / 全真(SB)：跳过 —— 「历史对空无需监测地面目标」，
                              且 RB 里刷地面靶意义不大，只会干扰空情判断。

        可用环境变量 WT_HUD_GROUND=1 / 0 强制覆盖自动判定。
        """
        override = os.environ.get("WT_HUD_GROUND", "").strip()
        if override == "1":
            return True
        if override == "0":
            return False
        return self.game_mode not in ("RB", "SB")

    # 「12点」锥角（度）：机头左右各这么多度内算正前方。
    # 正前方的目标玩家自己直接看得见，HUD 再提示就是噪音 → 选目标时跳过。
    NOSE_CONE_DEG = 25.0

    @staticmethod
    def _map_span(mi):
        """
        归一化坐标(0~1) → 米 的换算系数。

        ⚠ 关键坑：不能用 map_max[0] 直接当跨度。
        实测本机一局空战：
            map_max = [32768, 32768]
            map_min = [-32768, -32768]
            grid_size = [65536, 65536]
        归一化坐标 0~1 对应 map_min ~ map_max 这一段，
        所以真实跨度 = map_max - map_min = 65536 = grid_size。

        用本机 TAS 反推标定过：垂直俯冲时按 65536 算出的水平地速与
        sqrt(TAS² - Vy²) 吻合（误差 0.6%）；
        而按 map_max(32768) 低估一半，按 4096 更是低估 16 倍。
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
        return 4096.0

    def _select_target(self, targets, p_hdg):
        """
        选中要显示的目标：**最近的那个**。

        默认只看空中目标（地面目标由 show_ground_targets() 控制，
        默认不显示 —— 数量太多会把面板刷屏）。

        例外（可用 WT_HUD_NOSE_SKIP=0 关闭）：如果最近的目标正好在机头
        正前方（12点方向 ±NOSE_CONE_DEG），就跳过它、选下一个最近的 ——
        正前方的目标玩家自己直接看得见，HUD 的价值在于提示视野外的目标。
        注意：8111 只有 compass(机头航向)、没有视角朝向，
        用 C 自由视角环视时这个判定与实际所见不符，此时应关闭跳过。

        若所有候选都在正前方，则退回最近的那一个。
        """
        if not self.show_ground_targets():
            targets = [t for t in targets if t.kind == "air"]
        if not targets:
            return None
        ordered = sorted(targets, key=lambda t: t.dist)
        if not self.skip_nose_targets():
            return ordered[0]          # 自由视角：始终显示最近的
        for t in ordered:
            if abs(norm180(t.bearing - p_hdg)) > self.NOSE_CONE_DEG:
                return t
        return ordered[0]

    def fetch(self, path):
        try:
            req = urllib.request.Request(BASE + path, headers={"User-Agent": "WT-AirHUD"})
            with urllib.request.urlopen(req, timeout=1.5) as resp:
                return json.loads(resp.read().decode("utf-8", "replace"))
        except Exception:
            return None

    def run(self):
        while self.running:
            data = {"ts": time.time(), "in_battle": False}

            # /state - own aircraft full telemetry (AIR BATTLES ONLY)
            state = self.fetch("/state")
            data["state"] = state if isinstance(state, dict) else {}

            # /indicators - instruments
            ind = self.fetch("/indicators")
            data["indicators"] = ind if isinstance(ind, dict) else {}

            # /map_obj - all units on minimap
            mobj = self.fetch("/map_obj.json")
            data["map_obj"] = mobj if isinstance(mobj, list) else []

            # /map_info - map coordinate system
            mi = self.fetch("/map_info.json")
            data["map_info"] = mi if isinstance(mi, dict) else {}

            # /hudmsg - events
            try:
                url = f"{BASE}/hudmsg?lastEvt={self._last_evt}&lastDmg={self._last_dmg}"
                req = urllib.request.Request(url, headers={"User-Agent": "WT-AirHUD"})
                with urllib.request.urlopen(req, timeout=1.5) as resp:
                    hud = json.loads(resp.read().decode("utf-8", "replace"))
                    if isinstance(hud, dict):
                        for d in hud.get("damage", []):
                            self._last_dmg = max(self._last_dmg, d.get("id", 0))
                        for e in hud.get("events", []):
                            self._last_evt = max(self._last_evt, e.get("id", 0))
                    data["hudmsg"] = hud
            except Exception:
                data["hudmsg"] = {}

            # Determine if in battle
            data["in_battle"] = (data["state"].get("valid", False) or
                                 data["indicators"].get("valid", False))

            # Parse state data
            self._parse_state(data)

            # Parse map objects
            self._parse_map_objects(data)

            # Compute combat metrics
            self._compute_metrics(data)

            with self.lock:
                self.data = data

            time.sleep(0.1)  # 10Hz

    def _parse_state(self, data):
        """Parse /state into structured flight data"""
        s = data.get("state", {})
        if not s.get("valid", False):
            data["flight"] = None
            return

        flight = {
            "altitude": s.get("H, m", 0),           # meters
            "tas": s.get("TAS, km/h", 0),           # true airspeed
            "ias": s.get("IAS, km/h", 0),           # indicated airspeed
            "mach": s.get("M", 0),                  # mach number
            "aoa": s.get("AoA, deg", 0),            # angle of attack
            "aos": s.get("AoS, deg", 0),            # angle of sideslip
            "g_force": s.get("Ny", 1.0),            # load factor (G)
            "v_speed": s.get("Vy, m/s", 0),         # vertical speed
            "roll_rate": s.get("Wx, deg/s", 0),     # roll rate
            "fuel": s.get("Mfuel, kg", 0),          # current fuel
            "fuel_init": s.get("Mfuel0, kg", 0),    # initial fuel
            "aileron": s.get("aileron, %", 0),
            "elevator": s.get("elevator, %", 0),
            "rudder": s.get("rudder, %", 0),
            "flaps": s.get("flaps, %", 0),
        }

        # 比能量变化率 Ps = Vy + (V/g)·(dV/dt)
        # 单位 m/s，直观含义：每秒净赚(正)/净亏(负)多少能量高度。
        # 不需要飞机质量，用 TAS 的时间差分就能算 —— 阻力算不出来
        # （缺机翼面积/Cd/全重），但 Ps 是阻力+推力的综合结果，
        # 格斗时判断能不能咬住对方更实用。
        flight["ps"] = self._compute_ps(flight["tas"], flight["v_speed"])

        # Parse engine data (1, 2, 3, 4...)
        engines = []
        for i in range(1, 9):
            prefix = f"{i}"
            if f"throttle {prefix}, %" in s:
                engines.append({
                    "throttle": s.get(f"throttle {prefix}, %", 0),
                    "rpm": s.get(f"RPM {prefix}", 0),
                    "power": s.get(f"power {prefix}, hp", 0),
                    "oil_temp": s.get(f"oil temp {prefix}, C", 0),
                    "thrust": s.get(f"thrust {prefix}, kgs", 0),
                    "efficiency": s.get(f"efficiency {prefix}, %", 0),
                    "manifold_pressure": s.get(f"manifold pressure {prefix}, atm", 0),
                    "pitch": s.get(f"pitch {prefix}, deg", 0),
                    "magneto": s.get(f"magneto {prefix}", 0),
                    "mixture": s.get(f"mixture {prefix}, %", 0),
                })
            else:
                break
        flight["engines"] = engines

        # Get position from indicators (compass, attitude)
        ind = data.get("indicators", {})
        flight["compass"] = ind.get("compass", 0)
        flight["roll"] = ind.get("aviahorizon_roll", 0) or ind.get("bank", 0)
        flight["pitch"] = ind.get("aviahorizon_pitch", 0)
        flight["g_meter"] = ind.get("g_meter", 0)
        flight["g_max"] = ind.get("g_meter_max", 0)
        flight["g_min"] = ind.get("g_meter_min", 0)
        flight["type"] = ind.get("type", "?")

        data["flight"] = flight

    def _parse_map_objects(self, data):
        """Parse map_obj into structured unit data"""
        mobj = data.get("map_obj", [])
        flight = data.get("flight", {})

        # Own position from /state (air battles have lat/lon)
        own_lat = None
        own_lon = None

        # In air battles, state doesn't have lat/lon directly
        # But Player object in map_obj should have coordinates
        player = None
        enemies = []
        allies = []
        airfields = []
        zones = []
        targets = []   # 聚类去重后的逻辑目标（供边缘箭头使用）

        for obj in mobj:
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
            elif otype == "capture_zone":
                zones.append(obj)
            elif otype == "airfield":
                airfields.append(obj)
            elif otype in ("aircraft", "ground_model"):
                # 历史(RB)/全真(SB)空战不监测地面目标：直接跳过，
                # 让威胁列表、边缘箭头、战斗指标保持一致（都不含地面单位）。
                if otype == "ground_model" and not self.track_ground_targets():
                    continue
                # 空战地图同样有敌方地面单位（防空炮 / SPAA，颜色 #f00C00）。
                # 旧代码只处理 aircraft，导致全部地面目标被漏掉。
                # 同时旧的颜色白名单漏了 #f00C00 变体，改用 RGB 色系判定。
                is_enemy = is_enemy_color(color)
                is_friend = is_friend_color(color)
                unit = {
                    "icon": icon,
                    "color": color,
                    "kind": "air" if otype == "aircraft" else "ground",
                    "x": x, "y": y,
                    "dx": dx, "dy": dy,
                    "is_enemy": is_enemy,
                    "is_friend": is_friend,
                }
                if is_enemy:
                    enemies.append(unit)
                elif is_friend:
                    allies.append(unit)

        # Calculate distance and bearing to enemies
        if player:
            px, py = player.get("x", 0), player.get("y", 0)

            for e in enemies:
                # In air battles, coordinates might be lat/lon or normalized
                # Try to determine based on value range
                if abs(px) > 1.0 or abs(e["x"]) > 1.0:
                    # Likely lat/lon
                    e["dist"] = haversine(px, py, e["x"], e["y"])
                    e["bearing"] = bearing(px, py, e["x"], e["y"])
                else:
                    # Normalized coordinates (fallback)
                    mi = data.get("map_info", {})
                    span = self._map_span(mi)
                    dx_m = (e["x"] - px) * span
                    dy_m = (e["y"] - py) * span
                    e["dist"] = math.sqrt(dx_m**2 + dy_m**2)
                    # 8111 地图坐标 +y=南，必须转成罗盘角（0=北，顺时针）；
                    # 直接 atan2(dy,dx) 会整体偏 90°（2026-09-25 修）
                    e["bearing"] = bearing_compass(dx_m, dy_m)

                # Heading from dx/dy (aircraft direction vector)
                if e["dx"] != 0 or e["dy"] != 0:
                    e["heading"] = heading_compass(e["dx"], e["dy"])
                else:
                    e["heading"] = 0

                # Aspect angle: enemy heading relative to bearing from player to enemy
                # If aspect ~0, enemy is heading toward player
                # If aspect ~180, enemy is heading away (tail chase)
                e["aspect"] = angle_diff(e["heading"], e["bearing"])

                # 速度统一由聚类后的 TargetCluster.update_motion() 估算。
                # （早期这里用「图标+颜色+坐标」当历史 key，而坐标每帧都在变，
                #   历史长度恒为 1 → len(hist)>=2 永不成立 → 速度永远是 0。
                #   现在改用聚类后稳定的 cid 维护历史，见 wt_common。）
                e.setdefault("speed_kmh", 0)

            enemies.sort(key=lambda x: x.get("dist", 99999))

            # ---- 目标聚类去重：一个逻辑目标只产生一个箭头 ----
            # 8111 会把一个防空阵地拆成多条记录（3门炮+1辆SPAA，坐标只差几米），
            # 逐条画箭头会重叠成"残影"。这里先按位置合并。
            #
            # ⚠ 必须把真实地图跨度同步给聚类管理器：它内部用归一化坐标做
            #   匹配/聚类/测速，若仍按 4096 换算，在 65536 米的空战地图上
            #   匹配半径和速度都会差 16 倍（实测敌机 780km/h 被算成 36km/h）。
            self.target_mgr.map_span = self._map_span(data.get("map_info", {}))
            units = []
            for e in enemies:
                units.append({
                    "x": e["x"], "y": e["y"],
                    "kind": e.get("kind", "air"),
                    "icon": e["icon"],
                    "bearing": e.get("bearing", 0.0),
                    "dist": e.get("dist", 0.0),
                    "heading": e.get("heading", 0.0),
                })
            targets = self.target_mgr.update(units)
            # 玩家罗盘朝向（用于把绝对方位换算成相对方位）
            p_hdg = flight.get("compass", 0) if flight else 0
            for t in targets:
                # bearing 与 p_hdg 已统一为罗盘角（0=北，顺时针），
                # 直接相减即得相对方位（0=正前，右侧为正）
                t.rel_bearing = norm180(t.bearing - p_hdg)
                # 进入角：目标朝向相对"我→目标"方位，0=正对着我冲过来
                t.aspect = norm180(t.heading - t.bearing)
                # 注：目标的**高度拿不到** —— map_obj 只有二维小地图坐标，
                # 不含 altitude，也不提供目标相对本机的俯仰角；
                # /state 只有本机高度，/indicators 71 个字段里也没有目标相关项。

            # 「选中目标」：8111 没有锁定/选中数据（/lockon 等端点均 404），
            # 按最近距离选，并跳过正前方(12点)的目标。
            data["selected"] = self._select_target(targets, p_hdg)

            # 供边缘箭头使用的可见目标：默认过滤掉地面目标，
            # 保证「面板不显示的目标，箭头也不会画」，避免两处规则不一致。
            if self.show_ground_targets():
                data["visible_targets"] = list(targets)
            else:
                data["visible_targets"] = [t for t in targets if t.kind == "air"]

            for a in allies:
                if abs(px) > 1.0 or abs(a["x"]) > 1.0:
                    a["dist"] = haversine(px, py, a["x"], a["y"])
                    a["bearing"] = bearing(px, py, a["x"], a["y"])
                else:
                    mi = data.get("map_info", {})
                    span = self._map_span(mi)
                    dx_m = (a["x"] - px) * span
                    dy_m = (a["y"] - py) * span
                    a["dist"] = math.sqrt(dx_m**2 + dy_m**2)
                    a["bearing"] = bearing_compass(dx_m, dy_m)

        data["player"] = player
        data["enemies"] = enemies
        data["targets"] = targets
        data["allies"] = allies
        data["airfields"] = airfields
        data["zones"] = zones

        # ============ TRACK MANAGER (RB mode lost-target prediction) ============
        # Build unit list for track manager
        track_units = []
        for e in enemies:
            track_units.append({
                "x": e["x"], "y": e["y"],
                "heading": e.get("heading", 0),
                "icon": e["icon"], "color": e["color"],
                "is_enemy": True,
            })
        for a in allies:
            track_units.append({
                "x": a["x"], "y": a["y"],
                "heading": a.get("heading", 0),
                "icon": a["icon"], "color": a["color"],
                "is_enemy": False,
            })

        # 真实地图跨度同步给轨迹管理器（测速/预测外推用，空战图 65536）
        self.track_manager.map_span = self._map_span(data.get("map_info", {}))

        alive_tracks, lost_tracks = self.track_manager.update(track_units)

        # Detect game mode based on visible enemy density
        # AB (arcade): many enemies always visible on minimap
        # RB (realistic): only spotted/los enemies visible, fewer
        if data.get("in_battle"):
            self._mode_samples.append(len(enemies))
            if len(self._mode_samples) > 30:
                self._mode_samples = self._mode_samples[-30:]
            if len(self._mode_samples) >= 10:
                avg_enemies = sum(self._mode_samples) / len(self._mode_samples)
                # Arcade shows most enemies; realistic shows few
                if avg_enemies >= 4:
                    self.game_mode = "AB"
                elif avg_enemies >= 1:
                    self.game_mode = "RB"
                else:
                    self.game_mode = "SB"
        else:
            self._mode_samples = []
            self.game_mode = "unknown"

        data["game_mode"] = self.game_mode

        # Enrich alive tracks with distance/bearing
        if player:
            px, py = player.get("x", 0), player.get("y", 0)
            for t in alive_tracks:
                if abs(px) > 1.0:
                    t.dist = haversine(px, py, t.x, t.y)
                    t.bearing = bearing(px, py, t.x, t.y)
                else:
                    mi = data.get("map_info", {})
                    span = self._map_span(mi)
                    dx_m = (t.x - px) * span
                    dy_m = (t.y - py) * span
                    t.dist = math.sqrt(dx_m**2 + dy_m**2)
                    t.bearing = bearing_compass(dx_m, dy_m)
                t.aspect = angle_diff(t.heading, t.bearing)
                t.icon_short = t.icon

        # Enrich lost tracks with predicted position + distance
        if player:
            px, py = player.get("x", 0), player.get("y", 0)
            for t in lost_tracks:
                # Predict position: extrapolate based on how long lost
                lost_age = t.age()
                px_pred, py_pred = t.predict(lost_age)
                t.pred_x, t.pred_y = px_pred, py_pred
                if abs(px) > 1.0:
                    t.pred_dist = haversine(px, py, px_pred, py_pred)
                    t.pred_bearing = bearing(px, py, px_pred, py_pred)
                else:
                    mi = data.get("map_info", {})
                    span = self._map_span(mi)
                    dx_m = (px_pred - px) * span
                    dy_m = (py_pred - py) * span
                    t.pred_dist = math.sqrt(dx_m**2 + dy_m**2)
                    t.pred_bearing = bearing_compass(dx_m, dy_m)
                t.icon_short = t.icon

        data["tracks_alive"] = alive_tracks
        data["tracks_lost"] = lost_tracks

    def _compute_metrics(self, data):
        """Compute air combat metrics"""
        flight = data.get("flight")
        if not flight:
            data["metrics"] = None
            return

        m = {}

        # Energy state (simplified)
        # Specific Energy = altitude + V²/(2g)
        g = 9.81
        v_ms = flight["tas"] / 3.6  # km/h to m/s
        m["energy_altitude"] = flight["altitude"] + (v_ms**2) / (2 * g)

        # Fuel percentage
        if flight["fuel_init"] > 0:
            m["fuel_pct"] = (flight["fuel"] / flight["fuel_init"]) * 100
        else:
            m["fuel_pct"] = 100

        # Low fuel warning
        m["low_fuel"] = m["fuel_pct"] < 20

        # Low altitude warning (below 200m AGL, approximate)
        m["low_altitude"] = flight["altitude"] < 200

        # Stall warning (low speed + high AoA)
        m["stall_risk"] = (flight["ias"] < 200 and flight["aoa"] > 12)

        # Over-G warning
        m["over_g"] = flight["g_force"] > 7

        # Enemy on six (within 1km, behind player)
        enemies = data.get("enemies", [])
        compass = flight.get("compass", 0)
        m["enemy_on_six"] = False
        m["nearest_enemy_dist"] = 99999
        m["nearest_enemy_aspect"] = 0

        for e in enemies[:5]:
            if e.get("dist", 99999) < m["nearest_enemy_dist"]:
                m["nearest_enemy_dist"] = e.get("dist", 99999)
                m["nearest_enemy_aspect"] = e.get("aspect", 0)

            # Enemy on six: close and behind us
            # If aspect ~180, enemy is heading away from us (we're behind them)
            # If bearing from enemy to us ~ enemy heading, enemy is chasing us
            # Simplified: if enemy is close (<1500m) and heading toward us (aspect near 0)
            if e.get("dist", 99999) < 1500:
                # Check if enemy is behind us
                rel_bearing = angle_diff(e.get("bearing", 0), compass)
                if abs(rel_bearing) > 120:  # enemy behind (more than 120° off nose)
                    m["enemy_on_six"] = True

        # ============ RB MODE: lost target warnings ============
        # In realistic battles, enemies drop off minimap when out of line-of-sight.
        # TrackManager keeps their last known position and predicts forward.
        lost_tracks = data.get("tracks_lost", [])
        m["lost_targets"] = len(lost_tracks)
        m["lost_threat"] = False
        m["lost_threat_desc"] = ""

        # Only warn in RB mode (AB shows all targets so lost means destroyed)
        if data.get("game_mode") == "RB" and lost_tracks:
            # Find a lost enemy that was close and may still be a threat
            for t in lost_tracks:
                lost_age = t.age()
                # If it was within 5km and lost recently (<10s), it's a live threat
                if hasattr(t, "pred_dist") and t.pred_dist < 5000 and lost_age < 12:
                    m["lost_threat"] = True
                    m["lost_threat_desc"] = f"LOST {t.icon_short} {t.pred_dist:.0f}m {t.age():.0f}s ago"
                    break

        data["metrics"] = m

    def get_data(self):
        with self.lock:
            return self.data.copy() if self.data else {}


# For testing without Qt
if __name__ == "__main__":
    fetcher = AirDataFetcher()
    fetcher.start()
    time.sleep(2)
    data = fetcher.get_data()

    print(f"In battle: {data.get('in_battle', False)}")
    print(f"State valid: {data.get('state', {}).get('valid', False)}")

    flight = data.get("flight")
    if flight:
        print(f"\nFlight Data:")
        print(f"  Altitude: {flight['altitude']:.0f} m")
        print(f"  TAS: {flight['tas']:.0f} km/h")
        print(f"  IAS: {flight['ias']:.0f} km/h")
        print(f"  Mach: {flight['mach']:.2f}")
        print(f"  AoA: {flight['aoa']:.1f} deg")
        print(f"  G-Force: {flight['g_force']:.1f}")
        print(f"  V-Speed: {flight['v_speed']:.1f} m/s")
        print(f"  Compass: {flight['compass']:.0f} deg")
        print(f"  Fuel: {flight['fuel']:.0f}/{flight['fuel_init']:.0f} kg")
        for i, eng in enumerate(flight.get("engines", [])):
            print(f"  Engine {i+1}: throttle={eng['throttle']:.0f}% RPM={eng['rpm']:.0f} oil={eng['oil_temp']:.0f}C")

    enemies = data.get("enemies", [])
    print(f"\nEnemies: {len(enemies)}")
    for e in enemies[:3]:
        print(f"  {e['icon']} dist={e.get('dist',0):.0f}m bearing={e.get('bearing',0):.0f} heading={e.get('heading',0):.0f} aspect={e.get('aspect',0):.0f} spd={e.get('speed_kmh',0):.0f}km/h")

    metrics = data.get("metrics", {})
    if metrics:
        print(f"\nMetrics:")
        print(f"  Energy alt: {metrics['energy_altitude']:.0f} m")
        print(f"  Fuel: {metrics['fuel_pct']:.1f}%")
        print(f"  Stall risk: {metrics['stall_risk']}")
        print(f"  Enemy on six: {metrics['enemy_on_six']}")
