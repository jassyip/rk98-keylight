"""灯效帧生成 — 每帧返回 100 个 (r,g,b)（顺序对应 keymap.VKEYS）"""
import math

from keymap import VPOSITIONS

GRID_PERIOD = 44  # 相位循环: 37 列 + 前后各留 4 列尾部


def _falloff(u):
    """平滑衰减曲线: u∈[0,1] -> [1,0]。

    参考 RK 官方驱动(RK98-100KRGB KB.ini LedOpt)流水观感: 波前平滑点亮、
    拖尾平滑渐暗, 两端导数都为 0, 消除线性衰减的硬切跳变。
    曲线 = 0.5-0.5cos(pi*u): u=1 满色 / u=0 全暗 / 中点 0.5。
    """
    if u <= 0.0:
        return 0.0
    if u >= 1.0:
        return 1.0
    return 0.5 - 0.5 * math.cos(math.pi * u)


def hex_rgb(h):
    h = h.lstrip("#")
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))


def scale(rgb, k):
    return tuple(max(0, min(255, int(round(c * k)))) for c in rgb)


def bfactor(v, default=100):
    """亮度配置(0-100 百分比) -> 缩放系数(0-1)。

    2026-10-14 起亮度统一用百分比量表(默认 100 = 全亮), 替代早期的
    0-1 浮点(0.8 这种写法用户面板上不直观)。非法值回落 default。
    """
    try:
        return max(0.0, min(1.0, float(v) / 100.0))
    except (TypeError, ValueError):
        return default / 100.0


def solid_colors(rgb):
    return [rgb] * 100


def flow_frame(rgb, phase, tail=8.0):
    """相位流水波: 波前 phase (列坐标), 尾迹 tail 列。

    亮度 = _falloff(1-|x-phase|/tail) 平滑衰减(参考 RK 官方驱动观感);
    同列按键共享一个亮度值(按 x 缓存, 37 列 x 重复 100 键)。
    """
    kcol = {}
    out = []
    for (x, _y) in VPOSITIONS:
        k = kcol.get(x)
        if k is None:
            k = _falloff(1.0 - abs(x - phase) / tail)
            kcol[x] = k
        out.append(scale(rgb, k))
    return out


def rainbow_frame(phase, tail=8.0):
    """彩色流水: 色相随波前滚动, 亮度衰减同 flow_frame(平滑)"""
    cache = {}
    out = []
    for (x, _y) in VPOSITIONS:
        c = cache.get(x)
        if c is None:
            d = (x - phase) % GRID_PERIOD
            d = d if d < GRID_PERIOD / 2 else d - GRID_PERIOD
            k = _falloff(1.0 - abs(d) / tail)
            hue = ((x * 10 + phase * 8) % 360) / 360.0
            c = _hsv(hue, 1.0, k)
            cache[x] = c
        out.append(c)
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
    """排内流水: 按键亮度随距波前距离衰减(平滑曲线, 同 flow_frame);
    返回 {key: color}, 暗键为 (0,0,0)"""
    out = {}
    for i in keys:
        x = VPOSITIONS[i][0]
        k = _falloff(1.0 - abs(x - phase) / tail)
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


def row_breathe_map(keys, rgb, t, period=2.0, lit_count=None):
    """排内呼吸: 亮格(或整排)亮度正弦起伏; 返回 {key: color}, 暗键不出现。

    与流水的区别: 没有波前, 整排(亮区)同步渐明渐暗。rgb 传入前应已按
    亮度配置缩放, 这里只乘呼吸包络。lit_count 语义同 lit_row_flow_map。
    """
    k = 0.5 - 0.5 * math.cos(2 * math.pi * (t % period) / period)
    c = scale(rgb, k)
    if lit_count is None or lit_count >= len(keys):
        return {i: c for i in keys}
    lit_count = max(0, min(len(keys), int(lit_count)))
    ordered = sorted(keys, key=lambda i: VPOSITIONS[i][0])
    return {i: c for i in ordered[:lit_count]}
