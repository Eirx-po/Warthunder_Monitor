"""
War Thunder HUD 一键启动器 + 自动切换守护模式

用法：
    python wt_hud_launcher.py              # 单次检测启动
    python wt_hud_launcher.py air          # 强制空战 HUD
    python wt_hud_launcher.py ground       # 强制陆战 HUD
    python wt_hud_launcher.py --watch      # 守护模式：按当前载具自动切换
    python wt_hud_launcher.py -w           # 同上

守护模式说明：
    持续监控 8111 端口，检测当前载具模式：
    - 坦克/舰船 → 启动陆战 HUD
    - 飞机       → 启动空战 HUD
    - 机库/菜单  → 停止所有 HUD
    模式变化时自动停旧启新，无需手动干预。
"""
import json
import os
import subprocess
import sys
import time
import urllib.request

BASE = "http://localhost:8111"
PYTHON = r"C:\Users\Administrator\.workbuddy\binaries\python\versions\3.13.12\python.exe"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

AIR_HUD = os.path.join(BASE_DIR, "wt_air_hud", "main.py")
GROUND_HUD = os.path.join(BASE_DIR, "wt_hud_v2.py")

# 模式常量
MODE_NONE = "none"       # 机库/菜单/游戏未运行
MODE_AIR = "air"         # 空战
MODE_GROUND = "ground"   # 陆战（海战也归到这里，用同一个 HUD）
MODE_UNKNOWN = "unknown" # 认不出载具类型 → 保持当前 HUD，不乱切


def game_running():
    """
    游戏进程是否存在（不管在前台还是后台）。

    用途：游戏退了就把 HUD 收掉，避免留下一个孤零零的透明窗口。
    检测失败时返回 True —— 宁可不关，也不能误关。
    """
    try:
        from wt_foreground import is_war_thunder_running
        return is_war_thunder_running()
    except Exception:
        return True


def hud_pids():
    """扫出所有 HUD 进程 PID（含不是本守护拉起的），用于清理残留"""
    ps = ("Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
          "ForEach-Object { $_.CommandLine + ' ' + $_.ProcessId }")
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=15)
        pids = []
        for line in (out.stdout or "").splitlines():
            low = line.lower()
            if ("hud_overlay.py" in low) or ("wt_hud_v2.py" in low):
                for tok in reversed(line.replace(",", " ").split()):
                    if tok.isdigit():
                        pids.append(int(tok))
                        break
        return pids
    except Exception:
        return []


def kill_stray_huds():
    """杀掉所有 HUD 进程（含别的途径启动的），返回杀掉的数量"""
    n = 0
    for pid in hud_pids():
        try:
            subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                           capture_output=True, timeout=5)
            n += 1
        except Exception:
            pass
    return n


def check_8111():
    """检查 8111 端口是否可达，返回 indicators"""
    try:
        req = urllib.request.Request(BASE + "/indicators", headers={"User-Agent": "Launcher"})
        with urllib.request.urlopen(req, timeout=2) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except Exception:
        return None


def detect_mode(ind):
    """
    根据 indicators 判断当前模式。

    ⚠ 不能写「含 tank 就是陆战，否则一律空战」——
    舰船(shipModels)、直升机(helicopterModels)、以及载入过程中 type 还是
    "?" / 空字符串 的情况都会被误判成空战，导致 HUD 乱切。
    现在按 type 前缀 + army 字段显式分类，认不出来就返回 MODE_UNKNOWN
    （调用方据此保持现状、不切换）。
    """
    if not ind:
        return MODE_NONE, "8111 端口不可达（游戏未运行）"

    if not ind.get("valid", False):
        return MODE_NONE, "游戏中但不在战斗（机库/菜单）"

    army = str(ind.get("army", "") or "").lower()
    vtype = str(ind.get("type", "") or "").lower()
    # type 形如 "tankModels/us_t26e4_superpershing"，取前缀判断
    prefix = vtype.split("/")[0] if "/" in vtype else ""

    # 载具还没载入完：type 可能是 "?" 或空
    if vtype in ("", "?", "unknown") and not army:
        return MODE_UNKNOWN, "载具信息载入中…"

    # ---- 陆战 ----
    if prefix.startswith("tank") or army == "tank":
        return MODE_GROUND, f"陆战（{vtype.split('/')[-1]}）"

    # ---- 空战 ----
    if prefix.startswith("aircraft") or prefix.startswith("plane") \
            or prefix.startswith("helicopter") or army == "air":
        return MODE_AIR, f"空战（{vtype.split('/')[-1] or vtype}）"

    # ---- 海战：没有专门的 HUD，沿用陆战（同样基于 map_obj，能显示目标/方位）----
    if prefix.startswith("ship") or prefix.startswith("boat") or army == "ship":
        return MODE_GROUND, f"海战（{vtype.split('/')[-1]}）→ 用陆战 HUD"

    return MODE_UNKNOWN, f"未知载具类型（{vtype or '空'}），保持当前 HUD"


def launch_hud(script_path, label, proc_holder):
    """启动指定 HUD 为独立进程，返回 Popen 对象"""
    if proc_holder.get(script_path) is not None:
        proc = proc_holder[script_path]
        if proc.poll() is None:
            print(f"  [{label}] 已在运行，跳过")
            return

    print(f"  [{label}] 启动中...", flush=True)
    flags = subprocess.CREATE_NEW_CONSOLE if os.name == "nt" else 0
    proc = subprocess.Popen(
        [PYTHON, "-u", script_path],
        cwd=os.path.dirname(script_path),
        creationflags=flags,
    )
    proc_holder[script_path] = proc
    time.sleep(0.8)
    print(f"  [{label}] 已启动 (PID {proc.pid}) ✅", flush=True)
    return proc


def stop_hud(script_path, proc_holder):
    """停止指定 HUD 进程"""
    proc = proc_holder.get(script_path)
    if proc is None:
        return
    if proc.poll() is None:
        try:
            proc.kill()
            proc.wait(timeout=3)
            print(f"  [HUD] 已停止 (PID {proc.pid})", flush=True)
        except Exception:
            print(f"  [HUD] 停止失败，尝试 taskkill", flush=True)
            try:
                subprocess.run(["taskkill", "/F", "/PID", str(proc.pid)],
                               capture_output=True, shell=True)
            except Exception:
                pass
    proc_holder[script_path] = None


def run_watch():
    """守护模式：持续监控并自动切换"""
    print("=" * 50, flush=True)
    print("  WT HUD 守护模式 - 按载具自动切换", flush=True)
    print("=" * 50, flush=True)
    print("  监控 localhost:8111，Ctrl+C 退出", flush=True)
    print("", flush=True)

    proc_holder = {}  # script_path -> Popen or None
    current_mode = None
    mode_confirm_count = 0
    required_confirm = 3  # 连续 N 次相同才切换（去抖）
    last_print = ""

    def ensure_hud(mode):
        """确保对应模式的 HUD 在运行，停掉其他"""
        nonlocal current_mode
        if mode == MODE_AIR:
            launch_hud(AIR_HUD, "空战 HUD", proc_holder)
            stop_hud(GROUND_HUD, proc_holder)
        elif mode == MODE_GROUND:
            launch_hud(GROUND_HUD, "陆战 HUD", proc_holder)
            stop_hud(AIR_HUD, proc_holder)
        else:
            stop_hud(AIR_HUD, proc_holder)
            stop_hud(GROUND_HUD, proc_holder)
            # 顺带清理不是本守护拉起的 HUD（比如从 GUI 手动启的），
            # 否则游戏退了屏幕上还留着一个透明窗口
            n = kill_stray_huds()
            if n:
                print(f"  [HUD] 额外清理残留 HUD 进程 {n} 个", flush=True)

    try:
        while True:
            ind = check_8111()
            # 游戏进程没了就强制收起 HUD（不等 8111 超时）
            if not game_running():
                mode, desc = MODE_NONE, "游戏进程未运行，已收起 HUD"
            else:
                mode, desc = detect_mode(ind)

            # 认不出载具类型（载入中/新类型）：保持现状，不切换也不停 HUD
            if mode == MODE_UNKNOWN:
                mode_confirm_count = 0
                status_line = f"  [{time.strftime('%H:%M:%S')}] 模式: 保持   {desc}"
                if status_line != last_print:
                    print(status_line, flush=True)
                    last_print = status_line
                time.sleep(1.5)
                continue

            if mode != current_mode:
                mode_confirm_count += 1
                if mode_confirm_count >= required_confirm:
                    # 确认模式变化，执行切换
                    if current_mode is not None:
                        print(f"  模式切换: {current_mode} -> {mode}", flush=True)
                    else:
                        print(f"  检测到模式: {mode} ({desc})", flush=True)
                    current_mode = mode
                    mode_confirm_count = 0
                    ensure_hud(mode)
            else:
                mode_confirm_count = 0

            # 状态行（简短）
            status_line = f"  [{time.strftime('%H:%M:%S')}] 模式: {mode:6s} {desc}"
            if status_line != last_print:
                print(status_line, flush=True)
                last_print = status_line

            time.sleep(1.5)
    except KeyboardInterrupt:
        print("\n  守护模式已退出，停止所有 HUD...", flush=True)
        stop_hud(AIR_HUD, proc_holder)
        stop_hud(GROUND_HUD, proc_holder)


def main():
    print("=" * 50)
    print("  War Thunder HUD 一键启动器")
    print("=" * 50)
    print()

    # 守护模式
    if len(sys.argv) > 1 and sys.argv[1].lower() in ("--watch", "-w", "watch", "守护"):
        run_watch()
        return

    # 检查 8111
    ind = check_8111()
    mode, desc = detect_mode(ind)

    print(f"  8111 状态: {desc}")
    print()

    # 参数覆盖
    force = None
    if len(sys.argv) > 1:
        arg = sys.argv[1].lower()
        if arg in ("air", "空战", "a"):
            force = MODE_AIR
        elif arg in ("ground", "land", "陆战", "g", "tank"):
            force = MODE_GROUND

    if force:
        mode = force
        print(f"  手动指定: {'空战' if mode == 'air' else '陆战'} HUD")

    # 启动对应 HUD
    proc_holder = {}
    if mode == MODE_AIR:
        launch_hud(AIR_HUD, "空战 HUD", proc_holder)
    elif mode == MODE_GROUND:
        launch_hud(GROUND_HUD, "陆战 HUD", proc_holder)
    else:
        # 无法检测，询问用户
        print("  无法自动检测模式，请选择：")
        print("    [1] 空战 HUD")
        print("    [2] 陆战 HUD")
        print("    [3] 守护模式（自动切换）")
        print("    [4] 退出")
        try:
            choice = input("  选择: ").strip()
        except EOFError:
            choice = "4"
        if choice == "1":
            launch_hud(AIR_HUD, "空战 HUD", proc_holder)
        elif choice == "2":
            launch_hud(GROUND_HUD, "陆战 HUD", proc_holder)
        elif choice == "3":
            run_watch()
            return
        else:
            print("  已退出")
            return

    print()
    print("  提示：HUD 已独立窗口运行，关闭其窗口即可停止")
    print("  提示：全屏独占模式下看不到 HUD，请用窗口化/无边框")
    print("  提示：需要自动切换请运行: python wt_hud_launcher.py --watch")


if __name__ == "__main__":
    main()
