"""
动力曲线记录器（独立工具，不影响 HUD）

参考：
  - WTRTI 的 plot 窗口：X=IAS、Y=specific excess power，用来找最佳爬升速度
    （本工具的 Ps 曲线即同一思路）
  - wtapc.org：从飞行模型解包算**理论**推力曲线
  - 本工具做的是**实测**曲线：实时采样 8111 的 thrust/power，
    配合当时的高度与速度，画出你这架飞机真实飞出来的动力曲线。

数据来源（全部来自 /state）：
    thrust N, kgs   喷气发动机推力（多发送加）
    power  N, hp    螺旋桨轴马力
    TAS / IAS, km/h 速度
    H, m            高度
    throttle N, %   油门（用来区分最大油门点）

用法：
    python wt_power_curve.py

操作：
    进战斗后正常飞（想要最大推力曲线就保持满油门爬升），
    窗口会自动打点。可切换 X 轴（高度/速度）、导出 CSV。
"""
import csv
import json
import os
import sys
import time
import urllib.request
from collections import defaultdict

from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtGui import QColor, QFont, QPainter, QPen
from PyQt5.QtWidgets import (QApplication, QHBoxLayout, QLabel, QMainWindow,
                             QPushButton, QVBoxLayout, QWidget)

BG = QColor(16, 20, 28)
GRID = QColor(60, 70, 88)
AXIS = QColor(120, 132, 150)
TXT = QColor(215, 222, 235)
MUT = QColor(140, 150, 165)
CURVE = QColor(90, 200, 230)
MAXPT = QColor(255, 170, 60)
NOW = QColor(255, 90, 90)


def fetch_state():
    try:
        req = urllib.request.Request("http://localhost:8111/state",
                                     headers={"User-Agent": "WT-PowerCurve"})
        with urllib.request.urlopen(req, timeout=1.5) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except Exception:
        return None


def fetch_ind():
    try:
        req = urllib.request.Request("http://localhost:8111/indicators",
                                     headers={"User-Agent": "WT-PowerCurve"})
        with urllib.request.urlopen(req, timeout=1.5) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except Exception:
        return None


def safe_name(s, fallback="unknown"):
    """
    把载具名清洗成合法文件名。

    ⚠ 踩过的坑：未进入战斗时 /indicators 的 type 是 "?"，直接拼进文件名
    会得到 `power_curve_?_123.csv`，Windows 上 ? 是保留字符，
    open() 抛 [Errno 22] Invalid argument，被 except 吞掉表现为"导出没反应"。
    载具名里还可能带 / : * 等，一并替换。
    """
    bad = '<>:"/\\|?*'
    out = "".join("_" if c in bad or ord(c) < 32 else c for c in str(s or ""))
    out = out.replace(" ", "_").strip("._")
    return out or fallback


class CurveCanvas(QWidget):
    """
    曲线画布。

    注意：不要用 `widget.paintEvent = fn` 猴子补丁 —— Qt 对 paintEvent 的
    调用约定很敏感，补丁方式容易在 show() 时直接崩进程（无 traceback）。
    正确做法是子类化并重写。
    """

    def __init__(self, owner):
        super().__init__()
        self.owner = owner
        self.setMinimumHeight(400)

    def paintEvent(self, e):
        self.owner.draw_curve(e)


class CurveWin(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("动力曲线")
        self.resize(720, 520)
        self.samples = []
        self.x_key = "alt"          # 'alt' | 'ias' | 'tas'
        self.vehicle = "?"
        self.last = None

        root = QWidget()
        self.setCentralWidget(root)
        v = QVBoxLayout(root)
        v.setContentsMargins(10, 10, 10, 10)
        v.setSpacing(8)

        self.lb = QLabel("等待进入战斗…")
        self.lb.setFont(QFont("Consolas", 10))
        v.addWidget(self.lb)

        self.canvas = CurveCanvas(self)
        v.addWidget(self.canvas, 1)

        h = QHBoxLayout()
        self.bt_x = QPushButton("X轴: 高度")
        self.bt_clear = QPushButton("清除")
        self.bt_csv = QPushButton("导出 CSV")
        for b in (self.bt_x, self.bt_clear, self.bt_csv):
            b.setMinimumHeight(28)
            h.addWidget(b)
        v.addLayout(h)

        self.bt_x.clicked.connect(self.cycle_x)
        self.bt_clear.clicked.connect(self.clear)
        self.bt_csv.clicked.connect(self.export_csv)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(200)

    # ---- 采样 ----
    def tick(self):
        ind = fetch_ind()
        if not ind or not ind.get("valid", False):
            self.lb.setText("未进入战斗（等待 8111 有效数据）")
            return
        self.vehicle = ind.get("type", "?")

        s = fetch_state()
        if not s or not s.get("valid", False):
            return

        thrust = 0.0
        power = 0.0
        thr = 0
        for i in range(1, 9):
            t = s.get(f"thrust {i}, kgs")
            if t is not None:
                thrust += float(t)
            p = s.get(f"power {i}, hp")
            if p is not None:
                power += float(p)
            v = s.get(f"throttle {i}, %")
            if v is not None:
                thr = max(thr, float(v))

        rec = {
            "t": time.time(),
            "alt": float(s.get("H, m", 0)),
            "ias": float(s.get("IAS, km/h", 0)),
            "tas": float(s.get("TAS, km/h", 0)),
            "thrust": thrust,
            "power": power,
            "throttle": thr,
            "vy": float(s.get("Vy, m/s", 0)),
        }
        self.last = rec
        self.samples.append(rec)
        if len(self.samples) > 20000:
            self.samples = self.samples[-20000:]

        unit = "kgf" if thrust > 0 else "hp"
        val = thrust if thrust > 0 else power
        self.lb.setText(
            f"{self.vehicle}   采样 {len(self.samples)} 点   "
            f"当前: {val:.0f} {unit} @ {rec['alt']:.0f}m / {rec['ias']:.0f}km/h   "
            f"油门 {thr:.0f}%")
        self.canvas.update()

    def cycle_x(self):
        order = ["alt", "ias", "tas"]
        i = order.index(self.x_key)
        self.x_key = order[(i + 1) % len(order)]
        names = {"alt": "高度", "ias": "IAS", "tas": "TAS"}
        self.bt_x.setText(f"X轴: {names[self.x_key]}")
        self.canvas.update()

    def clear(self):
        self.samples = []
        self.canvas.update()

    def export_csv(self):
        if not self.samples:
            return
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            f"power_curve_{safe_name(self.vehicle)}"
                            f"_{int(time.time())}.csv")
        try:
            with open(path, "w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=list(self.samples[0].keys()))
                w.writeheader()
                for r in self.samples:
                    w.writerow(r)
            self.lb.setText(f"已导出 {len(self.samples)} 点 -> {os.path.basename(path)}")
        except Exception as e:
            self.lb.setText(f"导出失败: {e}")

    # ---- 绘图 ----
    def binned(self):
        """按 X 值分箱取平均，得到平滑曲线；同时统计最大油门点"""
        if not self.samples:
            return [], []
        xs = [s[self.x_key] for s in self.samples]
        xmin, xmax = min(xs), max(xs)
        if xmax - xmin < 1e-6:
            return [], []
        nbin = 60
        width = (xmax - xmin) / nbin

        def val(s):
            return s["thrust"] if any(r["thrust"] > 0 for r in self.samples[:50]) \
                else s["power"]

        acc = defaultdict(list)
        for s in self.samples:
            b = int((s[self.x_key] - xmin) / width)
            b = min(b, nbin - 1)
            acc[b].append(val(s))

        pts, maxpts = [], []
        for b in sorted(acc):
            cx = xmin + (b + 0.5) * width
            cy = sum(acc[b]) / len(acc[b])
            pts.append((cx, cy))
        # 满油门(>=99%)的样本单独标出
        for s in self.samples:
            if s["throttle"] >= 99:
                maxpts.append((s[self.x_key], val(s)))
        return pts, maxpts

    def draw_curve(self, _e):
        w = self.canvas
        p = QPainter(w)
        p.setRenderHint(QPainter.Antialiasing)
        p.fillRect(w.rect(), BG)

        left, top = 56, 16
        right, bottom = w.width() - 18, w.height() - 34
        if right <= left or bottom <= top:
            return

        jet = any(s["thrust"] > 0 for s in self.samples[-50:]) if self.samples else True
        unit = "kgf" if jet else "hp"
        xname = {"alt": "高度 (m)", "ias": "IAS (km/h)", "tas": "TAS (km/h)"}[self.x_key]

        pts, maxpts = self.binned()

        # 值域
        if pts:
            ys = [y for _, y in pts]
            ymin, ymax = min(ys), max(ys)
            if ymax - ymin < 1e-6:
                ymax = ymin + 1
            pad = (ymax - ymin) * 0.12
            ymin, ymax = max(0, ymin - pad), ymax + pad
            xs = [x for x, _ in pts]
            xmin, xmax = min(xs), max(xs)
            if xmax - xmin < 1e-6:
                xmax = xmin + 1
        else:
            xmin, xmax, ymin, ymax = 0, 10000, 0, 8000

        # ⚠ 必须转 int：PyQt5 的 drawLine/drawText 整数重载不接受 float，
        # 混用会在 show() 时直接崩进程（连 traceback 都没有）。
        def px(x):
            return int(left + (x - xmin) / (xmax - xmin) * (right - left))

        def py(y):
            return int(bottom - (y - ymin) / (ymax - ymin) * (bottom - top))

        # 网格 + 刻度
        p.setFont(QFont("Consolas", 8))
        p.setPen(QPen(GRID, 1))
        for i in range(5):
            yv = ymin + (ymax - ymin) * i / 4
            yy = py(yv)
            p.drawLine(left, yy, right, yy)
            p.setPen(MUT)
            p.drawText(2, yy + 4, f"{yv:.0f}")
            p.setPen(QPen(GRID, 1))
        for i in range(5):
            xv = xmin + (xmax - xmin) * i / 4
            xx = px(xv)
            p.drawLine(xx, top, xx, bottom)
            p.setPen(MUT)
            p.drawText(xx - 16, bottom + 16, f"{xv:.0f}")
            p.setPen(QPen(GRID, 1))

        p.setPen(QPen(AXIS, 1))
        p.drawLine(left, top, left, bottom)
        p.drawLine(left, bottom, right, bottom)

        p.setPen(TXT)
        p.setFont(QFont("Consolas", 9))
        p.drawText(left + 4, top + 12, f"推力/功率 ({unit})")
        p.drawText(right - 90, bottom + 16, xname)

        # 曲线
        if len(pts) >= 2:
            p.setPen(QPen(CURVE, 2))
            for i in range(len(pts) - 1):
                p.drawLine(px(pts[i][0]), py(pts[i][1]),
                           px(pts[i + 1][0]), py(pts[i + 1][1]))

        if maxpts:
            p.setPen(QPen(MAXPT, 1))
            for x, y in maxpts[::3]:
                p.drawEllipse(px(x) - 1, py(y) - 1, 3, 3)

        # 当前点
        if self.last is not None:
            v = self.last["thrust"] if jet else self.last["power"]
            p.setPen(QPen(NOW, 6))
            p.drawEllipse(px(self.last[self.x_key]) - 3, py(v) - 3, 6, 6)

        if not pts:
            p.setPen(MUT)
            p.setFont(QFont("Consolas", 10))
            p.drawText(left + 20, (top + bottom) // 2, "暂无数据 — 进入战斗后开始采样")
        p.end()


def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    w = CurveWin()
    w.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
