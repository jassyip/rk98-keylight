#!/usr/bin/env python3
"""wb-keylight 托盘启动器 — 带设置界面

用法:
  pythonw tray.py          # 无窗口常驻(开机自启用这个)
  python tray.py           # 带控制台, 调试用

行为:
  - 启动即常驻: 托盘图标随状态变色(运行中=彩色/待确认=红/完成=绿/平常=灰)
  - 左键或菜单"设置…"打开设置界面: 颜色/效果/速度/亮度/自启, 保存即热生效
  - 退出时停止一切灯效, 键盘自动交还自带灯效(点击扩散)
"""
import json
import logging
import os
import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import colorchooser, messagebox, ttk

import pystray
from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import keylight as KL  # noqa: E402

CONFIG_PATH = os.path.join(ROOT, "config.json")
PID_PATH = os.path.join(ROOT, "tray.pid")
FIRST_RUN_FLAG = os.path.join(ROOT, ".settings-shown")
STARTUP_DIR = os.path.join(
    os.environ.get("APPDATA", os.path.expanduser("~")),
    "Microsoft", "Windows", "Start Menu", "Programs", "Startup")
STARTUP_CMD = os.path.join(STARTUP_DIR, "wb-keylight.cmd")
PYTHONW = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
TRAY_SCRIPT = os.path.abspath(__file__)

MODE_LABELS = {"rainbow": "彩色流水", "flow": "单色流水", "breathe": "呼吸"}
LABEL_MODES = {v: k for k, v in MODE_LABELS.items()}

STATE_TEXT = {
    "IDLE": "平常 · 键盘自带灯效",
    "RUNNING": "运行中 · 彩色流水",
    "WAITING": "待确认 · 红灯流水",
    "DONE": "完成 · 绿灯闪烁",
    "FAIL": "失败 · 红闪(即将释放)",
}
STATE_ICON = {"IDLE": (110, 110, 110), "WAITING": (235, 30, 30),
              "DONE": (0, 200, 70), "FAIL": (255, 140, 0)}


def autostart_enabled():
    return os.path.exists(STARTUP_CMD)


def write_autostart(enable):
    """开/关开机自启(写入用户启动文件夹的 wb-keylight.cmd)"""
    if enable:
        os.makedirs(STARTUP_DIR, exist_ok=True)
        with open(STARTUP_CMD, "w", encoding="utf-8", newline="") as f:
            f.write("@echo off\r\n"
                    "rem wb-keylight autostart (tray)\r\n"
                    'start "" /min "%s" "%s"\r\n' % (PYTHONW, TRAY_SCRIPT))
        log.info("已开启开机自启: %s", STARTUP_CMD)
    elif os.path.exists(STARTUP_CMD):
        os.remove(STARTUP_CMD)
        log.info("已关闭开机自启")


def setup_logging():
    log = logging.getLogger("keylight")
    log.setLevel(logging.INFO)
    fh = logging.FileHandler(os.path.join(ROOT, "keylight.log"), encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(fh)
    return log


log = setup_logging()


# ---------------------------------------------------------------- 托盘图标
def _disc(rgba, size=64):
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    ImageDraw.Draw(img).ellipse([3, 3, size - 3, size - 3], fill=rgba + (255,))
    return img


def _rainbow_disc(size=64):
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    wedges = [(255, 60, 60), (255, 170, 40), (250, 230, 60),
              (60, 210, 90), (60, 160, 255), (170, 90, 255)]
    for i, c in enumerate(wedges):
        d.pieslice([3, 3, size - 3, size - 3], start=i * 60, end=(i + 1) * 60,
                   fill=c + (255,))
    d.ellipse([22, 22, size - 22, size - 22], fill=(20, 20, 20, 255))
    return img


def icon_for(state):
    if state == "RUNNING":
        return _rainbow_disc()
    return _disc(STATE_ICON.get(state, (110, 110, 110)))


# ---------------------------------------------------------------- 应用
class App:
    def __init__(self, root):
        self.root = root
        self.ui_q = queue.Queue()
        self.cfg = KL.load_config(CONFIG_PATH)
        try:
            self.svc = KL.start_service(self.cfg)
        except KL.ServiceAlreadyRunning:
            messagebox.showinfo("wb-keylight", "已经在运行了 — 直接看托盘图标即可。")
            sys.exit(0)
        except OSError as e:
            messagebox.showerror("wb-keylight", f"服务启动失败:\n{e}")
            sys.exit(2)
        self.settings_win = None
        self.last_state = None
        self.tray = None
        self.svc.on_ui_request = lambda: self.ui_q.put(("show",))

        with open(PID_PATH, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))

        self._build_tray()
        threading.Thread(target=self._poller, daemon=True).start()
        self.root.after(150, self._poll_ui)

        if not os.path.exists(FIRST_RUN_FLAG):
            with open(FIRST_RUN_FLAG, "w", encoding="utf-8") as f:
                f.write(time.strftime("%Y-%m-%d %H:%M:%S"))
            self.root.after(600, self.show_settings)

    # ---------------- 托盘 ----------------
    def _build_tray(self):
        menu = pystray.Menu(
            pystray.MenuItem("设置…", self._q_show, default=True),
            pystray.MenuItem("暂停灯效", self._toggle_pause,
                             checked=lambda item: self.svc.paused),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("测试: 运行中", lambda: self.ui_q.put(("test", "RUNNING"))),
            pystray.MenuItem("测试: 待确认", lambda: self.ui_q.put(("test", "WAITING"))),
            pystray.MenuItem("测试: 完成", lambda: self.ui_q.put(("test", "DONE"))),
            pystray.MenuItem("演示: 分区(3 任务)", lambda: self.ui_q.put(("demo",))),
            pystray.MenuItem("演示: 溢出(10 任务)", lambda: self.ui_q.put(("demo_overflow",))),
            pystray.MenuItem("恢复硬件灯效", lambda: self.ui_q.put(("restore",))),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("退出", lambda: self.ui_q.put(("quit",))),
        )
        self.tray = pystray.Icon("wb-keylight", icon_for(self.svc.state),
                                 "wb-keylight", menu)
        threading.Thread(target=self._run_tray, daemon=True).start()
        try:
            self.tray.notify("wb-keylight 已启动 — 左键托盘图标可设置",
                             "运行中=彩色流水 / 待确认=红灯流水 / 完成=绿灯闪")
        except Exception:
            pass

    def _run_tray(self):
        try:
            self.tray.run()
        except Exception as e:
            log.warning("托盘线程退出: %s", e)

    def _q_show(self, *_a):
        self.ui_q.put(("show",))

    def _toggle_pause(self, *_a):
        self.svc.paused = not self.svc.paused
        log.info("灯效暂停: %s", self.svc.paused)

    def _poller(self):
        """刷新托盘图标/提示 + 设置窗状态栏"""
        while True:
            try:
                st = self.svc.state
                if st != self.last_state:
                    self.last_state = st
                    try:
                        self.tray.icon = icon_for(st)
                    except Exception:
                        pass
                    self.ui_q.put(("status",))
                try:
                    self.tray.title = "wb-keylight · " + STATE_TEXT.get(st, st)
                except Exception:
                    pass
                self.ui_q.put(("status",))
            except Exception:
                pass
            time.sleep(0.5)

    # ---------------- UI 队列(全部在主线程执行) ----------------
    def _poll_ui(self):
        while True:
            try:
                cmd = self.ui_q.get_nowait()
            except queue.Empty:
                break
            try:
                self._handle_ui(cmd)
            except Exception as e:
                log.warning("UI 命令失败 %s: %s", cmd, e)
        self.root.after(150, self._poll_ui)

    def _handle_ui(self, cmd):
        kind = cmd[0]
        if kind == "show":
            self.show_settings()
        elif kind == "status":
            if self.settings_win is not None:
                self.settings_win.refresh_status()
        elif kind == "test":
            self.svc.paused = False
            self.svc.force_state(cmd[1])
        elif kind == "demo":
            self.svc.inject_demo(overflow=False, secs=30)
        elif kind == "demo_overflow":
            self.svc.inject_demo(overflow=True, secs=30)
        elif kind == "restore":
            self.svc.clear_sessions()
        elif kind == "quit":
            self.quit()

    # ---------------- 设置窗口 ----------------
    def show_settings(self):
        if self.settings_win is not None and self.settings_win.win.winfo_exists():
            self.settings_win.win.lift()
            self.settings_win.win.focus_force()
            return
        self.settings_win = SettingsWindow(self)
        log.info("设置窗口已打开")

    # ---------------- 退出 ----------------
    def quit(self):
        try:
            self.svc.stop_event.set()
            time.sleep(0.25)
            self.svc.kb.close()
        except Exception:
            pass
        try:
            if os.path.exists(PID_PATH):
                os.remove(PID_PATH)
        except OSError:
            pass
        try:
            self.tray.stop()
        except Exception:
            pass
        self.root.after(50, self.root.destroy)


# ---------------------------------------------------------------- 设置窗
class SettingsWindow:
    def __init__(self, app):
        self.app = app
        self.win = tk.Toplevel(app.root)
        self.win.title("wb-keylight 设置 — WorkBuddy × RK98")
        self.win.resizable(False, False)
        self.win.protocol("WM_DELETE_WINDOW", self._hide)
        cfg = app.svc.cfg

        pad = {"padx": 8, "pady": 4}
        row = 0

        # ---- 运行中 ----
        f_run = ttk.LabelFrame(self.win, text="运行中（WorkBuddy 干活时）")
        f_run.grid(row=row, column=0, columnspan=3, sticky="we", **pad)
        row += 1
        ttk.Label(f_run, text="效果:").grid(row=0, column=0, sticky="e", **pad)
        self.v_run_mode = tk.StringVar(value=MODE_LABELS.get(cfg["running"].get("mode", "rainbow"), "彩色流水"))
        ttk.Combobox(f_run, textvariable=self.v_run_mode, state="readonly", width=10,
                     values=list(MODE_LABELS.values())).grid(row=0, column=1, sticky="w", **pad)
        ttk.Label(f_run, text="颜色(单色流水/呼吸时用):").grid(row=0, column=2, sticky="e", **pad)
        self.v_run_color = tk.StringVar(value=cfg["running"].get("color", "#FF0000"))
        ttk.Entry(f_run, textvariable=self.v_run_color, width=9).grid(row=0, column=3, **pad)
        ttk.Button(f_run, text="…", width=3,
                   command=lambda: self._pick(self.v_run_color)).grid(row=0, column=4, **pad)
        self._slider(f_run, 1, "速度", cfg["running"].get("speed", 40), 5, 120, "v_run_speed")
        self._slider(f_run, 2, "帧率", cfg["running"].get("fps", 30), 10, 60, "v_run_fps")
        self._slider(f_run, 3, "亮度", cfg["running"].get("brightness", 0.8), 0.1, 1.0, "v_run_bri", res=0.05)

        # ---- 待确认 ----
        f_wait = ttk.LabelFrame(self.win, text="待确认（需要你拍板时 · 红灯流水）")
        f_wait.grid(row=row, column=0, columnspan=3, sticky="we", **pad)
        row += 1
        ttk.Label(f_wait, text="颜色:").grid(row=0, column=0, sticky="e", **pad)
        self.v_wait_color = tk.StringVar(value=cfg["waiting"].get("color", "#FF0000"))
        ttk.Entry(f_wait, textvariable=self.v_wait_color, width=9).grid(row=0, column=1, **pad)
        ttk.Button(f_wait, text="…", width=3,
                   command=lambda: self._pick(self.v_wait_color)).grid(row=0, column=2, **pad)
        self._slider(f_wait, 1, "速度", cfg["waiting"].get("speed", 40), 5, 120, "v_wait_speed")
        self._slider(f_wait, 2, "帧率", cfg["waiting"].get("fps", 30), 10, 60, "v_wait_fps")

        # ---- 完成 ----
        f_done = ttk.LabelFrame(self.win, text="完成（一轮回答结束时 · 绿灯闪烁）")
        f_done.grid(row=row, column=0, columnspan=3, sticky="we", **pad)
        row += 1
        ttk.Label(f_done, text="颜色:").grid(row=0, column=0, sticky="e", **pad)
        self.v_done_color = tk.StringVar(value=cfg["done"].get("color", "#00FF00"))
        ttk.Entry(f_done, textvariable=self.v_done_color, width=9).grid(row=0, column=1, **pad)
        ttk.Button(f_done, text="…", width=3,
                   command=lambda: self._pick(self.v_done_color)).grid(row=0, column=2, **pad)
        ttk.Label(f_done, text="闪烁次数:").grid(row=0, column=3, sticky="e", **pad)
        self.v_done_times = tk.IntVar(value=int(cfg["done"].get("times", 3)))
        ttk.Spinbox(f_done, from_=1, to=10, width=4,
                    textvariable=self.v_done_times).grid(row=0, column=4, **pad)
        ttk.Label(f_done, text="亮(ms):").grid(row=0, column=5, sticky="e", **pad)
        self.v_done_on = tk.IntVar(value=int(cfg["done"].get("on_ms", 200)))
        ttk.Entry(f_done, textvariable=self.v_done_on, width=5).grid(row=0, column=6, **pad)
        ttk.Label(f_done, text="灭(ms):").grid(row=0, column=7, sticky="e", **pad)
        self.v_done_off = tk.IntVar(value=int(cfg["done"].get("off_ms", 120)))
        ttk.Entry(f_done, textvariable=self.v_done_off, width=5).grid(row=0, column=8, **pad)

        # ---- 分区模式(M4: 每排一任务) ----
        f_part = ttk.LabelFrame(self.win, text="分区模式（每排一个任务 · numpad 溢出区）")
        f_part.grid(row=row, column=0, columnspan=3, sticky="we", **pad)
        row += 1
        self.v_part_on = tk.BooleanVar(
            value=bool(cfg.get("partition", {}).get("enabled", True)))
        ttk.Checkbutton(f_part, text="启用分区模式(每排一任务; 关闭则回到全局单状态灯效)",
                        variable=self.v_part_on).grid(
            row=0, column=0, columnspan=6, sticky="w", **pad)
        ttk.Label(f_part, text="WorkBuddy 运行色:").grid(row=1, column=0, sticky="e", **pad)
        self.v_src_wb = tk.StringVar(
            value=cfg.get("sources", {}).get("workbuddy", {}).get("color", "#00E676"))
        ttk.Entry(f_part, textvariable=self.v_src_wb, width=9).grid(row=1, column=1, **pad)
        ttk.Button(f_part, text="…", width=3,
                   command=lambda: self._pick(self.v_src_wb)).grid(row=1, column=2, **pad)
        ttk.Label(f_part, text="Codex 运行色:").grid(row=1, column=3, sticky="e", **pad)
        self.v_src_cx = tk.StringVar(
            value=cfg.get("sources", {}).get("codex", {}).get("color", "#2979FF"))
        ttk.Entry(f_part, textvariable=self.v_src_cx, width=9).grid(row=1, column=4, **pad)
        ttk.Button(f_part, text="…", width=3,
                   command=lambda: self._pick(self.v_src_cx)).grid(row=1, column=5, **pad)
        ttk.Label(f_part, text="失败闪烁(秒):").grid(row=2, column=0, sticky="e", **pad)
        self.v_fail_secs = tk.IntVar(
            value=int(cfg.get("partition", {}).get("fail_flash_secs", 30)))
        ttk.Spinbox(f_part, from_=5, to=120, width=5,
                    textvariable=self.v_fail_secs).grid(row=2, column=1, sticky="w", **pad)
        ttk.Label(f_part, text="等待 亮/灭(ms):").grid(row=2, column=3, sticky="e", **pad)
        self.v_wait_on = tk.IntVar(
            value=int(cfg.get("partition", {}).get("waiting_on_ms", 150)))
        self.v_wait_off = tk.IntVar(
            value=int(cfg.get("partition", {}).get("waiting_off_ms", 100)))
        ttk.Entry(f_part, textvariable=self.v_wait_on, width=5).grid(row=2, column=4, **pad)
        ttk.Entry(f_part, textvariable=self.v_wait_off, width=5).grid(row=2, column=5, **pad)
        ttk.Button(f_part, text="演示分区(3 任务)", command=self._demo).grid(
            row=3, column=0, columnspan=2, sticky="w", **pad)
        ttk.Button(f_part, text="演示溢出(10 任务)", command=self._demo_overflow).grid(
            row=3, column=2, columnspan=2, sticky="w", **pad)
        ttk.Label(f_part, text="演示任务 30 秒后自动绿闪收尾并停流; 键盘上看不到的文字身份见下方任务映射表",
                  foreground="#8a6d3b").grid(row=4, column=0, columnspan=6, sticky="w", **pad)

        # ---- 服务 ----
        f_svc = ttk.LabelFrame(self.win, text="服务")
        f_svc.grid(row=row, column=0, columnspan=3, sticky="we", **pad)
        row += 1
        ttk.Label(f_svc, text="僵死会话清理(秒):").grid(row=0, column=0, sticky="e", **pad)
        self.v_stale = tk.IntVar(value=int(cfg["service"].get("stale_session_secs", 600)))
        ttk.Spinbox(f_svc, from_=60, to=3600, increment=60, width=6,
                    textvariable=self.v_stale).grid(row=0, column=1, sticky="w", **pad)
        self.v_close_rk = tk.BooleanVar(value=bool(cfg["conflict"].get("close_rk_app", False)))
        ttk.Checkbutton(f_svc, text="RK 官方软件抢占 HID 时自动让它退出",
                        variable=self.v_close_rk).grid(row=0, column=2, columnspan=3, sticky="w", **pad)
        self.v_autostart = tk.BooleanVar(value=os.path.exists(STARTUP_CMD))
        ttk.Checkbutton(f_svc, text="开机自动启动(登录时)",
                        variable=self.v_autostart).grid(row=1, column=0, columnspan=3, sticky="w", **pad)
        ttk.Label(f_svc, text="提示: 灯效控制需要 USB 有线连接；蓝牙/2.4G 模式下无法控制(键盘只暴露标准 HID 接口)",
                  foreground="#8a6d3b").grid(row=2, column=0, columnspan=6, sticky="w", **pad)

        # ---- 任务映射表(分区模式: 键盘上显示不出的文字身份) ----
        f_tasks = ttk.LabelFrame(self.win, text="任务映射表（每 0.5s 刷新）")
        f_tasks.grid(row=row, column=0, columnspan=3, sticky="we", **pad)
        row += 1
        self.task_tree = ttk.Treeview(
            f_tasks, columns=("slot", "source", "state", "progress", "label"),
            show="headings", height=5)
        for c, txt, w in (("slot", "排", 44), ("source", "来源", 84),
                          ("state", "状态", 72), ("progress", "进度", 56),
                          ("label", "任务(cwd/消息)", 300)):
            self.task_tree.heading(c, text=txt)
            self.task_tree.column(c, width=w, anchor="w")
        self.task_tree.grid(row=0, column=0, columnspan=6, sticky="we", **pad)

        # ---- 按钮 ----
        f_btn = ttk.Frame(self.win)
        f_btn.grid(row=row, column=0, columnspan=3, sticky="we", **pad)
        row += 1
        ttk.Button(f_btn, text="保存并应用", command=self.save).pack(side=tk.LEFT, **pad)
        ttk.Button(f_btn, text="测试: 运行中",
                   command=lambda: self._test("RUNNING")).pack(side=tk.LEFT, **pad)
        ttk.Button(f_btn, text="测试: 待确认",
                   command=lambda: self._test("WAITING")).pack(side=tk.LEFT, **pad)
        ttk.Button(f_btn, text="测试: 完成",
                   command=lambda: self._test("DONE")).pack(side=tk.LEFT, **pad)
        ttk.Button(f_btn, text="恢复硬件灯效", command=self._restore).pack(side=tk.LEFT, **pad)

        # ---- 状态栏 ----
        self.v_status = tk.StringVar(value="…")
        ttk.Label(self.win, textvariable=self.v_status, relief=tk.SUNKEN,
                  anchor="w").grid(row=row, column=0, columnspan=3, sticky="we", **pad)

        self.refresh_status()
        self.win.lift()
        self.win.focus_force()

    # ---------- 控件辅助 ----------
    def _slider(self, parent, r, label, value, lo, hi, attr, res=None):
        ttk.Label(parent, text=label + ":").grid(row=r, column=0, sticky="e", padx=8, pady=2)
        var = tk.DoubleVar(value=float(value))
        kw = {"from_": lo, "to": hi, "orient": tk.HORIZONTAL, "length": 150,
              "variable": var}
        if res:
            kw["resolution"] = res
            kw["digits"] = 2
        # 注意: ttk.Scale 不支持 resolution(亮度需要 0.05 步长), 故用 tk.Scale
        s = tk.Scale(parent, showvalue=False, **kw)
        s.grid(row=r, column=1, columnspan=2, sticky="w", padx=4, pady=2)
        lbl = ttk.Label(parent, text=("%.2f" % float(value)) if res else str(int(value)), width=5)
        lbl.grid(row=r, column=3, sticky="w")
        s.configure(command=lambda _v, l=lbl, rs=res: l.configure(
            text=("%.2f" % float(_v)) if rs else str(int(float(_v)))))
        setattr(self, attr, var)

    def _pick(self, var):
        _, hx = colorchooser.askcolor(color=var.get() or "#FF0000",
                                      title="选择颜色", parent=self.win)
        if hx:
            var.set(hx.upper())

    # ---------- 动作 ----------
    def _test(self, state):
        self.app.svc.paused = False
        self.app.svc.force_state(state)

    def _demo(self):
        self.app.svc.inject_demo(overflow=False, secs=30)

    def _demo_overflow(self):
        self.app.svc.inject_demo(overflow=True, secs=30)

    def _restore(self):
        self.app.svc.clear_sessions()

    def _hide(self):
        self.win.withdraw()
        self.app.settings_win = None

    TASK_STATE_TEXT = {"RUNNING": "运行中", "WAITING": "待确认",
                       "DONE": "完成", "FAIL": "失败"}
    TASK_SOURCE_TEXT = {"workbuddy": "WorkBuddy", "codex": "Codex"}

    def refresh_status(self):
        svc = self.app.svc
        kb = "已连接" if svc.kb.dev is not None else "未连接"
        paused = " · 已暂停" if svc.paused else ""
        extra = ""
        snap = []
        if svc.partition_on and svc.mgr is not None:
            snap = svc.mgr.snapshot()
            on_row = [t for t in snap if not t["overflow"]]
            over = [t for t in snap if t["overflow"]]
            nc = len([t for t in over if t["source"] == "codex"])
            extra = " | 分区 %d/6 排" % len(on_row)
            if over:
                extra += " · 溢出 C%d/W%d" % (nc, len(over) - nc)
        else:
            run = len(svc.sessions)
            wait = len(svc.waiting)
            if run > 1 or wait:
                extra = " | 进行中 %d 个任务" % run
                if wait:
                    extra += " · %d 个等你确认" % wait
        self.v_status.set("状态: %s%s | 键盘: %s%s | 累计帧: %d"
                          % (STATE_TEXT.get(svc.state, svc.state), paused, kb,
                             extra, svc.kb.frames_sent))
        # 任务映射表
        for r in self.task_tree.get_children():
            self.task_tree.delete(r)
        for t in snap:
            slot = ("P%d" % (t["slot"] + 1)) if t["slot"] is not None else "溢出"
            if t.get("progress"):
                prog = "%d/%d" % (t["progress"][0], t["progress"][1])
            else:
                prog = "%d步" % t.get("steps", 0)
            self.task_tree.insert("", "end", values=(
                slot, self.TASK_SOURCE_TEXT.get(t["source"], t["source"]),
                self.TASK_STATE_TEXT.get(t["state"], t["state"]),
                prog, t.get("label", "")))

    def save(self):
        try:
            cfg = {
                "running": {
                    "mode": LABEL_MODES.get(self.v_run_mode.get(), "rainbow"),
                    "color": self._norm_color(self.v_run_color.get()),
                    "speed": int(self.v_run_speed.get()),
                    "fps": int(self.v_run_fps.get()),
                    "brightness": round(float(self.v_run_bri.get()), 2),
                },
                "waiting": {
                    "mode": "flow",
                    "color": self._norm_color(self.v_wait_color.get()),
                    "speed": int(self.v_wait_speed.get()),
                    "fps": int(self.v_wait_fps.get()),
                    "brightness": 0.8,
                },
                "done": {
                    "mode": "flash",
                    "color": self._norm_color(self.v_done_color.get()),
                    "times": int(self.v_done_times.get()),
                    "on_ms": int(self.v_done_on.get()),
                    "off_ms": int(self.v_done_off.get()),
                },
                "conflict": {"close_rk_app": bool(self.v_close_rk.get())},
                "partition": {
                    "enabled": bool(self.v_part_on.get()),
                    "fail_flash_secs": int(self.v_fail_secs.get()),
                    "waiting_on_ms": int(self.v_wait_on.get()),
                    "waiting_off_ms": int(self.v_wait_off.get()),
                },
                "sources": {
                    "workbuddy": {"color": self._norm_color(self.v_src_wb.get())},
                    "codex": {"color": self._norm_color(self.v_src_cx.get())},
                },
                "service": {"stale_session_secs": int(self.v_stale.get())},
            }
        except Exception as e:
            messagebox.showerror("保存失败", "数值填写有误:\n%s" % e, parent=self.win)
            return

        # 落盘(与现有配置深合并, 不动 device/source 等段)
        disk = {}
        if os.path.exists(CONFIG_PATH):
            try:
                with open(CONFIG_PATH, encoding="utf-8") as f:
                    disk = json.load(f)
            except Exception:
                disk = {}
        KL.deep_merge(disk, cfg)
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            f.write(json.dumps(disk, ensure_ascii=False, indent=2) + "\n")
        self.app.svc.apply_config(cfg)

        # 开机自启
        try:
            write_autostart(bool(self.v_autostart.get()))
        except OSError as e:
            messagebox.showwarning("自启设置失败", str(e), parent=self.win)

        messagebox.showinfo("已保存", "配置已保存并热生效(无需重启)", parent=self.win)

    @staticmethod
    def _norm_color(s):
        s = (s or "").strip().lstrip("#")
        if len(s) != 6:
            raise ValueError("颜色必须是 6 位十六进制, 当前: %r" % s)
        int(s, 16)
        return "#" + s.upper()


def main():
    root = tk.Tk()
    root.withdraw()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
