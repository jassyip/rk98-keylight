"""Codex 信号源 — 尾随 rollout JSONL(E3 实测定稿, 见 M4-DESIGN.md 5.2)

真实 schema(2026-10-14 抽样核实):
  顶层 {"timestamp","ordinal","type","payload"}
  type=session_meta -> payload.session_id / payload.cwd        = 任务建立
  type=event_msg    -> payload.type = task_started | item_completed
                                       | task_complete | turn_aborted
      item_completed -> payload.thread_id + payload.item.type
          UserMessage                                 = 新一轮用户输入 -> START
          CommandExecution/McpToolCall/AgentMessage/
          custom_tool_call                             = 有效工作步 -> HEARTBEAT
          Reasoning/Extension                          = 忽略(思考不是工作步)
      task_complete  -> END(payload.last_agent_message 可做标签)
      turn_aborted   -> FAIL
  其余类型(response_item/token_*/turn_context/world_state/...) -> 忽略

容错(设计 5.3):
  - torn 行: 只解析完整行, 残行留缓冲等下次读;
  - 新文件延迟 1s 再读(防半写 header);
  - 旧文件(服务启动前就结束的会话)只读尾部, 不补渲染;
  - .zst 压缩轮转: 原路径消失即关闭, 跳过 .zst 不解析;
  - --ephemeral 模式不落盘 -> 无信号(进度丢失, 至少不报错)。
"""
import glob
import json
import logging
import os
import threading
import time

log = logging.getLogger("keylight")

# item_completed 中计为"有效工作步"的 item 类型
STEP_ITEM_TYPES = {"CommandExecution", "McpToolCall", "AgentMessage",
                   "custom_tool_call", "WebSearch", "ImageGeneration"}


class CodexTailer(threading.Thread):
    def __init__(self, svc, cfg):
        super().__init__(daemon=True)
        self.svc = svc
        ccfg = cfg.get("codex", {})
        self.root = os.path.expanduser(ccfg.get("sessions_dir", "~/.codex/sessions"))
        self.poll = max(0.5, float(ccfg.get("poll_secs", 2)))
        self.stale = float(cfg.get("service", {}).get("stale_session_secs", 600))
        self.offsets = {}        # path -> 读偏移(-1 = 只读尾部, 尚未初始化)
        self.buffers = {}        # path -> 残行(torn line 缓冲)
        self.discovered = {}     # path -> 首次发现时间(延迟 1s 读)
        self.cur_thread = {}     # path -> 当前活跃 thread_id
        # 实测: task_complete/turn_aborted 的 payload 不带 thread_id(只有
        # turn_id), 直接派发会因 tid 为空被丢弃 -> 按文件记住最近见到的
        # thread_id(一个文件=一个会话), 完成信号落到当前活跃线程上。

    # ---------------- 主循环 ----------------
    def run(self):
        if not os.path.isdir(self.root):
            log.info("Codex sessions 目录不存在, 尾随未启动: %s", self.root)
            return
        log.info("Codex rollout 尾随启动: %s", self.root)
        while not self.svc.stop_event.is_set():
            try:
                self._scan()
            except Exception as e:
                log.warning("codex tailer 异常: %s", e)
            time.sleep(self.poll)

    def _scan(self):
        now = time.time()
        paths = set()
        for day in (0, 1, 2):    # 今天 + 昨天 + 前天(跨时区/午夜边界)
            d = time.strftime("%Y/%m/%d", time.localtime(now - day * 86400))
            paths.update(glob.glob(os.path.join(self.root, d, "rollout-*.jsonl")))
        for p in sorted(paths):
            if p.endswith(".zst"):           # 压缩文件不解析
                continue
            if p not in self.offsets:
                if p not in self.discovered:
                    self.discovered[p] = now
                    continue
                if now - self.discovered[p] < 1.0:   # 防半写 header
                    continue
                try:
                    mtime = os.path.getmtime(p)
                except OSError:
                    continue
                # 旧会话(服务启动前就结束的)不补渲染, 只跟新增
                self.offsets[p] = 0 if mtime > now - self.stale else -1
                log.info("Codex 会话文件%s: %s",
                         "(从头读)" if self.offsets[p] == 0 else "(只读尾部)",
                         os.path.basename(p))
            try:
                self._read(p)
            except (OSError, ValueError):
                continue
        # 已消失的文件(.zst 轮转 / 删除) -> 关闭跟踪
        for p in list(self.offsets):
            if p not in paths:
                self.offsets.pop(p, None)
                self.buffers.pop(p, None)
                self.cur_thread.pop(p, None)
                log.info("Codex 会话文件关闭: %s", os.path.basename(p))

    def _read(self, p):
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            if self.offsets[p] == -1:
                f.seek(0, os.SEEK_END)
                self.offsets[p] = f.tell()
                self.buffers.pop(p, None)
                return
            f.seek(self.offsets[p])
            data = f.read()
            if not data:
                return
            self.offsets[p] = f.tell()
            buf = self.buffers.get(p, "") + data
            parts = buf.split("\n")
            self.buffers[p] = parts.pop()     # 末段可能没写完, 留缓冲
            for line in parts:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                self._dispatch(d, p)

    # ---------------- 事件映射 ----------------
    def _dispatch(self, d, path=None):
        if not isinstance(d, dict):
            return
        t = d.get("type")
        pl = d.get("payload") if isinstance(d.get("payload"), dict) else {}

        if t == "session_meta":
            tid = str(pl.get("session_id") or pl.get("id") or "")
            if tid:
                if path:
                    self.cur_thread[path] = tid
                self.svc.apply_signal("codex", tid, "START",
                                      {"label": str(pl.get("cwd") or "")})
            return

        if t != "event_msg":
            return
        pt = pl.get("type")
        tid = str(pl.get("thread_id") or pl.get("session_id") or "")
        if tid:
            if path:
                self.cur_thread[path] = tid      # 记住最近活跃线程
        elif path:
            tid = self.cur_thread.get(path, "")  # task_complete 等无 thread_id
        if not tid:
            return

        if pt == "task_started":
            self.svc.apply_signal("codex", tid, "HEARTBEAT")
        elif pt == "item_completed":
            item = pl.get("item") if isinstance(pl.get("item"), dict) else {}
            it = str(item.get("type") or "")
            if it == "UserMessage":
                # 新一轮用户输入 -> 一个 turn = 一个任务(会话级文件多轮追加)
                self.svc.apply_signal("codex", tid, "START")
            elif it in STEP_ITEM_TYPES:
                self.svc.apply_signal("codex", tid, "HEARTBEAT")
        elif pt == "task_complete":
            self.svc.apply_signal("codex", tid, "END",
                                  {"label": str(pl.get("last_agent_message") or "")[:60]})
        elif pt == "turn_aborted":
            self.svc.apply_signal("codex", tid, "FAIL")
        # 其余 event_msg(token_count/thread_settings_applied/...) -> 忽略
