#!/usr/bin/env python3
"""WorkBuddy hook 转发器 — stdin JSON -> POST 本地 keylight 服务

由 install_hooks.py 注册进 ~/.workbuddy/settings.json（Claude Code 兼容 hooks）。
设计要点:
  - 裸 socket, 不 import urllib/requests —— 避免继承 HTTP_PROXY 走代理导致超时,
    也省掉重模块导入(WorkBuddy 给 hook 的预算只有 3 秒)。
  - 全程 1 秒超时, 任何异常都吞掉, 永远 exit 0, 绝不影响 WorkBuddy 自身。
  - 端口从 config.json 读, 与服务端保持一致。
"""
import json
import os
import socket
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(os.path.dirname(HERE), "config.json")
TIMEOUT = 1.0


def _port():
    try:
        with open(CONFIG, encoding="utf-8") as f:
            return int(json.load(f).get("service", {}).get("port", 18990))
    except Exception:
        return 18990


def post_event():
    raw = b""
    try:
        # 只取管道里已有的数据, 不无限等 EOF
        raw = sys.stdin.buffer.read1(65536) or b"{}"
    except Exception:
        raw = b"{}"
    try:
        payload = json.loads(raw.decode("utf-8", "replace") or "{}")
        if not isinstance(payload, dict):
            payload = {}
    except Exception:
        payload = {}
    body = json.dumps(payload).encode("utf-8")
    req = (b"POST /event HTTP/1.1\r\nHost: 127.0.0.1\r\n"
           b"Content-Type: application/json\r\nContent-Length: "
           + str(len(body)).encode() + b"\r\nConnection: close\r\n\r\n" + body)
    s = socket.create_connection(("127.0.0.1", _port()), timeout=TIMEOUT)
    try:
        s.settimeout(TIMEOUT)
        s.sendall(req)
        s.recv(64)
    finally:
        s.close()


def main():
    try:
        post_event()
    except Exception:
        pass
    try:
        sys.stdout.write("{}\n")
    except Exception:
        pass


if __name__ == "__main__":
    main()
    sys.exit(0)
