"""灯效帧生成 — 每帧返回 100 个 (r,g,b)（顺序对应 keymap.VKEYS）"""
import math

from keymap import VPOSITIONS

GRID_PERIOD = 44  # 相位循环: 37 列 + 前后各留 4 列尾部


def hex_rgb(h):
    h = h.lstrip("#")
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))


def scale(rgb, k):
    return tuple(max(0, min(255, int(round(c * k)))) for c in rgb)


def solid_colors(rgb):
    return [rgb] * 100


def flow_frame(rgb, phase, tail=8.0):
    """相位流水波: 波前 phase (列坐标), 尾迹 tail 列, 按键亮度随距波前距离衰减"""
    out = []
    for (x, _y) in VPOSITIONS:
        k = max(0.0, 1.0 - abs(x - phase) / tail)
        out.append(scale(rgb, k))
    return out


def rainbow_frame(phase, tail=8.0):
    """彩色流水: 色相随波前滚动"""
    out = []
    for (x, _y) in VPOSITIONS:
        d = (x - phase) % GRID_PERIOD
        k = max(0.0, 1.0 - abs(d if d < GRID_PERIOD / 2 else d - GRID_PERIOD) / tail)
        hue = ((x * 10 + phase * 8) % 360) / 360.0
        r, g, b = _hsv(hue, 1.0, k)
        out.append((r, g, b))
    return out


def breathe_frame(rgb, t, period=2.0):
    k = 0.5 - 0.5 * math.cos(2 * math.pi * (t % period) / period)
    return [scale(rgb, k)] * 100


def _hsv(h, s, v):
    i = int(h * 6)
    f = h * 6 - i
    p, q, t = v * (1 - s), v * (1 - s * f), v * (1 - s * (1 - f))
    r, g, b = [(v, t, p), (q, v, p), (p, v, t), (p, q, v), (t, p, v), (v, p, q)][i % 6]
    return int(r * 255), int(g * 255), int(b * 255)


# ---- M4 分区模式: 排级帧函数 ----
# 与全局帧函数(全键盘 37 列)不同, 这些函数只作用于"一排键"(keymap.ROWS 的一行
# 或 numpad 半区), 返回 {key_index: (r,g,b)} 映射, 由 partition.py 合成全帧。
def row_xrange(keys):
    """该排(键组)的网格 x 范围"""
    xs = [VPOSITIONS[i][0] for i in keys]
    return min(xs), max(xs)


def row_phase(now, speed, keys, tail=8.0):
    """该排当前流水波前位置: 在 [xmin-tail, xmax+tail] 间匀速循环
    (波前只在该排范围内扫描, 观感 = 波浪沿这一排跑)"""
    xmin, xmax = row_xrange(keys)
    span = float(xmax - xmin) + 2.0 * tail
    return xmin - tail + (now * speed) % span


def row_flow_map(keys, rgb, phase, tail=8.0):
    """排内流水: 按键亮度随距波前距离衰减; 返回 {key: color}, 暗键为 (0,0,0)"""
    out = {}
    for i in keys:
        x = VPOSITIONS[i][0]
        k = max(0.0, 1.0 - abs(x - phase) / tail)
        out[i] = scale(rgb, k) if k > 0.0 else (0, 0, 0)
    return out


def lit_row_flow_map(keys, rgb, phase, tail=8.0, lit_count=None):
    """亮格内流水: 按 x 排序前 lit_count 个键跑流水(进度), 其余键暗。

    进度语义: 亮格数 = 完成步骤/总步骤(从左到右填充); 流水只跑在亮区,
    亮区右缘就是进度的 leading edge。lit_count=None 表示无进度信息,
    整排流水(活动度, 不填充即不造假进度)。
    """
    if lit_count is None or lit_count >= len(keys):
        return row_flow_map(keys, rgb, phase, tail)
    lit_count = max(0, min(len(keys), int(lit_count)))
    ordered = sorted(keys, key=lambda i: VPOSITIONS[i][0])
    lit = set(ordered[:lit_count])
    base = row_flow_map(keys, rgb, phase, tail)
    return {i: (c if i in lit else (0, 0, 0)) for i, c in base.items()}
