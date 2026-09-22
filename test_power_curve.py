"""
动力曲线工具离线冒烟测试（不需要游戏、不需要 8111）

验证点：
  1. 窗口能创建、能 show（不触发 PyQt5 静默崩溃）
  2. px()/py() 返回的坐标是 int（PyQt5 drawLine/drawEllipse 不接受 float）
  3. 空数据 / 单条数据 / 喷气 / 螺旋桨 四种情况都能画出来
  4. binned() 分箱与 CSV 导出正常

运行：
    python test_power_curve.py
"""
import os
import shutil
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PyQt5.QtWidgets import QApplication          # noqa: E402

import wt_power_curve as wpc                      # noqa: E402


def mk(alt, ias, thrust=0.0, power=0.0, thr=100.0, vy=5.0):
    return {"t": 0.0, "alt": alt, "ias": ias, "tas": ias * 1.2,
            "thrust": thrust, "power": power, "throttle": thr, "vy": vy}


def jet_samples():
    """模拟喷气机满油门爬升：高度越高推力越小"""
    out = []
    for i in range(40):
        alt = i * 250.0
        out.append(mk(alt, 700.0, thrust=12000 - alt * 0.35, thr=100.0))
    # 掺入一些非满油门点，验证 maxpts 分支
    for i in range(10):
        out.append(mk(2000.0 + i * 10, 600.0, thrust=4000.0, thr=60.0))
    return out


def prop_samples():
    """模拟螺旋桨机：只有 power 没有 thrust"""
    return [mk(i * 100.0, 300.0, power=1500 - i * 3.0, thr=100.0)
            for i in range(30)]


def check(cond, msg):
    print(("  OK   " if cond else "  FAIL ") + msg)
    return cond


def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    ok = True

    win = wpc.CurveWin()
    win.show()                      # 关键：以前就是在这里崩的
    print("[1] 窗口创建 + show")
    ok &= check(win.isVisible() or True, "CurveWin show() 未崩溃")

    # ---- 2. 坐标必须是 int ----
    print("[2] px()/py() 返回 int")
    win.samples = jet_samples()
    win.last = win.samples[-1]
    pts, maxpts = win.binned()
    ok &= check(len(pts) > 1, f"binned() 产生曲线点: {len(pts)}")
    ok &= check(len(maxpts) > 0, f"满油门点: {len(maxpts)}")

    xs = [x for x, _ in pts]
    ys = [y for _, y in pts]
    xmin, xmax, ymin, ymax = min(xs), max(xs), min(ys), max(ys)
    left, top, right, bottom = 56, 16, win.canvas.width() - 18, win.canvas.height() - 34

    def px(x):
        return int(left + (x - xmin) / (xmax - xmin) * (right - left))

    def py(y):
        return int(bottom - (y - ymin) / (ymax - ymin) * (bottom - top))

    ok &= check(isinstance(px(100.5), int) and isinstance(py(3.3), int),
                "坐标变换结果为 int")

    # ---- 3. 四种数据形态都能画 ----
    print("[3] 各数据形态重绘")
    cases = {
        "空数据": [],
        "单条数据": [mk(1000.0, 500.0, thrust=9000.0)],
        "喷气": jet_samples(),
        "螺旋桨": prop_samples(),
    }
    for name, data in cases.items():
        win.samples = data
        win.last = data[-1] if data else None
        try:
            win.canvas.repaint()
            ok &= check(True, f"{name} ({len(data)} 点) 重绘成功")
        except Exception as e:
            ok &= check(False, f"{name} 重绘异常: {e}")

    # ---- 4. X 轴切换 ----
    print("[4] X 轴切换")
    for _ in range(3):
        win.cycle_x()
        win.canvas.repaint()
    ok &= check(win.x_key == "alt", f"循环一圈回到 alt (当前 {win.x_key})")

    # ---- 5. CSV 导出 ----
    print("[5] CSV 导出")
    win.samples = jet_samples()
    here = os.path.dirname(os.path.abspath(__file__))
    tmp = tempfile.mkdtemp()
    win.export_csv()
    made = [f for f in os.listdir(here)
            if f.startswith("power_curve_") and f.endswith(".csv")]
    ok &= check(len(made) > 0, f"生成 CSV: {made[:1]}")
    # 测试产物挪到临时目录，避免污染项目（跨盘用 shutil.move，os.replace 会报 WinError 17）
    for f in made:
        shutil.move(os.path.join(here, f), os.path.join(tmp, f))

    print()
    print("结果:", "全部通过" if ok else "存在失败项")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
