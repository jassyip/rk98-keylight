# -*- coding: utf-8 -*-
"""离线验证设置窗互斥置灰: 不截图, 直接查询控件 state。"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(HERE), "src")
sys.path.insert(0, SRC)

import tkinter as tk  # noqa: E402

import keylight as KL  # noqa: E402
import tray as TRAY  # noqa: E402


class MockKB:
    dev = None
    frames_sent = 0


class MockMgr:
    def snapshot(self):
        return []


class MockSvc:
    def __init__(self, cfg):
        self.cfg = cfg
        self.kb = MockKB()
        self.paused = False
        self.partition_on = bool(cfg.get("partition", {}).get("enabled", True))
        self.mgr = MockMgr() if self.partition_on else None
        self.sessions = []
        self.waiting = []
        self.state = "IDLE"


class MockApp:
    def __init__(self, cfg):
        self.root = tk.Tk()
        self.root.withdraw()          # 不显示主窗
        self.svc = MockSvc(cfg)


def collect(widget, out, depth=0):
    """递归收集 (类型, state, 文本) 三元组。"""
    try:
        st = widget.cget("state")
    except Exception:
        st = "?"
    txt = ""
    try:
        txt = widget.cget("text")
    except Exception:
        pass
    out.append((depth, widget.winfo_class(), str(st), txt[:18]))
    for ch in widget.winfo_children():
        collect(ch, out, depth + 1)


def states_of(sw, frame_attr):
    out = []
    collect(getattr(sw, frame_attr), out)
    return out


def summarize(rows, label):
    kinds = {}
    for depth, cls, st, txt in rows:
        if cls in ("TScale", "TCombobox", "TEntry", "TButton", "TLabel", "TSpinbox",
                   "TCheckbutton", "TFrame", "TLabelframe"):
            kinds.setdefault(cls, set()).add(st)
    print("[%s]" % label)
    for cls in sorted(kinds):
        print("   %-12s -> %s" % (cls, sorted(kinds[cls])))


def main():
    cfg = KL.load_config(os.path.join(os.path.dirname(SRC), "config.json"))
    app = MockApp(cfg)
    sw = TRAY.SettingsWindow(app)
    sw.win.update_idletasks()

    print("partition.enabled =", cfg["partition"].get("enabled"))
    print("hint:", sw.v_mode_hint.get())
    print("running.brightness =", cfg["running"].get("brightness"),
          "| waiting.brightness =", cfg["waiting"].get("brightness"),
          "| partition.brightness =", cfg["partition"].get("brightness"))

    summarize(states_of(sw, "f_run"), "分区开启: f_run(运行中, 全局专属)")
    summarize(states_of(sw, "part_body"), "分区开启: part_body(分区设置)")

    # 反向: 关分区
    sw.v_part_on.set(False)
    sw._sync_mode_ui()
    sw.win.update_idletasks()
    print("--- 关闭分区后 ---")
    print("hint:", sw.v_mode_hint.get())
    summarize(states_of(sw, "f_run"), "分区关闭: f_run")
    summarize(states_of(sw, "part_body"), "分区关闭: part_body")

    # 再开回分区
    sw.v_part_on.set(True)
    sw._sync_mode_ui()
    sw.win.update_idletasks()
    print("--- 再开分区后 ---")
    print("hint:", sw.v_mode_hint.get())
    summarize(states_of(sw, "f_run"), "分区开启: f_run")
    summarize(states_of(sw, "part_body"), "分区开启: part_body")

    sw.win.destroy()
    app.root.destroy()


if __name__ == "__main__":
    main()
