#!/usr/bin/env python3
"""安装/卸载 WorkBuddy hook 注册（Claude Code 兼容格式，追加式，不动现有 hook）

用法:
  python install_hooks.py install    # 备份 settings.json 后追加注册
  python install_hooks.py uninstall  # 移除我们注册的 hook 组
"""
import json
import os
import shutil
import sys
import time

HOME = os.path.expanduser("~")
SETTINGS = os.path.join(HOME, ".workbuddy", "settings.json")
HOOK_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hook.py")


def _fast_python():
    """挑一个启动最快、且不依赖 site-packages 的解释器。

    hook 只用标准库, 而实测同一台机器上:
      venv 解释器(默认加载 site) `-c pass` 0.86s / `-S` 0.53s
      基础解释器 `-I -S` 0.28s
    3 秒预算下这 0.6s 差别很关键, 所以优先用 sys._base_executable(venv 里指向
    基础解释器), 配 `-I -S`(隔离模式 + 不加载 site)。
    """
    base = getattr(sys, "_base_executable", "") or ""
    if not (base and os.path.exists(base)):
        base = sys.executable
    return base.replace("\\", "/")


PYTHON_EXE = _fast_python()
HOOK_COMMAND = f'"{PYTHON_EXE}" -I -S "{HOOK_SCRIPT}"'
# 实测：修掉无界 stdin 阻塞后内部最坏 1.05s、典型 ~0.3s。
# timeout 仍留 10s 余量：async hook 不阻塞对话，超时只会白扔一条事件。
HOOK_TIMEOUT = 10
MARKER = "rk98-keylight"

EVENTS = [
    "UserPromptSubmit",      # 用户提交 -> RUNNING
    "PreToolUse",            # 心跳
    "PostToolUse",           # 心跳
    "PostToolUseFailure",    # 心跳
    "PreCompact",            # 心跳
    "Stop",                  # 一轮结束 -> DONE
    "FinalStop",             # 全部结束 -> DONE
    "SessionEnd",            # 会话关闭 -> IDLE
    "Notification",          # 预留: 待确认
]


def our_group():
    return {
        "matcher": "",
        "hooks": [{
            "type": "command",
            "command": HOOK_COMMAND,
            "async": True,
            "timeout": HOOK_TIMEOUT,
        }],
    }


def _is_current(h):
    """该 hook 项是否已是当前期望的命令/超时(用于原地升级)"""
    if not isinstance(h, dict) or MARKER not in str(h.get("command", "")):
        return False
    try:
        timeout = int(h.get("timeout") or 0)
    except Exception:
        timeout = 0
    return str(h.get("command", "")) == HOOK_COMMAND and timeout == HOOK_TIMEOUT


def is_ours(group):
    hooks = group.get("hooks") if isinstance(group, dict) else None
    if isinstance(group, list):
        hooks = group
    if not isinstance(hooks, list):
        return False
    return any(MARKER in str(h.get("command", "")) for h in hooks if isinstance(h, dict))


def load():
    with open(SETTINGS, encoding="utf-8") as f:
        return json.load(f)


def save(doc):
    with open(SETTINGS, "w", encoding="utf-8") as f:
        f.write(json.dumps(doc, ensure_ascii=False, indent=2) + "\n")


def install():
    if not os.path.exists(SETTINGS):
        print(f"找不到 {SETTINGS}")
        sys.exit(2)
    backup = SETTINGS + ".bak-keylight-" + time.strftime("%Y%m%d-%H%M%S")
    shutil.copy2(SETTINGS, backup)
    print(f"已备份: {backup}")

    doc = load()
    hooks = doc.setdefault("hooks", {})
    added, existed, updated = [], [], []
    for ev in EVENTS:
        groups = hooks.setdefault(ev, [])
        ours = [g for g in groups if is_ours(g)]
        if not ours:
            groups.append(our_group())
            added.append(ev)
            continue
        # 已注册过: 命令/超时不是当前版本就原地升级(早期版本用 venv 解释器、
        # timeout=3, 且脚本存在无界 stdin 阻塞, 会报 Hook timed out)
        changed = False
        for g in ours:
            for h in g.get("hooks", []):
                if not isinstance(h, dict) or MARKER not in str(h.get("command", "")):
                    continue
                if _is_current(h):
                    continue
                h["command"] = HOOK_COMMAND
                h["timeout"] = HOOK_TIMEOUT
                h["async"] = True
                changed = True
        (updated if changed else existed).append(ev)
    save(doc)
    print(f"新注册事件 ({len(added)}): {', '.join(added) or '无'}")
    print(f"原地升级 ({len(updated)}): {', '.join(updated) or '无'}")
    print(f"已是最新跳过 ({len(existed)}): {', '.join(existed) or '无'}")
    print(f"hook 命令: {HOOK_COMMAND}")


def uninstall():
    if not os.path.exists(SETTINGS):
        print(f"找不到 {SETTINGS}")
        sys.exit(2)
    backup = SETTINGS + ".bak-keylight-rm-" + time.strftime("%Y%m%d-%H%M%S")
    shutil.copy2(SETTINGS, backup)
    doc = load()
    hooks = doc.get("hooks", {})
    removed = []
    for ev in list(hooks.keys()):
        before = hooks[ev]
        after = [g for g in before if not is_ours(g)] if isinstance(before, list) else before
        if isinstance(before, list) and len(after) != len(before):
            removed.append(ev)
            if after:
                hooks[ev] = after
            else:
                del hooks[ev]
    save(doc)
    print(f"已移除事件 ({len(removed)}): {', '.join(removed) or '无'}")
    print(f"备份: {backup}")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "install"
    {"install": install, "uninstall": uninstall}[cmd]()
