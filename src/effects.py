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
