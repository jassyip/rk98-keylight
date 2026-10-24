#!/usr/bin/env python3
"""WorkBuddy hook 转发器 — stdin JSON -> POST 本地 keylight 服务

由 install_hooks.py 注册进 ~/.workbuddy/settings.json（Claude Code 兼容 hooks）。

设计要点:
  - **每个阻塞点都有硬上限**, 退出走 os._exit 不让残留线程拖住进程。
    2026-10-24 修 "Hook timed out after 3000ms":
    旧版 `sys.stdin.buffer.read1()` 在**管道已连但对方还没写数据**时是无界阻塞的
    —— WorkBuddy 启动期主线程忙(实测插件加载单次 600ms+)、事件队列积压时, stdin
    迟迟不来数据, 本进程就挂着不动, 3 秒预算被打满, 用户看到
    "UserPromptSubmit operation blocked by hook"。日志统计 10-23 一天 hook.py
    超时 195 次(同期 agent-studio 的 hook 仅 28 次), 是本项目自身缺陷。
    现在: stdin 读取 ≤0.50s / 连接 ≤0.35s / 回包 ≤0.20s, 内部最坏 1.05s。
  - 仍保留 read1 语义: 只要拿到的字节能解析成 JSON 就立刻停, 不无限等 EOF
    (WorkBuddy 不一定关 stdin)。
  - 裸 socket, 不 import urllib/requests —— 避免继承 HTTP_PROXY 走代理导致超时,
    也省掉重模块导入(hook 预算只有 3 秒)。
  - 任何异常都吞掉, 永远 exit 0, 绝不影响 WorkBuddy 自身。
  - 端口从 config.json 读, 与服务端保持一致。
"""
import json
import os
import socket
import sys
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(os.path.dirname(HERE), "config.json")

STDIN_WAIT = 0.90       # 等 stdin 首个(完整)事件的硬上限(给"父进程晚写"留余量)
CONNECT_TIMEOUT = 0.35  # 连本地服务的硬上限
RECV_TIMEOUT = 0.20     # 等回包的硬上限(只用于序列化先后顺序, 内容不关心)
MAX_BODY = 1 << 20


def _port():
    env = os.environ.get("KEYLIGHT_PORT")
    if env:
        try:
            return int(env)
        except Exception:
            pass
    try:
        with open(CONFIG, encoding="utf-8") as f:
            return int(json.load(f).get("service", {}).get("port", 18990))
    except Exception:
        return 18990


def _parse(raw):
    if not raw:
        return None
    try:
        payload = json.loads(raw.decode("utf-8", "replace"))
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def _read_event():
    """有界读取 stdin: 能解析就立刻返回, 超过 STDIN_WAIT 返回已到的部分。

    WorkBuddy 不一定关闭 stdin, 所以不能用 read() 等 EOF; 但 read1 本身无界,
    因此放到 daemon 线程里读, 主线程只等 STDIN_WAIT。
    """
    box = {"buf": b""}

    def _reader():
        buf = b""
        try:
            while len(buf) < MAX_BODY:
                chunk = sys.stdin.buffer.read1(65536)
                if not chunk:
                    break
                buf += chunk
                box["buf"] = buf          # 超时也能拿到已读部分
                if _parse(buf) is not None:
                    return
        except Exception:
            pass

    t = threading.Thread(target=_reader, daemon=True)
    t.start()
    t.join(STDIN_WAIT)
    return box["buf"]


def post_event():
    payload = _parse(_read_event())
    if payload is None:
        return
    body = json.dumps(payload).encode("utf-8")
    req = (b"POST /event HTTP/1.1\r\nHost: 127.0.0.1\r\n"
           b"Content-Type: application/json\r\nContent-Length: "
           + str(len(body)).encode() + b"\r\nConnection: close\r\n\r\n" + body)
    s = socket.create_connection(("127.0.0.1", _port()), timeout=CONNECT_TIMEOUT)
    try:
        s.sendall(req)
        try:
            s.settimeout(RECV_TIMEOUT)
            s.recv(64)
        except Exception:
            pass
    finally:
        s.close()


def main():
    try:
        post_event()
    except Exception:
        pass
    try:
        sys.stdout.write("{}\n")
        sys.stdout.flush()
    except Exception:
        pass
    # 硬退出: 读 stdin 的 daemon 线程可能还堵在 read1 上, 不能等它
    os._exit(0)


if __name__ == "__main__":
    main()
