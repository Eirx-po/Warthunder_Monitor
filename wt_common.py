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
import time

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
