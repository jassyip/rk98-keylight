"""hook.py 回归测试 — 重点是"不许挂住"(Hook timed out after 3000ms 的根因)。

运行(系统 Python 即可, 无需 hidapi):
  python tests/test_hook.py

背景: 旧版用无界的 sys.stdin.buffer.read1() 读事件。管道已连但对方还没写数据时
它会一直阻塞, 于是 git 式"3 秒预算"被打满 -> WorkBuddy 报
"UserPromptSubmit operation blocked by hook"。这里把四种典型场景都钉住:
  1) 正常投递       —— 请求体完整送到 /event
  2) stdin 静默     —— 开了管道不给数据, 必须自己退出(旧版会永久卡住)
  3) 服务未启动     —— 连不上也要秒退, 不能等
  4) 服务受理不响应 —— recv 必须有超时, 不能被对方拖住
"""
import json
import os
import socket
import subprocess
import sys
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(HERE), "src")
HOOK = os.path.join(SRC, "hook.py")
sys.path.insert(0, SRC)

PY = getattr(sys, "_base_executable", "") or sys.executable
TIMEOUT_BUDGET = 3.0        # WorkBuddy 给 hook 的预算
ALLOW = 2.5                 # 我们要求 hook 自己必须在 2.5s 内结束(留 0.5s 保险)


def _run(port, stdin_data=None, keep_stdin=False):
    """跑一次 hook, 返回 (耗时, 退出码)。keep_stdin=True 时开了管道但不写也不关。"""
    env = dict(os.environ)
    env["KEYLIGHT_PORT"] = str(port)
    t0 = time.perf_counter()
    p = subprocess.Popen([PY, "-I", "-S", HOOK],
                         stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE,
                         env=env)
    if not keep_stdin:
        try:
            p.communicate(stdin_data or b"", timeout=10)
        except subprocess.TimeoutExpired:
            p.kill()
            return time.perf_counter() - t0, None
        return time.perf_counter() - t0, p.returncode
    try:
        p.wait(timeout=10)
    except subprocess.TimeoutExpired:
        p.kill()
        return time.perf_counter() - t0, None
    finally:
        for f in (p.stdin, p.stdout, p.stderr):
            try:
                f.close()
            except Exception:
                pass
    return time.perf_counter() - t0, p.returncode


class _Server:
    """极简 TCP 服务: mode=ok 收包返 200; mode=blackhole 收包不回。"""

    def __init__(self, mode="ok"):
        self.mode = mode
        self.body = b""
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(4)
        self.port = self.sock.getsockname()[1]
        self._stop = False
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        try:
            while not self._stop:
                conn, _ = self.sock.accept()
                threading.Thread(target=self._handle, args=(conn,),
                                 daemon=True).start()
        except OSError:
            pass

    def _handle(self, conn):
        try:
            data = b""
            conn.settimeout(3)
            while b"\r\n\r\n" not in data:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                data += chunk
            head, _, rest = data.partition(b"\r\n\r\n")
            length = 0
            for line in head.split(b"\r\n"):
                if line.lower().startswith(b"content-length:"):
                    length = int(line.split(b":")[1])
            while len(rest) < length:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                rest += chunk
            self.body = rest
            if self.mode == "ok":
                conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n"
                             b"Connection: close\r\n\r\n{}")
            else:
                time.sleep(5)          # 受理但不回包
        except OSError:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def close(self):
        self._stop = True
        try:
            self.sock.close()
        except OSError:
            pass


class TestHookNeverHangs(unittest.TestCase):

    def test_1_delivers_payload(self):
        srv = _Server("ok")
        try:
            ev = {"hook_event_name": "UserPromptSubmit",
                  "session_id": "t-hook", "prompt": "你好"}
            cost, rc = _run(srv.port, json.dumps(ev).encode())
            self.assertEqual(rc, 0)
            self.assertLess(cost, ALLOW)
            self.assertIn(b"t-hook", srv.body)
            self.assertIn(b"UserPromptSubmit", srv.body)
            self.assertLess(cost, TIMEOUT_BUDGET)
        finally:
            srv.close()

    def test_2_silent_stdin_must_not_hang(self):
        """旧版死在这: 管道开着不给数据 -> read1 永久阻塞 -> 3s 超时。"""
        srv = _Server("ok")
        try:
            cost, rc = _run(srv.port, keep_stdin=True)
            self.assertIsNotNone(rc, "hook 在 stdin 静默时挂住了")
            self.assertEqual(rc, 0)
            self.assertLess(cost, ALLOW)
            self.assertEqual(srv.body, b"", "无数据时不应发出空事件")
        finally:
            srv.close()

    def test_3_service_down_exits_fast(self):
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        dead = probe.getsockname()[1]
        probe.close()
        cost, rc = _run(dead, b'{"hook_event_name":"Stop","session_id":"t-die"}')
        self.assertEqual(rc, 0)
        self.assertLess(cost, ALLOW)
        self.assertLess(cost, TIMEOUT_BUDGET)
        self.assertGreater(dead, 0)

    def test_4_blackhole_server_does_not_stall(self):
        srv = _Server("blackhole")
        try:
            cost, rc = _run(srv.port, b'{"hook_event_name":"Stop","session_id":"t-bh"}')
            self.assertEqual(rc, 0)
            self.assertLess(cost, ALLOW, "对端不响应时 hook 被拖住")
            self.assertLess(cost, TIMEOUT_BUDGET)
        finally:
            srv.close()

    def test_5_budget_constants_sane(self):
        """内部最坏路径 + 解释器启动(实测 -I -S 0.28s) 必须远离 3s 预算。"""
        import hook as hook_mod
        worst = hook_mod.STDIN_WAIT + hook_mod.CONNECT_TIMEOUT + hook_mod.RECV_TIMEOUT
        self.assertLess(worst, 1.8, "内部最坏路径已达 %.2fs" % worst)
        self.assertLess(worst + 0.4, TIMEOUT_BUDGET - 0.5,
                        "总耗时可能顶到预算: 内部 %.2fs" % worst)


if __name__ == "__main__":
    unittest.main(verbosity=2)
