#!/usr/bin/env python3
"""M1 协议验证脚本 — RK98 HID 灯效控制

验证项（对应 workbuddy-switch 需求）:
  1. enumerate  找到灯效 HID 接口（usage_page 0xFF00）
  2. solid      全键盘常亮（验证逐键 RGB 写入 + RK 软件占用冲突）
  3. flow       全键盘红色流水（相位波，30fps）
  4. flash      全键盘绿色闪烁 x3

用法:
  python verify_m1.py enumerate
  python verify_m1.py solid [--color FF0000] [--secs 3]
  python verify_m1.py flow  [--color FF0000] [--secs 6] [--speed 40]
  python verify_m1.py flash [--color 00FF00] [--times 3]
  python verify_m1.py all            # 依次跑完 2/3/4
  python verify_m1.py off            # 停止流式，交还硬件灯效
"""
import argparse
import subprocess
import sys
import time

import hid

from keymap import VKEYS, VPOSITIONS, FRAME_BUF_SIZE

VID = 0x258A
PID = 0x00C1
USAGE_PAGE = 0xFF00
PREFERRED_COLLECTION = "Col05"  # SignalRGB 插件 Validate() 指定 collection 5

REPORT_ID = 0x0A
REPORT_LEN = 65
CHUNK = 62  # 每 stream 包负载字节数


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def find_interface():
    """返回 (path, description)；优先 Col05，其次任意 0xFF00 接口。"""
    devs = [d for d in hid.enumerate(VID, PID) if d.get("usage_page") == USAGE_PAGE]
    if not devs:
        return None, None
    for d in devs:
        if PREFERRED_COLLECTION.lower() in d["path"].decode(errors="ignore").lower():
            return d["path"], f"{PREFERRED_COLLECTION} (usage_page 0xFF00)"
    d = devs[0]
    return d["path"], f"fallback {d['path'].decode(errors='ignore')} (usage_page 0xFF00)"


def open_device():
    path, desc = find_interface()
    if not path:
        log("FAIL: 未找到 usage_page=0xFF00 的灯效接口")
        sys.exit(2)
    log(f"灯效接口: {desc}")
    dev = hid.device()
    dev.open_path(path)
    log("设备已打开")
    return dev


def build_frame(colors):
    """colors: [(r,g,b)] * 100 -> 425 字节帧缓冲"""
    buf = bytearray(FRAME_BUF_SIZE)
    for i, rgb in enumerate(colors):
        idx = VKEYS[i]
        buf[idx], buf[idx + 1], buf[idx + 2] = rgb
    return buf


def send_frame(dev, buf):
    """1:1 复刻 SignalRGB 插件的 8 报表帧结构（Feature 报表，ReportID 0x0A，共 65 字节）"""
    # init: [0A 07 01 06] + 61 字节
    _wr(dev, bytes([REPORT_ID, 0x07, 0x01, 0x06]) + bytes(buf[0:61]))
    time.sleep(0.001)
    # stream: [0A 07 zone] + 62 字节, zone 2..6
    for z in range(2, 7):
        start = 61 + CHUNK * (z - 2)
        _wr(dev, bytes([REPORT_ID, 0x07, z]) + bytes(buf[start:start + CHUNK]))
        time.sleep(0.001)
    # commit: [0A 07 06] / [0A 07 07] 零填充
    for z in (6, 7):
        _wr(dev, bytes([REPORT_ID, 0x07, z]) + bytes(CHUNK))
        time.sleep(0.001)


def _wr(dev, pkt):
    assert len(pkt) == REPORT_LEN, f"bad packet len {len(pkt)}"
    rc = dev.send_feature_report(pkt)
    if rc != REPORT_LEN:
        raise OSError(f"send_feature_report returned {rc}, expected {REPORT_LEN}")


def hex_rgb(h):
    h = h.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def scale(rgb, k):
    return tuple(max(0, min(255, int(c * k))) for c in rgb)


def check_conflict():
    """检测 RK 官方软件进程是否在运行（SignalRGB 标记的冲突进程）"""
    try:
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq DeviceDriver.exe",
                              "/NH"], capture_output=True, timeout=5,
                             encoding="utf-8", errors="replace")
        running = "devicedriver.exe" in out.stdout.lower()
    except Exception:
        running = None
    log(f"DeviceDriver.exe (RK 软件) 运行中: {running}")
    return running


def try_write(dev, buf, label):
    try:
        send_frame(dev, buf)
        return True
    except OSError as e:
        log(f"FAIL {label}: HID 写入被拒: {e}")
        return False


# ---------- 效果 ----------

def effect_solid(dev, color_hex, secs):
    rgb = hex_rgb(color_hex)
    colors = [rgb] * 100
    buf = build_frame(colors)
    t_end = time.time() + secs
    frames = 0
    while time.time() < t_end:
        if not try_write(dev, buf, "solid"):
            return False
        frames += 1
        time.sleep(0.1)
    log(f"solid 完成: {frames} 帧 @ 10fps")
    return True


def effect_flow(dev, color_hex, secs, speed):
    rgb = hex_rgb(color_hex)
    t0 = time.time()
    frames = 0
    period = 1 / 30
    while True:
        t = time.time() - t0
        if t >= secs:
            break
        phase = (t * speed) % 44 - 4  # 波前位置（列坐标 0..36）
        colors = []
        for (x, _y) in VPOSITIONS:
            k = max(0.0, 1.0 - abs(x - phase) / 8.0)
            colors.append(scale(rgb, k))
        if not try_write(dev, build_frame(colors), "flow"):
            return False
        frames += 1
        dt = period - ((time.time() - t0) % period)
        if dt > 0:
            time.sleep(dt)
    log(f"flow 完成: {frames} 帧 @ 30fps, speed={speed} 列/秒")
    return True


def effect_flash(dev, color_hex, times, on_ms=200, off_ms=120):
    rgb = hex_rgb(color_hex)
    buf_on = build_frame([rgb] * 100)
    buf_off = build_frame([(0, 0, 0)] * 100)
    for i in range(times):
        if not try_write(dev, buf_on, "flash-on"):
            return False
        time.sleep(on_ms / 1000)
        if not try_write(dev, buf_off, "flash-off"):
            return False
        time.sleep(off_ms / 1000)
    log(f"flash 完成: {times} 次")
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("step", choices=["enumerate", "solid", "flow", "flash", "off", "all"])
    ap.add_argument("--color", default=None)
    ap.add_argument("--secs", type=float, default=None)
    ap.add_argument("--speed", type=float, default=40)
    ap.add_argument("--times", type=int, default=3)
    args = ap.parse_args()

    if args.step == "enumerate":
        path, desc = find_interface()
        if path:
            log(f"PASS enumerate: {desc}")
            sys.exit(0)
        log("FAIL enumerate: 无 0xFF00 接口")
        sys.exit(2)

    check_conflict()
    dev = open_device()
    try:
        if args.step == "solid":
            ok = effect_solid(dev, args.color or "FF0000", args.secs or 3)
        elif args.step == "flow":
            ok = effect_flow(dev, args.color or "FF0000", args.secs or 6, args.speed)
        elif args.step == "flash":
            ok = effect_flash(dev, args.color or "00FF00", args.times)
        elif args.step == "off":
            try_write(dev, build_frame([(0, 0, 0)] * 100), "off")
            log("已停止流式，键盘应交还硬件灯效")
            ok = True
        elif args.step == "all":
            ok = (effect_solid(dev, "FF0000", 3)
                  and effect_flow(dev, "FF0000", 6, 40)
                  and effect_flash(dev, "00FF00", 3))
        log("结果: " + ("PASS" if ok else "FAIL"))
        sys.exit(0 if ok else 3)
    finally:
        dev.close()


if __name__ == "__main__":
    main()
