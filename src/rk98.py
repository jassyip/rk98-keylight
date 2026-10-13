"""RK98 HID 灯效驱动

协议要点（实测 + 插件逆出）:
- 接口: VID 258A / PID 00C1, usage_page 0xFF00, 优先 Col05 (collection 5)
- 命令走 **Feature 报表** (ReportID 0x0A, 总长 65 字节 = 1 ID + 64 负载)
  注意: 该接口没有任何 Output 报表, 用 write() 发 Output 会被 Windows 静默丢弃
- 每帧 8 个报表: init(zone1) + stream(zone2..6) + commit(zone6,7)
"""
import subprocess
import time

import hid

from keymap import VKEYS, FRAME_BUF_SIZE

VID = 0x258A
PID = 0x00C1
USAGE_PAGE = 0xFF00
PREFERRED_COLLECTION = "Col05"
REPORT_ID = 0x0A
REPORT_LEN = 65
CHUNK = 62
RK_APP_PROCESS = "DeviceDriver.exe"


class RK98Error(Exception):
    pass


class RK98:
    def __init__(self, vid=VID, pid=PID, preferred=PREFERRED_COLLECTION):
        self.vid = vid
        self.pid = pid
        self.preferred = preferred
        self.dev = None
        self.iface = None
        self.frames_sent = 0

    def _candidates(self):
        devs = [d for d in hid.enumerate(self.vid, self.pid)
                if d.get("usage_page") == USAGE_PAGE]
        pref = [d for d in devs
                if self.preferred.lower() in d["path"].decode(errors="ignore").lower()]
        return pref + [d for d in devs if d not in pref]

    def _diagnose_missing(self):
        """没找到灯效接口时, 区分具体原因 —— 蓝牙/2.4G 模式与接线不同"""
        all_by_pid = [d for d in hid.enumerate(self.vid, self.pid)]
        if not all_by_pid:
            return ("完全看不到 RK98 (VID %04X/PID %04X)。可能键盘没连上, "
                    "或已切到蓝牙/2.4G 模式 —— 这两种无线模式下不提供 0xFF00 厂商灯效接口"
                    % (self.vid, self.pid))
        pages = {}
        for d in all_by_pid:
            pages.setdefault(d.get("usage_page", 0), 0)
            pages[d.get("usage_page", 0)] += 1
        detail = ", ".join("0x%04X×%d" % (p, c) for p, c in sorted(pages.items()))
        # 有键盘接口但没有 0xFF00 -> 典型的无线模式(仅暴露标准键盘接口)
        if USAGE_PAGE not in pages:
            return ("RK98 在线(接口 %s), 但缺少 0xFF00 灯效接口 —— "
                    "这是蓝牙或 2.4G 模式的特征, 无线连接下无法控制灯效, 请插回 USB 线"
                    % detail)
        return "RK98 在线(接口 %s), 但 0xFF00 接口无法打开" % detail

    def open(self):
        last = None
        cands = self._candidates()
        for d in cands:
            try:
                dev = hid.device()
                dev.open_path(d["path"])
                self.dev = dev
                self.iface = d["path"].decode(errors="ignore")
                return True
            except OSError as e:
                last = e
        detail = self._diagnose_missing()
        raise RK98Error("无法打开灯效接口(%04X/%04X): %s | %s"
                         % (self.vid, self.pid, last, detail))

    def close(self):
        if self.dev is not None:
            try:
                self.dev.close()
            except Exception:
                pass
            self.dev = None
            self.iface = None

    def _wr(self, pkt):
        if len(pkt) != REPORT_LEN:
            raise RK98Error(f"报表长度错误: {len(pkt)} != {REPORT_LEN}")
        rc = self.dev.send_feature_report(pkt)
        if rc != REPORT_LEN:
            raise OSError(f"send_feature_report 返回 {rc}, 期望 {REPORT_LEN}")

    def send_frame(self, colors):
        """colors: [(r,g,b)] * 100 -> 8 个 Feature 报表"""
        buf = bytearray(FRAME_BUF_SIZE)
        for i, rgb in enumerate(colors):
            idx = VKEYS[i]
            buf[idx], buf[idx + 1], buf[idx + 2] = rgb
        self._wr(bytes([REPORT_ID, 0x07, 0x01, 0x06]) + bytes(buf[0:61]))
        time.sleep(0.001)
        for z in range(2, 7):
            s = 61 + CHUNK * (z - 2)
            self._wr(bytes([REPORT_ID, 0x07, z]) + bytes(buf[s:s + CHUNK]))
            time.sleep(0.001)
        for z in (6, 7):
            self._wr(bytes([REPORT_ID, 0x07, z]) + bytes(CHUNK))
            time.sleep(0.001)
        self.frames_sent += 1


def kill_rk_app():
    """结束 RK 官方软件进程 (SignalRGB 标记的冲突进程)"""
    try:
        subprocess.run(["taskkill", "/IM", RK_APP_PROCESS, "/F"],
                       capture_output=True, timeout=5,
                       encoding="utf-8", errors="replace")
        return True
    except Exception:
        return False


def rk_app_running():
    try:
        out = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {RK_APP_PROCESS}", "/NH"],
                             capture_output=True, timeout=5,
                             encoding="utf-8", errors="replace")
        return RK_APP_PROCESS.lower() in (out.stdout or "").lower()
    except Exception:
        return None
