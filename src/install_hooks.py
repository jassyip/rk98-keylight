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
# hook 只用标准库(裸 socket), 所以直接用当前解释器即可 —— 不硬编码任何用户路径
PYTHON_EXE = sys.executable or "python"
HOOK_COMMAND = f'"{PYTHON_EXE}" "{HOOK_SCRIPT}"'
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
            "timeout": 3,
        }],
    }


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
    added, existed = [], []
    for ev in EVENTS:
        groups = hooks.setdefault(ev, [])
        if any(is_ours(g) for g in groups):
            existed.append(ev)
            continue
        groups.append(our_group())
        added.append(ev)
    save(doc)
    print(f"新注册事件 ({len(added)}): {', '.join(added) or '无'}")
    print(f"已存在跳过 ({len(existed)}): {', '.join(existed) or '无'}")
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
