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
import urllib.request

from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtGui import QColor, QFont
from PyQt5.QtWidgets import (QApplication, QCheckBox, QComboBox, QDoubleSpinBox,
                             QFormLayout, QGroupBox, QHBoxLayout, QLabel,
                             QMainWindow, QPushButton, QSpinBox, QTextEdit,
                             QVBoxLayout, QWidget)

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
LAYOUT_JSON = _resolve("wt_air_hud", "hud_layout.json")
POWER_CURVE = _resolve("wt_power_curve.py")
DAEMON = _resolve("wt_hud_launcher.py")   # 守护模式：按载具自动切换空战/陆战

PYTHON = (r"C:\Users\Administrator\.workbuddy\binaries\python"
          r"\versions\3.13.12\python.exe")
if not os.path.exists(PYTHON):
    PYTHON = sys.executable

BG = QColor(18, 22, 30)
FG = QColor(225, 230, 240)
MUTED = QColor(140, 150, 165)
GREEN = QColor(90, 200, 120)
AMBER = QColor(230, 180, 70)
RED = QColor(225, 90, 90)


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


def _scan_pids(scripts):
    """按命令行关键字扫描 python 进程 PID（wmic 失败自动走 PowerShell CIM）"""
    try:
        out = subprocess.run(
            ["wmic", "process", "where", "name='python.exe'", "get",
             "processid,commandline", "/format:csv"],
            capture_output=True, text=True, encoding="gbk", errors="replace",
            timeout=5)
        pids = _pids_from_lines(out.stdout or "", scripts)
        if pids:
            return pids
    except Exception:
        pass

    ps = ("Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
          "ForEach-Object { $_.CommandLine + ' ' + $_.ProcessId }")
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=15)
        return _pids_from_lines(out.stdout or "", scripts)
    except Exception:
        return []


def hud_pids():
    """
    找出正在运行的 HUD python 进程 PID。

    ⚠ wmic 在 Win11 24H2+ 已被移除、在部分沙箱里也会被策略拦截，
    一旦它不可用且没有兜底，"停止 HUD" 会静默失效（残留 HUD 关不掉）。
    所以先试 wmic，失败再走 PowerShell CIM。
    """
    return _scan_pids(HUD_SCRIPTS)


def daemon_pids():
    """找出正在运行的守护进程 PID（自动按载具切换模式）"""
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
        g_lay = QGroupBox("面板位置")
        f3 = QFormLayout(g_lay)
        f3.setLabelAlignment(Qt.AlignRight)

        CORNERS = ["TL", "TR", "BL", "BR", "OFF"]
        self.cmb_flight = QComboBox()
        self.cmb_flight.addItems(CORNERS)
        self.cmb_target = QComboBox()
        self.cmb_target.addItems(CORNERS)
        self.cmb_combat = QComboBox()
        self.cmb_combat.addItems(CORNERS)

        self.sp_dx = QSpinBox()
        self.sp_dx.setRange(-1200, 1200)
        self.sp_dx.setSuffix(" px")
        self.sp_dy = QSpinBox()
        self.sp_dy.setRange(-1200, 1200)
        self.sp_dy.setSuffix(" px")

        f3.addRow("飞行/爬升面板:", self.cmb_flight)
        f3.addRow("目标 TARGET:", self.cmb_target)
        f3.addRow("战斗 COMBAT:", self.cmb_combat)
        f3.addRow("TARGET 左右偏移:", self.sp_dx)
        f3.addRow("TARGET 上下偏移:", self.sp_dy)

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
        self.refresh()

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
            flags = subprocess.CREATE_NEW_CONSOLE if os.name == "nt" else 0
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
        # 再清理残留（比如别的途径启动的）
        for pid in hud_pids():
            try:
                subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                               capture_output=True, timeout=5)
            except Exception:
                pass
        if not silent:
            self.say("已停止 HUD")

    def start_auto(self):
        """启动守护进程：按载具自动选空战/陆战 HUD（GUI 唯一的启动方式）"""
        # ⚠ 必须做系统级检查：只看 self.auto_proc 的话，
        # 已经有一个守护在跑（比如命令行拉起的）时会再起一个，两个守护互相打架
        existing = daemon_pids()
        if existing:
            self.auto_proc = None
            self.say(f"自动模式已在运行 (PID {', '.join(map(str, existing))})")
            return
        if not os.path.exists(DAEMON):
            self.say(f"找不到脚本: {DAEMON}")
            return
        try:
            flags = subprocess.CREATE_NEW_CONSOLE if os.name == "nt" else 0
            self.auto_proc = subprocess.Popen(
                [PYTHON, "-u", DAEMON, "--watch"], cwd=os.path.dirname(DAEMON),
                env=self.build_env(), creationflags=flags)
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
        # 清掉可能由别的途径拉起的守护进程
        for pid in daemon_pids():
            try:
                subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                               capture_output=True, timeout=5)
            except Exception:
                pass
        # 顺带收掉守护拉起的 HUD：否则关了自动切换，屏幕上还留着一个 HUD，很莫名
        for pid in hud_pids():
            try:
                subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                               capture_output=True, timeout=5)
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
            flags = subprocess.CREATE_NEW_CONSOLE if os.name == "nt" else 0
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
        self.say("重启 HUD（自动识别载具）…")
        self.stop(silent=True)
        self.start_auto()

    # ---- 布局读写 ----
    def load_layout_ui(self):
        """从 hud_layout.json 读入当前值，填进窗口控件"""
        try:
            with open(LAYOUT_JSON, "r", encoding="utf-8") as f:
                cfg = json.load(f)
        except Exception:
            cfg = {}
        if not isinstance(cfg, dict):
            cfg = {}
        self.cmb_flight.setCurrentText(str(cfg.get("flight", "BL")).upper())
        self.cmb_target.setCurrentText(str(cfg.get("target", "BR")).upper())
        self.cmb_combat.setCurrentText(str(cfg.get("combat", "OFF")).upper())
        off = cfg.get("offset") or {}
        t = off.get("target", [0, 0]) if isinstance(off, dict) else [0, 0]
        self.sp_dx.setValue(int(t[0]) if len(t) > 0 else 0)
        self.sp_dy.setValue(int(t[1]) if len(t) > 1 else 0)
        ui = cfg.get("ui") or {}
        if isinstance(ui, dict):
            try:
                self.sp_scale.setValue(float(ui.get("scale", 1.0)))
            except Exception:
                self.sp_scale.setValue(1.0)
            f = ui.get("font", "Consolas")
            if isinstance(f, str) and f.strip():
                self.cmb_font.setCurrentText(f.strip())

    def save_layout(self):
        """把窗口里的设置写回 hud_layout.json"""
        try:
            with open(LAYOUT_JSON, "r", encoding="utf-8") as f:
                cfg = json.load(f)
        except Exception:
            cfg = {}
        if not isinstance(cfg, dict):
            cfg = {}

        cfg["flight"] = self.cmb_flight.currentText()
        cfg["target"] = self.cmb_target.currentText()
        cfg["combat"] = self.cmb_combat.currentText()
        cfg["offset"] = {"target": [self.sp_dx.value(), self.sp_dy.value()]}
        cfg["ui"] = {"scale": round(self.sp_scale.value(), 2),
                     "font": self.cmb_font.currentText().strip() or "Consolas"}

        try:
            with open(LAYOUT_JSON, "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
            self.say(f"已保存: flight={cfg['flight']} "
                     f"target={cfg['target']} "
                     f"偏移=({self.sp_dx.value()},{self.sp_dy.value()}) "
                     f"缩放={cfg['ui']['scale']}倍 字体={cfg['ui']['font']} "
                     f"— 点『应用并重启』生效")
        except Exception as e:
            self.say(f"保存失败: {e}")

    def open_layout(self):
        if not os.path.exists(LAYOUT_JSON):
            self.say("布局文件不存在")
            return
        try:
            os.startfile(LAYOUT_JSON)
            self.say("已打开 hud_layout.json（改完重启 HUD 生效）")
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

        pids = hud_pids()
        if pids:
            self.lb_hud.setText(f"HUD: 运行中 (PID {', '.join(map(str, pids))})")
            self.lb_hud.setStyleSheet(f"color: {GREEN.name()}")
        else:
            self.lb_hud.setText("HUD: 未运行")
            self.lb_hud.setStyleSheet(f"color: {RED.name()}")


def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    w = Launcher()
    w.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
