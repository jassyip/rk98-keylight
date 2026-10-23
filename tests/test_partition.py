"""partition.py 离线单元测试 — 设备无读回通道, 这里是验证主力。

运行(系统 Python 即可, 无需 hidapi):
  python tests/test_partition.py
"""
import copy
import math
import os
import sys
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(HERE), "src")
sys.path.insert(0, SRC)

from effects import (bfactor, flow_frame, hex_rgb, rainbow_frame,  # noqa: E402
                     row_breathe_map, row_flow_map, row_phase, scale,
                     sine_frame, solid_colors, stars_frame, stars_map,
                     wave_frame)
from effects import _falloff, _star_k, lit_row_wave_map, row_xrange  # noqa: E402
from keymap import NUMPAD_LEFT, NUMPAD_RIGHT, ROWS, VPOSITIONS  # noqa: E402
from partition import PartitionManager  # noqa: E402

ZERO = (0, 0, 0)
CFG = {
    "partition": {"enabled": True, "fail_flash_secs": 0.3,
                  "waiting_on_ms": 150, "waiting_off_ms": 100,
                  "effect": "flow", "breathe_period": 2.0,
                  "speed": 40, "fps": 30, "brightness": 80, "tail": 8.0},
    "sources": {"workbuddy": {"color": "#00E676"}, "codex": {"color": "#2979FF"}},
    "waiting": {"color": "#FF0000", "brightness": 80},
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


class TestBrightnessAndEffects(unittest.TestCase):
    """亮度量表迁移(0-100 百分比)与分区呼吸效果的离线验证。"""

    # ---------- bfactor 百分比换算 ----------
    def test_bfactor_percent_scale(self):
        self.assertEqual(bfactor(100), 1.0)
        self.assertEqual(bfactor(50), 0.5)
        self.assertEqual(bfactor(0), 0.0)
        self.assertEqual(bfactor(150), 1.0)                # 超 100 截断
        self.assertEqual(bfactor(-5), 0.0)                 # 负值截断

    def test_bfactor_invalid_falls_back(self):
        self.assertEqual(bfactor("abc"), 1.0)              # default=100
        self.assertEqual(bfactor(None), 1.0)
        self.assertEqual(bfactor("", default=80), 0.8)

    # ---------- row_breathe_map 呼吸包络 ----------
    def test_breathe_envelope(self):
        keys = ROWS[0]
        full = (0, 255, 0)
        half = scale(full, 0.5)
        m_half = row_breathe_map(keys, full, t=1.0)        # period=2.0 -> 半周期满色
        for i in keys:
            self.assertEqual(m_half[i], full)
        m_zero = row_breathe_map(keys, full, t=0.0)        # 零点最暗
        for i in keys:
            self.assertEqual(m_zero[i], ZERO)
        m_quarter = row_breathe_map(keys, full, t=0.5)     # 1/4 周期 ~ 半亮
        for i in keys:                                     # cos(π/2) 浮点误差, 容差 1
            for ch in range(3):
                self.assertAlmostEqual(m_quarter[i][ch], half[ch], delta=1)

    def test_breathe_lit_count_limits_keys(self):
        keys = sorted(ROWS[1], key=lambda i: VPOSITIONS[i][0])
        m = row_breathe_map(keys, (0, 255, 0), t=0.5, lit_count=3)
        self.assertEqual(set(m), set(keys[:3]))            # 只亮最左 3 格
        m_all = row_breathe_map(keys, (0, 255, 0), t=0.5, lit_count=99)
        self.assertEqual(set(m_all), set(keys))            # 超过排长 -> 整排

    # ---------- compose_frame 的 breathe 分支 ----------
    def test_breathe_effect_frame(self):
        cfg = copy.deepcopy(CFG)
        cfg["partition"]["effect"] = "breathe"
        cfg["partition"]["breathe_period"] = 2.0
        m = PartitionManager(cfg)
        m.signal("workbuddy", "a", "START")
        full = scale(hex_rgb("#00E676"), 0.8)               # brightness=80
        colors = m.compose_frame(now=1.0)                  # 半周期 -> 满色
        for i in ROWS[0]:
            self.assertEqual(colors[i], full)
        colors = m.compose_frame(now=0.0)                  # 零点 -> 全暗
        self.assertEqual(nonzero_keys(colors) & set(ROWS[0]), set())
        colors = m.compose_frame(now=0.5)                  # 1/4 -> 半亮
        for i in ROWS[0]:
            self.assertEqual(colors[i], scale(full, 0.5))

    def test_breathe_with_progress(self):
        cfg = copy.deepcopy(CFG)
        cfg["partition"]["effect"] = "breathe"
        m = PartitionManager(cfg)
        m.signal("workbuddy", "a", "START", {"progress": (1, 2)})
        keys = sorted(ROWS[0], key=lambda i: VPOSITIONS[i][0])
        lit = set(keys[:int(round(len(keys) * 0.5))])
        for k in range(20):                                # 任意时刻只亮进度格
            colors = m.compose_frame(now=0.05 * k)
            nz = nonzero_keys(colors) & set(ROWS[0])
            self.assertTrue(nz <= lit)

    # ---------- brightness=100 时峰值键=来源色满值 ----------
    def _phase_now(self, keys, target, speed=40.0, tail=8.0):
        """反解 row_phase: 求让 target 键恰好位于流水波前的 now。"""
        xs = [VPOSITIONS[i][0] for i in keys]
        xmin, xmax = min(xs), max(xs)
        span = float(xmax - xmin) + 2.0 * tail
        return ((VPOSITIONS[target][0] - (xmin - tail)) % span) / speed

    def test_brightness_100_peak_is_full_source_color(self):
        cfg = copy.deepcopy(CFG)
        cfg["partition"]["brightness"] = 100
        m = PartitionManager(cfg)
        m.signal("codex", "x", "START")
        keys = sorted(ROWS[0], key=lambda i: VPOSITIONS[i][0])
        target = keys[4]
        colors = m.compose_frame(now=self._phase_now(keys, target))
        self.assertEqual(colors[target], hex_rgb("#2979FF"),
                         "brightness=100 时峰值键应为来源色满值")

    def test_brightness_50_scales_peak(self):
        cfg = copy.deepcopy(CFG)
        cfg["partition"]["brightness"] = 50
        m = PartitionManager(cfg)
        m.signal("codex", "x", "START")
        keys = sorted(ROWS[0], key=lambda i: VPOSITIONS[i][0])
        target = keys[4]
        colors = m.compose_frame(now=self._phase_now(keys, target))
        self.assertEqual(colors[target], scale(hex_rgb("#2979FF"), 0.5),
                         "brightness=50 时峰值键应为来源色半亮")

    def test_waiting_brightness_percent(self):
        cfg = copy.deepcopy(CFG)
        cfg["waiting"]["brightness"] = 50
        m = PartitionManager(cfg)
        m.signal("workbuddy", "a", "ASK")
        red = scale(hex_rgb("#FF0000"), 0.5)
        on_colors = m.compose_frame(now=0.0)                # 0 % 250 < 150 -> 亮
        for i in ROWS[0]:
            self.assertEqual(on_colors[i], red)

    # ---------- _migrate_brightness 旧浮点迁移 ----------
    def test_migrate_brightness_legacy(self):
        import keylight
        old = {"running": {"brightness": 0.8},
               "waiting": {"brightness": 1.0},
               "partition": {"brightness": 0.5}}
        out = keylight._migrate_brightness(copy.deepcopy(old))
        self.assertEqual(out["running"]["brightness"], 80)
        self.assertEqual(out["waiting"]["brightness"], 100)
        self.assertEqual(out["partition"]["brightness"], 50)

    def test_migrate_brightness_new_untouched(self):
        import keylight
        new = {"running": {"brightness": 100},
               "waiting": {"brightness": 30},
               "partition": {"brightness": 80}}
        out = keylight._migrate_brightness(copy.deepcopy(new))
        self.assertEqual(out, new)                          # 新整数不动

    def test_defaults_brightness_is_100(self):
        import keylight
        for sec in ("running", "waiting", "partition"):
            self.assertEqual(keylight.DEFAULTS[sec]["brightness"], 100)


class TestFlowSmoothing(unittest.TestCase):
    """流水衰减曲线平滑化(参考 RK98 官方驱动 KB.ini 流水观感)的离线验证。

    RK 官方驱动(RK98-100KRGB, Dev/00C1/KB.ini LedOpt)的流水是软波前+平滑
    拖尾; 本项目早期用线性衰减 1-|x-phase|/tail, 波前硬切、相邻键等差跳变,
    观感生硬。2026-10-19 换成余弦平滑 _falloff = 0.5-0.5cos(pi*u)。
    """

    def test_falloff_endpoints_and_midpoint(self):
        self.assertEqual(_falloff(1.0), 1.0)                  # 波前满色
        self.assertEqual(_falloff(0.0), 0.0)                  # 尾迹外全暗
        self.assertAlmostEqual(_falloff(0.5), 0.5, delta=1e-9)  # 中点半亮
        self.assertAlmostEqual(_falloff(0.9), 0.5 + 0.5 * math.cos(0.1 * math.pi),
                               delta=1e-9)                    # ≈0.9755, 线性只有 0.9

    def test_falloff_monotonic(self):
        prev = -1.0
        for i in range(101):
            u = i / 100.0
            k = _falloff(u)
            self.assertGreaterEqual(k, prev - 1e-12)
            prev = k

    def test_falloff_soft_edges(self):
        # 平滑的意义: 两端变化率 < 中点(线性衰减全程等差)
        end_step = _falloff(1.0) - _falloff(0.99)             # 亮端每 1% 的变化
        mid_step = _falloff(0.50) - _falloff(0.49)            # 中点每 1% 的变化
        self.assertLess(end_step, mid_step)
        dark_step = _falloff(0.01) - _falloff(0.00)           # 暗端同样更缓
        self.assertLess(dark_step, mid_step)

    def test_flow_frame_column_cache_consistency(self):
        # 同列(x 相同)按键必须同色 —— 列缓存正确性
        colors = flow_frame((0, 200, 255), 12.0)
        by_x = {}
        for i, (x, _y) in enumerate(VPOSITIONS):
            by_x.setdefault(x, colors[i])
        for i, (x, _y) in enumerate(VPOSITIONS):
            self.assertEqual(colors[i], by_x[x])

    def test_flow_frame_smooth_no_hard_jump(self):
        # 相邻列亮度跳变 <= 20% (线性衰减在波前附近是每格 12.5% 等差,
        # 但波前内外沿是 100%->87.5% 的硬切; 余弦最大格间差 ≈ 0.197)
        colors = flow_frame((255, 255, 255), 12.0)
        xs = sorted({x for x, _ in VPOSITIONS})
        by_x = {}
        for i, (x, _y) in enumerate(VPOSITIONS):
            by_x[x] = colors[i][0]
        for a, b in zip(xs, xs[1:]):
            self.assertLessEqual(abs(by_x[a] - by_x[b]), 0.21 * 255)

    def test_row_flow_map_smooth_peak_and_tail(self):
        keys = ROWS[0]
        phase = VPOSITIONS[keys[4]][0]
        m = row_flow_map(keys, (0, 255, 0), phase)
        self.assertEqual(m[keys[4]], (0, 255, 0))             # 波前仍精确满色
        far = [k for k in keys if abs(VPOSITIONS[k][0] - phase) > 8]
        for k in far:
            self.assertEqual(m[k], ZERO)                      # 尾迹外仍精确全暗
        # 波前右侧(拖尾方向)相邻格平滑递减
        right = sorted([k for k in keys if 0 < VPOSITIONS[k][0] - phase <= 3],
                       key=lambda k: VPOSITIONS[k][0])
        vals = [m[k][1] for k in right]
        self.assertTrue(all(a >= b for a, b in zip(vals, vals[1:])), vals)

    def test_rainbow_frame_tail_off(self):
        colors = rainbow_frame(12.0)
        for i, (x, _y) in enumerate(VPOSITIONS):
            d = abs(((x - 12.0) % 44 + 22) % 44 - 22)
            if d > 8:
                self.assertEqual(colors[i], ZERO)             # 尾迹外全暗(含彩虹)


class TestOfficialEffects(unittest.TestCase):
    """RK 官方驱动效果移植(tc_led_mode 清单): 正弦光波/繁星点点/随波逐流/常亮"""

    FULL = hex_rgb("#2979FF")

    @staticmethod
    def _is_scale_of(c, base):
        """c 是否为 base 的等比缩放(含 ZERO; 容忍 round 误差 ±1)"""
        if c == ZERO:
            return True
        k = max(c) / max(base)
        return all(abs(ch - round(b * k)) <= 1 for ch, b in zip(c, base))

    # ---------- 正弦光波 ----------
    def test_sine_smooth_gradient(self):
        # 正弦波观感 = 全列连续渐变(非二值): 同一帧里亮度档位多,
        # 且相邻列跳变平缓(正弦关于波峰对称, 档位数天然折叠到 ~13)
        colors = sine_frame(self.FULL, 0.0, speed=30.0)
        levels = {c for c in colors if c != ZERO}
        self.assertGreaterEqual(len(levels), 12, "亮度档位太少, 不像正弦渐变")
        by_x = {}
        for i, (x, _y) in enumerate(VPOSITIONS):
            by_x[x] = max(colors[i]) / 255.0
        # 只比对真正相邻的列(b=a+1); 缺列(29/31/33/35 无键)处的间隔是布局
        # 固有, 不算跳变
        jumps = [abs(by_x[a + 1] - by_x[a]) for a in sorted(by_x) if a + 1 in by_x]
        self.assertLessEqual(max(jumps), 0.20, "相邻列跳变过大: %s" % jumps)

    def test_sine_bounded_and_travels(self):
        for t in (0.0, 0.37, 1.2):
            colors = sine_frame(self.FULL, t, speed=30.0)
            for c in colors:                       # 不亮过源色, 且是源色等比缩放
                self.assertTrue(all(a <= b for a, b in zip(c, self.FULL)))
                self.assertTrue(self._is_scale_of(c, self.FULL), c)
        self.assertNotEqual(sine_frame(self.FULL, 0.0, speed=30.0),
                            sine_frame(self.FULL, 0.5, speed=30.0))

    def test_sine_crest_is_full(self):
        # 波峰处亮度 = 源色满值
        colors = sine_frame(self.FULL, 0.0, speed=0.0, period=24.0)
        self.assertIn(self.FULL, colors)

    # ---------- 繁星点点 ----------
    def test_stars_deterministic(self):
        # 相位由键索引决定(无全局 RNG): 同一 t 帧可复现
        self.assertEqual(stars_frame(self.FULL, 1.23),
                         stars_frame(self.FULL, 1.23))

    def test_stars_twinkle_over_time(self):
        # 扫一段时间, 整体亮键数有明显起伏(星星在眨)
        lit_series = [sum(1 for c in stars_frame(self.FULL, t * 0.05)
                          if c != ZERO) for t in range(40)]
        self.assertGreater(max(lit_series), 10)
        self.assertLess(min(lit_series), 90)
        self.assertGreater(max(lit_series) - min(lit_series), 3)

    def test_star_k_range(self):
        for i in (0, 7, 33, 99):
            for t in (0.0, 0.31, 1.77):
                k = _star_k(i, t)
                self.assertGreaterEqual(k, 0.0)
                self.assertLessEqual(k, 1.0)

    def test_stars_map_lit_subset(self):
        keys = ROWS[0]
        lit = set(keys[:5])
        m = stars_map(keys, self.FULL, 1.0, lit=lit)
        for i in keys:
            if i in lit:                            # 亮格内: 源色等比缩放(含暗中)
                self.assertTrue(self._is_scale_of(m[i], self.FULL), m[i])
            else:                                   # 亮格外: 全暗
                self.assertEqual(m[i], ZERO)

    # ---------- 随波逐流 ----------
    def test_wave_rows_out_of_phase(self):
        # 同一时刻不同排的亮键分布不同(错峰), 且全帧 100 键
        colors = wave_frame(self.FULL, 1.0, speed=30.0)
        self.assertEqual(len(colors), 100)
        rows = [set(i for i in keys if colors[i] != ZERO) for keys in ROWS]
        self.assertTrue(any(rows[i] != rows[j]
                            for i in range(len(rows)) for j in range(i + 1, len(rows))),
                        "所有排同相位, 没有错峰")

    def test_wave_colors_are_source_scaled(self):
        colors = wave_frame(self.FULL, 0.7, speed=30.0)
        for c in colors:
            self.assertTrue(self._is_scale_of(c, self.FULL), c)

    def test_lit_row_wave_offset_shifts_phase(self):
        keys = ROWS[0]
        m0 = lit_row_wave_map(keys, self.FULL, 1.0, 30.0, 8.0, 0.0)
        m1 = lit_row_wave_map(keys, self.FULL, 1.0, 30.0, 8.0, 5.0)
        self.assertNotEqual(m0, m1)                            # 错峰量改变波形

    # ---------- 常亮 ----------
    def test_solid(self):
        self.assertEqual(solid_colors(self.FULL), [self.FULL] * 100)

    # ---------- 分区 compose 分支 ----------
    def test_partition_effect_stars(self):
        cfg = copy.deepcopy(CFG)
        cfg["partition"]["effect"] = "stars"
        m = PartitionManager(cfg)
        m.signal("workbuddy", "a", "START")
        colors = m.compose_frame(now=1.5)
        nz = nonzero_keys(colors) & set(ROWS[0])
        self.assertTrue(nz, "stars 效果下任务排应有亮键")

    def test_partition_effect_wave(self):
        # 波以 40 列/秒扫过各排(排宽不同, 到达时间不同) — 扫一段时间,
        # 两个任务排都应有各自被点亮的时刻
        cfg = copy.deepcopy(CFG)
        cfg["partition"]["effect"] = "wave"
        m = PartitionManager(cfg)
        m.signal("codex", "x", "START")
        m.signal("workbuddy", "y", "START")
        hit = {0: False, 1: False}
        for k in range(120):
            colors = m.compose_frame(now=k * 0.05)
            if nonzero_keys(colors) & set(ROWS[0]):
                hit[0] = True
            if nonzero_keys(colors) & set(ROWS[1]):
                hit[1] = True
        self.assertTrue(all(hit.values()), "wave 效果下应扫过两个任务排: %s" % hit)

    def test_partition_unknown_effect_falls_back(self):
        # 未知 effect 回落 flow 不炸
        cfg = copy.deepcopy(CFG)
        cfg["partition"]["effect"] = "nonexistent"
        m = PartitionManager(cfg)
        m.signal("workbuddy", "a", "START")
        colors = m.compose_frame(now=1.0)
        self.assertTrue(nonzero_keys(colors) & set(ROWS[0]))


class TestTimerResolution(unittest.TestCase):
    """Windows 定时器精度: timeBeginPeriod(1) 修复 sleep 粒度掉帧"""

    def test_raise_and_restore(self):
        import os
        if os.name != "nt":
            self.skipTest("非 Windows")
        from rk98 import (_timer_raised, raise_timer_resolution,
                          restore_timer_resolution)
        first = raise_timer_resolution()
        second = raise_timer_resolution()      # 幂等: 已提升则返回 False
        self.assertTrue(first)
        self.assertFalse(second)
        restore_timer_resolution()
        self.assertFalse(_timer_raised)


class TestMultiTaskRows(unittest.TestCase):
    """多任务并行: 每个任务独占一排, 且各排流水必须错峰。

    2026-10-23 用户报障"四个任务只有一条灯条在跑"——
    ① 任务被 FinalStop 误杀(见 test_rescan.test_finalstop_keeps_other_sessions);
    ② 即使四排都在亮, 若相位完全相同, 光带落在同一列, 视觉上仍像一条竖线。
    """

    def test_row_phase_offset_stays_in_span(self):
        """offset 必须在 span 内取模, 否则波前越界会让整排全黑"""
        keys = ROWS[0]
        xmin, xmax = row_xrange(keys)
        tail = 8.0
        span = (xmax - xmin) + 2 * tail
        for off in (0.0, 5.0, 30.0, 100.0, span * 3):
            p = row_phase(1.7, 30.0, keys, tail, off)
            self.assertGreaterEqual(p, xmin - tail - 1e-9)
            self.assertLess(p, xmin - tail + span + 1e-9)

    def test_four_tasks_occupy_four_rows(self):
        m = PartitionManager(copy.deepcopy(CFG))
        for i in range(4):
            m.signal("workbuddy", "sid-%d" % i, "START")
        self.assertEqual(sorted(t.slot for t in m.tasks.values()), [0, 1, 2, 3])

    def test_four_rows_lit_and_out_of_phase(self):
        """四排都能亮, 且存在四排同时亮但重心分散的时刻(相位错峰生效)"""
        m = PartitionManager(copy.deepcopy(CFG))
        for i in range(4):
            m.signal("workbuddy", "sid-%d" % i, "START")
        lit = {r: False for r in range(4)}
        spread = None
        for k in range(240):
            colors = m.compose_frame(now=k * 0.05)
            xs_by_row = []
            for r in range(4):
                xs = [VPOSITIONS[i][0] for i in ROWS[r] if colors[i] != ZERO]
                lit[r] = lit[r] or bool(xs)
                xs_by_row.append(xs)
            if all(xs_by_row):
                c = [sum(xs) / float(len(xs)) for xs in xs_by_row]
                d = max(c) - min(c)
                if spread is None or d > spread[0]:
                    spread = (d, c)
        for r, ok in lit.items():
            self.assertTrue(ok, "第 %d 排从未亮过(多任务应各亮一排)" % r)
        self.assertIsNotNone(spread, "从未出现四排同时亮的时刻")
        self.assertGreater(spread[0], 2.0,
                           "四排流水同相位(看起来像一条灯条): %s" % (spread[1],))


if __name__ == "__main__":
    unittest.main(verbosity=2)
