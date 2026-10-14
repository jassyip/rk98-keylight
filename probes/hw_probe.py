#!/usr/bin/env python3
"""M4 硬件探针 — 无人值守验证（不依赖肉眼观察灯效）

用法（需先停止 keylight 服务以独占 HID）:
  python hw_probe.py readback       E0: HID Feature 报表读回能力
  python hw_probe.py single-row     E2: 单排渲染 + fps 实测
  python hw_probe.py zone-partial   E1: zone 局部推送是否生效 + 耗时对比

判定原则:
  - 若 get_feature_report 能回显我们写入的帧 -> 读回通道可用, "是否生效"可机器判定;
  - 若不可读 -> 只能判定"写入被 ACK"与耗时, 视觉效果标注为待演示按钮确认。
"""
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(HERE), "src")
sys.path.insert(0, SRC)

from rk98 import RK98, REPORT_ID, REPORT_LEN, CHUNK  # noqa: E402
from keymap import (VKEYS, VPOSITIONS, FRAME_BUF_SIZE,  # noqa: E402
                    ROWS, NUMPAD_LEFT, NUMPAD_RIGHT)

ZONE_PALETTE = [(255, 0, 0), (0, 255, 0), (0, 0, 255),
                (255, 255, 0), (0, 255, 255), (255, 0, 255)]


def build_buf(colors):
    buf = bytearray(FRAME_BUF_SIZE)
    for i, rgb in enumerate(colors):
        idx = VKEYS[i]
        buf[idx], buf[idx + 1], buf[idx + 2] = rgb
    return buf


def zone_packet(z, buf):
    if z == 1:
        return bytes([REPORT_ID, 0x07, 0x01, 0x06]) + bytes(buf[0:61])
    if 2 <= z <= 6:
        s = 61 + CHUNK * (z - 2)
        return bytes([REPORT_ID, 0x07, z]) + bytes(buf[s:s + CHUNK])
    return bytes([REPORT_ID, 0x07, z]) + bytes(CHUNK)


def zone_of(i):
    v = VKEYS[i]
    if v < 61:
        return 1
    return 2 + (v - 61) // 62


def main_rows():
    """主键区 6 排 — 引用 keymap.ROWS (显式定义, 网格阈值法会误杀导航键, E2 实测踩坑)"""
    return ROWS


def numpad_halves():
    """numpad 17 键按物理列分半: 左半 10 键(Codex 溢出) / 右半 7 键(WB 溢出)"""
    return NUMPAD_LEFT, NUMPAD_RIGHT


def try_readback(dev, n=3):
    """尝试读回 Feature 报表; 返回 (ok, [hex...])"""
    res = []
    ok = False
    for _ in range(n):
        try:
            data = bytes(dev.get_feature_report(REPORT_ID, REPORT_LEN))
            res.append(data.hex())
            if data[:4] == bytes([REPORT_ID, 0x07, 0x01, 0x06]):
                ok = True
        except Exception as e:
            res.append("ERR:%s" % e)
    return ok, res


def send_full(kb, buf):
    for z in range(1, 8):
        kb.dev.send_feature_report(zone_packet(z, buf))


def cmd_readback():
    kb = RK98()
    kb.open()
    print("iface:", kb.iface[:70])
    print("== 读回(未发送) ==")
    ok0, res0 = try_readback(kb.dev)
    for r in res0:
        print("  ", r[:78])
    # 可辨识帧: 6 个 zone 各一个颜色
    colors = [(0, 0, 0)] * 100
    for i in range(100):
        colors[i] = ZONE_PALETTE[zone_of(i) - 1]
    send_full(kb, build_buf(colors))
    time.sleep(0.2)
    print("== 读回(发送 zone 色帧后) ==")
    ok1, res1 = try_readback(kb.dev)
    for r in res1:
        print("  ", r[:78])
    echo = any(bytes.fromhex(r)[:4] == bytes([REPORT_ID, 0x07, 0x01, 0x06])
               and r.startswith("0a070106") and "ff0000" in r
               for r in res1 if not r.startswith("ERR"))
    print("READBACK_AVAILABLE:", bool(ok1))
    print("READBACK_ECHO_MATCHES_SENT:", bool(echo))
    kb.close()
    print("(设备已停止发送, 键盘自动回硬件灯效)")


def cmd_single_row(secs=5.0, fps=30):
    kb = RK98()
    kb.open()
    rows = main_rows()
    p1 = rows[0]
    print("P1 键数: %d (其余 89 键全暗)" % len(p1))
    ok, _ = try_readback(kb.dev, n=1)
    print("READBACK_AVAILABLE:", ok)

    for label, paced in (("paced-30fps", True), ("unpaced-max", False)):
        t0 = time.time()
        n = 0
        interval = 1.0 / fps
        while time.time() - t0 < secs:
            t = time.time() - t0
            colors = [(0, 0, 0)] * 100
            phase = (t * 40) % 44 - 4
            for k in p1:
                x = VPOSITIONS[k][0]
                b = max(0.0, 1.0 - abs(x - phase) / 8.0)
                colors[k] = (0, int(255 * b), int(80 * b))
            buf = build_buf(colors)
            ts = time.time()
            send_full(kb, buf)
            n += 1
            if paced:
                d = t0 + n * interval - time.time()
                if d > 0:
                    time.sleep(d)
        dt = time.time() - t0
        print("E2 %-12s fps=%.1f (%d 帧/%.1fs)" % (label, n / dt, n, dt))

    # 读回验证: 发一帧"只亮 P1 第 0 键红", 检查回显
    colors = [(0, 0, 0)] * 100
    colors[p1[0]] = (255, 0, 0)
    send_full(kb, build_buf(colors))
    time.sleep(0.2)
    ok2, res2 = try_readback(kb.dev)
    if ok2:
        d = bytes.fromhex(res2[0])
        # 第 0 键在 buf 索引 VKEYS[0]=1
        print("读回校验: key0 RGB =", tuple(d[VKEYS[0]:VKEYS[0] + 3]),
              "(期望 (255,0,0)); key19 RGB =", tuple(d[VKEYS[19]:VKEYS[19] + 3]),
              "(期望 (0,0,0))")
    else:
        print("读回校验: 跳过(无读回通道) — 单排内容正确性待演示按钮人工确认")
    kb.close()
    print("(设备已停止发送, 键盘自动回硬件灯效)")


def cmd_zone_partial():
    kb = RK98()
    kb.open()
    ok, _ = try_readback(kb.dev, n=1)
    print("READBACK_AVAILABLE:", ok)

    # 基线: 全帧, zone1..6 各一色
    colors = [(0, 0, 0)] * 100
    for i in range(100):
        colors[i] = ZONE_PALETTE[zone_of(i) - 1]
    send_full(kb, build_buf(colors))
    time.sleep(0.2)
    print("== 基线: zone1..6 = 红/绿/蓝/黄/青/品红 ==")
    if ok:
        _, r = try_readback(kb.dev)
        print("  echo:", r[0][:78])

    # 实验1: 只推 zone3(白) + 双 commit, 不发 init
    kb.dev.send_feature_report(zone_packet(3, _zone3_white_buf()))
    kb.dev.send_feature_report(zone_packet(6, bytearray(FRAME_BUF_SIZE)))
    kb.dev.send_feature_report(zone_packet(7, bytearray(FRAME_BUF_SIZE)))
    time.sleep(0.2)
    print("== 实验1: 仅 zone3=白 + commit×2 (无 init) ==")
    if ok:
        _, r = try_readback(kb.dev)
        d = bytes.fromhex(r[0])
        z3keys = [i for i in range(100) if zone_of(i) == 3]
        z3rgb = set(tuple(d[VKEYS[i]:VKEYS[i] + 3]) for i in z3keys)
        z2keys = [i for i in range(100) if zone_of(i) == 2]
        z2rgb = set(tuple(d[VKEYS[i]:VKEYS[i] + 3]) for i in z2keys)
        print("  zone3 键色集合:", z3rgb, " zone2 键色集合:", z2rgb)
        print("  PARTIAL_NO_INIT_EFFECTIVE:", z3rgb == {(255, 255, 255)})

    # 实验2: 只推 zone3(黑), 单 commit
    kb.dev.send_feature_report(zone_packet(3, bytearray(FRAME_BUF_SIZE)))
    kb.dev.send_feature_report(zone_packet(6, bytearray(FRAME_BUF_SIZE)))
    time.sleep(0.2)
    print("== 实验2: 仅 zone3=黑 + 单 commit ==")
    if ok:
        _, r = try_readback(kb.dev)
        d = bytes.fromhex(r[0])
        z3keys = [i for i in range(100) if zone_of(i) == 3]
        z3rgb = set(tuple(d[VKEYS[i]:VKEYS[i] + 3]) for i in z3keys)
        print("  zone3 键色集合:", z3rgb, " PARTIAL_SINGLE_COMMIT_EFFECTIVE:",
              z3rgb == {(0, 0, 0)})

    # 实验3: 耗时对比 全帧(8 报表) vs 局部(init+zone3+commit×2 = 4 报表)
    def timeit(packets, rounds=60):
        t0 = time.time()
        for _ in range(rounds):
            for p in packets:
                kb.dev.send_feature_report(p)
        return (time.time() - t0) / rounds * 1000

    empty = bytearray(FRAME_BUF_SIZE)
    full_ms = timeit([zone_packet(z, empty) for z in range(1, 8)])
    part_ms = timeit([zone_packet(z, empty) for z in (1, 3, 6, 7)])
    print("== 实验3: 耗时 ==")
    print("  全帧 8 报表: %.2f ms/帧 (理论 %.0f fps)" % (full_ms, 1000 / full_ms))
    print("  局部 4 报表: %.2f ms/帧 (理论 %.0f fps)" % (part_ms, 1000 / part_ms))
    print("  加速比: %.2fx" % (full_ms / part_ms))
    kb.close()
    print("(设备已停止发送, 键盘自动回硬件灯效)")


def _zone3_white_buf():
    colors = [(0, 0, 0)] * 100
    for i in range(100):
        if zone_of(i) == 3:
            colors[i] = (255, 255, 255)
    return build_buf(colors)


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "readback"
    {"readback": cmd_readback,
     "single-row": cmd_single_row,
     "zone-partial": cmd_zone_partial}[cmd]()


if __name__ == "__main__":
    main()
