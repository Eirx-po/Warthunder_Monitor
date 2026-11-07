"""
War Thunder HUD 启动器窗口

一个图形界面，替代命令行守护模式：
  - 实时显示游戏状态（是否在战斗、当前载具、空战/陆战）
  - 一键启动 / 停止 HUD
  - 常用开关（勾选后重启 HUD 生效）
  - 直接打开布局配置文件

运行：
    python wt_hud_launcher_gui.py

开关通过环境变量传给 HUD 子进程，改动后需重启 HUD（点"重启 HUD"）。
"""
import json
import os
import subprocess
import sys
import time
import traceback
import urllib.request

from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtGui import QColor, QFont, QIcon, QPixmap, QPainter
from PyQt5.QtWidgets import (QAction, QApplication, QCheckBox, QComboBox,
                             QDoubleSpinBox, QFormLayout, QGroupBox,
                             QHBoxLayout, QLabel, QMainWindow, QMenu,
                             QPushButton, QSpinBox, QSystemTrayIcon,
                             QTextEdit, QVBoxLayout, QWidget)

HERE = os.path.dirname(os.path.abspath(__file__))


def _resolve(*parts):
    """
    解析脚本路径。

    本启动器放在 J:\\Quant\\ 根目录，所以：
      空战 HUD -> wt_air_hud\\hud_overlay.py
      陆战 HUD -> wt_hud_v2.py
    （早期版本误把 HERE 当成 wt_air_hud 目录，导致找 J:\\Quant\\hud_overlay.py 失败）
    """
    p = os.path.normpath(os.path.join(HERE, *parts))
    if os.path.exists(p):
        return p
    # 兼容：万一以后脚本被挪进 wt_air_hud 目录
    p2 = os.path.normpath(os.path.join(HERE, "..", *parts))
    if os.path.exists(p2):
        return p2
    return p      # 都不存在就返回主路径，让调用方报错时能显示出来


AIR_HUD = _resolve("wt_air_hud", "hud_overlay.py")
GROUND_HUD = _resolve("wt_hud_v2.py")
# 布局配置按"空战 / 陆战"拆成两个独立文件，路径由 self._layout_path() 决定（见下方）
POWER_CURVE = _resolve("wt_power_curve.py")
DAEMON = _resolve("wt_hud_launcher.py")   # 守护模式：按载具自动切换空战/陆战
DAEMON_LOG = os.path.join(HERE, "wt_daemon.log")

def _resolve_python():
    """挑一个带 PyQt5 的解释器：优先工作区内的持久 venv。

    managed 主环境的 site-packages 会被环境重置周期性清掉（PyQt5 反复丢失，
    表现为双击 bat 只报 "Launcher exited with an error."，因为 pythonw 无控制台、
    traceback 被吞）。放到工作区自己的 .venv_wt 里就不会被清。
    """
    for rel in ((".venv_wt", "Scripts", "pythonw.exe"),
                (".venv_wt", "Scripts", "python.exe")):
        cand = os.path.join(HERE, *rel)
        if os.path.exists(cand):
            return cand
    if sys.executable and os.path.exists(sys.executable):
        return sys.executable
    return (r"C:\Users\Administrator\.workbuddy\binaries\python"
            r"\versions\3.13.12\python.exe")


PYTHON = _resolve_python()

# ⚠ 子进程一律 CREATE_NO_WINDOW（0x08000000），不要用 CREATE_NEW_CONSOLE。
# 陆战里上飞机/被打下来会来回切 HUD，每切一次弹一个黑色 CMD 窗口非常干扰游戏。
CREATE_NO_WINDOW = 0x08000000


def _spawn_flags():
    return CREATE_NO_WINDOW if os.name == "nt" else 0

BG = QColor(18, 22, 30)
FG = QColor(225, 230, 240)
MUTED = QColor(140, 150, 165)
GREEN = QColor(90, 200, 120)
AMBER = QColor(230, 180, 70)
RED = QColor(225, 90, 90)


def make_hud_icon():
    """自绘 HUD 图标：任务栏按钮 + 系统托盘共用。

    默认 pythonw 图标是通用 Python 蟒蛇，缩到任务栏后很难从一堆图标里认出来；
    折叠的托盘区里更找不到。用蓝底白 H 让两个入口都好认。
    """
    try:
        pm = QPixmap(64, 64)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing)
        p.setBrush(QColor(38, 110, 190))
        p.setPen(Qt.NoPen)
        p.drawRoundedRect(2, 2, 60, 60, 12, 12)
        p.setPen(QColor(255, 255, 255))
        p.setFont(QFont("Consolas", 30, QFont.Bold))
        p.drawText(pm.rect(), Qt.AlignCenter, "H")
        p.end()
        return QIcon(pm)
    except Exception:
        return QIcon()


def fetch_indicators():
    try:
        req = urllib.request.Request("http://localhost:8111/indicators",
                                     headers={"User-Agent": "WT-Launcher"})
        with urllib.request.urlopen(req, timeout=1.5) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except Exception:
        return None


# 需要匹配的 HUD 脚本（小写比较）
HUD_SCRIPTS = ("hud_overlay.py", "wt_hud_v2.py")


def _pids_from_lines(text, scripts):
    """从含 '命令行 ... PID' 的文本里挑出匹配脚本名的进程 PID"""
    pids = []
    for line in text.splitlines():
        low = line.lower()
        if not any(s in low for s in scripts):
            continue
        # 取行内最后一个整数当 PID（兼容 csv / 表格两种输出）
        for tok in reversed(line.replace(",", " ").split()):
            if tok.isdigit():
                pids.append(int(tok))
                break
    return pids


def _hidden_run(args, timeout=15, encoding="utf-8"):
    """
    静默执行外部命令（wmic / powershell / taskkill）。

    ⚠ 必须带 CREATE_NO_WINDOW：这些都是控制台程序，直接 subprocess.run
    会闪一个黑色窗口出来。守护每轮、GUI 每秒都要扫进程，闪窗根本没法打游戏。
    """
    try:
        return subprocess.run(args, capture_output=True, text=True,
                              encoding=encoding, errors="replace",
                              timeout=timeout,
                              creationflags=_spawn_flags(),
                              stdin=subprocess.DEVNULL)
    except Exception:
        return None


def _scan_pids(scripts):
    """
    【冷路径兜底】按命令行关键字扫进程 —— 会拉起 powershell，很慢且有开销。

    ⚠ 只在 PID 文件缺失时用（比如 HUD 是旧版本启动的、没写 PID 文件）。
    热路径（刷新状态、启动判断）一律走 wt_hud_launcher 的 PID 文件，
    否则每秒开一次 powershell 会严重影响游戏。
    """
    ps = ("Get-CimInstance Win32_Process -Filter \""
          "Name='python.exe' OR Name='pythonw.exe'\" | "
          "ForEach-Object { $_.CommandLine + ' ' + $_.ProcessId }")
    out = _hidden_run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
        timeout=15)
    return _pids_from_lines((out.stdout if out else "") or "", scripts)


def hud_pids():
    """
    当前活着的 HUD 进程 —— **只读 PID 文件，零开销、零子进程**。

    ⚠ 热路径（每秒刷新状态）必须走这里。不要在里面做进程扫描：
    PID 文件缺失时会退化成"每 3 秒开一次 powershell"。
    """
    from wt_hud_launcher import hud_pids as _by_file
    return _by_file()


def hud_pids_scan():
    """
    【仅用户主动操作调用】进程级扫描，能捞到没写 PID 文件的旧 HUD。
    会拉起 powershell，所以只在"停止/清理"这种一次性动作里用。
    """
    return _scan_pids(HUD_SCRIPTS)


def daemon_pids():
    """当前活着的守护进程 PID —— 要求心跳新鲜，避免陈旧 PID 让启动器误判"已在运行" """
    from wt_hud_launcher import read_pid, PID_DAEMON, daemon_alive
    if not daemon_alive():
        return []
    pid = read_pid(PID_DAEMON)
    return [pid] if pid else []


def daemon_pids_scan():
    """【仅一次性操作调用】进程级扫描守护进程"""
    return _scan_pids(("wt_hud_launcher.py",))


def detect_mode(ind):
    """模式判定统一走 wt_hud_launcher，避免 GUI 里再复制一份（会走样）"""
    from wt_hud_launcher import detect_mode as _dm
    return _dm(ind)


def game_running():
    """游戏进程是否存在（进程级，不看前台）"""
    try:
        from wt_foreground import is_war_thunder_running
        return is_war_thunder_running()
    except Exception:
        return True


class Launcher(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("War Thunder HUD 启动器")
        self.setMinimumWidth(430)
        self.proc = None          # HUD 进程（受启停管理）
        self.tool_proc = None     # 独立工具进程（不与 HUD 联动）
        self.auto_proc = None     # 守护进程（自动切换模式）

        root = QWidget()
        self.setCentralWidget(root)
        v = QVBoxLayout(root)
        v.setSpacing(10)
        v.setContentsMargins(14, 14, 14, 14)

        # ---- 状态 ----
        g_state = QGroupBox("状态")
        f1 = QVBoxLayout(g_state)
        self.lb_game = QLabel("游戏: 检测中…")
        self.lb_mode = QLabel("模式: —")
        self.lb_hud = QLabel("HUD: 未运行")
        for lb in (self.lb_game, self.lb_mode, self.lb_hud):
            lb.setFont(QFont("Consolas", 10))
            f1.addWidget(lb)
        v.addWidget(g_state)

        # ---- 按钮 ----
        # GUI 不再区分空战/陆战：只有一个「启动 HUD」，由守护进程按载具自动选。
        # 手动选模式容易选错，且自动分类现在已可靠（见 detect_mode）。
        h = QHBoxLayout()
        self.bt_start = QPushButton("启动 HUD（自动识别载具）")
        self.bt_stop = QPushButton("停止 HUD")
        for b in (self.bt_start, self.bt_stop):
            b.setMinimumHeight(30)
            h.addWidget(b)
        v.addLayout(h)

        # 工具按钮独占一行：动力曲线是独立进程，不参与 HUD 的启停管理
        h2t = QHBoxLayout()
        self.bt_curve = QPushButton("动力曲线（独立工具）")
        self.bt_curve.setMinimumHeight(30)
        h2t.addWidget(self.bt_curve)
        v.addLayout(h2t)

        self.bt_start.clicked.connect(self.start_auto)
        self.bt_stop.clicked.connect(self.stop)
        self.bt_curve.clicked.connect(self.start_curve)

        # ---- 开关 ----
        g_opt = QGroupBox("开关（改动后需重启 HUD）")
        f2 = QVBoxLayout(g_opt)
        self.cb_blur = QCheckBox("失去焦点时隐藏 HUD")
        self.cb_ground = QCheckBox("显示地面目标")
        self.cb_nose = QCheckBox("跳过正前方(12点)目标")
        self.cb_full = QCheckBox("详细面板（飞行+战斗完整信息）")
        self.cb_arrow = QCheckBox("忽略距离阈值，显示全部箭头（调试）")
        self.cb_nose.setChecked(True)      # 与代码默认一致
        for cb in (self.cb_blur, self.cb_ground, self.cb_nose,
                   self.cb_full, self.cb_arrow):
            cb.setFont(QFont("Consolas", 9))
            f2.addWidget(cb)
        v.addWidget(g_opt)

        # ---- 布局设置（直接写在窗口里，不用开 json 文件）----
        # 空战 / 陆战 两套独立配置，分别设置互不影响
        self.layout_mode = "air"
        # 屏幕可用分辨率：把"角落默认位置"换算成具体像素（面板约 255x175，边距 15）
        try:
            _geo = QApplication.primaryScreen().availableGeometry()
            self._screen_w, self._screen_h = _geo.width(), _geo.height()
        except Exception:
            self._screen_w, self._screen_h = 1920, 1080

        g_lay = QGroupBox("面板位置（空战 / 陆战 分别设置）")
        f3 = QFormLayout(g_lay)
        f3.setLabelAlignment(Qt.AlignRight)

        # 模式切换：空战 / 陆战 各读各的布局文件
        self.cmb_mode = QComboBox()
        self.cmb_mode.addItems(["空战", "陆战"])
        self.cmb_mode.setFont(QFont("Consolas", 9))
        self.cmb_mode.setToolTip("空战与陆战使用各自独立的布局文件，分别设置互不影响")
        self.cmb_mode.currentTextChanged.connect(self._on_mode_changed)
        f3.addRow("设置模式:", self.cmb_mode)

        def _xy_spin(tip_x, tip_y):
            sx = QSpinBox(); sx.setRange(-2000, 8000); sx.setSuffix(" px")
            sx.setToolTip(tip_x)
            sy = QSpinBox(); sy.setRange(-2000, 8000); sy.setSuffix(" px")
            sy.setToolTip(tip_y)
            return sx, sy

        def _scale_spin():
            sp = QDoubleSpinBox(); sp.setRange(0.5, 3.0)
            sp.setSingleStep(0.1); sp.setDecimals(1)
            sp.setValue(1.0); sp.setSuffix(" 倍")
            return sp

        # 面板1 = flight（空战：飞行/爬升；陆战：载具状态）
        # 面板2 = target（空战：目标 TARGET；陆战：战场态势 TARGET）
        # 两种模式的面板集合相同、只是叫法不同，所以设置页共用，标签随模式切换。
        # 定位只用「绝对坐标 X/Y」一种，与 TARGET 保持一致（不再有角落/偏移两套）。
        self.sp_fx, self.sp_fy = _xy_spin(
            "该面板左上角的屏幕 X（左上角为原点）",
            "该面板左上角的屏幕 Y（左上角为原点）")
        self.sp_tx, self.sp_ty = _xy_spin(
            "TARGET 面板左上角的屏幕 X；右下角约 = 屏幕宽 - 270",
            "TARGET 面板左上角的屏幕 Y；右下角约 = 屏幕高 - 190")
        self.sp_fscale = _scale_spin()
        self.sp_tscale = _scale_spin()

        # 标签随模式变化（见 _update_mode_labels）
        self.lbl_fx = QLabel("飞行/爬升面板 X:")
        self.lbl_fy = QLabel("飞行/爬升面板 Y:")
        self.lbl_fscale = QLabel("飞行/爬升面板缩放:")
        self.lbl_tx = QLabel("目标 TARGET X:")
        self.lbl_ty = QLabel("目标 TARGET Y:")
        self.lbl_tscale = QLabel("TARGET 单独缩放:")

        f3.addRow(self.lbl_fx, self.sp_fx)
        f3.addRow(self.lbl_fy, self.sp_fy)
        f3.addRow(self.lbl_fscale, self.sp_fscale)
        f3.addRow(self.lbl_tx, self.sp_tx)
        f3.addRow(self.lbl_ty, self.sp_ty)
        f3.addRow(self.lbl_tscale, self.sp_tscale)

        # 引擎面板（长条，仅空战有）：独立开关 + 位置 + 缩放。
        # 打包进一个容器，切模式时整组显隐（陆战没有引擎面板）。
        self.eng_box = QWidget()
        fe = QFormLayout(self.eng_box)
        fe.setContentsMargins(0, 0, 0, 0)
        fe.setLabelAlignment(Qt.AlignRight)
        self.chk_engine = QCheckBox("显示引擎面板（长条）")
        self.chk_engine.setFont(QFont("Consolas", 9))
        self.chk_engine.setToolTip("勾选后空战 HUD 显示横向长条的引擎面板（不必再设环境变量）")
        self.sp_ex, self.sp_ey = _xy_spin(
            "引擎长条左上角的屏幕 X；右下角约 = 屏幕宽 - 750",
            "引擎长条左上角的屏幕 Y；右下角约 = 屏幕高 - 90")
        self.sp_escale = _scale_spin()
        fe.addRow(self.chk_engine)
        fe.addRow("引擎面板 X:", self.sp_ex)
        fe.addRow("引擎面板 Y:", self.sp_ey)
        fe.addRow("引擎面板缩放:", self.sp_escale)
        f3.addRow(self.eng_box)

        # 缩放：一处生效即面板尺寸+字号+内部间距一起变
        self.sp_scale = QDoubleSpinBox()
        self.sp_scale.setRange(0.5, 3.0)
        self.sp_scale.setSingleStep(0.1)
        self.sp_scale.setDecimals(1)
        self.sp_scale.setSuffix(" 倍")

        # 字体：可下拉选，也可直接手输字体名
        self.cmb_font = QComboBox()
        self.cmb_font.addItems(["Consolas", "Microsoft YaHei", "SimHei",
                                "Arial", "Courier New", "Tahoma"])
        self.cmb_font.setEditable(True)

        f3.addRow("整体缩放:", self.sp_scale)
        f3.addRow("字体:", self.cmb_font)
        v.addWidget(g_lay)

        self.bt_save = QPushButton("保存布局设置")
        self.bt_save.setMinimumHeight(30)
        v.addWidget(self.bt_save)
        self.bt_save.clicked.connect(self.save_layout)

        self.load_layout_ui()

        # ---- 按钮 ----
        h2 = QHBoxLayout()
        self.bt_layout = QPushButton("打开 json（高级）")
        self.bt_apply = QPushButton("应用并重启")
        for b in (self.bt_layout, self.bt_apply):
            b.setMinimumHeight(28)
            h2.addWidget(b)
        v.addLayout(h2)
        self.bt_layout.clicked.connect(self.open_layout)
        self.bt_apply.clicked.connect(self.restart)

        # ---- 日志 ----
        self.log = QTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumHeight(110)
        self.log.setFont(QFont("Consolas", 8))
        v.addWidget(self.log)

        self.say("启动器就绪。游戏需在窗口化/无边框模式下才能看到 HUD。")

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(1000)
        # PID 扫描走 PowerShell（外部进程），不能每秒都拉：
        # 缓存 3 秒，状态文字照样每秒更新
        self._pids_cache = []
        self._pids_ts = 0.0
        self.refresh()
        self._init_tray()

    def _cached_hud_pids(self, max_age=3.0):
        now = time.time()
        if now - self._pids_ts >= max_age:
            self._pids_cache = hud_pids()
            self._pids_ts = now
        return self._pids_cache

    def say(self, msg):
        ts = time.strftime("%H:%M:%S")
        self.log.append(f"[{ts}] {msg}")

    # ---- 环境 ----
    def build_env(self):
        env = os.environ.copy()
        if self.cb_blur.isChecked():
            env["WT_HUD_HIDE_ON_BLUR"] = "1"
        else:
            env.pop("WT_HUD_HIDE_ON_BLUR", None)
        if self.cb_ground.isChecked():
            env["WT_HUD_GROUND_TARGETS"] = "1"
        else:
            env.pop("WT_HUD_GROUND_TARGETS", None)
        env["WT_HUD_NOSE_SKIP"] = "1" if self.cb_nose.isChecked() else "0"
        if self.cb_full.isChecked():
            env["WT_HUD_FULL"] = "1"
        else:
            env.pop("WT_HUD_FULL", None)
        if self.cb_arrow.isChecked():
            env["WT_ARROW_DEBUG"] = "1"
        else:
            env.pop("WT_ARROW_DEBUG", None)
        return env

    # ---- 控制 ----
    def start(self, script):
        # 手动指定模式 ⇒ 先退掉自动切换，否则守护进程会把 HUD 又换回去
        self.stop_auto(silent=True)
        self.stop(silent=True)
        if not os.path.exists(script):
            self.say(f"找不到脚本: {script}")
            return
        name = os.path.basename(script)
        try:
            flags = _spawn_flags()
            self.proc = subprocess.Popen(
                [PYTHON, "-u", script], cwd=os.path.dirname(script),
                env=self.build_env(), creationflags=flags)
            self.say(f"已启动 {name} (PID {self.proc.pid})")
        except Exception as e:
            self.say(f"启动失败: {e}")

    def stop(self, silent=False):
        # 先停自己拉起的
        if self.proc is not None and self.proc.poll() is None:
            try:
                self.proc.kill()
            except Exception:
                pass
        self.proc = None
        # 自动切换也一并关掉：否则守护进程会立刻把 HUD 又拉起来，
        # 用户点了"停止 HUD"却看到它自己复活，很困惑
        self.stop_auto(silent=True)
        # 再清理残留：PID 文件是主路径（最可靠），进程扫描捞没登记 PID 文件的旧 HUD
        pids = set(hud_pids()) | set(hud_pids_scan())
        for pid in pids:
            _hidden_run(["taskkill", "/F", "/PID", str(pid)], timeout=5)
        if not silent:
            self.say("已停止 HUD" if pids else "没有运行中的 HUD")

    def start_auto(self):
        """启动守护进程：按载具自动选空战/陆战 HUD（GUI 唯一的启动方式）"""
        # ⚠ 必须做系统级检查：只看 self.auto_proc 的话，
        # 已经有一个守护在跑（比如命令行拉起的）时会再起一个，两个守护互相打架。
        # 启动时才做一次进程扫描（一次性开销），刷新状态不做。
        existing = daemon_pids() or daemon_pids_scan()
        if existing:
            self.auto_proc = None
            self.say(f"自动模式已在运行 (PID {', '.join(map(str, existing))})")
            return
        if not os.path.exists(DAEMON):
            self.say(f"找不到脚本: {DAEMON}")
            return
        try:
            flags = _spawn_flags()
            # ⚠ 必须重定向 stdout：GUI 是 pythonw（没有控制台），不重定向的话
            #   守护的 print 会全部丢失 —— 出问题时日志一片空白、无从查起
            #   （2026-09-27 就因为 wt_daemon.log 停在 9-22 白白多查很久）
            try:
                logf = open(DAEMON_LOG, "a", encoding="utf-8", errors="replace")
            except Exception:
                logf = subprocess.DEVNULL
            self.auto_proc = subprocess.Popen(
                [PYTHON, "-u", DAEMON, "--watch"], cwd=os.path.dirname(DAEMON),
                env=self.build_env(), creationflags=flags,
                stdout=logf, stderr=subprocess.STDOUT)
            self.say(f"自动切换已开启 (PID {self.auto_proc.pid}) — "
                     f"进战斗后按载具自动选空战/陆战 HUD")
        except Exception as e:
            self.say(f"启动失败: {e}")

    def stop_auto(self, silent=False):
        if self.auto_proc is not None and self.auto_proc.poll() is None:
            try:
                self.auto_proc.kill()
            except Exception:
                pass
        self.auto_proc = None
        # 清掉可能由别的途径拉起的守护进程（一次性操作，可用扫描）
        for pid in daemon_pids() or daemon_pids_scan():
            _hidden_run(["taskkill", "/F", "/PID", str(pid)], timeout=5)
        # 顺带收掉守护拉起的 HUD：否则关了自动切换，屏幕上还留着一个 HUD，很莫名
        for pid in set(hud_pids()) | set(hud_pids_scan()):
            _hidden_run(["taskkill", "/F", "/PID", str(pid)], timeout=5)
        # ⚠ taskkill /F 强杀不会跑守护的清理逻辑，旧 PID/心跳文件会残留，
        #   导致下一次 start_auto 误判"已在运行"而不重启 → 改完布局不重载。
        #   这里显式清掉，保证「应用并重启」一定能拉起新的守护+HUD 重新读配置。
        try:
            from wt_hud_launcher import clear_pid, PID_DAEMON, HEARTBEAT
            clear_pid(PID_DAEMON)
            clear_pid(HEARTBEAT)
        except Exception:
            pass
        if not silent:
            self.say("已关闭自动切换")

    def start_curve(self):
        """
        启动动力曲线工具。

        与 HUD 的区别：
          - 独立进程，用 self.tool_proc 单独持有，不会被 stop()/start() 连带杀掉
          - 重复点击时若上一实例仍存活，只提示不重复拉起
        """
        if self.tool_proc is not None and self.tool_proc.poll() is None:
            self.say(f"动力曲线已在运行 (PID {self.tool_proc.pid})")
            return
        if not os.path.exists(POWER_CURVE):
            self.say(f"找不到脚本: {POWER_CURVE}")
            return
        try:
            flags = _spawn_flags()
            self.tool_proc = subprocess.Popen(
                [PYTHON, "-u", POWER_CURVE], cwd=os.path.dirname(POWER_CURVE),
                creationflags=flags)
            self.say(f"已启动 动力曲线 (PID {self.tool_proc.pid}) — "
                     f"进战斗后满油门爬升即可取样")
        except Exception as e:
            self.say(f"启动失败: {e}")

    def restart(self):
        """
        应用布局设置后重启。

        ⚠ 不要硬编码成空战 HUD（旧代码就是这么写的）——
        GUI 已经不区分模式了，重启一律回到自动识别。
        """
        self.say("保存并重启 HUD（自动识别载具）…")
        self.save_layout()
        self.stop(silent=True)
        self.start_auto()

    # ---- 布局读写 ----
    def _layout_path(self):
        """当前模式（空战 / 陆战）对应的布局配置文件路径"""
        name = ("hud_layout_air.json" if self.layout_mode == "air"
                else "hud_layout_ground.json")
        return os.path.join(HERE, "wt_air_hud", name)

    def _default_pos(self, corner):
        """把角落代号换算成默认像素坐标（面板约 255x175，边距 15）—— 与渲染端角落一致"""
        w, h = self._screen_w, self._screen_h
        if corner == "TL":
            return (15, 15)
        if corner == "TR":
            return (w - 255 - 15, 15)
        if corner == "BL":
            return (15, h - 175 - 15)
        return (w - 255 - 15, h - 175 - 15)      # BR

    def _update_mode_labels(self):
        """面板叫法随模式变化：空战=飞行/爬升+目标，陆战=载具状态+战场态势"""
        if self.layout_mode == "air":
            f_name, t_name = "飞行/爬升面板", "目标 TARGET"
        else:
            f_name, t_name = "载具状态面板", "战场态势 TARGET"
        self.lbl_fx.setText(f"{f_name} X:")
        self.lbl_fy.setText(f"{f_name} Y:")
        self.lbl_fscale.setText(f"{f_name}缩放:")
        self.lbl_tx.setText(f"{t_name} X:")
        self.lbl_ty.setText(f"{t_name} Y:")
        self.lbl_tscale.setText(f"{t_name}缩放:")
        # 引擎面板只有空战有：陆战模式整组隐藏
        self.eng_box.setVisible(self.layout_mode == "air")

    def _on_mode_changed(self, text):
        """切换模式：先自动保存当前模式的改动，再载入新模式，避免丢失"""
        try:
            self.save_layout(silent=True)
        except Exception:
            pass
        self.layout_mode = "air" if text == "空战" else "ground"
        self.load_layout_ui()
        self.say(f"已切换到【{text}】布局（已自动保存上一模式）")

    def load_layout_ui(self):
        """从当前模式的布局文件读入当前值，填进窗口控件"""
        try:
            with open(self._layout_path(), "r", encoding="utf-8") as f:
                cfg = json.load(f)
        except Exception:
            cfg = {}
        if not isinstance(cfg, dict):
            cfg = {}
        ui = cfg.get("ui") or {}
        if isinstance(ui, dict):
            try:
                self.sp_scale.setValue(float(ui.get("scale", 1.0)))
            except Exception:
                self.sp_scale.setValue(1.0)
            f = ui.get("font", "Consolas")
            if isinstance(f, str) and f.strip():
                self.cmb_font.setCurrentText(f.strip())

        # 两个面板都用绝对坐标定位；没配过就按屏幕分辨率给"角落默认值"
        pcfg = cfg.get("pos") if isinstance(cfg.get("pos"), dict) else {}
        for key, sx, sy, corner in (("flight", self.sp_fx, self.sp_fy, "TL"),
                                    ("target", self.sp_tx, self.sp_ty, "BR")):
            pv = pcfg.get(key)
            if isinstance(pv, list) and len(pv) >= 2:
                sx.setValue(int(pv[0]))
                sy.setValue(int(pv[1]))
            else:
                dx, dy = self._default_pos(corner)
                sx.setValue(dx)
                sy.setValue(dy)

        # 两个面板各自的单独缩放
        psc = cfg.get("panel_scale") if isinstance(cfg.get("panel_scale"), dict) else {}
        for key, sp in (("flight", self.sp_fscale), ("target", self.sp_tscale)):
            try:
                sp.setValue(float(psc.get(key, 1.0)))
            except Exception:
                sp.setValue(1.0)

        # 引擎面板（仅空战）：开关 + 位置 + 缩放
        eng = str(cfg.get("engine", "OFF")).upper()
        self.chk_engine.setChecked(eng not in ("OFF", ""))
        ep = pcfg.get("engine")
        if isinstance(ep, list) and len(ep) >= 2:
            self.sp_ex.setValue(int(ep[0]))
            self.sp_ey.setValue(int(ep[1]))
        else:
            self.sp_ex.setValue(self._screen_w - 750)
            self.sp_ey.setValue(self._screen_h - 90)
        try:
            self.sp_escale.setValue(float(psc.get("engine", 1.0)))
        except Exception:
            self.sp_escale.setValue(1.0)

        self._update_mode_labels()

    def save_layout(self, silent=False):
        """把窗口里的设置写回当前模式的布局文件"""
        try:
            with open(self._layout_path(), "r", encoding="utf-8") as f:
                cfg = json.load(f)
        except Exception:
            cfg = {}
        if not isinstance(cfg, dict):
            cfg = {}

        cfg["ui"] = {"scale": round(self.sp_scale.value(), 2),
                     "font": self.cmb_font.currentText().strip() or "Consolas"}
        # 面板定位只用绝对坐标一种：flight / target 都写进 pos
        cfg["pos"] = {"flight": [self.sp_fx.value(), self.sp_fy.value()],
                      "target": [self.sp_tx.value(), self.sp_ty.value()],
                      "engine": [self.sp_ex.value(), self.sp_ey.value()]}
        # offset 已废弃（GUI 不再暴露偏移），清掉以免叠加到绝对坐标上
        cfg.pop("offset", None)
        # 保留配置里的角落代号（flight:TL / target:BR）作兜底：pos 优先，缺省时按角落显示

        # 引擎面板（仅空战）：开关写 engine 键（非 OFF 即显示）
        cfg["engine"] = "ON" if self.chk_engine.isChecked() else "OFF"

        # 各面板单独的缩放（覆盖"整体缩放"）
        psc = cfg.get("panel_scale") if isinstance(cfg.get("panel_scale"), dict) else {}
        psc["flight"] = round(self.sp_fscale.value(), 2)
        psc["target"] = round(self.sp_tscale.value(), 2)
        psc["engine"] = round(self.sp_escale.value(), 2)
        cfg["panel_scale"] = psc

        try:
            with open(self._layout_path(), "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
            if not silent:
                self.say(f"已保存【{'空战' if self.layout_mode == 'air' else '陆战'}】布局: "
                         f"面板1=({self.sp_fx.value()},{self.sp_fy.value()}) "
                         f"缩放{psc['flight']}倍 "
                         f"TARGET=({self.sp_tx.value()},{self.sp_ty.value()}) "
                         f"缩放{psc['target']}倍 "
                         f"整体={cfg['ui']['scale']}倍 字体={cfg['ui']['font']}"
                         f"{' 引擎=开' if self.chk_engine.isChecked() else ''}"
                         f" — 点『应用并重启』生效")
        except Exception as e:
            if not silent:
                self.say(f"保存失败: {e}")

    def open_layout(self):
        if not os.path.exists(self._layout_path()):
            self.say("布局文件不存在")
            return
        try:
            os.startfile(self._layout_path())
            self.say(f"已打开【{'空战' if self.layout_mode == 'air' else '陆战'}】"
                     f"布局文件（改完重启 HUD 生效）")
        except Exception as e:
            self.say(f"打开失败: {e}")

    # ---- 刷新 ----
    def refresh(self):
        ind = fetch_indicators()
        if ind is None:
            # 8111 不可达 ≠ 游戏没开（刚启动时接口还没起来），用进程检测区分
            if game_running():
                self.lb_game.setText("游戏: 进程已运行，等待 8111 接口…")
                self.lb_game.setStyleSheet(f"color: {AMBER.name()}")
            else:
                self.lb_game.setText("游戏: 未运行")
                self.lb_game.setStyleSheet(f"color: {MUTED.name()}")
            self.lb_mode.setText("模式: —")
            self.lb_mode.setStyleSheet(f"color: {MUTED.name()}")
        elif not ind.get("valid", False):
            self.lb_game.setText("游戏: 运行中，但不在战斗（机库/菜单）")
            self.lb_game.setStyleSheet(f"color: {AMBER.name()}")
            self.lb_mode.setText("模式: —")
            self.lb_mode.setStyleSheet(f"color: {MUTED.name()}")
        else:
            vtype = ind.get("type", "?")
            # 模式判定复用守护进程的实现，不在这里另写一份
            _mode, desc = detect_mode(ind)
            self.lb_game.setText(f"游戏: 战斗中  {vtype}")
            self.lb_game.setStyleSheet(f"color: {GREEN.name()}")
            # desc 已含中文模式名（如"空战（f_16xl）"），不要再拼一次
            self.lb_mode.setText(f"模式: {desc}")
            self.lb_mode.setStyleSheet(f"color: {GREEN.name()}")

        pids = self._cached_hud_pids()
        if pids:
            self.lb_hud.setText(f"HUD: 运行中 (PID {', '.join(map(str, pids))})")
            self.lb_hud.setStyleSheet(f"color: {GREEN.name()}")
        else:
            self.lb_hud.setText("HUD: 未运行")
            self.lb_hud.setStyleSheet(f"color: {RED.name()}")

    # ---- 缩到任务栏（主）+ 系统托盘（辅）----
    def _init_tray(self):
        """托盘只作为辅助入口；主路径是「缩到任务栏按钮」。

        曾把『✕』做成 hide() 到托盘，但 Windows 11 会把新托盘图标折叠进
        「隐藏的图标」，窗口等于凭空消失、任务栏上也点不回来。现在一律用
        showMinimized()（保留任务栏按钮），hide 只作为托盘菜单里的可选项。
        """
        self._really_quit = False
        self._tray_ok = False
        self._tray_hint_shown = False
        try:
            if not QSystemTrayIcon.isSystemTrayAvailable():
                self.say("系统托盘不可用：『✕』将正常关闭启动器。")
                return
            app = QApplication.instance()
            icon = app.windowIcon()
            if icon.isNull():
                icon = make_hud_icon()
            self.tray = QSystemTrayIcon(icon, self)
            self.tray.setToolTip("War Thunder HUD 启动器")

            menu = QMenu(self)
            act_show = QAction("显示启动器", self)
            act_show.triggered.connect(self._show_window)
            menu.addAction(act_show)
            act_min = QAction("缩到任务栏", self)
            act_min.triggered.connect(self._minimize_to_taskbar)
            menu.addAction(act_min)
            act_hide = QAction("隐藏到托盘（不占任务栏）", self)
            act_hide.triggered.connect(self._hide_to_tray)
            menu.addAction(act_hide)
            menu.addSeparator()
            act_quit = QAction("退出启动器", self)
            act_quit.triggered.connect(self._quit)
            menu.addAction(act_quit)
            self.tray.setContextMenu(menu)
            self.tray.activated.connect(self._on_tray_activated)
            self.tray.show()
            self._tray_ok = True
            self.say("点『—』或『✕』= 缩到任务栏（任务栏按钮可点回）；"
                     "托盘右键可退出、或选『隐藏到托盘』完全不占任务栏。")
        except Exception as e:
            self.say(f"系统托盘初始化失败，『✕』将正常关闭启动器: {e}")

    def _minimize_to_taskbar(self):
        """缩到任务栏：最小化成任务栏按钮，不 hide，随时能点回来。"""
        self.showMinimized()
        if not self._tray_hint_shown:
            self._tray_hint_shown = True
            try:
                self.tray.showMessage(
                    "WT HUD 启动器",
                    "已缩到任务栏：点任务栏按钮或双击托盘图标恢复。"
                    "托盘右键可退出（HUD 继续运行）。",
                    QSystemTrayIcon.Information, 4000)
            except Exception:
                pass
        self.say("已缩到任务栏（HUD 继续运行）")

    def _hide_to_tray(self):
        """完全隐藏到托盘（仅托盘菜单主动选择时才用）。"""
        if not self._tray_ok:
            self._minimize_to_taskbar()
            return
        self.hide()
        try:
            self.tray.showMessage(
                "WT HUD 启动器",
                "已隐藏到托盘。双击图标恢复，右键可退出（HUD 继续运行）。",
                QSystemTrayIcon.Information, 4000)
        except Exception:
            pass
        self.say("已隐藏到托盘（HUD 继续运行）")

    def _show_window(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _on_tray_activated(self, reason):
        if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick):
            self._show_window()

    def _quit(self):
        """托盘菜单『退出启动器』：只关启动器，不停止正在运行的 HUD。"""
        self._really_quit = True
        try:
            self.tray.hide()
        except Exception:
            pass
        QApplication.instance().quit()

    def closeEvent(self, event):
        """点『✕』= 缩到任务栏（保留任务栏按钮），不再 hide 到托盘。

        托盘不可用时没有「退出」入口，所以退化为正常关闭，避免窗口关不掉。
        """
        if self._really_quit or not self._tray_ok:
            event.accept()
            return
        event.ignore()
        self._minimize_to_taskbar()


def _install_crash_log():
    """pythonw 无控制台：把 stdout/stderr 与未捕获异常写进 wt_gui_error.log。

    VBS 启动器在 GUI 崩溃时会读这个日志弹窗，避免又出现「只看到
    Launcher exited with an error.」却查不到 traceback 的情况。
    """
    path = os.path.join(HERE, "wt_gui_error.log")
    try:
        f = open(path, "w", encoding="utf-8", errors="replace")
    except Exception:
        return
    if getattr(sys, "stdout", None) is None:
        sys.stdout = f
    if getattr(sys, "stderr", None) is None:
        sys.stderr = f

    def hook(exc_type, exc_value, exc_tb):
        traceback.print_exception(exc_type, exc_value, exc_tb, file=f)
        f.flush()
    sys.excepthook = hook


def main():
    _install_crash_log()
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    # 任务栏按钮 / 托盘共用图标：默认 pythonw 蟒蛇图标缩到任务栏后认不出来
    app.setWindowIcon(make_hud_icon())
    w = Launcher()
    w.show()
    # 只有托盘可用时才禁止「关掉最后一个窗口即退出」：托盘不可用时保持
    # Qt 默认行为，关闭窗口就正常结束进程，不会留下无窗口的僵尸进程。
    if w._tray_ok:
        app.setQuitOnLastWindowClosed(False)
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
