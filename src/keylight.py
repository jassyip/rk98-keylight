#!/usr/bin/env python3
"""wb-keylight 常驻服务 — WorkBuddy 运行状态 -> RK98 键盘灯效

状态机:
  全局模式(partition.enabled=false, 保留 M1-M3 行为):
    IDLE     平常: 不发送任何帧, 键盘自动回退自带灯效(点击扩散)
    RUNNING  运行中: 彩色流水(默认 rainbow)
    WAITING  待确认: 红灯流水(默认 red flow, 由 Notification 触发)
    DONE     完成: 绿灯闪烁(默认 green flash) -> 自动回 IDLE
  分区模式(partition.enabled=true, M4):
    每任务独占一排: WB=绿流水 / Codex=蓝流水 / 等待=红闪 / 完成=绿闪xN
    详见 partition.py 与 M4-DESIGN.md

事件源:
  1. HTTP POST /event (hook.py 转发, install_hooks.py 注册)
  2. 尾随 hook 事件 JSONL 文件 (通道A, 路径见 config.json 的 source.hook_events_path, 按 (session,event,gen) 去重)
  3. 尾随 ~/.codex/sessions/**(Codex rollout JSONL, codex_tail.py)
"""
import argparse
import glob
import json
import logging
import os
import socket
import sys
import threading
import time
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from rk98 import RK98, RK98Error, kill_rk_app, rk_app_running  # noqa: E402
from effects import hex_rgb, scale, solid_colors, flow_frame, rainbow_frame, breathe_frame  # noqa: E402
from partition import PartitionManager  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DEFAULT_CONFIG = os.path.join(ROOT, "config.json")
STATUS_FILE = os.path.join(ROOT, "status.json")

DEFAULTS = {
    "device": {"vid": "258A", "pid": "00C1", "preferred_collection": "Col05"},
    "running": {"mode": "rainbow", "color": "#FF0000", "fps": 30, "brightness": 0.8, "speed": 40},
    "done": {"color": "#00FF00", "mode": "flash", "times": 3, "on_ms": 200, "off_ms": 120},
    "waiting": {"mode": "flow", "color": "#FF0000", "fps": 30, "brightness": 0.8, "speed": 40},
    "idle": {"mode": "hardware"},
    "conflict": {"close_rk_app": False},
    "partition": {"enabled": True, "fail_flash_secs": 30,
                  "waiting_on_ms": 150, "waiting_off_ms": 100,
                  "speed": 40, "fps": 30, "brightness": 0.8, "tail": 8.0},
    "sources": {"workbuddy": {"color": "#00E676"}, "codex": {"color": "#2979FF"}},
    "codex": {"enabled": True, "sessions_dir": "~/.codex/sessions", "poll_secs": 2},
    "service": {"port": 18990, "stale_session_secs": 600, "final_grace_secs": 30,
                "notice_secs": 20},
    "source": {"tail_hook_events": True,
               "hook_events_path": "",   # 填你自己的 hook 事件文件路径
               "projects_root": ""},
}

RUNNING_EVENTS = {"UserPromptSubmit"}
WAITING_EVENTS = {"Notification"}
HEARTBEAT_EVENTS = {"PreToolUse", "PostToolUse", "PostToolUseFailure", "PreCompact"}
# 这些工具表示"我需要用户拍板" -> 红灯流水(比 Notification 更可靠)
ASK_TOOLS = {"AskUserQuestion", "ask_user_question", "RequestUserInput", "request_user_input"}
END_EVENTS = {"Stop"}
CLOSE_EVENTS = {"SessionEnd"}

log = logging.getLogger("keylight")


def deep_merge(base, override):
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            deep_merge(base[k], v)
        else:
            base[k] = v


def load_config(path):
    cfg = json.loads(json.dumps(DEFAULTS))
    if path and os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                deep_merge(cfg, json.load(f))
        except Exception as e:
            log.warning("配置读取失败, 使用默认值: %s", e)
    return cfg


def write_status(svc):
    """原子写 status.json(临时文件 + replace), 多线程事件源并发安全"""
    try:
        data = {
            "state": svc.state,
            "paused": svc.paused,
            "sessions": len(svc.sessions),
            "waiting": len(svc.waiting),
            "partition": svc.partition_on,
            "tasks": svc.mgr.snapshot() if (svc.partition_on and svc.mgr) else [],
            "frames_sent": svc.kb.frames_sent,
            "updated": time.strftime("%H:%M:%S"),
        }
        tmp = STATUS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, STATUS_FILE)
    except Exception:
        pass


def parse_todos(obj):
    """从 TodoWrite 的 tool_input 解析 (done, total); 无有效 todo 返回 None"""
    todos = obj.get("todos") if isinstance(obj, dict) else None
    if not isinstance(todos, list) or not todos:
        return None
    total = len(todos)
    done = sum(1 for x in todos if isinstance(x, dict)
               and str(x.get("status", "")).lower() in ("completed", "complete", "done"))
    return (done, total)


class Service:
    def __init__(self, cfg):
        self.cfg = cfg
        self.state = "IDLE"
        self.epoch = 0
        self.paused = False
        self.sessions = {}          # sid -> 最后活动时间(全局模式的运行中任务)
        self.waiting = set()        # 全局模式: 正在"等你拍板"的会话集合
        self.notice_until = 0.0     # 全局模式: Notification 信号有效期
        # M4 分区模式: 任务 -> 排槽位(与 sessions/waiting 互斥, 按 partition_on 分叉)
        self.partition_on = bool(cfg.get("partition", {}).get("enabled", False))
        self.mgr = PartitionManager(cfg) if self.partition_on else None
        self.seen = OrderedDict()
        self.lock = threading.RLock()   # 分区模式下 _sync_global_state 会在锁内被调, 需可重入
        self.stop_event = threading.Event()
        self.httpd = None
        d = cfg["device"]
        self.kb = RK98(vid=int(d["vid"], 16), pid=int(d["pid"], 16),
                       preferred=d.get("preferred_collection", "Col05"))

    # ---------- 键盘 ----------
    def kb_ensure(self):
        if self.kb.dev is not None:
            return True
        try:
            self.kb.open()
            log.info("键盘已连接: %s", self.kb.iface)
            return True
        except (RK98Error, OSError) as e:
            log.warning("键盘连接失败: %s", e)
            return False

    def kb_send(self, colors):
        if not self.kb_ensure():
            return False
        try:
            self.kb.send_frame(colors)
            return True
        except (OSError, RK98Error) as e:
            log.warning("HID 写入失败: %s — 重连中", e)
            self.kb.close()
            if self.cfg["conflict"].get("close_rk_app") and rk_app_running():
                if kill_rk_app():
                    log.info("已结束 RK 官方软件进程以让出 HID")
            return self.kb_ensure()

    # ---------- 事件 ----------
    def handle_event(self, payload):
        if not isinstance(payload, dict):
            return
        ev = payload.get("hook_event_name") or payload.get("event") or ""
        sid = str(payload.get("session_id") or "unknown")
        gen = str(payload.get("generation_id") or "")
        key = (sid, ev, gen)
        with self.lock:
            if key in self.seen:
                return
            self.seen[key] = True
            while len(self.seen) > 4000:
                self.seen.popitem(last=False)
            now = time.time()
            if self.partition_on and self.mgr is not None:
                self._handle_partition(payload, ev, sid, now)
                log.info("事件 %-18s session=%s… tool=%-14s -> %s(分区)",
                         ev, sid[:8],
                         str(payload.get("tool_name") or "-")[:14], self.state)
                write_status(self)
                return
            if ev in RUNNING_EVENTS:
                # 用户提交 -> 该会话转为运行中
                self.sessions[sid] = now
                self.waiting.discard(sid)   # 新一轮提交 = 之前的提问已被回答
                self._recompute()
            elif ev in WAITING_EVENTS:
                # Notification(权限/等待输入弹窗)。实测该事件常常**没有 session_id**
                # (payload 里是 "unknown"), 若直接登记会导致这个假会话永远留在
                # waiting 集合里 -> 状态永久卡在红灯。因此这里不把 waiting_sid
                # 记进集合, 只作为"此刻需要你确认"的信号, 并且下一次任何非
                # Notification 的事件就会解除(见下方 waiting 的清除条件)。
                self._recompute(note_waiting=True)
            elif ev in HEARTBEAT_EVENTS:
                tool = str(payload.get("tool_name") or "")
                self.sessions[sid] = now
                if ev == "PreToolUse" and tool in ASK_TOOLS:
                    # 该会话正在向你提问 -> 记入等待集合
                    self.waiting.add(sid)
                else:
                    # 任何其它工具(含提问工具返回) -> 该会话不再等待
                    self.waiting.discard(sid)
                self._recompute()
            elif ev in END_EVENTS:
                # 一轮结束 -> 移出运行集合; 全体结束才算完成闪烁
                self.sessions.pop(sid, None)
                self.waiting.discard(sid)
                self._recompute(done_ok=True)
            elif ev == "FinalStop":
                # 全部结束: 顺带清掉"沉默"的残留会话(此前没收到 Stop 的孤儿 id),
                # 否则 sessions 永远非空, 绿灯闪不会触发
                self.sessions.pop(sid, None)
                self.waiting.discard(sid)
                grace = float(self.cfg["service"].get("final_grace_secs", 30))
                cutoff = now - grace
                orphans = [s for s, t in self.sessions.items() if t < cutoff]
                for s in orphans:
                    self.sessions.pop(s, None)
                    self.waiting.discard(s)
                if orphans:
                    log.info("FinalStop 清理沉默会话: %s", [o[:8] for o in orphans])
                self._recompute(done_ok=True)
            elif ev in CLOSE_EVENTS:
                # 会话关闭 -> 直接回平常(不算完成)
                self.sessions.pop(sid, None)
                self.waiting.discard(sid)
                self._recompute(done_ok=False)
        log.info("事件 %-18s session=%s… tool=%-14s -> %s", ev, sid[:8],
                 str(payload.get("tool_name") or "-")[:14], self.state)
        write_status(self)

    def _handle_partition(self, payload, ev, sid, now):
        """M4 分区模式: WB hook 事件 -> 归一化信号 -> PartitionManager

        与全局模式的差异:
        - 每个 session 是独立任务(独占一排), 不再收敛成单一全局状态;
        - 等待信号只由 PreToolUse(AskUserQuestion) 驱动 -> 持久 WAITING;
          Notification(回合结束/空闲提示) -> NOTICE 短时红闪脉冲
          (notice_secs, 到期自动转 DONE 绿闪释放, 不会一直红闪);
          无 session_id 的 Notification 无法定位任务 -> 忽略;
        - TodoWrite 解析进度 -> 亮格填充(有总量才算真进度)。
        """
        mgr = self.mgr
        if ev in RUNNING_EVENTS:
            # 用户提交 -> 新一轮开始(已在 WAITING/DONE 的任务恢复 RUNNING)
            mgr.signal("workbuddy", sid, "START",
                       {"label": str(payload.get("cwd") or "")})
        elif ev in WAITING_EVENTS:
            # Notification = 回合结束/空闲提示, 不是"待确认" -> 短时脉冲,
            # 到期自动转完成(2026-10-14 修复: 曾被误映射为持久 ASK,
            # 导致没有待确认状态时该排一直红闪)
            if sid and sid != "unknown":
                mgr.signal("workbuddy", sid, "NOTICE")
        elif ev in HEARTBEAT_EVENTS:
            tool = str(payload.get("tool_name") or "")
            if ev == "PreToolUse" and tool in ASK_TOOLS:
                mgr.signal("workbuddy", sid, "ASK")
            elif ev == "PreToolUse" and tool == "TodoWrite":
                prog = parse_todos(payload.get("tool_input") or payload.get("tool_response"))
                mgr.signal("workbuddy", sid, "HEARTBEAT",
                           {"progress": prog} if prog else None)
            else:
                # 任何其它工具(含提问工具返回) = 已答复, 解除等待
                mgr.signal("workbuddy", sid, "HEARTBEAT")
        elif ev in END_EVENTS or ev == "FinalStop":
            if ev == "FinalStop":
                # 清理没收到 Stop 的沉默会话(否则永远占着排)
                grace = float(self.cfg["service"].get("final_grace_secs", 30))
                dead = mgr.close_stale("workbuddy", now - grace)
                if dead:
                    log.info("FinalStop 清理沉默任务: %s", [d[:8] for d in dead])
            mgr.signal("workbuddy", sid, "END")
        elif ev in CLOSE_EVENTS:
            # 会话关闭 -> 直接释放不闪绿(不算完成)
            mgr.signal("workbuddy", sid, "CLOSE")
        self._sync_global_state()

    def apply_signal(self, source, task_id, kind, extra=None):
        """归一化信号入口(供 codex_tail 等外部源调用; 分区模式未启用时忽略)"""
        if self.partition_on and self.mgr is not None:
            self.mgr.signal(source, task_id, kind, extra)
            self._sync_global_state()
        else:
            log.info("分区模式未启用, 忽略 %s 信号: %s %s", source,
                     str(task_id)[:8], kind)

    # ---------- 重新扫描(状态重建) ----------
    def rescan_sessions(self):
        """重新抓取当前任务状态(托盘"重新扫描当前任务" / HTTP action=RESCAN)。

        数据源: WorkBuddy 会话记录 <projects>/*/*.jsonl
          - 文件名 = session_id, 文件 mtime = 会话最后活动时间;
          - <projects> 根目录从 hook-events.jsonl 的 transcript_path 字段
            推导(payload 自带, 不硬编码个人路径), 也可由
            source.projects_root 显式指定。
        为什么不是重放 hook-events.jsonl: 该文件只旁路记录 Stop/FinalStop,
        没有 UserPromptSubmit/PreToolUse 等建立事件, 重放重建不出任务
        (2026-10-14 第一版实现的实测结论)。

        规则: 文件 mtime 在 stale_session_secs 内 = 活跃会话 -> 建任务;
        Stop/FinalStop 只表示"一轮回答结束", 不阻止恢复 —— 用户点扫描
        就是要抓"当前还在的对话"。返回恢复的会话数。"""
        now = time.time()
        stale = float(self.cfg["service"].get("stale_session_secs", 600))
        src = self.cfg.get("source", {}) or {}
        root = src.get("projects_root")
        labels = {}
        hpath = src.get("hook_events_path")
        if hpath and os.path.exists(hpath):
            try:
                with open(hpath, "r", encoding="utf-8", errors="replace") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            d = json.loads(line)
                        except Exception:
                            continue
                        tp = d.get("transcript_path")
                        if not root and tp:
                            # .../projects/<proj>/<sid>.jsonl -> .../projects
                            root = os.path.dirname(os.path.dirname(str(tp)))
                        sid = str(d.get("session_id") or "")
                        cwd = str(d.get("cwd") or "")
                        if sid and cwd and sid not in labels:
                            labels[sid] = cwd
            except OSError as e:
                log.warning("重新扫描: 读取 hook 事件失败 %s", e)
        found = []                          # (sid, label, mtime)
        if root and os.path.isdir(root):
            for p in glob.glob(os.path.join(root, "*", "*.jsonl")):
                try:
                    mt = os.path.getmtime(p)
                except OSError:
                    continue
                if now - mt > stale:
                    continue                 # 僵死/历史会话不恢复
                sid = os.path.splitext(os.path.basename(p))[0]
                found.append((sid, labels.get(sid)
                              or os.path.basename(os.path.dirname(p)), mt))
        with self.lock:
            if self.partition_on and self.mgr is not None:
                self.mgr.tasks.clear()
                for sid, label, mt in found:
                    self.mgr.replay("workbuddy", sid, "START",
                                    {"label": label}, ts=mt)
                self._sync_global_state()
            else:
                self.sessions = {sid: mt for sid, _, mt in found}
                self.waiting.clear()
                self.notice_until = 0.0
                self._recompute()
            write_status(self)
        log.info("重新扫描完成: %d 个活跃会话恢复 (root=%s)", len(found), root)
        return len(found)

    def toggle_partition(self):
        """分区/全局双模式切换(托盘快捷入口; 设置界面开关同步同一配置)。
        切换后立即按新模式重扫历史, 真实任务直接上新模式的键盘显示。"""
        with self.lock:
            p = self.cfg.setdefault("partition", {})
            p["enabled"] = not bool(p.get("enabled", True))
            self.partition_on = bool(p["enabled"])
            self.sessions.clear()
            self.waiting.clear()
            self.notice_until = 0.0
            if self.partition_on:
                if self.mgr is None:
                    self.mgr = PartitionManager(self.cfg)
            else:
                if self.mgr is not None:
                    self.mgr.clear_all()
                self.mgr = None
            self.paused = False
            self._set_state("IDLE")
        log.info("已切换为%s模式", "分区(每排一任务)" if self.partition_on else "全局(单状态)")
        return self.partition_on

    def _sync_global_state(self):
        """分区模式: 由任务表推导全局态(托盘图标/状态栏)"""
        if self.partition_on and self.mgr is not None:
            gs = self.mgr.global_state()
            if gs != self.state:
                self._set_state(gs)

    def inject_demo(self, overflow=False, secs=30):
        """注入演示任务(不依赖真实 agent); 分区模式未开启时自动开启"""
        with self.lock:
            if not self.partition_on or self.mgr is None:
                self.partition_on = True
                self.mgr = PartitionManager(self.cfg)
            self.paused = False
            ids = (self.mgr.inject_demo_overflow(secs) if overflow
                   else self.mgr.inject_demo(secs))
            self._sync_global_state()
        log.info("演示任务已注入: %s", ids)
        return ids

    def _recompute(self, done_ok=False, note_waiting=False):
        """由 sessions/waiting 推导应显示的状态 — 多任务并发的唯一决策点

        优先级: 有任务在等我确认(WAITING) > 有任务在跑(RUNNING) > 完成(DONE) > 平常(IDLE)

        键盘只有一块, 多个 WorkBuddy 任务并发时不做"分区显示", 而是按紧急度
        取最该让你注意的那个 —— 你没拍板的红灯, 优先级高于任何彩色流水。

        note_waiting=True 用于 Notification: 它不带有效 session_id, 不能登记进
        waiting 集合(会永远残留), 只置一个短时信号, 到期或被正常事件覆盖即解除。
        """
        if note_waiting:
            self.notice_until = time.time() + float(
                self.cfg["service"].get("notice_secs", 20))

        # Notification 信号已过期 -> 视为你已处理完(比如关掉了弹窗)
        if self.notice_until and time.time() > self.notice_until:
            self.notice_until = 0.0

        if self.waiting or self.notice_until:
            self._set_state("WAITING")
        elif self.sessions:
            if self.state in ("IDLE", "DONE", "WAITING"):
                self._set_state("RUNNING")
        else:
            if done_ok and self.state in ("RUNNING", "WAITING"):
                # 全体正常收工 -> 绿闪庆祝
                self._set_state("DONE")
            elif not done_ok and self.state in ("RUNNING", "WAITING"):
                # 会话被关闭/僵死, 不算完成, 直接回平常(避免误闪绿)
                self._set_state("IDLE")
            elif self.state == "WAITING":
                # 无会话无通知却停在 WAITING -> 兜底解除, 防止永久红灯
                self.notice_until = 0.0
                self._set_state("IDLE")

    def _set_state(self, s):
        if self.state != s:
            log.info("状态切换: %s -> %s", self.state, s)
        self.state = s
        self.epoch += 1
        write_status(self)

    def force_state(self, s):
        """调试/测试用: 强制进入某状态(不登记会话)

        分区模式下没有"无任务的全局状态", 旧测试按钮映射为注入对应状态的
        演示任务(30s 后自动收尾); IDLE = 清空全部任务。
        """
        with self.lock:
            if self.partition_on and self.mgr is not None:
                self.paused = False
                if s == "IDLE":
                    self.mgr.clear_all()
                    self._sync_global_state()
                elif s == "RUNNING":
                    self.mgr.clear_all()
                    self.mgr.inject_demo(secs=30)
                    self._sync_global_state()
                elif s == "WAITING":
                    self.mgr.clear_all()
                    self.mgr.signal("workbuddy", "demo-wait", "ASK",
                                    {"label": "测试 · 待确认"})
                    self._sync_global_state()
                elif s == "DONE":
                    self.mgr.clear_all()
                    self.mgr.signal("workbuddy", "demo-done", "START",
                                    {"label": "测试 · 完成"})
                    self.mgr.signal("workbuddy", "demo-done", "END")
                    self._sync_global_state()
                return
            self._set_state(s)

    def clear_sessions(self):
        """清除全部会话(含僵死会话)并回 IDLE — 键盘交还自带灯效"""
        with self.lock:
            n = len(self.sessions)
            self.sessions.clear()
            self.waiting.clear()
            self.notice_until = 0.0
            if self.partition_on and self.mgr is not None:
                n += self.mgr.clear_all()
            if self.state in ("RUNNING", "WAITING", "DONE"):
                self._set_state("IDLE")
        log.info("已清除 %d 个会话, 回 IDLE", n)

    def apply_config(self, cfg):
        """设置界面保存后热更新(渲染器每帧读 cfg, 立即生效);
        分区模式开关在这里热切换(开: 建 PartitionManager; 关: 清任务回全局)"""
        with self.lock:
            deep_merge(self.cfg, cfg or {})
            want = bool(self.cfg.get("partition", {}).get("enabled", False))
            if want and not self.partition_on:
                self.partition_on = True
                self.mgr = PartitionManager(self.cfg)
                log.info("分区模式已开启(热切换)")
            elif not want and self.partition_on:
                self.partition_on = False
                if self.mgr is not None:
                    self.mgr.clear_all()
                self.mgr = None
                self.sessions.clear()
                self.waiting.clear()
                self.notice_until = 0.0
                if self.state in ("RUNNING", "WAITING", "DONE", "FAIL"):
                    self._set_state("IDLE")
                log.info("分区模式已关闭(热切换), 回到全局模式")
        log.info("配置已热更新")

    def janitor(self):
        stale = float(self.cfg["service"].get("stale_session_secs", 600))
        while not self.stop_event.is_set():
            time.sleep(3)
            now = time.time()
            with self.lock:
                if self.partition_on and self.mgr is not None:
                    # 分区模式: DONE/FAIL 到期释放, 僵死任务直接释放,
                    # 演示任务到期走完整绿闪(全部由 PartitionManager.janitor 负责)
                    if self.mgr.janitor(now):
                        self._sync_global_state()
                    continue
                # 1) 通知信号到期 -> 自动解除(否则没人发事件时红灯会一直亮)
                if self.notice_until and now > self.notice_until:
                    self.notice_until = 0.0
                    log.info("Notification 信号到期, 自动解除等待态")
                    if not self.sessions and not self.waiting and self.state == "WAITING":
                        self._set_state("IDLE")
                # 2) 僵死会话清理
                dead = [s for s, t in self.sessions.items() if now - t > stale]
                for s in dead:
                    self.sessions.pop(s, None)
                    self.waiting.discard(s)
                if dead:
                    log.info("清理僵死会话: %s", [d[:8] for d in dead])
                    if not self.sessions and self.state in ("RUNNING", "WAITING"):
                        # 僵死会话不算"完成", 直接回平常, 避免误闪绿灯
                        self._set_state("IDLE")


class Renderer(threading.Thread):
    def __init__(self, svc):
        super().__init__(daemon=True)
        self.svc = svc

    def run(self):
        while not self.svc.stop_event.is_set():
            try:
                if self.svc.paused:
                    time.sleep(0.2)
                    continue
                if self.svc.partition_on and self.svc.mgr is not None:
                    self.render_partition()
                elif self.svc.state == "RUNNING":
                    self.render_running()
                elif self.svc.state == "DONE":
                    self.render_done()
                elif self.svc.state == "WAITING":
                    self.render_waiting()
                else:
                    time.sleep(0.2)
            except Exception as e:
                log.warning("renderer 异常: %s", e)
                time.sleep(1)

    def render_partition(self):
        """M4 分区渲染: 每帧合成一张 100 键全帧(6 排 + 溢出半区共享一帧缓冲,
        与全局模式同为 8 个 Feature 报表/帧)。无任务时停流(键盘回硬件灯效)。"""
        svc = self.svc
        mgr = svc.mgr
        pcfg = svc.cfg.get("partition", {})
        fps = max(float(pcfg.get("fps", 30)), 1.0)
        interval = 1.0 / fps
        while (not svc.stop_event.is_set() and not svc.paused
               and svc.partition_on and mgr is not None):
            if not mgr.has_active():
                svc._sync_global_state()      # 任务全部释放 -> IDLE -> 停流
                time.sleep(0.15)
                continue
            t0 = time.time()
            colors = mgr.compose_frame(t0)
            if not svc.kb_send(colors):
                time.sleep(0.5)
                continue
            svc._sync_global_state()
            d = t0 + interval - time.time()
            if d > 0:
                time.sleep(d)

    def _alive(self, epoch):
        svc = self.svc
        return (svc.state != "IDLE" and svc.epoch == epoch
                and not svc.paused and not svc.stop_event.is_set())

    def render_running(self):
        svc, cfg = self.svc, self.svc.cfg["running"]
        rgb = scale(hex_rgb(cfg["color"]), float(cfg.get("brightness", 1.0)))
        fps = max(float(cfg.get("fps", 30)), 1)
        interval = 1.0 / fps
        speed = float(cfg.get("speed", 40))
        mode = cfg.get("mode", "rainbow")
        epoch = svc.epoch
        t0 = time.time()
        while self._alive(epoch):
            t = time.time() - t0
            if mode == "rainbow":
                colors = rainbow_frame((t * speed) % 44 - 4)
            elif mode == "breathe":
                colors = breathe_frame(rgb, t)
            else:
                colors = flow_frame(rgb, (t * speed) % 44 - 4)
            if not svc.kb_send(colors):
                time.sleep(0.5)
                continue
            nxt = t0 + (int(t / interval) + 1) * interval
            d = nxt - time.time()
            if d > 0:
                time.sleep(d)

    def render_done(self):
        svc, cfg = self.svc, self.svc.cfg["done"]
        rgb = hex_rgb(cfg["color"])
        times = int(cfg.get("times", 3))
        on_s = float(cfg.get("on_ms", 200)) / 1000
        off_s = float(cfg.get("off_ms", 120)) / 1000
        epoch = svc.epoch
        for _ in range(times):
            if not self._alive(epoch):
                return
            if svc.kb_send(solid_colors(rgb)):
                time.sleep(on_s)
            else:
                time.sleep(0.3)
                continue
            if not self._alive(epoch):
                return
            if svc.kb_send(solid_colors((0, 0, 0))):
                time.sleep(off_s)
            else:
                time.sleep(0.3)
        with svc.lock:
            if svc.state == "DONE":
                svc._set_state("IDLE")
        log.info("完成闪烁结束, 回到 IDLE (键盘交还自带灯效)")

    def render_waiting(self):
        svc, cfg = self.svc, self.svc.cfg["waiting"]
        rgb = scale(hex_rgb(cfg["color"]), float(cfg.get("brightness", 1.0)))
        fps = max(float(cfg.get("fps", 30)), 1)
        interval = 1.0 / fps
        speed = float(cfg.get("speed", 40))
        mode = cfg.get("mode", "flow")
        epoch = svc.epoch
        t0 = time.time()
        while self._alive(epoch):
            t = time.time() - t0
            if mode == "rainbow":
                colors = rainbow_frame((t * speed) % 44 - 4)
            elif mode == "breathe":
                colors = breathe_frame(rgb, t)
            else:
                colors = flow_frame(rgb, (t * speed) % 44 - 4)
            if not svc.kb_send(colors):
                time.sleep(0.5)
                continue
            nxt = t0 + (int(t / interval) + 1) * interval
            d = nxt - time.time()
            if d > 0:
                time.sleep(d)


class Tailer(threading.Thread):
    """通道A: 尾随 hook-events.jsonl（用户现有 hook.sh 已写入的事件）"""

    def __init__(self, svc, path):
        super().__init__(daemon=True)
        self.svc = svc
        self.path = path

    def run(self):
        if not self.path:
            return
        log.info("尾随事件文件: %s", self.path)
        while not self.svc.stop_event.is_set():
            try:
                with open(self.path, "r", encoding="utf-8", errors="replace") as f:
                    f.seek(0, os.SEEK_END)
                    while not self.svc.stop_event.is_set():
                        line = f.readline()
                        if line:
                            try:
                                self.svc.handle_event(json.loads(line))
                            except Exception:
                                pass
                            continue
                        time.sleep(0.2)
                        try:
                            if os.path.getsize(self.path) < f.tell():
                                break  # 文件被截断/轮转, 重新打开
                        except OSError:
                            break
            except FileNotFoundError:
                time.sleep(2)
            except Exception as e:
                log.warning("tailer 异常: %s", e)
                time.sleep(2)


class Handler(BaseHTTPRequestHandler):
    service = None

    def do_POST(self):
        if self.path not in ("/event", "/test"):
            self.send_error(404)
            return
        try:
            n = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            payload = {}
        if self.path == "/event":
            self.service.handle_event(payload)
        else:
            # 手动触发某状态(设置界面的测试按钮 / 脚本化演示)
            action = str(payload.get("action") or "").upper()
            if action == "DEMO":
                # 注入演示任务: WB x2 + Codex x1(等确认), secs 秒后绿闪收尾
                self.service.inject_demo(
                    overflow=False, secs=min(120, max(3, float(payload.get("secs", 30)))))
                log.info("演示分区已启动")
                self._reply(b'{"ok":true}')
                return
            if action == "DEMO_OVERFLOW":
                # 溢出演示: 7 WB + 3 Codex -> 6 上排 + 两边溢出半区
                self.service.inject_demo(
                    overflow=True, secs=min(120, max(3, float(payload.get("secs", 30)))))
                log.info("演示溢出已启动")
                self._reply(b'{"ok":true}')
                return
            if action == "RESCAN":
                # 重新扫描会话记录, 重建当前任务/会话状态
                n = self.service.rescan_sessions()
                self._reply(json.dumps(
                    {"ok": True, "restored": n}).encode("utf-8"))
                return
            if action == "TOGGLE_PARTITION":
                # 分区/全局双模式切换
                on = self.service.toggle_partition()
                self.service.rescan_sessions()
                self._reply(json.dumps(
                    {"ok": True, "partition": on}).encode("utf-8"))
                return
            state = str(payload.get("state") or "RUNNING").upper()
            if state == "IDLE":
                self.service.clear_sessions()
            elif state == "UI":
                # 请求托盘弹出设置界面(供自检/脚本调用)
                cb = getattr(self.service, "on_ui_request", None)
                if cb:
                    cb()
            elif state in ("RUNNING", "WAITING", "DONE"):
                self.service.paused = False
                self.service.force_state(state)
            log.info("手动触发状态: %s", state)
        body = b"{}"
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _reply(self, body):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path != "/status":
            self.send_error(404)
            return
        svc = self.service
        body = json.dumps({
            "state": svc.state,
            "paused": svc.paused,
            "sessions": len(svc.sessions),
            "partition": svc.partition_on,
            "tasks": (svc.mgr.snapshot()
                      if (svc.partition_on and svc.mgr) else []),
            "frames_sent": svc.kb.frames_sent,
            "connected": svc.kb.dev is not None,
        }, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


class ServiceAlreadyRunning(Exception):
    """端口已被占用 = 已有一个实例在跑。Windows 上 SO_REUSEADDR 允许两个实例
    双重绑定同一端口, 会导致两边同时开灯、hook 事件被分摊, 所以必须显式拦截。"""


def _port_busy(port):
    try:
        s = socket.create_connection(("127.0.0.1", port), timeout=0.5)
        s.close()
        return True
    except OSError:
        return False


def start_service(cfg):
    """启动全部线程, 返回 Service(非阻塞) — 供托盘程序复用"""
    port = int(cfg["service"].get("port", 18990))
    if _port_busy(port):
        raise ServiceAlreadyRunning(port)
    svc = Service(cfg)
    Handler.service = svc
    svc.kb_ensure()
    if not svc.kb.dev:
        log.warning("键盘暂未连接, 将在事件到达时自动重试")

    Renderer(svc).start()
    threading.Thread(target=svc.janitor, daemon=True).start()
    if cfg["source"].get("tail_hook_events"):
        Tailer(svc, cfg["source"].get("hook_events_path")).start()
    if cfg.get("codex", {}).get("enabled", True):
        from codex_tail import CodexTailer
        CodexTailer(svc, cfg).start()

    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    svc.httpd = httpd
    log.info("wb-keylight 服务已启动: http://127.0.0.1:%d/event", port)
    return svc


def run_service(cfg):
    svc = start_service(cfg)
    log.info("状态: %s (config: %s)", svc.state, DEFAULT_CONFIG)
    try:
        while not svc.stop_event.is_set():
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        svc.stop_event.set()
        if svc.httpd:
            svc.httpd.shutdown()
        svc.kb.close()


def run_test(cfg, state, secs):
    """手动触发一个状态, 用于调试（不经过事件）"""
    svc = Service(cfg)
    if not svc.kb_ensure():
        log.error("键盘连接失败")
        sys.exit(2)
    svc._set_state(state)
    r = Renderer(svc)
    t0 = time.time()
    r.start()
    while time.time() - t0 < secs and r.is_alive():
        time.sleep(0.2)
    svc.stop_event.set()
    time.sleep(0.3)
    svc.kb.close()
    log.info("test 结束: state=%s secs=%s frames=%d", state, secs, svc.kb.frames_sent)


def main():
    ap = argparse.ArgumentParser(description="WorkBuddy -> RK98 键盘状态灯")
    ap.add_argument("cmd", choices=["run", "test"])
    ap.add_argument("--config", default=DEFAULT_CONFIG)
    ap.add_argument("--state", default="RUNNING",
                    choices=["IDLE", "RUNNING", "DONE", "WAITING"])
    ap.add_argument("--secs", type=float, default=6)
    ap.add_argument("--log-file", default=os.path.join(ROOT, "keylight.log"))
    args = ap.parse_args()

    handlers = [logging.StreamHandler()]
    if args.log_file:
        handlers.append(logging.FileHandler(args.log_file, encoding="utf-8"))
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s",
                        handlers=handlers)

    cfg = load_config(args.config)
    if args.cmd == "run":
        run_service(cfg)
    else:
        run_test(cfg, args.state, args.secs)


if __name__ == "__main__":
    main()
