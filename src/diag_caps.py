#!/usr/bin/env python3
"""诊断2: 通过 HidP API 读取 RK98 灯效接口的报表能力
- UsagePage / Usage
- Input/Output/Feature 报表长度
- Output/Feature 报表的 ReportID 列表
若灯效命令走 Feature 报表，hidapi 的 write()(Output) 会被静默丢弃 —— 这是"写入成功但键盘无反应"的头号疑点。
"""
import ctypes
from ctypes import wintypes

import hid

VID, PID, USAGE_PAGE = 0x258A, 0x00C1, 0xFF00

UCHAR = ctypes.c_ubyte
BOOLEAN = ctypes.c_ubyte

hid_dll = ctypes.WinDLL("hid.dll")

GENERIC_READ, GENERIC_WRITE = 0x80000000, 0x40000000
FILE_SHARE_READ, FILE_SHARE_WRITE = 1, 2
OPEN_EXISTING = 3
INVALID_HANDLE_VALUE = -1


class HIDP_CAPS(ctypes.Structure):
    _fields_ = [
        ("Usage", wintypes.USHORT),
        ("UsagePage", wintypes.USHORT),
        ("InputReportByteLength", wintypes.USHORT),
        ("OutputReportByteLength", wintypes.USHORT),
        ("FeatureReportByteLength", wintypes.USHORT),
        ("Reserved", wintypes.USHORT * 17),
        ("NumberLinkCollectionNodes", wintypes.USHORT),
    ]


class HIDP_VALUE_CAPS(ctypes.Structure):
    _fields_ = [
        ("UsagePage", wintypes.USHORT),
        ("ReportID", UCHAR),
        ("IsAlias", BOOLEAN),
        ("BitField", wintypes.USHORT),
        ("LinkCollection", wintypes.USHORT),
        ("LinkUsage", wintypes.USHORT),
        ("LinkUsagePage", wintypes.USHORT),
        ("IsRange", wintypes.USHORT),
        ("IsStringRange", wintypes.USHORT),
        ("IsDesignatorRange", wintypes.USHORT),
        ("IsAbsolute", wintypes.USHORT),
        ("HasNull", wintypes.USHORT),
        ("Reserved", UCHAR * 10),
        ("Range", wintypes.USHORT * 8),
    ]


def open_path(path_str):
    h = ctypes.windll.kernel32.CreateFileW(
        path_str, GENERIC_READ | GENERIC_WRITE,
        FILE_SHARE_READ | FILE_SHARE_WRITE, None, OPEN_EXISTING, 0, None)
    if h == INVALID_HANDLE_VALUE:
        raise OSError(f"CreateFile failed: {ctypes.GetLastError()}")
    return h


def probe(path_str, label):
    print(f"\n=== {label}: {path_str[:70]}... ===")
    h = open_path(path_str)
    try:
        ppd = ctypes.c_void_p()
        if not hid_dll.HidD_GetPreparsedData(wintypes.HANDLE(h), ctypes.byref(ppd)):
            print("  HidD_GetPreparsedData FAILED")
            return
        caps = HIDP_CAPS()
        if hid_dll.HidP_GetCaps(ppd, ctypes.byref(caps)) != 0x110000:  # HIDP_STATUS_SUCCESS
            print("  HidP_GetCaps FAILED")
            return
        print(f"  UsagePage=0x{caps.UsagePage:04X} Usage=0x{caps.Usage:04X}")
        print(f"  InputReportByteLength   = {caps.InputReportByteLength}")
        print(f"  OutputReportByteLength  = {caps.OutputReportByteLength}")
        print(f"  FeatureReportByteLength = {caps.FeatureReportByteLength}")
        print(f"  LinkCollectionNodes     = {caps.NumberLinkCollectionNodes}")
        for kind, rid in (("Output", 1), ("Feature", 2)):  # HidP_Output=1, HidP_Feature=2
            count = wintypes.USHORT(0)
            hid_dll.HidP_GetValueCaps(rid, None, ctypes.byref(count), ppd)
            if count.value == 0:
                print(f"  {kind} value caps: (none)")
                continue
            arr = (HIDP_VALUE_CAPS * count.value)()
            st = hid_dll.HidP_GetValueCaps(rid, arr, ctypes.byref(count), ppd)
            if st != 0x110000:
                print(f"  {kind} value caps: FAILED 0x{st:08X}")
                continue
            ids = sorted({c.ReportID for c in arr})
            pages = sorted({c.UsagePage for c in arr})
            print(f"  {kind} value caps: count={count.value} ReportIDs={[hex(i) for i in ids]} UsagePages={[hex(p) for p in pages]}")
    finally:
        ctypes.windll.kernel32.CloseHandle(wintypes.HANDLE(h))


def main():
    devs = [d for d in hid.enumerate(VID, PID) if d.get("usage_page") == USAGE_PAGE]
    for d in devs:
        col = "Col05" if "col05" in d["path"].decode(errors="ignore").lower() else \
              "Col03" if "col03" in d["path"].decode(errors="ignore").lower() else "other"
        try:
            probe(d["path"].decode(), f"{col} (usage_page 0xFF00)")
        except OSError as e:
            print(f"\n=== {col}: open failed: {e}")


if __name__ == "__main__":
    main()
