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
# 分区模式运行效果: 颜色=来源已定, 只有动画模式可选(流水/呼吸)
PART_EFFECT_LABELS = {"flow": "流水", "breathe": "呼吸"}
PART_LABEL_EFFECTS = {v: k for k, v in PART_EFFECT_LABELS.items()}

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
            pystray.MenuItem("重新扫描当前任务", lambda: self.ui_q.put(("rescan",))),
            pystray.MenuItem(
                lambda item: ("切换: 分区(每排一任务) → 全局" if self.svc.partition_on
                              else "切换: 全局(单状态) → 分区"),
                lambda: self.ui_q.put(("toggle_mode",))),
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
        elif kind == "rescan":
            # 重新抓取当前任务状态(演示/清空后恢复真实会话)
            n = self.svc.rescan_sessions()
            self._notify("已重新扫描: %d 条事件已重放" % n)
        elif kind == "toggle_mode":
            # 分区(每排一任务) / 全局(单状态) 双模式切换
            on = self.svc.toggle_partition()
            self.svc.rescan_sessions()
            self._notify("已切换: %s" % ("分区模式 · 每排一任务" if on
                                        else "全局模式 · 单状态流水"))
        elif kind == "restore":
            self.svc.clear_sessions()
        elif kind == "quit":
            self.quit()

    def _notify(self, msg):
        try:
            self.tray.notify(msg, "wb-keylight")
        except Exception:
            log.info("托盘通知: %s", msg)

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

        # ---- 模式互斥提示(分区/全局二选一, 当前哪套生效) ----
        self.v_mode_hint = tk.StringVar(value="…")
        ttk.Label(self.win, textvariable=self.v_mode_hint,
                  foreground="#8a6d3b").grid(
            row=row, column=0, columnspan=3, sticky="w", **pad)
        row += 1

        # ---- 运行中(仅全局模式; 分区模式启用时置灰) ----
        f_run = ttk.LabelFrame(self.win, text="运行中（WorkBuddy 干活时）· 仅全局模式")
        self.f_run = f_run
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
        self._slider(f_run, 1, "速度", cfg["running"].get("speed", 30), 5, 120, "v_run_speed")
        self._slider(f_run, 2, "帧率", cfg["running"].get("fps", 30), 10, 60, "v_run_fps")
        self._slider(f_run, 3, "亮度", cfg["running"].get("brightness", 100), 5, 100, "v_run_bri")

        # ---- 待确认(颜色/亮度两种模式共用: 分区红闪也用这俩) ----
        f_wait = ttk.LabelFrame(self.win, text="待确认（需要你拍板时 · 红灯流水）· 颜色/亮度两模式共用")
        f_wait.grid(row=row, column=0, columnspan=3, sticky="we", **pad)
        row += 1
        ttk.Label(f_wait, text="颜色:").grid(row=0, column=0, sticky="e", **pad)
        self.v_wait_color = tk.StringVar(value=cfg["waiting"].get("color", "#FF0000"))
        ttk.Entry(f_wait, textvariable=self.v_wait_color, width=9).grid(row=0, column=1, **pad)
        ttk.Button(f_wait, text="…", width=3,
                   command=lambda: self._pick(self.v_wait_color)).grid(row=0, column=2, **pad)
        self._slider(f_wait, 1, "速度", cfg["waiting"].get("speed", 30), 5, 120, "v_wait_speed")
        self._slider(f_wait, 2, "帧率", cfg["waiting"].get("fps", 30), 10, 60, "v_wait_fps")
        self._slider(f_wait, 3, "亮度", cfg["waiting"].get("brightness", 100), 5, 100, "v_wait_bri")

        # ---- 完成(闪灯参数两种模式共用) ----
        f_done = ttk.LabelFrame(self.win, text="完成（一轮回答结束时 · 绿灯闪烁）· 两模式共用")
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
                        variable=self.v_part_on,
                        command=self._sync_mode_ui).grid(
            row=0, column=0, columnspan=8, sticky="w", **pad)
        # 设置项全收进 body 子框: 互斥置灰时整框禁用, 复选框永远可点(能切回来)
        part_body = ttk.Frame(f_part)
        part_body.grid(row=1, column=0, columnspan=8, sticky="we", **pad)
        self.part_body = part_body
        ttk.Label(part_body, text="WorkBuddy 运行色:").grid(row=0, column=0, sticky="e", **pad)
        self.v_src_wb = tk.StringVar(
            value=cfg.get("sources", {}).get("workbuddy", {}).get("color", "#00E676"))
        ttk.Entry(part_body, textvariable=self.v_src_wb, width=9).grid(row=0, column=1, **pad)
        ttk.Button(part_body, text="…", width=3,
                   command=lambda: self._pick(self.v_src_wb)).grid(row=0, column=2, **pad)
        ttk.Label(part_body, text="Codex 运行色:").grid(row=0, column=3, sticky="e", **pad)
        self.v_src_cx = tk.StringVar(
            value=cfg.get("sources", {}).get("codex", {}).get("color", "#2979FF"))
        ttk.Entry(part_body, textvariable=self.v_src_cx, width=9).grid(row=0, column=4, **pad)
        ttk.Button(part_body, text="…", width=3,
                   command=lambda: self._pick(self.v_src_cx)).grid(row=0, column=5, **pad)
        ttk.Label(part_body, text="失败闪烁(秒):").grid(row=1, column=0, sticky="e", **pad)
        self.v_fail_secs = tk.IntVar(
            value=int(cfg.get("partition", {}).get("fail_flash_secs", 30)))
        ttk.Spinbox(part_body, from_=5, to=120, width=5,
                    textvariable=self.v_fail_secs).grid(row=1, column=1, sticky="w", **pad)
        ttk.Label(part_body, text="等待 亮/灭(ms):").grid(row=1, column=3, sticky="e", **pad)
        self.v_wait_on = tk.IntVar(
            value=int(cfg.get("partition", {}).get("waiting_on_ms", 150)))
        self.v_wait_off = tk.IntVar(
            value=int(cfg.get("partition", {}).get("waiting_off_ms", 100)))
        ttk.Entry(part_body, textvariable=self.v_wait_on, width=5).grid(row=1, column=4, **pad)
        ttk.Entry(part_body, textvariable=self.v_wait_off, width=5).grid(row=1, column=5, **pad)
        # 效果/速度/帧率/亮度 — 与全局"运行中"同规格(2026-10-14 用户要求)
        pcfg0 = cfg.get("partition", {})
        ttk.Label(part_body, text="效果:").grid(row=2, column=0, sticky="e", **pad)
        self.v_part_effect = tk.StringVar(
            value=PART_EFFECT_LABELS.get(pcfg0.get("effect", "flow"), "流水"))
        ttk.Combobox(part_body, textvariable=self.v_part_effect, state="readonly",
                     width=8, values=list(PART_EFFECT_LABELS.values())).grid(
            row=2, column=1, sticky="w", **pad)
        self._cslider(part_body, 2, 2, "速度", pcfg0.get("speed", 30), 5, 120, "v_part_speed")
        self._cslider(part_body, 3, 0, "帧率", pcfg0.get("fps", 30), 10, 60, "v_part_fps")
        self._cslider(part_body, 3, 3, "亮度", pcfg0.get("brightness", 100), 5, 100, "v_part_bri")
        ttk.Button(part_body, text="演示分区(3 任务)", width=15, command=self._demo).grid(
            row=4, column=0, columnspan=2, sticky="w", **pad)
        ttk.Button(part_body, text="演示溢出(10 任务)", width=15, command=self._demo_overflow).grid(
            row=4, column=2, columnspan=2, sticky="w", **pad)
        ttk.Button(part_body, text="重新扫描当前任务", width=15, command=self._rescan).grid(
            row=4, column=4, columnspan=2, sticky="w", **pad)
        ttk.Label(part_body, text="演示任务 30 秒后自动绿闪收尾并停流; 测试后点\"重新扫描\"可从 hook 历史恢复真实在跑的任务; 键盘上看不到的文字身份见下方任务映射表",
                  foreground="#8a6d3b").grid(row=5, column=0, columnspan=8, sticky="w", **pad)
        self._sync_mode_ui()

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

        # ---- 按钮(两行分组: 上=保存/确认/恢复, 下=三个测试) ----
        f_btn = ttk.Frame(self.win)
        f_btn.grid(row=row, column=0, columnspan=3, sticky="we", **pad)
        row += 1
        btn1 = ttk.Frame(f_btn)
        btn1.pack(fill=tk.X)
        ttk.Button(btn1, text="保存并应用", width=12, command=self.save).pack(
            side=tk.LEFT, **pad)
        ttk.Button(btn1, text="确认并关闭", width=12, command=self._ok).pack(
            side=tk.LEFT, **pad)
        ttk.Button(btn1, text="恢复硬件灯效", command=self._restore).pack(
            side=tk.LEFT, **pad)
        btn2 = ttk.Frame(f_btn)
        btn2.pack(fill=tk.X)
        ttk.Button(btn2, text="测试: 运行中", width=12,
                   command=lambda: self._test("RUNNING")).pack(side=tk.LEFT, **pad)
        ttk.Button(btn2, text="测试: 待确认", width=12,
                   command=lambda: self._test("WAITING")).pack(side=tk.LEFT, **pad)
        ttk.Button(btn2, text="测试: 完成", width=12,
                   command=lambda: self._test("DONE")).pack(side=tk.LEFT, **pad)

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
        if res:                              # 浮点步进(旧亮度 0.05 用, 现保留能力)
            var = tk.DoubleVar(value=float(value))
        else:
            var = tk.IntVar(value=int(value))
        kw = {"from_": lo, "to": hi, "orient": tk.HORIZONTAL, "length": 150,
              "variable": var}
        if res:
            kw["resolution"] = res
            kw["digits"] = 2
        # 注意: ttk.Scale 不支持 resolution, 故用 tk.Scale
        s = tk.Scale(parent, showvalue=False, **kw)
        s.grid(row=r, column=1, columnspan=2, sticky="w", padx=4, pady=2)
        lbl = ttk.Label(parent, text=("%.2f" % float(value)) if res else str(int(value)), width=5)
        lbl.grid(row=r, column=3, sticky="w")
        s.configure(command=lambda _v, l=lbl, rs=res: l.configure(
            text=("%.2f" % float(_v)) if rs else str(int(float(_v)))))
        setattr(self, attr, var)

    def _cslider(self, parent, r, c, label, value, lo, hi, attr):
        """紧凑整数滑条(分区模式一行多控件用): 标签@c, 滑条@c+1, 数值@c+2"""
        ttk.Label(parent, text=label + ":").grid(row=r, column=c, sticky="e", padx=8, pady=2)
        var = tk.IntVar(value=int(value))
        s = tk.Scale(parent, from_=lo, to=hi, orient=tk.HORIZONTAL, length=105,
                     showvalue=False, variable=var)
        s.grid(row=r, column=c + 1, sticky="w", padx=4, pady=2)
        lbl = ttk.Label(parent, text=str(int(value)), width=4)
        lbl.grid(row=r, column=c + 2, sticky="w")
        s.configure(command=lambda _v, l=lbl: l.configure(text=str(int(float(_v)))))
        setattr(self, attr, var)

    # ---------- 模式互斥(分区/全局二选一) ----------
    def _set_enabled(self, widget, on):
        """递归启用/禁用框架内所有控件(ttk/tk 混用, 逐个 try)"""
        try:
            widget.configure(state=tk.NORMAL if on else tk.DISABLED)
        except tk.TclError:
            pass
        for ch in widget.winfo_children():
            self._set_enabled(ch, on)

    def _sync_mode_ui(self, *_a):
        """分区开关 <-> 两套效果区的互斥置灰 + 顶部提示文字。
        注意: 待确认颜色/亮度、完成闪灯参数两种模式共用, 不置灰。"""
        on = bool(self.v_part_on.get())
        self._set_enabled(self.f_run, not on)        # 运行中: 全局专属
        self._set_enabled(self.part_body, on)        # 分区设置项(复选框在框外, 永远可点)
        if on:
            self.v_mode_hint.set(
                "当前生效: 分区模式 — \"运行中\"为全局模式专用, 已停用; "
                "待确认颜色/亮度与完成闪灯为共用参数仍可调")
        else:
            self.v_mode_hint.set(
                "当前生效: 全局模式 — 分区效果设置已停用; "
                "勾选\"启用分区模式\"可切回每排一任务")

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

    def _rescan(self):
        """重新抓取当前任务状态: 重读 hook 事件历史重建任务表
        (演示/清空后恢复真实在跑的会话, 不用等下一条 hook 事件)"""
        n = self.app.svc.rescan_sessions()
        self.v_status.set("已重新扫描: %d 条事件已重放" % n)

    def _restore(self):
        self.app.svc.clear_sessions()

    def _ok(self):
        """确认: 保存并应用, 然后关闭窗口"""
        self.save(quiet=True)
        if self.win.winfo_exists():
            self._hide()

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

    def save(self, quiet=False):
        try:
            cfg = {
                "running": {
                    "mode": LABEL_MODES.get(self.v_run_mode.get(), "rainbow"),
                    "color": self._norm_color(self.v_run_color.get()),
                    "speed": int(self.v_run_speed.get()),
                    "fps": int(self.v_run_fps.get()),
                    "brightness": int(self.v_run_bri.get()),
                },
                "waiting": {
                    "mode": "flow",
                    "color": self._norm_color(self.v_wait_color.get()),
                    "speed": int(self.v_wait_speed.get()),
                    "fps": int(self.v_wait_fps.get()),
                    "brightness": int(self.v_wait_bri.get()),
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
                    "effect": PART_LABEL_EFFECTS.get(self.v_part_effect.get(), "flow"),
                    "fail_flash_secs": int(self.v_fail_secs.get()),
                    "waiting_on_ms": int(self.v_wait_on.get()),
                    "waiting_off_ms": int(self.v_wait_off.get()),
                    "speed": int(self.v_part_speed.get()),
                    "fps": int(self.v_part_fps.get()),
                    "brightness": int(self.v_part_bri.get()),
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

        if not quiet:
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
