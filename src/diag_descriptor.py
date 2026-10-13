#!/usr/bin/env python3
"""诊断: 解析 RK98 厂商 HID 接口的 Report Descriptor，确认 Output 报表 ID 与长度"""
import sys
import winreg

COLLECTIONS = ["Col03", "Col05"]

def find_devkeys():
    """返回 {Col03: [instance_subkey, ...], Col05: [...]}"""
    base = r"SYSTEM\CurrentControlSet\Enum\HID"
    out = {}
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, base) as k:
        i = 0
        while True:
            try:
                parent = winreg.EnumKey(k, i)
            except OSError:
                break
            i += 1
            if "VID_258A&PID_00C1" not in parent.upper():
                continue
            for col in COLLECTIONS:
                if f"&{col.upper()}" not in parent.upper():
                    continue
                try:
                    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, rf"{base}\{parent}") as pk:
                        j = 0
                        while True:
                            try:
                                inst = winreg.EnumKey(pk, j)
                            except OSError:
                                break
                            j += 1
                            out.setdefault(col, []).append(rf"{parent}\{inst}")
                except OSError:
                    pass
    return out

def read_report_descriptor(subkey):
    path = rf"SYSTEM\CurrentControlSet\Enum\HID\{subkey}"
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path) as k:
        try:
            data, _ = winreg.QueryValueEx(k, "ReportDescriptor")
            return bytes(data)
        except FileNotFoundError:
            return None

def parse(desc):
    """极简 HID report descriptor 解析: 提取 (usage_page, report_id, type, size_bits)"""
    items = []
    i = 0
    usage_page = None
    report_id = 0
    report_size = 0
    report_count = 0
    while i < len(desc):
        b = desc[i]; i += 1
        size = b & 0x03
        if size == 3:
            size = 4
        typ = (b >> 2) & 0x03
        tag = (b >> 4) & 0x0F
        data = desc[i:i+size]; i += size
        val = int.from_bytes(data, "little") if data else 0
        if typ == 1:  # Global
            if tag == 0:  # Usage Page
                usage_page = val
            elif tag == 7:  # Report Size
                report_size = val
            elif tag == 9:  # Report Count
                report_count = val
            elif tag == 8:  # Report ID
                report_id = val
        elif typ == 0:  # Main
            if tag in (8, 9):  # Output(8) / Feature(9)
                bits = report_size * report_count
                items.append({
                    "kind": "Output" if tag == 8 else "Feature",
                    "usage_page": usage_page,
                    "report_id": report_id,
                    "bytes": (bits + 7) // 8,
                })
    return items

for col, subs in sorted(find_devkeys().items()):
    for sub in subs:
        desc = read_report_descriptor(sub)
        print(f"\n=== {col}  ({sub}) ===")
        if not desc:
            print("  (无 ReportDescriptor)")
            continue
        print(f"  descriptor: {len(desc)} bytes")
        for it in parse(desc):
            print(f"  {it['kind']:8s} usage_page=0x{it['usage_page']:04X} "
                  f"report_id=0x{it['report_id']:02X} payload={it['bytes']} bytes "
                  f"(+1 ID = {it['bytes']+1} total)")
