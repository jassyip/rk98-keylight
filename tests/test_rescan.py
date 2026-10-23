# -*- coding: utf-8 -*-
"""重新扫描(rescan)与双模式切换的离线测试。

rescan v2 数据源 = WorkBuddy 会话记录 <projects>/*/*.jsonl
(文件名=sid, mtime=最后活动时间; hook-events.jsonl 只旁路记录 Stop/FinalStop,
没有建立事件, 重放重建不出任务 —— 2026-10-14 实测结论)。

运行(系统 Python 即可, 无需 hidapi):
  python tests/test_rescan.py
"""
import copy
import json
import os
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))

import keylight  # noqa: E402
from partition import PartitionManager  # noqa: E402


def make_env(tmpdir, sessions, hook_events=None):
    """构造测试环境:
    - sessions: [(sid, age_secs)] -> tmp/projects/<proj>/<sid>.jsonl
      (age_secs=文件"多久以前"最后活动, 用 os.utime 回拨)
    - hook_events: 写入 tmp/hook-events.jsonl 的行(可带 transcript_path
      指向上面的会话文件, 用于推导 projects 根目录 + sid->cwd 映射)
    返回 (cfg, projects_root)
    """
    root = os.path.join(tmpdir, "projects")
    now = time.time()
    for sid, age in sessions:
        proj = os.path.join(root, "proj-" + sid[:8])
        os.makedirs(proj, exist_ok=True)
        p = os.path.join(proj, sid + ".jsonl")
        with open(p, "w", encoding="utf-8") as f:
            f.write(json.dumps({"type": "session_meta", "sid": sid}) + "\n")
        t = now - age
        os.utime(p, (t, t))
    hpath = os.path.join(tmpdir, "hook-events.jsonl")
    with open(hpath, "w", encoding="utf-8") as f:
        for sid, age in sessions:
            proj = os.path.join(root, "proj-" + sid[:8])
            f.write(json.dumps({
                "session_id": sid,
                "hook_event_name": "Stop",
                "cwd": "D:\\work\\" + sid[:8],
                "transcript_path": os.path.join(proj, sid + ".jsonl"),
            }) + "\n")
    cfg = copy.deepcopy(keylight.DEFAULTS)
    cfg["partition"] = {"enabled": True}
    cfg["source"] = {"tail_hook_events": False,
                     "hook_events_path": hpath,
                     "projects_root": ""}
    return cfg, root, hpath


class TestRescanPartition(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="kl-rescan-")

    def test_rescan_restores_active_sessions(self):
        cfg, root, hpath = make_env(self.tmp, [("sid-a", 5), ("sid-b", 60)])
        svc = keylight.Service(cfg)
        n = svc.rescan_sessions()
        self.assertEqual(n, 2)
        self.assertEqual(set(svc.mgr.tasks), {"sid-a", "sid-b"})
        for t in svc.mgr.tasks.values():
            self.assertEqual(t.state, "RUNNING")
            self.assertIsNotNone(t.slot)          # 已分配排槽
        self.assertEqual(svc.mgr.tasks["sid-a"].label, "D:\\work\\sid-a")

    def test_rescan_skips_stale_sessions(self):
        """mtime 超过 stale_session_secs(600s) -> 僵死, 不恢复"""
        cfg, root, hpath = make_env(self.tmp, [("sid-live", 5), ("sid-dead", 3600)])
        svc = keylight.Service(cfg)
        n = svc.rescan_sessions()
        self.assertEqual(n, 1)
        self.assertEqual(set(svc.mgr.tasks), {"sid-live"})

    def test_rescan_no_projects_root(self):
        """hook 文件缺失 -> 推不出 projects 根 -> 恢复 0 个, 不崩"""
        cfg, root, hpath = make_env(self.tmp, [("sid-a", 5)])
        os.remove(hpath)
        svc = keylight.Service(cfg)
        self.assertEqual(svc.rescan_sessions(), 0)
        self.assertEqual(svc.mgr.tasks, {})

    def test_rescan_explicit_projects_root(self):
        """source.projects_root 显式指定时无需 hook 文件"""
        cfg, root, hpath = make_env(self.tmp, [("sid-a", 5)])
        os.remove(hpath)
        cfg["source"]["projects_root"] = root
        svc = keylight.Service(cfg)
        n = svc.rescan_sessions()
        self.assertEqual(n, 1)
        self.assertIn("sid-a", svc.mgr.tasks)

    def test_rescan_label_falls_back_to_projdir(self):
        """hook 无 cwd 的 sid -> label 用项目目录名"""
        cfg, root, hpath = make_env(self.tmp, [("sid-a", 5)])
        # 清掉 cwd 映射(重写 hook 文件, 只留 transcript_path)
        with open(hpath, "w", encoding="utf-8") as f:
            proj = os.path.join(root, "proj-sid-a")
            f.write(json.dumps({"session_id": "sid-a",
                                "transcript_path": os.path.join(proj, "sid-a.jsonl")}) + "\n")
        svc = keylight.Service(cfg)
        svc.rescan_sessions()
        self.assertEqual(svc.mgr.tasks["sid-a"].label, "proj-sid-a")

    def test_rescan_replaces_old_tasks(self):
        """重复扫描 -> 任务表整体重建(演示残留被清掉)"""
        cfg, root, hpath = make_env(self.tmp, [("sid-a", 5)])
        svc = keylight.Service(cfg)
        svc.mgr.signal("workbuddy", "demo-x", "START")     # 假装演示残留
        self.assertIn("demo-x", svc.mgr.tasks)
        svc.rescan_sessions()
        self.assertEqual(set(svc.mgr.tasks), {"sid-a"})

    # ---------- 多任务并发(2026-10-23 用户报障"四个任务只有一条灯条") ----------
    def test_four_sessions_get_distinct_slots(self):
        """4 个活跃会话 -> 各占一排, slot 必须是 0/1/2/3 且互不重复"""
        sids = ["sid-a", "sid-b", "sid-c", "sid-d"]
        cfg, root, hpath = make_env(self.tmp, [(s, 5) for s in sids])
        svc = keylight.Service(cfg)
        self.assertEqual(svc.rescan_sessions(), 4)
        slots = [svc.mgr.tasks[s].slot for s in sids]
        self.assertEqual(sorted(slots), [0, 1, 2, 3],
                         "会话槽位重复或缺失: %s" % dict(zip(sids, slots)))

    def test_finalstop_keeps_other_sessions(self):
        """A 会话 FinalStop 不得误杀 B/C — 即使它们已经 40 秒没事件。

        历史 bug: FinalStop 时 close_stale(now - final_grace_secs=30)
        把其它"沉默"会话一并清掉, 多任务并行时表现为只剩一条灯条。
        """
        cfg, root, hpath = make_env(self.tmp, [("sid-a", 5), ("sid-b", 5), ("sid-c", 5)])
        svc = keylight.Service(cfg)
        self.assertEqual(svc.rescan_sessions(), 3)
        now = time.time()
        # 模拟 B/C 正在长生成: 40 秒没有任何 hook 事件(> 旧的 30s 宽限)
        for tid in ("sid-b", "sid-c"):
            svc.mgr.tasks[tid].last_seen = now - 40
        svc._handle_partition({"session_id": "sid-a", "cwd": "D:\\work\\a"},
                              "FinalStop", "sid-a", now)
        self.assertIn("sid-b", svc.mgr.tasks, "FinalStop 误杀了他会话 B")
        self.assertIn("sid-c", svc.mgr.tasks, "FinalStop 误杀了他会话 C")
        self.assertEqual(svc.mgr.tasks["sid-b"].state, "RUNNING")
        self.assertEqual(svc.mgr.tasks["sid-c"].state, "RUNNING")
        # 本会话 A 正常进入完成态(绿闪后由 janitor 释放), 不影响他人
        self.assertEqual(svc.mgr.tasks["sid-a"].state, "DONE")

    def test_many_sessions_overflow_to_numpad(self):
        """超过 6 个会话 -> 多出的进溢出区(slot=None), 不占别人的排"""
        sids = ["sid-%d" % i for i in range(8)]
        cfg, root, hpath = make_env(self.tmp, [(s, 5) for s in sids])
        svc = keylight.Service(cfg)
        self.assertEqual(svc.rescan_sessions(), 8)
        slots = sorted(t.slot for t in svc.mgr.tasks.values() if t.slot is not None)
        self.assertEqual(slots, [0, 1, 2, 3, 4, 5])
        overflow = [t for t in svc.mgr.tasks.values() if t.slot is None]
        self.assertEqual(len(overflow), 2)


class TestRescanGlobal(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="kl-rescan-g-")

    def test_global_rescan_sessions(self):
        cfg, root, hpath = make_env(self.tmp, [("sid-a", 5), ("sid-b", 60)])
        cfg["partition"] = {"enabled": False}
        svc = keylight.Service(cfg)
        n = svc.rescan_sessions()
        self.assertEqual(n, 2)
        self.assertEqual(set(svc.sessions), {"sid-a", "sid-b"})
        self.assertEqual(svc.state, "RUNNING")

    def test_global_rescan_stale_only(self):
        cfg, root, hpath = make_env(self.tmp, [("sid-dead", 3600)])
        cfg["partition"] = {"enabled": False}
        svc = keylight.Service(cfg)
        svc.rescan_sessions()
        self.assertEqual(svc.sessions, {})
        self.assertEqual(svc.state, "IDLE")


class TestReplayLowLevel(unittest.TestCase):
    """PartitionManager.replay 低层语义(rescan 建任务复用)"""

    def test_replay_start_creates(self):
        m = PartitionManager({"partition": {}, "done": {"times": 3}})
        m.replay("workbuddy", "a", "START", {"label": "D:\\x"}, ts=1000.0)
        self.assertEqual(m.tasks["a"].state, "RUNNING")
        self.assertEqual(m.tasks["a"].last_seen, 1000.0)
        self.assertEqual(m.tasks["a"].label, "D:\\x")

    def test_replay_end_removes(self):
        """低层 END = 移除(无绿闪演出)"""
        m = PartitionManager({"partition": {}, "done": {"times": 3}})
        m.replay("workbuddy", "a", "START", ts=1000.0)
        m.replay("workbuddy", "a", "END", ts=1001.0)
        self.assertEqual(m.tasks, {})


class TestTogglePartition(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="kl-toggle-")

    def test_toggle_roundtrip(self):
        cfg, root, hpath = make_env(self.tmp, [("sid-a", 5)])
        svc = keylight.Service(cfg)
        self.assertTrue(svc.partition_on)
        on = svc.toggle_partition()
        self.assertFalse(on)
        self.assertIsNone(svc.mgr)
        on = svc.toggle_partition()
        self.assertTrue(on)
        self.assertIsNotNone(svc.mgr)

    def test_then_rescan_after_toggle(self):
        """切到全局后重扫 -> sessions; 切回分区后重扫 -> 任务"""
        cfg, root, hpath = make_env(self.tmp, [("sid-a", 5)])
        svc = keylight.Service(cfg)
        svc.toggle_partition()                        # -> 全局
        svc.rescan_sessions()
        self.assertEqual(set(svc.sessions), {"sid-a"})
        svc.toggle_partition()                        # -> 分区
        svc.rescan_sessions()
        self.assertEqual(set(svc.mgr.tasks), {"sid-a"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
