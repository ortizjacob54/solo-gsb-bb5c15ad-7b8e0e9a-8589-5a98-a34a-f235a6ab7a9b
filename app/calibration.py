"""纯计算逻辑：异常判定、校准偏差、线性插值、分段线性积分、超限区间求解、预算累积反解与最长稳定窗口求解。不触碰数据库，便于单测。"""
from __future__ import annotations

import math
from collections import deque
from itertools import combinations
from statistics import median


class CalibrationError(ValueError):
    """输入读数无法计算有效校准偏差时抛出。"""


def split_outliers(readings: list[float], tolerance: float) -> tuple[float, list[float], list[float]]:
    """按“偏离组中位数超过容差即异常”拆分读数。

    返回 (组中位数, 有效读数, 异常读数)。
    """
    group_median = median(readings)
    inliers = [r for r in readings if abs(r - group_median) <= tolerance]
    outliers = [r for r in readings if abs(r - group_median) > tolerance]
    return group_median, inliers, outliers


def compute_bias(reference: float, tolerance: float, readings: list[float]) -> tuple[float, float, list[float], list[float]]:
    """校准偏差 = 参考值 - 有效读数（剔除异常后）的中位数。

    返回 (bias, 组中位数, 有效读数, 异常读数)。
    """
    if len(readings) < 3:
        raise CalibrationError(f"need at least 3 readings, got {len(readings)}")
    group_median, inliers, outliers = split_outliers(readings, tolerance)
    if not inliers:
        raise CalibrationError(
            "all readings deviate from the group median by more than the tolerance; "
            "cannot compute a calibration bias"
        )
    bias = reference - median(inliers)
    return bias, group_median, inliers, outliers


def interpolate(t: float, t0: float, b0: float, t1: float, b1: float) -> float:
    """在相邻时点 (t0, b0) 与 (t1, b1) 之间对 t 做线性插值。"""
    if t1 == t0:
        return b0
    return b0 + (b1 - b0) * (t - t0) / (t1 - t0)


def integrate_segment(t0: float, b0: float, t1: float, b1: float) -> tuple[float, float]:
    """对线性段 (t0, b0)-(t1, b1) 下的面积做解析积分，返回 (signed, absolute)。

    signed 为梯形面积 (b0+b1)/2·Δt。absolute 为 |bias| 的面积：端点严格异号时
    段内恰有一个零点 t* = t0 − b0·Δt/(b1−b0)，在 t* 处拆成两个三角形求和
    （面积为 (b0² + b1²)·Δt / (2·|b1−b0|)）；否则 |bias| 不变号，面积即 |signed|。
    全程解析解，不做固定步长采样。
    """
    dt = t1 - t0
    if dt <= 0:
        return 0.0, 0.0
    signed = (b0 + b1) * 0.5 * dt
    if b0 * b1 < 0:  # 严格异号：段内恰有一个零点，按解析交点拆分
        absolute = (b0 * b0 + b1 * b1) * 0.5 * dt / abs(b1 - b0)
    else:  # 同号或端点触零：不变号
        absolute = abs(signed)
    return signed, absolute


def abs_subsegments(t0: float, b0: float, t1: float, b1: float) -> list[tuple[float, float, float, float]]:
    """把线性 bias 段在解析过零点拆分，返回 |bias| 线性的子段 [(s, |b_s|, e, |b_e|)]。

    端点严格异号时段内恰有一个零点 t* = t0 − b0·Δt/(b1−b0)，在 t* 处一分为二；
    否则 |bias| 全程线性，原段直接返回。每个子段上 |bias| 都是线性函数，
    其累积面积是时长的二次函数，可解析反解（见 time_to_accumulate）。
    """
    if b0 * b1 < 0:  # 严格异号：段内恰有一个零点
        t_cross = t0 - b0 * (t1 - t0) / (b1 - b0)
        return [(t0, abs(b0), t_cross, 0.0), (t_cross, 0.0, t1, abs(b1))]
    return [(t0, abs(b0), t1, abs(b1))]


def time_to_accumulate(abs_b0: float, abs_b1: float, duration: float, area: float) -> float:
    """反解 |bias| 线性段的累积函数：从段首累积 area（|bias|·秒）所需的时长。

    |bias| 从 abs_b0 线性变化到 abs_b1（斜率 k = (abs_b1 − abs_b0)/duration），
    累积面积 A(Δt) = abs_b0·Δt + k·Δt²/2 是 Δt 的二次函数，解析求根：
    k ≠ 0 时取数值稳定形式 Δt = 2·area / (abs_b0 + √(abs_b0² + 2k·area))，
    k = 0 时退化为线性 Δt = area / abs_b0。全程解析解，不做固定步长采样。
    调用前须保证 0 < area ≤ 段内总面积（段内 |bias| 不恒为零）；
    判别式与结果做浮点误差钳制，area 恰等于段内总面积时返回 duration。
    """
    k = (abs_b1 - abs_b0) / duration
    if k == 0.0:  # |bias| 恒定：累积随时间线性增长
        return area / abs_b0
    discriminant = max(0.0, abs_b0 * abs_b0 + 2.0 * k * area)
    offset = 2.0 * area / (abs_b0 + math.sqrt(discriminant))
    return min(offset, duration)


def _above_threshold(t0: float, v0: float, t1: float, v1: float, limit: float) -> tuple[float, float] | None:
    """线性函数 v 在 [t0, t1] 上严格大于 limit 的子区间端点 (s, e)；无则 None。

    恰等于 limit 不计入；超限集本身是开/半开区间，这里返回其闭包端点
    （端点瞬时时长为零，不影响持续秒数与合并判定）。
    """
    dt = t1 - t0
    if dt <= 0:
        return None
    above0 = v0 > limit
    above1 = v1 > limit
    if above0 and above1:
        return t0, t1
    if not above0 and not above1:
        return None
    # 恰一端超限：解析交点 t* = t0 + (limit − v0)·Δt/(v1 − v0)
    t_cross = t0 + (limit - v0) * dt / (v1 - v0)
    if above0:  # v0 > limit ≥ v1：段首到交点
        return t0, t_cross
    return t_cross, t1  # v1 > limit ≥ v0：交点到段尾


def exceedance_subranges(t0: float, b0: float, t1: float, b1: float, limit: float) -> list[tuple[float, float, str]]:
    """求线性段 (t0,b0)-(t1,b1) 内 |bias| > limit 的解析子区间。

    返回 [(s, e, direction), ...]，按时间排序，至多两段：高端（bias > limit，
    direction="high"）与低端（bias < −limit，direction="low"，通过对 bias 取负
    复用同一交点求解）。恰等于 limit 不计入。全程解析解，不做固定步长采样。
    """
    out = []
    high = _above_threshold(t0, b0, t1, b1, limit)
    if high is not None:
        out.append((high[0], high[1], "high"))
    low = _above_threshold(t0, -b0, t1, -b1, limit)
    if low is not None:
        out.append((low[0], low[1], "low"))
    out.sort(key=lambda r: r[0])  # 同一段内高低两段不重叠，按起点排即时间序
    return out


# ---------- 最长稳定窗口（max(bias)−min(bias) ≤ max_spread） ----------

def _constraints_for_block(
    a_s: float, a_e: float, y_sp: float, y_eq: float,
    H: float, h: float, spread: float,
) -> list[tuple[float, float, float]]:
    """构造跨段块 (p, q)（p < q）在局部参数 (u, v) ∈ [0,1]² 上的全部线性约束 A·u + B·v ≤ C。

    s 位于入段 [t_p, t_{p+1}]：s = t_p + dt_s·u，s_bias = y_sp + a_s·u（a_s = y_{p+1}−y_p）
    e 位于出段 [t_q, t_{q+1}]：e = t_q + dt_e·v，e_bias = y_eq + a_e·v（a_e = y_{q+1}−y_q）
    块内部包含观测点 t_{p+1}..t_q，其极差为 H−h（H/h 为这些点的最大/最小偏差；
    s ≤ t_{p+1} ≤ t_q ≤ e，故 s ≤ e 自动成立，无需额外约束）。极差 ≤ spread
    等价于 s_bias、e_bias 与内部最值两两之差的绝对值都 ≤ spread，即下列线性
    不等式；可行域为凸多边形，线性目标（窗口时长）的极值必在顶点，只需解析
    枚举边界直线交点，不做任何步长采样。
    """
    # 内部极差 + s/e 分别与 H、h 四对 + s 与 e 一对，共 11 行
    return [
        (0.0, 0.0, spread - (H - h)),
        (a_s, 0.0, spread - (y_sp - H)),
        (-a_s, 0.0, spread - (H - y_sp)),
        (0.0, a_e, spread - (y_eq - H)),
        (0.0, -a_e, spread - (H - y_eq)),
        (a_s, 0.0, spread - (y_sp - h)),
        (-a_s, 0.0, spread - (h - y_sp)),
        (0.0, a_e, spread - (y_eq - h)),
        (0.0, -a_e, spread - (h - y_eq)),
        (a_s, -a_e, spread - (y_sp - y_eq)),
        (-a_s, a_e, spread - (y_eq - y_sp)),
    ]


def _normalized_constraints(
    cons: list[tuple[float, float, float]],
) -> list[tuple[float, float, float]]:
    """按系数行的最大绝对值归一化，使各约束可共用统一的浮点容差。"""
    out = []
    for a, b, c in cons:
        scale = max(abs(a), abs(b), 1.0)
        out.append((a / scale, b / scale, c / scale))
    return out


# 盒边界 0 ≤ u ≤ 1 与 0 ≤ v ≤ 1
_BOX_LINES = [(1.0, 0.0, 0.0), (1.0, 0.0, 1.0), (0.0, 1.0, 0.0), (0.0, 1.0, 1.0)]


def _polygon_best(
    dt_s: float, a_s: float, dt_e: float, a_e: float,
    y_sp: float, y_eq: float, H: float, h: float, spread: float,
    duration_scale: float,
) -> tuple[float, float, float, float] | None:
    """求一个跨段块在 (u, v) 多边形可行域上使窗口最长的顶点，返回 (u, v, min_bias, max_bias)。

    可行域由线性约束 A·u + B·v ≤ C 与盒约束 0 ≤ u,v ≤ 1 交出，是凸多边形；
    时长 e − s = dt_e·v − dt_s·u 为线性目标，最优解必在多边形顶点。顶点由
    两条边界直线（约束直线或盒边界直线）的解析交点给出，逐一求出并按可行性
    过滤，全程无固定步长采样。并列时长取 u 更小（起点 s 更早）者；u 相同再
    取 v 更大者（同样为并列时的确定性选择）。
    """
    rows = _normalized_constraints(
        _constraints_for_block(a_s, a_e, y_sp, y_eq, H, h, spread)
    )
    box = _normalized_constraints(_BOX_LINES)
    lines = rows + box
    tol = 1e-9
    candidates: list[tuple[float, float]] = []
    for (a1, b1, c1), (a2, b2, c2) in combinations(lines, 2):
        det = a1 * b2 - a2 * b1
        if abs(det) < 1e-12:
            continue  # 两线平行（或退化），无唯一交点
        candidates.append(((c1 * b2 - c2 * b1) / det, (a1 * c2 - a2 * c1) / det))

    def feasible(u: float, v: float) -> bool:
        if u < -tol or u > 1.0 + tol or v < -tol or v > 1.0 + tol:
            return False
        return all(a * u + b * v <= c + tol for a, b, c in rows)

    dur_tol = 1e-9 * max(1.0, duration_scale)
    best = None
    for u, v in candidates:
        if not feasible(u, v):
            continue
        u = min(1.0, max(0.0, u))
        v = min(1.0, max(0.0, v))
        s_bias = y_sp + a_s * u
        e_bias = y_eq + a_e * v
        min_bias = min(s_bias, e_bias, H, h)
        max_bias = max(s_bias, e_bias, H, h)
        score = dt_e * v - dt_s * u
        if (
            best is None
            or score > best[0] + dur_tol
            or (
                abs(score - best[0]) <= dur_tol
                and (u < best[1] - tol or (abs(u - best[1]) <= tol and v > best[2] + tol))
            )
        ):
            best = (score, u, v, min_bias, max_bias)
    if best is None:
        return None
    _score, u, v, min_bias, max_bias = best
    return u, v, min_bias, max_bias


def _block_feasible_at_v1(rows: list[tuple[float, float, float]]) -> bool:
    """v=1（e 走到出段末端）时块是否存在可行 u（一维区间交集判定）。"""
    tol = 1e-9
    lo, hi = 0.0, 1.0
    for a, b, c in rows:
        rhs = c - b
        if abs(a) < 1e-12:
            if rhs < -tol:
                return False
            continue
        bound = rhs / a
        if a > 0:
            hi = min(hi, bound)
        else:
            lo = max(lo, bound)
    return lo <= hi + tol


def longest_stability_window(
    nodes: list[tuple[float, float]], frm: float, to: float, max_spread: float
) -> dict:
    """在分段线性 bias 上解析求 [frm, to] 内极差不超过 max_spread 的最长连续子区间。

    nodes 为按时间排序的 (ts, bias) 观测节点；frm/to 可落在插值段内部，先裁剪
    出查询范围内的完整折线（边界处补线性插值端点），记为 t_0..t_m 上的 m 段。

    可行窗口按其起点所在段 p 与终点所在段 q 分“块”枚举：
    - p = q（窗口完全落在单段内）：段内 bias 线性，最长窗口长度为
      min(段长, max_spread/|斜率|)，并列时起点取段首（最早），直接解析给出；
    - p < q（窗口跨过至少一个内部观测点 t_{p+1}..t_q）：起点 s 在入段内由
      u ∈ [0,1] 参数化、终点 e 在出段内由 v ∈ [0,1] 参数化；s/e 偏差与内部
      最值之差全部 ≤ max_spread 给出一组线性不等式，可行 (u,v) 构成凸多边形，
      线性目标 e−s 的最大值必在多边形顶点，由边界直线解析交点精确定位
      （见 _polygon_best），全程无固定步长采样。

    扫描中内部观测点集合用最小/最大两个单调队列维护；对每个出段 q，从最小
    可行 p 开始判定，块在 v=1 都不可行时才弹出该 p（弹出前仍评估其 v<1 的
    可行窗口），每个块至多评估一次，总开销 O(m)。全局并列时长返回起点最早者。
    """
    # 查询范围裁剪：严格位于 (frm, to) 内的观测点，外加两个裁剪边界端点
    inner = [(t, b) for t, b in nodes if frm < t < to]
    b_lo = next((b for t, b in nodes if t == frm), None)
    b_hi = next((b for t, b in nodes if t == to), None)
    if b_lo is None:
        t0, y0, t1, y1 = next(
            (t0, y0, t1, y1)
            for (t0, y0), (t1, y1) in zip(nodes, nodes[1:]) if t0 < frm < t1
        )
        b_lo = interpolate(frm, t0, y0, t1, y1)
    if b_hi is None:
        t0, y0, t1, y1 = next(
            (t0, y0, t1, y1)
            for (t0, y0), (t1, y1) in zip(nodes, nodes[1:]) if t0 < to < t1
        )
        b_hi = interpolate(to, t0, y0, t1, y1)
    pts = [(frm, b_lo)] + inner + [(to, b_hi)]
    times = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    m = len(pts) - 1  # 裁剪后折线段数（frm < to 且在覆盖内，至少为 1）

    best = None  # (duration, start, end, min_bias, max_bias)

    def consider(s: float, e: float, min_bias: float, max_bias: float) -> None:
        nonlocal best
        dur = e - s
        tol = 1e-9 * max(1.0, to - frm)
        if best is None or dur > best[0] + tol or (abs(dur - best[0]) <= tol and s < best[1] - tol):
            best = (dur, s, e, min_bias, max_bias)

    # ---- p = q：窗口完全落在单个线性段内，直接解析 ----
    for q in range(m):
        t0, t1, y0, y1 = times[q], times[q + 1], ys[q], ys[q + 1]
        dt = t1 - t0
        span = abs(y1 - y0)
        if span <= max_spread:
            dur = dt                                  # 整段偏差极差已满足
        else:
            dur = dt * max_spread / span             # 线性段内 |Δbias| = |斜率|·时长
        # 并列时起点取段首（最早）：bias 从 y0 沿段方向走到 y0+(y1−y0)·dur/dt
        s_bias = y0
        e_bias = y0 + (y1 - y0) * dur / dt
        consider(t0, t0 + dur, min(s_bias, e_bias), max(s_bias, e_bias))

    # ---- p < q：跨段块，双指针 + 单调队列 ----
    min_dq: deque[int] = deque()  # 内部观测点中 bias 非降的下标队列（队首为最小）
    max_dq: deque[int] = deque()  # 内部观测点中 bias 非增的下标队列（队首为最大）
    p_lo = 0
    for q in range(1, m):
        if p_lo >= q:
            # p_lo 单调不减：一旦当前 q 无跨段块，后续更大的 q 也不会有
            break
        # 进入出段 q：内部新增观测点 t_q（它正是上一出段 q−1 的末端）
        while max_dq and ys[max_dq[-1]] <= ys[q]:
            max_dq.pop()
        max_dq.append(q)
        while min_dq and ys[min_dq[-1]] >= ys[q]:
            min_dq.pop()
        min_dq.append(q)
        dt_e = times[q + 1] - times[q]
        a_e = ys[q + 1] - ys[q]
        while True:
            while min_dq and min_dq[0] < p_lo + 1:
                min_dq.popleft()
            while max_dq and max_dq[0] < p_lo + 1:
                max_dq.popleft()
            if p_lo >= q:
                break  # 已退化为单段块，由 p=q 分支覆盖
            dt_s = times[p_lo + 1] - times[p_lo]
            a_s = ys[p_lo + 1] - ys[p_lo]
            H, h = ys[max_dq[0]], ys[min_dq[0]]
            rows = _normalized_constraints(
                _constraints_for_block(a_s, a_e, ys[p_lo], ys[q], H, h, max_spread)
            )
            found = _polygon_best(
                dt_s, a_s, dt_e, a_e, ys[p_lo], ys[q], H, h, max_spread, to - frm
            )
            if found is not None:
                u, v, min_bias, max_bias = found
                consider(times[p_lo] + dt_s * u, times[q] + dt_e * v, min_bias, max_bias)
            if _block_feasible_at_v1(rows):
                break  # e 可延伸到 t_{q+1}：更大的 p 对本 q 不再更优
            p_lo += 1

    dur, s, e, min_bias, max_bias = best
    return {
        "t_start": s,
        "t_end": e,
        "duration_seconds": dur,
        "min_bias": min_bias,
        "max_bias": max_bias,
    }
