"""
前台窗口检测：判断 War Thunder 是否处于前台。

HUD 是透明置顶窗口，切出去聊天/看网页时它会浮在别的窗口上面挡视线。
这里让 HUD 只在游戏处于前台时才显示。

判定优先级：
1. 前台窗口所属进程名（最可靠）—— War Thunder 主程序是 aces.exe
2. 窗口标题关键词（兜底）
"""
import os
import ctypes
from ctypes import wintypes

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

# War Thunder 主进程名（Dagor Engine）。不同发行渠道可能不同，一并列出。
# 实测本机：aces.exe（主程序）、aces_BE.exe（BattlEye 反作弊）
WT_PROCESS_NAMES = {
    "aces.exe",           # 官方 / Steam 主程序
    "aces_be.exe",        # BattlEye 反作弊（与主程序同前台会话）
    "warthunder.exe",
    "warthundersteam.exe",
}

# 窗口标题关键词（小写），用于兜底判定
WT_TITLE_KEYWORDS = ("war thunder", "战争雷霆")


def _pid_of(hwnd):
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def _process_name(pid):
    """取进程的完整路径再取文件名"""
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return None
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(1024)
        if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return os.path.basename(buf.value)
    finally:
        kernel32.CloseHandle(h)
    return None


def _window_title(hwnd):
    n = user32.GetWindowTextLengthW(hwnd)
    if n <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


# ---- 进程是否存在（不关心前台/后台）----
TH32CS_SNAPPROCESS = 0x00000002
INVALID_HANDLE_VALUE = -1


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_void_p),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", wintypes.WCHAR * 260),
    ]


def war_thunder_pids():
    """
    列出所有 War Thunder 相关进程 PID（不管在前台还是后台）。

    用 Toolhelp32 快照，不开子进程，守护循环里每 1.5 秒调也不心疼。
    """
    pids = []
    try:
        snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if snap == INVALID_HANDLE_VALUE or snap is None:
            return pids
        try:
            entry = PROCESSENTRY32W()
            entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
            ok = kernel32.Process32FirstW(snap, ctypes.byref(entry))
            while ok:
                name = (entry.szExeFile or "").lower()
                if name in WT_PROCESS_NAMES:
                    pids.append(entry.th32ProcessID)
                ok = kernel32.Process32NextW(snap, ctypes.byref(entry))
        finally:
            kernel32.CloseHandle(snap)
    except Exception:
        pass
    return pids


def is_war_thunder_running():
    """游戏进程是否存在（用于"游戏退了就自动关 HUD"）"""
    try:
        return bool(war_thunder_pids())
    except Exception:
        # 检测失败时宁可认为还在运行，避免误关 HUD
        return True


def foreground_info():
    """返回 (进程名, 窗口标题)，失败返回 (None, '')"""
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return None, ""
    return _process_name(_pid_of(hwnd)), _window_title(hwnd)


def is_war_thunder_foreground():
    """当前前台窗口是否属于 War Thunder"""
    try:
        name, title = foreground_info()
        if name and name.lower() in WT_PROCESS_NAMES:
            return True
        t = (title or "").lower()
        return any(k in t for k in WT_TITLE_KEYWORDS)
    except Exception:
        # 检测失败时宁可显示，不要因为异常把 HUD 永久藏起来
        return True


if __name__ == "__main__":
    import time
    print("3 秒内请把焦点切到 War Thunder 窗口...")
    time.sleep(3)
    name, title = foreground_info()
    print(f"前台进程: {name}")
    print(f"窗口标题: {title!r}")
    print(f"判定为 War Thunder: {is_war_thunder_foreground()}")
