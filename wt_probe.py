"""
wt_probe.py —— 8111 实飞探针（开发用，不属于 HUD 运行时）

两件事：
  A. 速度法校验：拿**自己**当对照组。
     map_obj 差分算出的地速 vs /state 给的真值水平分量 sqrt(TAS² - Vy²)，
     得出差分测速的实际误差。
  B. 目标建档：每个可见飞机的 地速 / 速度σ / 转向率 / 位移-航向一致性 /
     编队距离 —— 既给「能不能分辨 AI 与真人」提供真值，也验证方向换算。

用法（游戏在战斗中时）：
    python wt_probe.py            # 采 25 秒
    python wt_probe.py 40         # 采 40 秒
"""
import json
import math
import sys
import time
import urllib.request

BASE = "http://127.0.0.1:8111"
DT = 0.35


def get(path, timeout=3):
    try:
        r = urllib.request.Request(BASE + path, headers={"User-Agent": "wt-probe"})
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except Exception:
        return None


def map_span(mi):
    """与 data_fetcher._map_span 同款：跨度 = map_max - map_min，回退 grid_size"""
    try:
        if isinstance(mi, dict):
            mmax, mmin = mi.get("map_max"), mi.get("map_min")
            if isinstance(mmax, list) and isinstance(mmin, list) and len(mmax) and len(mmin):
                span = float(mmax[0]) - float(mmin[0])
                if span > 0:
                    return span
            gs = mi.get("grid_size")
            if isinstance(gs, list) and len(gs) and float(gs[0]) > 0:
                return float(gs[0])
    except Exception:
        pass
    return 65536.0


MODE_EXTRA_CPOS = 0    # 占位，保持简单


def main():
    sec = float(sys.argv[1]) if len(sys.argv) > 1 else 25.0
    mi = get("/map_info.json") or {}
    span = map_span(mi)
    print(f"地图跨度 span = {span:.0f} m   (采样 {sec:.0f}s, {1/DT:.1f}Hz)")
    print("注：飞机停着不动时测不准/无意义，请在飞行中采样\n")

    tracks, next_id = {}, 1
    self_rows = []
    MATCH_R = 700.0 / span
    t0 = time.time()

    while time.time() - t0 < sec:
        now = time.time()
        objs = get("/map_obj.json")
        state = get("/state") or {}
        if not objs:
            time.sleep(DT)
            continue

        used = set()
        for o in objs:
            if o.get("type") != "aircraft":
                continue
            icon, color = o.get("icon", "?"), o.get("color", "")
            x, y = o.get("x", 0.0), o.get("y", 0.0)
            dx, dy = o.get("dx", 0.0), o.get("dy", 0.0)

            # 最近邻跨帧匹配（自机与敌人分开，避免抢同一条航迹）
            kind = ("SELF" if icon == "Player" else (icon, color))
            best, bd = None, MATCH_R
            for tid, t in tracks.items():
                if t["kind"] != kind or tid in used:
                    continue
                d = math.hypot(t["x"] - x, t["y"] - y)
                if d < bd:
                    bd, best = d, t
            if best is None:
                best = {"id": next_id, "kind": kind, "icon": icon,
                        "color": color, "hist": []}
                next_id += 1
                tracks[best["id"]] = best
            used.add(best["id"])
            best["x"], best["y"] = x, y
            best["hist"].append((now, x, y, dx, dy))

            # 自机：同步记录 /state 的真值
            if icon == "Player":
                tas = state.get("TAS, km/h", 0.0) or 0.0
                vy = state.get("Vy, m/s", 0.0) or 0.0
                self_rows.append((now, best["id"], tas, vy))

        time.sleep(DT)

    # ---------- A. 自机测速精度 ----------
    print("=" * 78)
    print("A. 测速法校验（自己当对照组：差分地速 vs /state 真值水平分量）")
    print("=" * 78)
    self_track = None
    for t in tracks.values():
        if t["kind"] == "SELF":
            self_track = t
            break
    if not self_track or len(self_track["hist"]) < 3:
        print("  没采到自机（停飞/未进战斗），跳过\n")
    else:
        meas = []       # (t_mid, v_kmh)
        h = self_track["hist"]
        for i in range(1, len(h)):
            dt = h[i][0] - h[i - 1][0]
            if dt <= 0.05:
                continue
            d = math.hypot(h[i][1] - h[i - 1][1], h[i][2] - h[i - 1][2]) * span
            meas.append(((h[i][0] + h[i - 1][0]) / 2, d / dt * 3.6))

        print(f"  {'时刻':>8} {'差分地速':>10} {'真值水平分量':>14} {'误差':>8}")
        errs = []
        for tm, vk in meas:
            ref = min(self_rows, key=lambda r: abs(r[0] - tm), default=None)
            if not ref:
                continue
            tas, vy = ref[2], ref[3]
            horiz = math.sqrt(max((tas / 3.6) ** 2 - vy ** 2, 0.0)) * 3.6
            if horiz < 20:            # 几乎静止时相对误差无意义
                continue
            err = (vk - horiz) / horiz * 100
            errs.append(abs(err))
            print(f"  {time.strftime('%H:%M:%S', time.localtime(tm)):>8} "
                  f"{vk:>10.0f} {horiz:>14.0f} {err:>+7.1f}%")
        if errs:
            print(f"\n  平均绝对误差 = {sum(errs)/len(errs):.1f}%  "
                  f"（样本 {len(errs)}，剔除静止点）")
        print()

    # ---------- B. 目标档案 ----------
    print("=" * 78)
    print("B. 目标档案（AI/真人判别的原始素材 + 方向换算一致性检查）")
    print("=" * 78)
    rows = []
    for tid, t in tracks.items():
        if t["kind"] == "SELF":
            continue
        h = t["hist"]
        if len(h) < 3:
            continue
        spds, turns, consis = [], [], []
        for i in range(1, len(h)):
            dt = h[i][0] - h[i - 1][0]
            if dt <= 0.05:
                continue
            mvx = (h[i][1] - h[i - 1][1]) * span
            mvy = (h[i][2] - h[i - 1][2]) * span
            v = math.hypot(mvx, mvy) / dt * 3.6
            spds.append(v)
            a0 = math.degrees(math.atan2(h[i - 1][4], h[i - 1][3]))
            a1 = math.degrees(math.atan2(h[i][4], h[i][3]))
            turns.append(abs((a1 - a0 + 540) % 360 - 180) / dt)
            # 位移方向 vs 接口给的航向向量(dx,dy)：两者应当一致
            if v > 30:
                lv = math.degrees(math.atan2(mvy, mvx))
                consis.append(abs((lv - a0 + 540) % 360 - 180))
        if not spds:
            continue
        mean_s = sum(spds) / len(spds)
        sd = (sum((s - mean_s) ** 2 for s in spds) / len(spds)) ** 0.5
        rows.append({
            "id": tid, "icon": t["icon"], "color": t["color"],
            "n": len(h), "spd": mean_s, "sd": sd,
            "turn": sum(turns) / len(turns),
            "cons": (sum(consis) / len(consis)) if consis else float("nan"),
            "x": t["x"], "y": t["y"],
        })

    print(f"  {'id':>3} {'icon':<9} {'color':<8} {'帧':>3} {'地速':>7} {'σ':>6} "
          f"{'转°/s':>6} {'位移↔航向':>9} {'编队m':>7}")
    print("  " + "-" * 68)
    for r in rows:
        fd = 1e9
        for o in rows:
            if o["id"] == r["id"] or o["icon"] != r["icon"] or o["color"] != r["color"]:
                continue
            fd = min(fd, math.hypot(r["x"] - o["x"], r["y"] - o["y"]) * span)
        form = f"{fd:.0f}" if fd < 1e8 else "-"
        cons = "-" if r["cons"] != r["cons"] else f"{r['cons']:.0f}"
        print(f"  {r['id']:>3} {r['icon']:<9} {r['color']:<8} {r['n']:>3} "
              f"{r['spd']:>7.0f} {r['sd']:>6.0f} {r['turn']:>6.1f} "
              f"{cons:>9} {form:>7}")
    print("""
  说明：
   地速σ      —— 越小越像 AI 巡航（油门恒定）
   转°/s      —— AI 通常平缓恒定，真人机动剧烈
   位移↔航向  —— 由坐标差分得到的运动方向 与 map_obj 的 dx/dy 航向的夹角，
                应接近 0°；偏大说明坐标/方向换算有问题（此值也用于验证方向修复）
   编队m      —— 与最近同型号目标的距离，AI 常成队贴飞""")


if __name__ == "__main__":
    main()
