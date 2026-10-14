"""partition.py 离线单元测试 — 设备无读回通道, 这里是验证主力。

运行(系统 Python 即可, 无需 hidapi):
  python tests/test_partition.py
"""
import os
import sys
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(HERE), "src")
sys.path.insert(0, SRC)

from effects import hex_rgb, row_flow_map, row_phase, scale  # noqa: E402
from keymap import NUMPAD_LEFT, NUMPAD_RIGHT, ROWS, VPOSITIONS  # noqa: E402
from partition import PartitionManager  # noqa: E402

ZERO = (0, 0, 0)
CFG = {
    "partition": {"enabled": True, "fail_flash_secs": 0.3,
                  "waiting_on_ms": 150, "waiting_off_ms": 100,
                  "speed": 40, "fps": 30, "brightness": 0.8, "tail": 8.0},
    "sources": {"workbuddy": {"color": "#00E676"}, "codex": {"color": "#2979FF"}},
    "waiting": {"color": "#FF0000", "brightness": 0.8},
    "done": {"color": "#00FF00", "times": 3, "on_ms": 200, "off_ms": 120},
    "service": {"stale_session_secs": 600},
}


def mgr():
    return PartitionManager(CFG)


def nonzero_keys(colors):
    return {i for i, c in enumerate(colors) if c != ZERO}


class TestKeymapPartition(unittest.TestCase):
    def test_partition_constants(self):
        self.assertEqual([len(r) for r in ROWS], [19, 14, 14, 13, 13, 10])
        self.assertEqual((len(NUMPAD_LEFT), len(NUMPAD_RIGHT)), (10, 7))
        self.assertEqual(sorted(sum(ROWS, []) + NUMPAD_LEFT + NUMPAD_RIGHT),
                         list(range(100)))

    def test_flow_map_peak(self):
        keys = ROWS[0]
        phase = VPOSITIONS[keys[4]][0]          # 波前正对第 5 键
        m = row_flow_map(keys, (0, 255, 0), phase)
        self.assertEqual(m[keys[4]], (0, 255, 0))          # 波前处满色
        self.assertNotEqual(m[keys[1]], ZERO)              # 尾迹内半亮
        far = [k for k in keys if abs(VPOSITIONS[k][0] - phase) > 8]
        for k in far:
            self.assertEqual(m[k], ZERO)                   # 尾迹外全暗


class TestSlotAllocation(unittest.TestCase):
    def test_fifo_slots_and_reuse(self):
        m = mgr()
        m.signal("workbuddy", "a", "START")
        m.signal("workbuddy", "b", "START")
        m.signal("codex", "c", "START")
        self.assertEqual([m.tasks[k].slot for k in "abc"], [0, 1, 2])
        m.signal("workbuddy", "b", "CLOSE")                 # 中间槽释放
        m.signal("workbuddy", "d", "START")
        self.assertEqual(m.tasks["d"].slot, 1)              # 取最小空闲槽

    def test_slot_stable_no_reshuffle(self):
        m = mgr()
        for k in "abc":
            m.signal("workbuddy", k, "START")
        m.signal("workbuddy", "a", "CLOSE")                 # 不触发重排
        self.assertEqual(m.tasks["b"].slot, 1)
        self.assertEqual(m.tasks["c"].slot, 2)

    def test_overflow_by_source(self):
        m = mgr()
        for i in range(4):
            m.signal("workbuddy", "wb-%d" % i, "START")
        for i in range(3):
            m.signal("codex", "cx-%d" % i, "START")
        on_row = [t for t in m.tasks.values() if t.slot is not None]
        over_wb = [t for t in m.tasks.values() if t.slot is None and t.source == "workbuddy"]
        over_cx = [t for t in m.tasks.values() if t.slot is None and t.source == "codex"]
        self.assertEqual((len(on_row), len(over_wb), len(over_cx)), (6, 0, 1))


class TestFrameComposition(unittest.TestCase):
    def test_running_row_only(self):
        m = mgr()
        m.signal("workbuddy", "a", "START")
        colors = m.compose_frame(now=123.456)
        nz = nonzero_keys(colors)
        self.assertTrue(nz, "该排应有灯亮")
        self.assertTrue(nz <= set(ROWS[0]), "只有 slot0 排亮, 其余全暗")
        # 亮键颜色必须是 WB 绿的缩放: c == scale(full_rgb, k), 即同一 k 下逐通道符合
        full = hex_rgb("#00E676")
        for i in nz:
            c = colors[i]
            nz_ch = [ch for ch in range(3) if full[ch]]
            k = c[nz_ch[0]] / float(full[nz_ch[0]])
            for ch in nz_ch:
                self.assertEqual(c[ch], max(0, min(255, int(round(full[ch] * k)))))

    def test_progress_lights_only_left_part(self):
        m = mgr()
        m.signal("workbuddy", "a", "START", {"progress": (1, 4)})
        keys = sorted(ROWS[0], key=lambda i: VPOSITIONS[i][0])
        lit = int(round(len(keys) * 0.25))                  # 与渲染同公式: 5
        right_part = set(keys[lit:])
        left_part = set(keys[:lit])
        # 遍历多个相位: 未亮部分必须始终全暗
        for k in range(30):
            colors = m.compose_frame(now=1000.0 + k * 0.37)
            for i in right_part:
                self.assertEqual(colors[i], ZERO,
                                 "进度 1/4 时未亮部分不应亮 (phase=%.2f)" % k)
        # 已亮部分在某个相位必须亮过
        ever_lit = any(nonzero_keys(m.compose_frame(now=1000.0 + k * 0.37))
                       & left_part for k in range(30))
        self.assertTrue(ever_lit, "已亮部分应有流水经过")

    def test_waiting_flash_phases(self):
        m = mgr()
        m.signal("workbuddy", "a", "ASK")
        red = scale(hex_rgb("#FF0000"), 0.8)
        on_colors = m.compose_frame(now=0.0)                # 0 % 250 < 150 -> 亮
        off_colors = m.compose_frame(now=0.2)               # 200 % 250 >= 150 -> 灭
        for i in ROWS[0]:
            self.assertEqual(on_colors[i], red)
            self.assertEqual(off_colors[i], ZERO)

    def test_done_flash_and_release(self):
        m = mgr()
        m.signal("workbuddy", "a", "START")
        m.signal("workbuddy", "a", "END")
        t = m.tasks["a"]
        self.assertEqual(t.state, "DONE")
        base = t.done_since
        green = hex_rgb("#00FF00")
        colors = m.compose_frame(now=base + 0.01)           # 第 1 次亮
        for i in ROWS[0]:
            self.assertEqual(colors[i], green)
        colors = m.compose_frame(now=base + 0.21)           # 第 1 次灭
        self.assertEqual(nonzero_keys(colors), set())
        colors = m.compose_frame(now=base + 3 * 0.32 + 0.01)  # 闪完(3 次) -> 黑
        self.assertEqual(nonzero_keys(colors), set())
        self.assertTrue(m.janitor(now=base + 99))           # 到期释放
        self.assertNotIn("a", m.tasks)

    def test_fail_flash_and_auto_release(self):
        m = mgr()
        m.signal("codex", "x", "START")
        m.signal("codex", "x", "FAIL")
        self.assertEqual(m.tasks["x"].state, "FAIL")
        colors = m.compose_frame(now=0.0)
        for i in ROWS[0]:
            self.assertEqual(colors[i], scale(hex_rgb("#FF0000"), 0.8))
        time.sleep(0.6)                                     # > max(0.5, 0.3)
        self.assertTrue(m.janitor())
        self.assertNotIn("x", m.tasks)

    def test_overflow_pixels_count(self):
        m = mgr()
        for i in range(7):
            m.signal("workbuddy", "wb-%d" % i, "START")
        for i in range(3):
            m.signal("codex", "cx-%d" % i, "START")
        # 6 个上排(wb-1..6); WB 溢 1 颗(右半), Codex 溢 3 颗(左半)
        colors = m.compose_frame(now=1234.5)
        cx_lit = nonzero_keys(colors) & set(NUMPAD_LEFT)
        wb_lit = nonzero_keys(colors) & set(NUMPAD_RIGHT)
        self.assertLessEqual(len(cx_lit), 3)
        self.assertLessEqual(len(wb_lit), 1)
        # 上排的 6 个任务槽也都在渲染
        for s in range(6):
            self.assertTrue(nonzero_keys(m.compose_frame(now=1234.5)) & set(ROWS[s]))

    def test_stop_when_empty(self):
        m = mgr()
        m.signal("workbuddy", "a", "START")
        self.assertTrue(m.has_active())
        self.assertEqual(m.global_state(), "RUNNING")
        m.clear_all()
        self.assertFalse(m.has_active())
        self.assertEqual(m.global_state(), "IDLE")

    def test_global_state_priority(self):
        m = mgr()
        m.signal("workbuddy", "a", "START")
        m.signal("workbuddy", "b", "START")
        m.signal("workbuddy", "b", "ASK")                   # WAITING
        m.signal("workbuddy", "a", "END")                   # DONE
        self.assertEqual(m.global_state(), "WAITING")
        m.signal("workbuddy", "b", "CLOSE")
        self.assertEqual(m.global_state(), "DONE")          # WAITING 没了 -> DONE
        m.clear_all()

    def test_demo_injection(self):
        m = mgr()
        ids = m.inject_demo(secs=10)
        self.assertEqual(len(m.tasks), 3)
        self.assertEqual([m.tasks[i].slot for i in ids], [0, 1, 2])
        self.assertEqual(m.tasks["demo-codex-1"].state, "WAITING")
        self.assertEqual(m.tasks["demo-wb-1"].progress, (2, 4))
        m.clear_all()                                       # 演示按钮: 溢出演示先清场
        ids = m.inject_demo_overflow(secs=10)
        self.assertEqual(len(m.tasks), 10)
        self.assertEqual(len([t for t in m.tasks.values() if t.slot is None]), 4)

    def test_heartbeat_revives_from_waiting(self):
        m = mgr()
        m.signal("workbuddy", "a", "ASK")
        self.assertEqual(m.tasks["a"].state, "WAITING")
        m.signal("workbuddy", "a", "HEARTBEAT")             # 普通工具调用 = 已答复
        self.assertEqual(m.tasks["a"].state, "RUNNING")
        self.assertEqual(m.tasks["a"].steps, 1)

    def test_stale_release_not_counted_as_done(self):
        m = mgr()
        m.signal("workbuddy", "a", "START")
        m.tasks["a"].last_seen = time.time() - 601          # 超过 stale 600s
        self.assertTrue(m.janitor())
        self.assertEqual(m.tasks, {})                       # 直接释放, 无绿闪


class TestTransientNotice(unittest.TestCase):
    """Notification 脉冲: 回合结束提示 != 待确认, 不得变成"一直红闪"
    (2026-10-14 用户报障 bug: Notification 曾被映射为持久 ASK)"""

    def test_pulse_then_done_then_release(self):
        m = mgr()
        m.signal("workbuddy", "a", "START")
        m.signal("workbuddy", "a", "NOTICE")
        t = m.tasks["a"]
        self.assertEqual(t.state, "WAITING")
        self.assertGreater(t.wait_until, time.time())       # 有截止 = 瞬时
        # 截止前 janitor 不动作(脉冲期间持续红闪)
        self.assertFalse(m.janitor(now=t.wait_until - 0.5))
        self.assertEqual(m.tasks["a"].state, "WAITING")
        # 截止 -> DONE(绿闪), 再到期 -> 释放
        self.assertTrue(m.janitor(now=t.wait_until + 0.1))
        self.assertEqual(m.tasks["a"].state, "DONE")
        self.assertTrue(m.janitor(now=t.done_until + 0.1))
        self.assertNotIn("a", m.tasks)

    def test_heartbeat_clears_pulse(self):
        m = mgr()
        m.signal("workbuddy", "a", "START")
        m.signal("workbuddy", "a", "NOTICE")
        m.signal("workbuddy", "a", "HEARTBEAT")             # 回合还在继续
        t = m.tasks["a"]
        self.assertEqual(t.state, "RUNNING")
        self.assertEqual(t.wait_until, 0.0)
        self.assertFalse(m.janitor(now=time.time() + 30))   # 不会被脉冲残留拖死

    def test_notice_does_not_cut_ask_short(self):
        m = mgr()
        m.signal("workbuddy", "a", "ASK")                   # 持久等待(AskUserQuestion)
        t = m.tasks["a"]
        self.assertEqual((t.state, t.wait_until), ("WAITING", 0.0))
        m.signal("workbuddy", "a", "NOTICE")                # 脉冲不得覆盖持久等待
        self.assertEqual((t.state, t.wait_until), ("WAITING", 0.0))
        self.assertFalse(m.janitor(now=time.time() + 30))   # 30s 后仍在等

    def test_notice_on_done_ignored(self):
        m = mgr()
        m.signal("workbuddy", "a", "START")
        m.signal("workbuddy", "a", "END")                   # -> DONE 绿闪中
        m.signal("workbuddy", "a", "NOTICE")                # 不干预
        self.assertEqual(m.tasks["a"].state, "DONE")
        self.assertEqual(m.tasks["a"].wait_until, 0.0)

    def test_notice_unknown_task_ignored(self):
        m = mgr()
        m.signal("workbuddy", "ghost", "NOTICE")            # 无此任务 -> 忽略
        self.assertEqual(m.tasks, {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
