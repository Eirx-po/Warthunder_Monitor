"""
War Thunder 8111 HUD - 共享目标处理模块

解决的问题：
1. 8111 的 map_obj.json 中，同一个目标（如一个防空阵地 = 3门炮 + 1辆SPAA）
   会拆成多条独立记录，坐标只差几米。若逐条画箭头，会重叠成"残影"。
   → 用位置聚类把同一目标合并成一个，一个目标只出一个箭头。

2. 目标坐标每帧有微小抖动，导致箭头位置抖动 → 视觉拖影。
   → 用 EMA（指数滑动平均）平滑方位角与距离。

3. 敌我颜色判定：WT 的红色有多种变体（#fa0C00 / #f00C00 / #e10B00 ...），
   硬编码列表会漏判。→ 用 RGB 分量做色系判定。

4. 距离阈值边界处目标会闪烁（一会儿出现一会儿消失）。
   → 用滞回（hysteresis）：进入隐藏要更近，退出隐藏要更远。
"""
import math
import os
import shutil
import time
from collections import deque


def ensure_local_config(path):
    """首次运行时从版本库里的 *.example.json 模板生成本地配置文件。

    布局 json 是用户本机设置（GUI「保存布局」会反复改写），只跟踪模板、不跟踪
    本地文件，这样调窗口位置不会污染 git status / 提交。本地文件已存在时原样返回；
    模板不存在或复制失败时静默跳过 —— 各 HUD 内部都有 DEFAULT_LAYOUT 兜底。
    """
    try:
        if os.path.exists(path):
            return path
        template = (path[:-5] + ".example.json") if path.endswith(".json") else (path + ".example")
        if os.path.exists(template):
            shutil.copyfile(template, path)
    except Exception:
        pass
    return path


# ------------------------------------------------- 在局判据（四路信号并集）

def battle_signals(indicators=None, state=None, mission=None, map_info=None):
    """8111「是否在战斗中」的四路信号并集，返回 (in_battle, 命中的信号名列表)。

    ⚠ 端点是按空战 / 陆战**互补可用**，不是被永久掏空（2026-11-13 实测）：
      - 空战：/state valid=true + 完整气动仪表；/indicators 恒 {"valid": false}
      - 陆战：/indicators valid=true（含 army/type 等 71 字段）；/state 恒 {"valid": false}
      - /mission.json 的 status="running" 是社区公认最稳的在局判据
      - /map_info.json 的 valid=true 表示地图已加载

    只看其中一路会把在局误判成机库 → 守护反复停 HUD、面板显示「未在战斗中」。
    四路任一有效即算在局；全部无效才判机库。集中放这里，避免守护 / GUI /
    两个 HUD 各写一份走样。
    """
    sources = []
    if isinstance(indicators, dict) and indicators.get("valid", False):
        sources.append("indicators")
    if isinstance(state, dict) and state.get("valid", False):
        sources.append("state")
    if isinstance(mission, dict) and \
            str(mission.get("status", "") or "").lower() == "running":
        sources.append("mission")
    if isinstance(map_info, dict) and map_info.get("valid", False):
        sources.append("map_info")
    return bool(sources), sources


# ------------------------------------------------- 目标数据可达性（面板显隐）

def map_data_available(data, frame_stale_s=5.0):
    """「目标 / 轰炸战区」数据是否可达，决定这两块面板该不该画。

    8111 常出现 /state 正常、但 /map_obj.json 返回空(200 / size=0)、
    /map_info.json 返回 {"valid": false} 的情况：战斗数据看着"在线"，
    目标列表却根本算不出来，TARGET / BOMB ZONE 只会挂一块"无目标"空壳。
    抓取线程会给出 map_ok（最近 2 秒内拿到过非空小地图对象）；
    这里再校验整帧时间戳，防止数据线程卡死后旧面板一直留在屏幕上。

    没有这两个字段的旧数据默认判为可达，保持向后兼容。
    """
    if not isinstance(data, dict):
        return False
    if not bool(data.get("map_ok", True)):
        return False
    try:
        ts = float(data.get("ts", 0) or 0)
    except (TypeError, ValueError):
        ts = 0.0
    # ts<=0 表示没有时间戳信息（离线测试 / 旧数据），跳过陈旧检查
    if ts > 0 and (time.time() - ts) > frame_stale_s:
        return False
    return True


# ---------------------------------------------------------------- 颜色工具

def parse_color(c):
    """'#fa0C00' -> (250, 12, 0)；解析失败返回 None"""
    if not c or not isinstance(c, str):
        return None
    c = c.strip().lstrip('#')
    if len(c) != 6:
        return None
    try:
        return (int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16))
    except ValueError:
        return None


def is_enemy_color(c):
    """敌对阵营：红色系（R 高，G/B 很低）"""
    rgb = parse_color(c)
    if rgb is None:
        return False
    r, g, b = rgb
    return r >= 180 and g <= 70 and b <= 70


def is_friend_color(c):
    """友军阵营：蓝色系（B 高，R 低）"""
    rgb = parse_color(c)
    if rgb is None:
        return False
    r, g, b = rgb
    return b >= 170 and r <= 110 and g <= 150


# ---------------------------------------------------------------- 角度工具

def norm180(a):
    """归一化到 (-180, 180]"""
    return (a + 540) % 360 - 180


def ema_angle(prev, new, alpha):
    """角度的指数滑动平均（处理 360° 环绕）"""
    if prev is None:
        return new
    return (prev + norm180(new - prev) * alpha + 360) % 360


def ema_value(prev, new, alpha):
    """数值的指数滑动平均"""
    if prev is None:
        return new
    return prev + (new - prev) * alpha


# ---------------------------------------------------------------- 8111 坐标换算
#
# ⚠ 8111 map_obj.json 的 x/y 是绘制在「北朝上」地图上的网格坐标：
#   +x = 东，+y = 南（和屏幕/地图图像同向）。
#   在这个坐标系里 atan2(dy, dx) 得到的是「地图角」：
#   0=东、90=南、180=西、270=北，顺时针为正 —— 不是罗盘角（0=北）！
#   （2026-09-25 修：空战 HUD 曾把它当罗盘角用 → 所有方位整体偏 90°；
#    陆战 HUD 又当逆时针数学角多取一次负 → 左右镜像。）
#   两个 HUD 的方位/航向一律在源头转成真罗盘角（0=北，顺时针），下游不再换算。

def bearing_compass(dx_m, dy_m):
    """8111 地图位移（+x=东，+y=南）→ 罗盘方位角（0=北，顺时针，0~360）"""
    return (math.degrees(math.atan2(dx_m, -dy_m)) + 360.0) % 360.0


def heading_compass(dx, dy):
    """8111 地图方向向量（+x=东，+y=南）→ 罗盘航向（0=北，顺时针）；零向量返回 0"""
    if dx == 0 and dy == 0:
        return 0.0
    return (math.degrees(math.atan2(dx, -dy)) + 360.0) % 360.0


# ---------------------------------------------------------------- 目标集群

class TargetCluster:
    """
    一个逻辑目标 = 若干条 map_obj 记录的聚类结果。

    字段：
      cid          稳定 ID（跨帧不变）
      kind         'air' | 'ground'
      icons        该簇包含的图标名集合
      count        该簇合并了多少条原始记录
      bearing      平滑后的方位角
      dist         平滑后的距离
      raw_bearing  本帧原始方位角
      raw_dist     本帧原始距离
    """

    def __init__(self, cid, kind, x, y, icon, bearing, dist, ts, heading=0):
        self.cid = cid
        self.kind = kind
        self.x = x
        self.y = y
        self.icons = {icon} if icon else set()
        self.count = 1
        self.bearing = bearing
        self.dist = dist
        self.heading = heading
        self.raw_bearing = bearing
        self.raw_dist = dist
        self.visible = True          # 本帧是否有数据支撑
        self.hide_latched = False    # 距离滞回状态
        self.last_seen = ts
        self.age = 0.0

        # 运动学量（由 update_motion 逐帧差分得到）
        # 8111 不提供目标的速度/高度，只能靠位置历史估算。
        self.speed_kmh = 0.0     # 水平地速 km/h
        self.closure_ms = 0.0    # 接近率 m/s，正 = 正在接近
        self._prev_x = x
        self._prev_y = y
        self._prev_dist = dist
        self._prev_ts = ts
        self._speed_ready = False

    def update_motion(self, ts, map_span_m=4096.0):
        """
        用本帧与上一帧的位置差估算目标速度与接近率。

        注意：这里必须用聚类后稳定的 cid 来维持历史。
        早期实现用「图标+颜色+坐标」当 key，坐标每帧都变导致
        历史长度恒为 1，速度算出来永远是 0。
        """
        dt = ts - self._prev_ts
        if dt < 0.08:              # 采样太密，差分误差被放大，跳过
            return

        # 水平位移 → 地速
        d_m = math.hypot(self.x - self._prev_x, self.y - self._prev_y) * map_span_m
        v = (d_m / dt) * 3.6       # m/s -> km/h
        if v < 3000:               # 过滤目标瞬移/重匹配造成的尖峰
            if self._speed_ready:
                self.speed_kmh = self.speed_kmh * 0.6 + v * 0.4
            else:
                self.speed_kmh = v
                self._speed_ready = True

        # 距离变化 → 接近率（正 = 在接近）
        dd = (self._prev_dist - self.dist) / dt
        if abs(dd) < 2000:
            self.closure_ms = self.closure_ms * 0.5 + dd * 0.5

        self._prev_x = self.x
        self._prev_y = self.y
        self._prev_dist = self.dist
        self._prev_ts = ts

    @property
    def icon(self):
        """主图标：优先非通用名"""
        if not self.icons:
            return "?"
        return sorted(self.icons, key=lambda s: (s.lower() == "none", len(s)))[0]

    def absorb(self, x, y, icon, bearing, dist, ts):
        """并入一条新记录（同一目标的另一条 map_obj 记录）"""
        n = self.count
        self.x = (self.x * n + x) / (n + 1)
        self.y = (self.y * n + y) / (n + 1)
        if icon:
            self.icons.add(icon)
        self.count = n + 1
        # 原始值取最近一次的（不平均，避免抖动被放大）
        self.raw_bearing = bearing
        self.raw_dist = min(self.raw_dist, dist) if dist else dist
        self.visible = True
        self.last_seen = ts


class TargetManager:
    """
    把每帧的原始目标列表聚类成"逻辑目标"，并保持跨帧 ID 稳定。

    参数：
      cluster_radius_m   同一目标内部的记录聚类半径（米）。
                         8111 中一个防空阵地的炮位相距 3~30m，设 120m 足够合并。
      match_radius_m     跨帧匹配半径（米）。用于保持 ID 稳定。
      smooth_alpha       平滑系数，越小越平滑（0.35 = 较平滑且响应快）
      ttl_s              目标消失后保留多久（秒）
    """

    def __init__(self,
                 cluster_radius_m=120.0,
                 match_radius_m=400.0,
                 smooth_alpha=0.35,
                 ttl_s=3.0,
                 map_span_m=4096.0):
        self.clusters = {}       # cid -> TargetCluster
        self._next_id = 1
        self.cluster_radius = cluster_radius_m
        self.match_radius = match_radius_m
        self.alpha = smooth_alpha
        self.ttl = ttl_s
        # 归一化坐标 1.0 对应多少米。由 map_max - map_min（= grid_size）得到，
        # 不能直接用 map_max —— 见 data_fetcher._map_span() 的说明。
        self.map_span = map_span_m

    # -- 内部：在已有簇中找最近的一个 -------------------------------
    def _find_nearest(self, x, y, used=None):
        """
        找最近的、且本帧尚未被占用的旧簇。

        必须排除已被占用的簇：否则多个本帧簇会抢到同一个旧簇，
        互相覆盖导致目标被吞掉（实测 32 条记录只剩 1 个目标）。
        """
        best, best_d = None, self._match_radius()
        for c in self.clusters.values():
            if used is not None and c.cid in used:
                continue
            d = math.hypot(c.x - x, c.y - y)
            if d < best_d:
                best_d, best = d, c
        return best

    def update(self, units):
        """
        units: list of dict，至少含 x, y, kind, icon, bearing, dist
               （x/y 为**归一化地图坐标**，或任意与 match_radius 同量纲的坐标）

        返回：去重并平滑后的 TargetCluster 列表（按距离升序）
        """
        now = time.time()

        # 1) 本帧内部聚类：把相距很近的记录合并成同一目标
        frame_clusters = []
        for u in units:
            x, y = u.get("x", 0.0), u.get("y", 0.0)
            placed = False
            for fc in frame_clusters:
                if math.hypot(fc["x"] - x, fc["y"] - y) <= self._xy_radius():
                    # 同一种类才合并（不要把地面炮和空中飞机混在一起）
                    if fc["kind"] == u.get("kind"):
                        fc["items"].append(u)
                        fc["x"] = (fc["x"] * (len(fc["items"]) - 1) + x) / len(fc["items"])
                        fc["y"] = (fc["y"] * (len(fc["items"]) - 1) + y) / len(fc["items"])
                        placed = True
                        break
            if not placed:
                frame_clusters.append({
                    "x": x, "y": y,
                    "kind": u.get("kind", "ground"),
                    "items": [u],
                })
        # 记录每簇的代表朝向（用于 aspect 计算）
        for fc in frame_clusters:
            rep = min(fc["items"], key=lambda i: i.get("dist", 9e9))
            fc["heading"] = rep.get("heading", 0.0)

        # 2) 与上一帧的簇做跨帧匹配，保持 ID 稳定
        matched_ids = set()
        used = set()          # 本帧已占用的旧簇，防止一个旧簇被多个新簇抢走
        for fc in frame_clusters:
            items = fc["items"]
            # 用簇内所有记录的加权中心
            cx, cy = fc["x"], fc["y"]
            kind = fc["kind"]
            # 代表性 bearing/dist：取最近的一条
            rep = min(items, key=lambda i: i.get("dist", 9e9))
            bearing = rep.get("bearing", 0.0)
            dist = rep.get("dist", 0.0)
            heading = fc.get("heading", 0.0)

            prev = self._find_nearest(cx, cy, used)
            if prev is not None and prev.kind == kind:
                used.add(prev.cid)
                prev.x, prev.y = cx, cy
                prev.bearing = ema_angle(prev.bearing, bearing, self.alpha)
                prev.dist = ema_value(prev.dist, dist, self.alpha)
                prev.heading = heading
                prev.raw_bearing = bearing
                prev.raw_dist = dist
                prev.icons = set()
                prev.count = 0
                for it in items:
                    if it.get("icon"):
                        prev.icons.add(it["icon"])
                    prev.count += 1
                prev.visible = True
                prev.last_seen = now
                prev.age = 0.0
                prev.update_motion(now, self.map_span)   # 估算速度 / 接近率
                matched_ids.add(prev.cid)
            else:
                cid = self._next_id
                self._next_id += 1
                c = TargetCluster(cid, kind, cx, cy,
                                  rep.get("icon"), bearing, dist, now, heading)
                for it in items:
                    if it.get("icon"):
                        c.icons.add(it["icon"])
                c.count = len(items)
                self.clusters[cid] = c
                c.update_motion(now, self.map_span)      # 估算速度 / 接近率
                matched_ids.add(cid)

        # 3) 本帧没匹配到的：标记不可见，超时删除
        stale = []
        for cid, c in self.clusters.items():
            if cid not in matched_ids:
                c.visible = False
                c.age = now - c.last_seen
                if c.age > self.ttl:
                    stale.append(cid)
        for cid in stale:
            del self.clusters[cid]

        out = [c for c in self.clusters.values() if c.visible]
        out.sort(key=lambda c: c.dist)
        return out

    def _xy_radius(self):
        """
        聚类半径：units 的 x/y 是归一化坐标（0~1），
        需把米换算成归一化单位（除以地图跨度）。
        """
        return self.cluster_radius / max(self.map_span, 1.0)

    def _match_radius(self):
        """
        跨帧匹配半径，同样要把米换算成归一化单位。
        （注意：坐标是 0~1，不能直接拿米数比较）
        """
        return self.match_radius / max(self.map_span, 1.0)

    def reset(self):
        self.clusters.clear()
        self._next_id = 1


# ---------------------------------------------------------------- 箭头布局

def layout_arrows(targets, sw, sh, margin=35, min_gap=40.0):
    """
    把目标列表布局到屏幕边缘，并做去重叠处理。

    targets: TargetCluster 列表（需含 bearing / dist）
    返回: list of dict {x, y, angle, cluster, pushed}
          angle 为箭头旋转角（度，Qt rotate 用，正=顺时针）

    去重叠：沿屏幕边缘方向把过近的箭头推开，避免叠成一团"残影"。

    注意：调用方需自行处理"数学角 vs 罗盘角"的差异（见 angle_mode）。
    """
    cx, cy = sw / 2.0, sh / 2.0
    half_w = cx - margin
    half_h = cy - margin

    placed = []
    for t in targets:
        rel = t.rel_bearing                      # 相对方位：0=正前 ±180=正后
        rad = math.radians(rel)
        # rel 已在前方为 0、右侧为正的约定下（调用方转换好）
        dx = math.sin(rad)
        dy = -math.cos(rad)

        tx = half_w / abs(dx) if abs(dx) > 1e-6 else float("inf")
        ty = half_h / abs(dy) if abs(dy) > 1e-6 else float("inf")
        tt = min(tx, ty)
        ax = cx + dx * tt
        ay = cy + dy * tt

        placed.append({
            "x": ax, "y": ay,
            "dx": dx, "dy": dy,
            "angle": rel,
            "cluster": t,
            "pushed": False,
        })

    # ---- 去重叠：沿边缘切线方向推开 ----
    # 边缘切线方向 = (-dy, dx)
    for i in range(len(placed)):
        for _ in range(6):
            moved = False
            for j in range(len(placed)):
                if i == j:
                    continue
                a, b = placed[i], placed[j]
                d = math.hypot(a["x"] - b["x"], a["y"] - b["y"])
                if d >= min_gap:
                    continue
                need = (min_gap - d) / 2.0 + 0.5
                # 沿 i 的切线推开
                sx, sy = -a["dy"], a["dx"]
                a["x"] += sx * need
                a["y"] += sy * need
                b["x"] -= sx * need
                b["y"] -= sy * need
                a["pushed"] = True
                moved = True
            if not moved:
                break

    # ---- 夹回屏幕范围内 ----
    for a in placed:
        a["x"] = max(margin * 0.5, min(sw - margin * 0.5, a["x"]))
        a["y"] = max(margin * 0.5, min(sh - margin * 0.5, a["y"]))

    return placed


# ---------------------------------------------------------------- EST 姿态估算
#
# 思路来自 unknown-obj/wt-map（其文档致谢 A-common-Chinese/WarThunder-8111-HUD）。
# 游戏 2.59.0 之后 MiG-35 / JF-17 / Z-10A 等机型不再通过 /indicators 提供
# aviahorizon_pitch / aviahorizon_roll / bank。空战 HUD 原本只有
# pitch = asin(Vy / TAS)（航迹倾角），漏了迎角，也没标注这个数是估算的。
#
#   pitch = gamma + AoA，gamma = asin(Vy / (TAS/3.6))，指数平滑 tau=0.25s
#   roll  = atan(V * dpsi/dt / g)，1.4s 窗口、±80° 限幅、<6° 归零
#          —— 只给「左/右带杆」方向提示，不画数值、不画指针
#   |Ny - 1| > 4 时机动过猛 → 置信度 0.35，标识 EST?
# 无仪表且 /state 暂不可用时保持上一帧最多 2 秒，之后明确标 NO ATT，不编造姿态。

class AttitudeEstimator:
    """8111 姿态数据统一入口：真实仪表优先，缺失时用 /state 估算并标注来源。

    update() 返回 dict:
      pitch       俯仰角（度，爬升为正）
      source      "instrument" / "est" / "none"
      label       "" / "EST" / "EST?" / "NO ATT"
      confidence  1.0 正常，0.35 低置信度
      roll_hint   "" / "LEFT" / "RIGHT"（只有方向，没有坡度数值）
      available   是否有可用姿态
      hold        是否为「保持上一帧」的过渡结果
    """
    PITCH_TAU = 0.25
    ROLL_WINDOW_S = 1.4
    HOLD_S = 2.0
    LOW_CONF = 0.35
    G = 9.80665
    _INST_KEYS = ("aviahorizon_pitch", "aviahorizon_roll", "bank")

    def __init__(self, clock=None):
        self._clock = clock or time.time
        self.reset()

    def reset(self):
        self._pitch = None
        self._last_ts = None
        self._last_ok_ts = None
        self._hdg_hist = deque()
        self._last = self._empty()

    @staticmethod
    def _empty():
        return {"pitch": 0.0, "source": "none", "label": "NO ATT",
                "confidence": 0.0, "roll_hint": "", "available": False,
                "hold": False}

    @staticmethod
    def _num(d, key, default=0.0):
        try:
            v = d.get(key, default)
            if isinstance(v, bool):
                return default
            v = float(v)
            return v if math.isfinite(v) else default
        except (TypeError, ValueError, AttributeError):
            return default

    def _smooth(self, raw, now):
        """一阶指数平滑 k = 1 - exp(-dt/tau)，对 10Hz/20Hz 都适用。"""
        if self._pitch is None or self._last_ts is None:
            self._pitch = raw
        else:
            dt = max(0.0, now - self._last_ts)
            k = 1.0 - math.exp(-dt / self.PITCH_TAU)
            self._pitch += (raw - self._pitch) * k
        self._last_ts = now
        return self._pitch

    def _roll_hint(self, compass, tas_kmh, now):
        """由航向变化率估算坡度方向；只返回 LEFT / RIGHT / ''，不给数值。"""
        if compass is None or tas_kmh <= 1.0:
            return ""
        try:
            hdg = float(compass) % 360.0
        except (TypeError, ValueError):
            return ""
        hist = self._hdg_hist
        hist.append((now, hdg))
        while len(hist) > 2 and (now - hist[1][0]) > self.ROLL_WINDOW_S:
            hist.popleft()
        if len(hist) < 2:
            return ""
        t0, h0 = hist[0]
        dt = now - t0
        if dt < 0.15:
            return ""
        rate = math.radians(norm180(hdg - h0)) / dt
        roll = math.degrees(math.atan((tas_kmh / 3.6) * rate / self.G))
        roll = max(-80.0, min(80.0, roll))
        if abs(roll) < 6.0:
            return ""
        return "RIGHT" if roll > 0 else "LEFT"

    def _commit(self, now, pitch, source, confidence, label, roll_hint=""):
        d = {"pitch": float(pitch), "source": source, "label": label,
             "confidence": float(confidence), "roll_hint": roll_hint,
             "available": True, "hold": False}
        self._last = d
        self._last_ok_ts = now
        return d

    def update(self, state=None, indicators=None, compass=None, now=None):
        """喂一帧 8111 数据，返回姿态 dict。

        state       /state（dict，valid=true 才有气动仪表）
        indicators  /indicators（dict；只看有没有 aviahorizon_* / bank 字段，
                    不看 valid —— 空战里这个接口恒 valid:false）
        compass     本机罗盘航向（度），来自 indicators.compass 或 map_obj dx/dy
        now         可注入时钟，便于离线测试
        """
        now = self._clock() if now is None else float(now)

        # 1) 真实姿态仪优先：有任一姿态字段就直接用，不估算、不标 EST。
        #    valid:false 表示接口整体失效（可能残留上一局字段）→ 不信任，改走 EST。
        if isinstance(indicators, dict):
            inst_ok = indicators.get("valid", True) is not False
            has_inst = inst_ok and any(
                isinstance(indicators.get(k), (int, float))
                and not isinstance(indicators.get(k), bool)
                for k in self._INST_KEYS
            )
            if has_inst:
                pitch = self._num(indicators, "aviahorizon_pitch", 0.0)
                self._pitch = None       # 仪表/估算互切时不要沿用旧平滑值
                self._last_ts = None
                return self._commit(now, pitch, "instrument", 1.0, "")

        # 2) EST：/state 有效且空速足够。
        if isinstance(state, dict) and state.get("valid", False):
            tas = self._num(state, "TAS, km/h", 0.0)
            v_ms = tas / 3.6
            if v_ms > 1.0:
                vy = self._num(state, "Vy, m/s", 0.0)
                aoa = self._num(state, "AoA, deg", 0.0)
                ny = self._num(state, "Ny", 1.0)
                ratio = max(-1.0, min(1.0, vy / v_ms))
                gamma = math.degrees(math.asin(ratio))
                pitch = self._smooth(gamma + aoa, now)
                conf = self.LOW_CONF if abs(ny - 1.0) > 4.0 else 1.0
                label = "EST?" if conf < 0.6 else "EST"
                hint = self._roll_hint(compass, tas, now)
                return self._commit(now, pitch, "est", conf, label, hint)

        # 3) 没有有效输入：2 秒内保持上一帧（防瞬时丢包 / 换载具闪烁），
        #    超时后明确不可用，不编造姿态。
        if self._last_ok_ts is not None and (now - self._last_ok_ts) <= self.HOLD_S:
            held = dict(self._last)
            held["hold"] = True
            return held
        if self._last.get("available") or self._last.get("hold"):
            self.reset()
        return dict(self._last)
