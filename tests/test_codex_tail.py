"""codex_tail.py 离线测试 — 用真实 rollout 样本验证事件映射(不起线程不碰键盘)

运行:
  python tests/test_codex_tail.py
"""
import glob
import json
import os
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(HERE), "src")
sys.path.insert(0, SRC)

from codex_tail import CodexTailer  # noqa: E402

CFG = {"codex": {"sessions_dir": "~/.codex/sessions", "poll_secs": 2},
       "service": {"stale_session_secs": 600}}


class FakeSvc:
    """记录 apply_signal 调用; stop_event 供 CodexTailer 构造用"""

    def __init__(self):
        self.calls = []
        self.stop_event = _Event()

    def apply_signal(self, source, task_id, kind, extra=None):
        self.calls.append((source, task_id[:8], kind))


class _Event:
    def is_set(self):
        return False


def make_tailer(svc, root):
    cfg = dict(CFG)
    cfg["codex"] = dict(CFG["codex"], sessions_dir=root)
    t = CodexTailer.__new__(CodexTailer)          # 不跑线程, 只测解析逻辑
    CodexTailer.__init__(t, svc, cfg)
    return t


class TestRealSampleMapping(unittest.TestCase):
    """对真实 rollout 文件逐行派发, 断言映射正确(存在性 × 映射分离)"""

    def test_real_rollout_dispatch(self):
        files = sorted(glob.glob(os.path.expanduser(
            "~/.codex/sessions/2026/*/*/rollout-*.jsonl")),
            key=os.path.getmtime)[-3:]                   # 最近 3 个样本
        self.assertTrue(files, "本机应有 codex rollout 样本")
        svc = FakeSvc()
        t = make_tailer(svc, "~/.codex/sessions")
        # 先统计原始文件里各事件的实际行数
        raw = {"session_meta": 0, "user_msg": 0, "step_item": 0,
               "task_started": 0, "task_complete": 0, "turn_aborted": 0}
        for p in files:
            with open(p, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        d = json.loads(line)
                    except Exception:
                        continue
                    if d.get("type") == "session_meta":
                        raw["session_meta"] += 1
                        t._dispatch(d, p)
                        continue
                    if d.get("type") != "event_msg":
                        continue
                    pl = d.get("payload") or {}
                    pt = pl.get("type")
                    if pt == "item_completed":
                        it = str((pl.get("item") or {}).get("type") or "")
                        if it == "UserMessage":
                            raw["user_msg"] += 1
                        elif it in ("CommandExecution", "McpToolCall",
                                    "AgentMessage", "custom_tool_call"):
                            raw["step_item"] += 1
                    elif pt == "task_complete":
                        raw["task_complete"] += 1
                    elif pt == "turn_aborted":
                        raw["turn_aborted"] += 1
                    elif pt == "task_started":
                        raw["task_started"] += 1
                    t._dispatch(d, p)
        got = {}
        for _, _, kind in svc.calls:
            got[kind] = got.get(kind, 0) + 1
        print("  真实样本: 原始事件 %s" % raw)
        print("  真实样本: 派发统计 %s" % got)
        self.assertTrue(all(c[0] == "codex" for c in svc.calls))
        # 映射严格性: 每个该派发的事件都派发了, 且一个不多
        self.assertEqual(got.get("START", 0), raw["session_meta"] + raw["user_msg"])
        self.assertEqual(got.get("HEARTBEAT", 0), raw["step_item"] + raw["task_started"])
        self.assertEqual(got.get("END", 0), raw["task_complete"])
        self.assertEqual(got.get("FAIL", 0), raw["turn_aborted"])
        # 覆盖性: 样本里至少要有 START/HEARTBEAT/END(否则断言是空转)
        self.assertGreaterEqual(raw["session_meta"] + raw["user_msg"], 1)
        self.assertGreaterEqual(raw["step_item"], 1)
        self.assertGreaterEqual(raw["task_complete"], 1)


class TestSyntheticMapping(unittest.TestCase):
    def test_turn_aborted_maps_to_fail(self):
        svc = FakeSvc()
        t = make_tailer(svc, "~/.codex/sessions")
        t._dispatch({"type": "event_msg",
                     "payload": {"type": "turn_aborted", "thread_id": "abc123"}})
        self.assertEqual(svc.calls, [("codex", "abc123", "FAIL")])

    def test_ignored_types(self):
        svc = FakeSvc()
        t = make_tailer(svc, "~/.codex/sessions")
        for d in ({"type": "response_item", "payload": {"type": "message"}},
                  {"type": "token_usage_record", "payload": {}},
                  {"type": "event_msg", "payload": {"type": "token_count"}},
                  {"type": "turn_context", "payload": {}}):
            t._dispatch(d)
        self.assertEqual(svc.calls, [])

    def test_torn_line_buffered(self):
        svc = FakeSvc()
        t = make_tailer(svc, "~/.codex/sessions")
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "rollout-x.jsonl")
            line1 = json.dumps({"type": "event_msg", "payload": {
                "type": "task_complete", "thread_id": "tid-1"}})
            with open(p, "w", encoding="utf-8") as f:
                f.write(line1[:20])                     # 半个 JSON 行(torn)
            t.offsets[p] = 0
            t._read(p)
            self.assertEqual(svc.calls, [])             # 残行不派发
            with open(p, "a", encoding="utf-8") as f:
                f.write(line1[20:] + "\n")              # 补全
            t._read(p)
            self.assertEqual(svc.calls, [("codex", "tid-1", "END")])
            t._read(p)                                  # 无新增 -> 不重复
            self.assertEqual(len(svc.calls), 1)

    def test_old_file_reads_tail_only(self):
        """服务启动前就结束的旧会话: 只读尾部, 不补渲染历史"""
        svc = FakeSvc()
        t = make_tailer(svc, "~/.codex/sessions")
        with tempfile.TemporaryDirectory() as d:
            root = os.path.join(d, "sessions")
            day = time.strftime("%Y/%m/%d")
            os.makedirs(os.path.join(root, day))
            p = os.path.join(root, day, "rollout-old.jsonl")
            with open(p, "w", encoding="utf-8") as f:
                f.write(json.dumps({"type": "session_meta", "payload": {
                    "session_id": "old-1", "cwd": "D:\\\\x"}}) + "\n")
            old = time.time() - 3600
            os.utime(p, (old, old))                     # mtime 1 小时前
            t.root = root
            t._scan()                                   # 第一轮: 记录发现时间
            self.assertNotIn(p, t.offsets)
            time.sleep(1.1)                             # 等满防半写延迟 1s
            t._scan()                                   # 第二轮: 发现是旧文件 -> 只读尾部
            # 同一轮内即初始化 offset=EOF(文件大小), 历史不派发
            self.assertEqual(t.offsets.get(p), os.path.getsize(p))
            self.assertEqual(svc.calls, [])             # 历史不补渲染
            with open(p, "a", encoding="utf-8") as f:   # 追加新事件
                f.write(json.dumps({"type": "event_msg", "payload": {
                    "type": "task_complete", "thread_id": "old-1"}}) + "\n")
            t._scan()
            self.assertEqual(svc.calls, [("codex", "old-1", "END")])


if __name__ == "__main__":
    unittest.main(verbosity=2)
