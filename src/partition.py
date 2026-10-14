"""M4 分区模式 — 任务 -> 键盘排槽位 的状态机与帧合成

编码规则(用户 2026-10-14 拍板): 两个正交维度
  颜色 = 来源  : WorkBuddy 绿(#00E676) / Codex 蓝(#2979FF)
  模式 = 状态  : 流水=运行 / 红闪=待确认(或失败) / 绿闪xN=完成

键位分区(来自 keymap.py 显式常量, E2 探针实测修正过推导 bug):
  主键区 6 排 = 6 个任务槽(19/14/14/13/13/10 键)
  numpad 17 键 = 溢出区: 左半 10 键 Codex / 右半 7 键 WorkBuddy

本模块是纯逻辑 + 纯帧合成(不碰 HID/线程/文件), 由 keylight.Service 驱动,
全部行为可离线单元测试(设备无读回通道, 这是验证主力, 见 M4-DESIGN.md 第 8 节)。
"""
import logging
import threading
import time

from effects import (bfactor, hex_rgb, lit_row_flow_map, row_breathe_map,  # noqa: E402
                     row_phase, scale)
from keymap import NUMPAD_LEFT, NUMPAD_RIGHT, ROWS

log = logging.getLogger("keylight")

SLOT_COUNT = len(ROWS)                      # 6 个任务槽
SOURCE_COLORS = {"workbuddy": "#00E676", "codex": "#2979FF"}
ZERO = (0, 0, 0)

# 溢出半区动画/全局态推导的优先级: 等确认 > 失败 > 完成 > 运行
PRIO = {"WAITING": 3, "FAIL": 2, "DONE": 1, "RUNNING": 0}

# 信号 kind(WorkBuddy hook 与 Codex rollout 都归一化成这一组)
#   START     任务建立/新一轮开始(已存在则恢复 RUNNING)
#   HEARTBEAT 心跳(刷新 last_seen, steps+1; 若在 WAITING 则视为已答复)
#   ASK       等你拍板(该任务红闪, 进度冻结)
#   ANSWER    显式解除等待(HEARTBEAT/START 已隐含此语义, 保留给显式调用)
#   PLAN      更新进度 (done, total) — 来自 TodoWrite / plan 事件
#   END       正常收工 -> DONE 绿闪, 播完释放
#   FAIL      失败 -> 红闪, fail_flash_secs 后释放
#   CLOSE     会话关闭 -> 直接释放, 不闪绿


class TaskRec:
    __slots__ = ("source", "task_id", "slot", "state", "progress", "steps",
                 "label", "last_seen", "done_since", "done_until",
                 "fail_until", "wait_until", "demo_until")

    def __init__(self, source, task_id, slot, label, now, state="RUNNING"):
        self.source = source
        self.task_id = task_id
        self.slot = slot            # 0..5 = 主键区排; None = 溢出区
        self.state = state          # RUNNING / WAITING / DONE / FAIL
        self.progress = None        # (done_steps, total_steps) 或 None
        self.steps = 0              # 活动度步数(无 plan 时的填充参考)
        self.label = label or ""    # cwd/首条消息, 仅面板显示
        self.last_seen = now
        self.done_since = 0.0       # DONE 绿闪相位基准
        self.done_until = 0.0       # DONE 播放截止(到期由 janitor 释放)
        self.fail_until = 0.0       # FAIL 红闪截止
        self.wait_until = 0.0       # 瞬时 WAITING(Notification 脉冲)截止; 0 = 持久(ASK)
        self.demo_until = 0.0       # 演示任务到期时刻(0 = 非演示)


class PartitionManager:
    """任务表 + 槽位分配 + 帧合成。线程安全(内部 RLock)。"""

    def __init__(self, cfg):
        self.cfg = cfg
        self.tasks = {}             # task_id -> TaskRec
        self.lock = threading.RLock()

    # ---------------- 信号归一化 ----------------
    def signal(self, source, task_id, kind, extra=None):
        """唯一信号入口: WB hook / Codex rollout / 演示注入 都走这里"""
        extra = extra or {}
        now = time.time()
        with self.lock:
            t = self.tasks.get(task_id)

            if kind == "CLOSE":
                if t is not None:
                    self.tasks.pop(task_id, None)
                return

            if kind == "NOTICE":
                # Notification(回合结束/空闲等你输入) != "待确认"。
                # 只给一个短时红闪脉冲(notice_secs), 到期由 janitor 转 DONE
                # 绿闪释放 —— 修复"没有待确认状态却一直红闪"的 bug
                # (2026-10-14 用户报障: Notification 曾被映射成持久 ASK)。
                # 不覆盖持久 ASK 等待; DONE/FAIL 不干预。
                if t is None:
                    return
                if t.state == "WAITING" and not t.wait_until:
                    return                      # 正在持久等你拍板, 不打断
                if t.state not in ("RUNNING", "WAITING"):
                    return
                secs = float(self.cfg.get("service", {}).get("notice_secs", 20))
                t.state = "WAITING"
                t.wait_until = now + max(1.0, secs)
                return

            if kind in ("START", "HEARTBEAT", "ASK"):
                if t is None:
                    t = self._create(source, task_id, now,
                                     label=str(extra.get("label") or ""),
                                     state="WAITING" if kind == "ASK" else "RUNNING")
                else:
                    t.last_seen = now
                    t.source = source
                    if kind == "ASK":
                        t.state = "WAITING"
                        t.wait_until = 0.0        # ASK = 持久等待, 无截止
                    elif t.state in ("WAITING", "DONE"):
                        # 新提交/普通工具调用 = 提问已被答复 / 重新开工
                        t.state = "RUNNING"
                        t.done_since = t.done_until = 0.0
                        t.wait_until = 0.0
                if kind == "HEARTBEAT":
                    t.steps += max(1, int(extra.get("steps_delta", 1)))
                if extra.get("progress"):
                    try:
                        t.progress = (int(extra["progress"][0]),
                                      int(extra["progress"][1]))
                    except (TypeError, ValueError, IndexError):
                        pass
                if extra.get("label") and not t.label:
                    t.label = str(extra["label"])
                return

            if kind == "PLAN":
                if t is not None and extra.get("progress"):
                    try:
                        t.progress = (int(extra["progress"][0]),
                                      int(extra["progress"][1]))
                        t.last_seen = now
                    except (TypeError, ValueError, IndexError):
                        pass
                return

            if kind == "ANSWER":
                if t is not None and t.state == "WAITING":
                    t.state = "RUNNING"
                    t.wait_until = 0.0
                    t.last_seen = now
                return

            if kind == "END":
                if t is not None and t.state in ("RUNNING", "WAITING"):
                    self._start_done(t, now)
                return

            if kind == "FAIL":
                if t is None:
                    return
                secs = float(extra.get("fail_secs")
                             or self.cfg.get("partition", {}).get("fail_flash_secs", 30))
                t.state = "FAIL"
                t.fail_until = now + max(0.5, secs)
                return

            log.warning("未知信号 kind=%r (source=%s task=%s)", kind, source, task_id)

    def replay(self, source, task_id, kind, extra=None, ts=None):
        """重放历史事件(重新扫描/状态重建用)。

        与 signal() 的差异:
        - last_seen 用历史时间戳 ts(保留活性判断, 僵死清理才准);
        - END/CLOSE 直接移除任务, 不触发 DONE 绿闪 —— 重放是状态重建,
          不应该有灯光演出(用户报障后补的功能: 2026-10-14)。
        """
        extra = extra or {}
        ts = time.time() if ts is None else ts
        with self.lock:
            t = self.tasks.get(task_id)

            if kind == "CLOSE":
                self.tasks.pop(task_id, None)
                return

            if kind == "END":
                # 重放中的"已收工" = 该会话结束, 直接移除(不闪绿)
                self.tasks.pop(task_id, None)
                return

            if kind in ("START", "HEARTBEAT", "ASK"):
                if t is None:
                    t = self._create(source, task_id, ts,
                                     label=str(extra.get("label") or ""),
                                     state="WAITING" if kind == "ASK" else "RUNNING")
                else:
                    t.last_seen = ts
                    t.source = source
                    if kind == "ASK":
                        t.state = "WAITING"
                        t.wait_until = 0.0
                    elif t.state in ("WAITING", "DONE"):
                        t.state = "RUNNING"
                        t.done_since = t.done_until = 0.0
                        t.wait_until = 0.0
                if kind == "HEARTBEAT":
                    t.steps += max(1, int(extra.get("steps_delta", 1)))
                if extra.get("progress"):
                    try:
                        t.progress = (int(extra["progress"][0]),
                                      int(extra["progress"][1]))
                    except (TypeError, ValueError, IndexError):
                        pass
                if extra.get("label") and not t.label:
                    t.label = str(extra["label"])
                return

            if kind == "PLAN":
                if t is not None and extra.get("progress"):
                    try:
                        t.progress = (int(extra["progress"][0]),
                                      int(extra["progress"][1]))
                        t.last_seen = ts
                    except (TypeError, ValueError, IndexError):
                        pass
                return

            # NOTICE(瞬时脉冲) 等其余 kind 在重放中无意义, 跳过

    def _create(self, source, task_id, now, label="", state="RUNNING"):
        """建任务并分配槽位: 取最小空闲槽; 无空闲槽则进溢出区(slot=None)"""
        slot = self._free_slot()
        t = TaskRec(source, task_id, slot, label, now, state)
        self.tasks[task_id] = t
        log.info("任务上%s: %s %s slot=%s",
                 "排" if slot is not None else "溢出区", source, task_id[:12], slot)
        return t

    def _free_slot(self):
        used = {t.slot for t in self.tasks.values() if t.slot is not None}
        for s in range(SLOT_COUNT):
            if s not in used:
                return s
        return None

    def _start_done(self, t, now):
        d = self.cfg.get("done", {})
        times = max(1, int(d.get("times", 3)))
        on = max(1.0, float(d.get("on_ms", 200)))
        off = max(1.0, float(d.get("off_ms", 120)))
        t.state = "DONE"
        t.done_since = now
        t.done_until = now + times * (on + off) / 1000.0 + 0.05

    def close_stale(self, source, cutoff):
        """清理 source 下 last_seen < cutoff 的僵死任务(FinalStop 用)"""
        with self.lock:
            dead = [tid for tid, t in self.tasks.items()
                    if t.source == source and t.last_seen < cutoff]
            for tid in dead:
                self.tasks.pop(tid, None)
        return dead

    # ---------------- 帧合成 ----------------
    def compose_frame(self, now=None):
        """合成 100 键全帧颜色数组(顺序对应 keymap.VKEYS)。纯函数, 可离线断言。"""
        now = time.time() if now is None else now
        pcfg = self.cfg.get("partition", {})
        colors = [ZERO] * 100
        with self.lock:
            for t in list(self.tasks.values()):
                if t.slot is None:
                    continue
                self._paint(colors, ROWS[t.slot], t, now, pcfg)
            # 溢出半区: 颗数 = 溢出任务数; 动画取半区内最高优先级任务
            for source, keys in (("codex", NUMPAD_LEFT), ("workbuddy", NUMPAD_RIGHT)):
                group = [t for t in self.tasks.values()
                         if t.slot is None and t.source == source]
                if not group:
                    continue
                top = max(group, key=lambda t: PRIO.get(t.state, 0))
                lit = keys[:min(len(group), len(keys))]
                self._paint(colors, lit, top, now, pcfg)
        return colors

    def _paint(self, colors, keys, t, now, pcfg):
        if t.state in ("WAITING", "FAIL"):
            # 红闪: 整排同相位(绝对时间相位天然同步所有等待排)
            w = self.cfg.get("waiting", {})
            rgb = scale(hex_rgb(w.get("color", "#FF0000")),
                        bfactor(w.get("brightness", 100)))
            on_ms = max(1.0, float(pcfg.get("waiting_on_ms", 150)))
            off_ms = max(1.0, float(pcfg.get("waiting_off_ms", 100)))
            if (now * 1000.0) % (on_ms + off_ms) < on_ms:
                for i in keys:
                    colors[i] = rgb
            return

        if t.state == "DONE":
            d = self.cfg.get("done", {})
            rgb = hex_rgb(d.get("color", "#00FF00"))
            times = max(1, int(d.get("times", 3)))
            on = max(1.0, float(d.get("on_ms", 200)))
            off = max(1.0, float(d.get("off_ms", 120)))
            cyc = on + off
            el = (now - t.done_since) * 1000.0
            idx = int(el // cyc)
            if idx >= times:
                return                       # 闪完, 黑屏等 janitor 释放
            if el % cyc < on:
                for i in keys:
                    colors[i] = rgb
            return

        # RUNNING: 来源色排内动画(有进度则只亮已完成的格)
        # effect=flow 流水(波前扫描) / breathe 呼吸(整排渐明渐暗), 面板可切
        src = self.cfg.get("sources", {}).get(t.source, {})
        rgb = scale(hex_rgb(src.get("color", SOURCE_COLORS.get(t.source, "#FFFFFF"))),
                    bfactor(pcfg.get("brightness", 100)))
        speed = float(pcfg.get("speed", 40))
        tail = float(pcfg.get("tail", 8.0))
        lit = None
        if t.progress and t.progress[1] > 0:
            frac = max(0.0, min(1.0, t.progress[0] / float(t.progress[1])))
            lit = int(round(len(keys) * frac))
        if str(pcfg.get("effect", "flow")) == "breathe":
            m = row_breathe_map(keys, rgb, now,
                                float(pcfg.get("breathe_period", 2.0)), lit)
        else:
            phase = row_phase(now, speed, keys, tail)
            m = lit_row_flow_map(keys, rgb, phase, tail, lit)
        for i, c in m.items():
            if c != ZERO:
                colors[i] = c

    # ---------------- 生命周期维护 ----------------
    def janitor(self, now=None):
        """释放到期的 DONE/FAIL、僵死任务; 演示任务到期走完整绿闪。返回是否有变化。"""
        now = time.time() if now is None else now
        changed = False
        with self.lock:
            stale = float(self.cfg.get("service", {}).get("stale_session_secs", 600))
            for tid, t in list(self.tasks.items()):
                if t.demo_until and now > t.demo_until and t.state in ("RUNNING", "WAITING"):
                    self._start_done(t, now)      # 演示到期 -> 绿闪后自然释放
                    changed = True
                    continue
                if (t.state == "WAITING" and t.wait_until
                        and now >= t.wait_until):
                    # Notification 脉冲到期且无后续事件 = 回合已结束
                    # -> 转 DONE 绿闪后释放(不再一直红闪)
                    self._start_done(t, now)
                    changed = True
                    continue
                if t.state == "DONE" and now >= t.done_until:
                    self.tasks.pop(tid, None)
                    changed = True
                elif t.state == "FAIL" and now >= t.fail_until:
                    self.tasks.pop(tid, None)
                    changed = True
                elif t.state in ("RUNNING", "WAITING") and now - t.last_seen > stale:
                    self.tasks.pop(tid, None)     # 僵死: 直接释放, 不算完成
                    changed = True
        return changed

    def has_active(self):
        with self.lock:
            return bool(self.tasks)

    def global_state(self):
        """推导全局态(托盘图标/状态栏用)。优先级: WAITING > FAIL > RUNNING > DONE"""
        with self.lock:
            if not self.tasks:
                return "IDLE"
            states = {t.state for t in self.tasks.values()}
            for s in ("WAITING", "FAIL", "RUNNING", "DONE"):
                if s in states:
                    return s
            return "IDLE"

    def snapshot(self):
        """任务映射表数据(排号/来源/状态/进度/步数/标签) — 键盘上不可见的文字身份"""
        with self.lock:
            rows = []
            for t in sorted(self.tasks.values(),
                            key=lambda t: (t.slot if t.slot is not None else 9, t.task_id)):
                rows.append({
                    "slot": t.slot,
                    "source": t.source,
                    "state": t.state,
                    "progress": list(t.progress) if t.progress else None,
                    "steps": t.steps,
                    "label": t.label,
                    "overflow": t.slot is None,
                })
            return rows

    def clear_all(self):
        with self.lock:
            n = len(self.tasks)
            self.tasks.clear()
        return n

    # ---------------- 演示(不依赖真实 agent 的观感验证) ----------------
    def inject_demo(self, secs=30):
        """WB x2(一个带 50% 进度) + Codex x1(等确认红闪), secs 秒后自动绿闪收尾"""
        now = time.time()
        deadline = now + max(3.0, float(secs))
        with self.lock:
            t1 = self._create("workbuddy", "demo-wb-1", now, label="演示 · WB 任务 1(带进度)")
            t1.progress = (2, 4)
            t2 = self._create("workbuddy", "demo-wb-2", now, label="演示 · WB 任务 2")
            t3 = self._create("codex", "demo-codex-1", now,
                              label="演示 · Codex 任务(等确认)", state="WAITING")
            for t in (t1, t2, t3):
                t.demo_until = deadline
                t.last_seen = now
        return ["demo-wb-1", "demo-wb-2", "demo-codex-1"]

    def inject_demo_overflow(self, secs=30):
        """7 WB + 3 Codex: 6 个上排, WB 溢 1 / Codex 溢 2 — 演示溢出半区"""
        now = time.time()
        deadline = now + max(3.0, float(secs))
        ids = []
        with self.lock:
            for i in range(7):
                tid = "demo-wb-%d" % (i + 1)
                self._create("workbuddy", tid, now, label="演示溢出 · WB %d" % (i + 1))
                ids.append(tid)
            for i in range(3):
                tid = "demo-cx-%d" % (i + 1)
                self._create("codex", tid, now, label="演示溢出 · Codex %d" % (i + 1))
                ids.append(tid)
            for tid in ids:
                self.tasks[tid].demo_until = deadline
                self.tasks[tid].last_seen = now
        return ids
