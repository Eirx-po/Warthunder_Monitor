# War Thunder HUD 工具集 / War Thunder HUD Toolkit

基于 PyQt5 的《战争雷霆》屏幕覆盖层工具集：空战 HUD、陆战 HUD、图形化启动器、动力曲线记录器。
数据来源是游戏客户端内置的本地 HTTP 服务 **`localhost:8111`**——纯 HTTP GET，不读内存、不注入、不拆包，无封号风险。

> ⚠ 游戏必须以 **窗口化 / 无边框窗口** 模式运行。全屏独占模式下透明窗口会被完全遮盖。
> ⚠ The game **must run in Windowed / Borderless mode**. In fullscreen-exclusive mode the transparent overlay is completely hidden.

A PyQt5-based screen-overlay toolkit for War Thunder: air-battle HUD, ground-battle HUD, a GUI launcher, and a power-curve recorder.
All data comes from the game client's built-in local HTTP service **`localhost:8111`** — plain HTTP GET, no memory reading, no injection, no datamining, no ban risk.

---

## 环境要求 / Requirements

| 项 / Item | 要求 / Requirement |
|---|---|
| Python | **必须**用 `C:\Users\Administrator\.workbuddy\binaries\python\versions\3.13.12\python.exe`（PyQt5 装在这里，换解释器会 `ImportError`）<br>**Must** be the managed 3.13.12 interpreter (PyQt5 lives in its site-packages; other interpreters fail with `ImportError`) |
| PyQt5 | 5.15.x。环境被重置后重装：`python -m pip install PyQt5 --no-cache-dir`<br>Reinstall if the environment gets reset: `python -m pip install PyQt5 --no-cache-dir` |
| 游戏 / Game | 运行中且 8111 端口可用；窗口化/无边框<br>Running with port 8111 alive; windowed / borderless |

---

## 快速开始 / Quick Start

推荐用法：**双击 `start_hud_gui.bat`，点「启动 HUD（自动识别载具）」**，之后不用管——
进战斗后守护进程按载具类型自动拉起空战或陆战 HUD，**游戏退出则自动收起**。
Recommended: **double-click `start_hud_gui.bat`, click "Start HUD (auto-detect)"** —
a daemon picks the air or ground HUD per vehicle and **clears it when the game exits**.

> GUI 只有「启动 / 停止」两个按钮，**不区分空战陆战**——手动选模式容易选错，
> 分类交给守护进程。The GUI has only Start/Stop; it does **not** ask you to pick a mode.

| 双击运行 / Double-click | 打开的是 / What it opens | 说明 / Notes |
|---|---|---|
| `start_hud_gui.bat` | **启动器窗口**（推荐）/ **Launcher GUI** (recommended) | 状态显示 + 启停按钮 + **自动切换** + 布局设置<br>Status + start/stop + **auto-switch** + layout settings |
| `start_wt_hud.bat` | 守护模式 / Daemon mode | 无窗口，按载具自动切空战/陆战 HUD（`--watch`）<br>Headless; auto-switches air/ground HUD by vehicle (`--watch`) |
| `start_power_curve.bat` | 动力曲线窗口 / Power-curve window | 独立工具，与 HUD 互不影响<br>Standalone tool; independent of the HUD |
| `wt_air_hud\start_air_hud.bat` | 仅空战 HUD / Air HUD only | 直接拉起空战覆盖层<br>Launches the air overlay directly |

命令行方式（等效于双击 bat）/ Command line (equivalent to the bat files):

```
C:\Users\Administrator\.workbuddy\binaries\python\versions\3.13.12\python.exe -u J:\Quant\wt_hud_launcher_gui.py
```

---

## 目录结构 / Project Layout

```
J:\Quant\
├── wt_air_hud\              空战 HUD（主项目）/ Air-battle HUD (main project)
│   ├── main.py              入口 / Entry point
│   ├── hud_overlay.py       覆盖层渲染（面板/箭头/警报）/ Overlay rendering (panels/arrows/alerts)
│   ├── data_fetcher.py      8111 数据抓取 + 目标聚类 + Ps 计算 / Fetching + target clustering + Ps
│   ├── hud_layout.json      布局配置（启动器可直接改）/ Layout config (editable via launcher)
│   └── start_air_hud.bat
├── wt_hud_v2.py             陆战 HUD / Ground-battle HUD
├── wt_hud_launcher.py       守护启动器（命令行）/ Daemon launcher (CLI)
├── wt_hud_launcher_gui.py   图形启动器 / GUI launcher
├── wt_common.py             共享模块：目标聚类去重 / 颜色判定 / 箭头布局
│                            Shared: target clustering / color classification / arrow layout
├── wt_foreground.py         前台检测（游戏进程 aces.exe）/ Foreground detection (aces.exe)
├── wt_power_curve.py        动力曲线记录器（独立工具）/ Power-curve recorder (standalone)
├── test_power_curve.py      动力曲线离线冒烟测试 / Offline smoke test for the curve tool
├── start_*.bat              各类启动脚本 / Launcher scripts
├── *.html                   8111 接口调研 / GitHub 项目调研报告
│                            8111 API research / GitHub project survey reports
└── daily_stock_analyzer\    另一个独立项目（A股分析，见其内部说明）
                             Separate project (A-share market analysis; see its own docs)
```

---

## 空战 HUD / Air-Battle HUD

### 当前面板布局（默认配置）/ Current panel layout (defaults)

| 位置 / Position | 面板 / Panel | 内容 / Contents |
|---|---|---|
| 左下 / Bottom-left | ENGINE / CLIMB | 推力/功率、爬升率 Vy、**Ps 比能量**（绿=攒能量 / 红=烧能量）<br>Thrust/power, climb rate Vy, **specific excess power Ps** (green = gaining, red = burning energy) |
| 右下（左移 300px）/ Bottom-right (shifted 300px left) | TARGET | 最近空中目标：距离 / 速度 / 接近率；**跳过机头 ±25°（12 点）内目标**<br>Nearest air target: range / speed / closure; **skips targets within ±25° of the nose (12 o'clock)** |
| 屏幕四边缘 / Screen edges | 方向箭头 / Direction arrows | 仅空中目标的方位指示<br>Bearing cues for air targets only |
| 右上 / 左上 / Top-right / Top-left | — | 留空给游戏自带小地图和界面<br>Left empty for the game's own minimap and UI |

威胁列表、地面目标、详细飞行数据默认全关——设计原则是**极简，不重复游戏内已有读数**。
Threat list, ground targets, and detailed flight data are all off by default — the design principle is **minimalism: never duplicate readouts the game already shows**.

### 布局配置 `wt_air_hud\hud_layout.json` / Layout config

```jsonc
{
  "flight": "BL",            // TL/TR/BL/BR/OFF
  "target": "BR",
  "combat": "OFF",           // 已并入 flight，别单独开（内容会重复）
                             // Merged into "flight"; enabling it duplicates content
  "engine": "OFF",
  "threat": "OFF",
  "offset": { "target": [-300, 0] },   // 像素微调：负 dx 左移、负 dy 上移
                                       // Pixel nudge: negative dx = left, negative dy = up
  "ui": { "scale": 0.8, "font": "Microsoft YaHei" }
}
```

改完重启 HUD 生效。**推荐直接在启动器 GUI 里改**，不用碰 json。
Changes take effect after a HUD restart. **Editing via the launcher GUI is recommended** — no need to touch the JSON.

### 常用环境变量开关 / Environment-variable switches

| 变量 / Variable | 作用 / Effect | 默认 / Default |
|---|---|---|
| `WT_HUD_GROUND` | 地面目标**采集**（AB=1，RB/SB=0）<br>Ground-target **collection** | 按模式 / by mode |
| `WT_HUD_GROUND_TARGETS` | 地面目标**显示**<br>Ground-target **display** | 0（地面单位太多会刷屏 / too many units, floods the screen） |
| `WT_HUD_NOSE_SKIP` | 12 点跳过逻辑<br>Skip 12-o'clock logic | 1。**用 C 键自由视角时设 0**（8111 拿不到视角朝向，跳过逻辑会误把你正看着的目标跳掉）<br>**Set 0 when using C free-look** (8111 exposes no camera heading; the skip logic would wrongly drop the target you're looking at) |
| `WT_HUD_FULL` | 详细版飞行面板 / Detailed flight panel | 0 |
| `WT_HUD_HIDE_ON_BLUR` | 失焦时隐藏 / Hide on focus loss | 0（默认常驻显示 / shown persistently） |
| `WT_HUD_STANDBY` | 非战斗状态显示提示框<br>Show standby notice out of battle | 0（默认整屏留空 / screen stays empty） |
| `WT_ARROW_DEBUG` | 忽略箭头距离阈值（调试）/ Ignore arrow range threshold (debug) | 0 |
| `WT_HUD_SCALE` / `WT_HUD_FONT` | 覆盖 json 里的缩放/字体<br>Override JSON scale/font | — |

优先级：环境变量 > json > 默认值。
Precedence: environment variables > JSON > built-in defaults.

---

## 动力曲线 / Power Curve `wt_power_curve.py`

独立窗口，**实测**记录推力/功率曲线（与 wtapc.org 拆包飞行模型算的**理论**曲线互补）。
A standalone window that records **measured** thrust/power curves (complementary to the **theoretical** curves wtapc.org computes from datamined flight models).

- 每 200ms 采样 `/state`：`thrust N, kgs`（喷气，多发送机求和）/ `power N, hp`（螺旋桨），配 `H, m`、`IAS`、`TAS`、油门
  Samples `/state` every 200 ms: `thrust N, kgs` (jets, summed across engines) / `power N, hp` (props), with `H, m`, `IAS`, `TAS`, throttle
- X 轴可切：高度 / IAS / TAS — X-axis toggle: altitude / IAS / TAS
- **橙点** = 满油门（≥99%）样本，**红点** = 当前状态
  **Orange dots** = full-throttle (≥99%) samples, **red dot** = current state
- 分箱取平均平滑成曲线；可导出 CSV（文件名里的载具名已做合法性清洗）
  Binned-averaged smoothing; CSV export (vehicle name is sanitized for the filename)
- 与 HUD 互不干扰：点"停止 HUD"不会关掉它，反之亦然
  Fully independent of the HUD: "Stop HUD" won't kill it, and vice versa

取样方法：进战斗后**满油门爬升**测最大推力曲线，或**平飞满油**测某高度定点值。
How to sample: **full-throttle climb** for the max-thrust curve, or **level flight at full throttle** for a fixed-altitude data point.

离线冒烟测试（不需要游戏）/ Offline smoke test (no game required):

```
python J:\Quant\test_power_curve.py
```

5 组检查：窗口 show 不崩、坐标 int、四种数据形态重绘、X 轴循环、CSV 导出。
Five checks: window show() stability, int coordinates, four data-shape repaints, X-axis cycling, CSV export.

---

## 8111 接口：已知能力与限制 / The 8111 API: capabilities & limits

**能拿到 / Available**: `/indicators`（71 字段，本机 / own vehicle, 71 fields）、`/state`（32 字段，本机 / own vehicle, 32 fields）、`/map_obj.json`（小地图目标 / map objects）、`/map_info.json`（地图边界 / map bounds）。

**拿不到 / Not available**（全部 404 或无字段 / all 404 or absent）: 目标高度 target altitude、雷达锁定数据 radar-lock data（`/radar.json` `/lockon.json` `/targets.json`）、自由视角朝向 free-look camera heading。因此 / Therefore:

- 目标速度只能靠**位置差分**估算 — Target speed is estimated by **position differencing** only
- "选中目标"用「机头 ±45° 锥角内最近目标」模拟 — "Selected target" is simulated as the nearest target inside a ±45° nose cone
- 12 点判定只能相对机头，不能相对视角 — 12-o'clock is computed relative to the nose, not the camera

**坐标换算（最容易踩的坑）/ Coordinate math (the biggest trap)**:
归一化 0~1 覆盖 `map_min`~`map_max` 整段，真实跨度 = `map_max − map_min`（空战 65536，陆战典型 4096）。**绝不能用 `map_max[0]`**，用错距离/速度差 16 倍。改了换算必须同步 `target_mgr.map_span`。
Normalized 0–1 spans the full `map_min`~`map_max` range; the real span is `map_max − map_min` (65536 in air battles, typically 4096 on the ground). **Never use `map_max[0]` alone** — getting this wrong skews range/speed by 16×. If you change the span, sync `target_mgr.map_span` too.

**同一目标会拆成多条记录 / One target, many records**:
一个防空阵地 = 3 炮 + 1 SPAA，相距 3~30m。必须按位置聚类去重，否则箭头重叠成"残影"。
One AA position = 3 guns + 1 SPAA within 3–30 m. Cluster by position and dedupe, or the edge arrows overlap into "ghosting".

---

## 自动区分空战 / 陆战 / Auto mode detection

守护进程每 1.5 秒读一次 `/indicators`，按 `type` 前缀和 `army` 字段判断载具，
连续 3 次一致才切换（去抖，避免载入过程中乱切）。
The daemon polls `/indicators` every 1.5 s and classifies by `type` prefix and `army`,
switching only after 3 consistent reads (debounce against the loading screen).

| 载具 / Vehicle | `type` 前缀 / prefix | 结果 / Result |
|---|---|---|
| 坦克 / Tank | `tankModels/`、`army=tank` | 陆战 HUD |
| 飞机 / Aircraft | `aircraftModels/`、`planeModels/` | 空战 HUD |
| 直升机 / Helicopter | `helicopterModels/` | 空战 HUD |
| 舰船 / Ship | `shipModels/` | 陆战 HUD（无专用海战 HUD，同样基于 `map_obj`） |
| 载入中 / Loading | `?` 或空 | **保持现状，不切换** |
| 机库 / Hangar | `valid=false` | 收起所有 HUD |

> ⚠ 旧逻辑是「不含 tank 就当空战」，会把舰船、直升机和载入中的 `?` 全误判成空战。
> 现在认不出类型一律返回 `unknown` 并保持当前 HUD。
> The old "anything not `tank` is air" rule misclassified ships, helicopters and a
> half-loaded `?` as air. Unrecognized types now yield `unknown` and keep the current HUD.

开启方式：启动器 GUI 里点「启动 HUD（自动识别载具）」，或命令行
`python wt_hud_launcher.py --watch`。
Enable via the launcher's "Start HUD (auto-detect)" button, or
`python wt_hud_launcher.py --watch`.

**游戏退出自动收起 / Auto-close when the game exits**
守护每轮还会检查 `aces.exe` 是否还在（Toolhelp32 快照，进程级、不管前台）。
一旦游戏进程消失，立刻收起全部 HUD，**包括不是它自己拉起的**（避免留下孤儿透明窗口）。
检测失败时按"游戏还在"处理——宁可不关，也不误关。
The daemon also checks whether `aces.exe` is still alive (Toolhelp32 snapshot, process-level).
If the game is gone it clears every HUD, **including ones it did not spawn**.
On detection failure it assumes the game is running — never close by mistake.

---

## 启动器 GUI / Launcher GUI

状态区（游戏检测 / 模式 / HUD 进程）+ 按钮（空战 / 陆战 / 停止 / 重启 / 动力曲线）+ 开关 + 布局设置（角落 / 偏移 / 缩放 / 字体）+ 日志。
Status area (game detection / mode / HUD process) + buttons (air / ground / stop / restart / power curve) + toggles + layout settings (corner / offset / scale / font) + log.

- 改动布局后点「保存布局设置」再「应用并重启」
  After layout edits: "Save layout settings", then "Apply & restart"
- 停止 HUD 走 wmic → PowerShell CIM 两级进程查找（Win11 24H2 移除了 wmic 也能工作）
  "Stop HUD" uses a two-tier wmic → PowerShell CIM process lookup (still works on Win11 24H2+ where wmic was removed)

---

## 已知坑（PyQt5 相关）/ Known pitfalls (PyQt5)

1. **坐标必须 int / Coordinates must be int**: `drawLine`/`drawText`/`drawEllipse` 不接受 float，混用会**无 traceback 直接崩进程**。所有坐标变换最后都要 `int()`。
   Floats crash the process **silently, with no traceback**. Always end coordinate transforms with `int()`.
2. **不要猴子补丁 `paintEvent` / Never monkey-patch `paintEvent`**: `widget.paintEvent = fn` 会崩，必须子类化 QWidget 重写。
   `widget.paintEvent = fn` crashes; subclass QWidget and override instead.
3. 透明层 / Layered windows: 不能调 `SetLayeredWindowAttributes`（与 Qt 冲突刷屏报错）；paintEvent 需 `CompositionMode_Clear` 显式清屏防残影。
   Don't call `SetLayeredWindowAttributes` (conflicts with Qt, error spam); paintEvent must explicitly clear with `CompositionMode_Clear` to prevent ghosting.
4. 敌人颜色有变体（`#f00C00` / `#fa0C00` / `#e10B00`），判定用 RGB 色系，别硬编码白名单。
   Enemy colors come in variants (`#f00C00` / `#fa0C00` / `#e10B00`); classify by RGB family, not a hardcoded whitelist.
5. 陆战角度是**数学角**（0=东，逆时针）；空战是**罗盘角**（0=北，顺时针）。
   Ground battles use **math angles** (0 = east, counter-clockwise); air battles use **compass bearings** (0 = north, clockwise).

---

## 测试 / Tests

| 命令 / Command | 覆盖 / Coverage |
|---|---|
| `python test_power_curve.py` | 动力曲线（offscreen，无需游戏），5 组检查<br>Power curve (offscreen, no game), 5 checks |
| `python test_arrows.py` | 边缘箭头布局 / Edge-arrow layout |
| `python test_hud_layout.py` | 面板布局（离屏渲染 + 像素统计）<br>Panel layout (offscreen render + pixel stats) |

> `test_arrows.py` / `test_hud_layout.py` 已随 2026-09-21 清理移入 `_trash_20260921\`，需要时拖回即可。
> `test_arrows.py` / `test_hud_layout.py` were moved into `_trash_20260921\` during the 2026-09-21 cleanup; drag them back if needed.
