"""Per-plate measured ink response calibration with monotone compensation.

Measurements are (nominal input, measured coverage) pairs in 0..1 per ink.
Duplicate levels are averaged with their repetition kept as fit weight and
small dips are pooled by a weighted isotonic (non-decreasing) fit, so the
response never reverses and no measured point is ever dropped. The inverse
of that monotone response maps desired coverage back to the nominal ink to
send to screening; flats invert to the midpoint of the level interval, so
the compensation is bounded in [0,1] and can never go negative.
"""
import hashlib
import json
import numpy as np
from image_lab.validation import Invalid

INKS = ('C', 'M', 'Y', 'K')
MAX_POINTS = 64
GRID = 65
LUT = 257


def default():
    return {'enabled': False, 'label': '', 'inks': {name: [] for name in INKS}}


def point(pair):
    if not isinstance(pair, (list, tuple)) or len(pair) != 2:
        raise Invalid('测量点须为[输入墨量,实际覆盖]数对')
    out = []
    for value, label in zip(pair, ('输入墨量', '实际覆盖')):
        try:
            number = float(value)
        except (TypeError, ValueError):
            raise Invalid(label + '必须为数字')
        if not 0 <= number <= 1:
            raise Invalid(label + '须在0至1之间')
        out.append(number)
    return out


def validate(raw):
    if raw is None:
        return default()
    if not isinstance(raw, dict):
        raise Invalid('校准数据须为对象')
    inks = raw.get('inks', {})
    if not isinstance(inks, dict):
        raise Invalid('校准通道须为对象')
    label = ''.join(c for c in str(raw.get('label', '')) if ord(c) >= 32)[:60]
    clean = {'enabled': bool(raw.get('enabled', False)), 'label': label, 'inks': {}}
    for name in INKS:
        pairs = inks.get(name, [])
        if not isinstance(pairs, list):
            raise Invalid('校准测量须为列表')
        if len(pairs) > MAX_POINTS:
            raise Invalid(f'每版最多{MAX_POINTS}个测量点')
        clean['inks'][name] = [point(p) for p in pairs]
    return clean


def merge(points):
    """Group identical input levels; every reading stays as a group member."""
    groups = {}
    for x, y in points:
        groups.setdefault(x, []).append(y)
    xs = sorted(groups)
    return xs, [groups[x] for x in xs]


def isotonic(values, weights):
    """Weighted pool-adjacent-violators fit, non-decreasing."""
    blocks = []
    for v, w in zip(values, weights):
        blocks.append([w, v * w, 1])
        while len(blocks) > 1 and blocks[-2][1] / blocks[-2][0] >= blocks[-1][1] / blocks[-1][0]:
            w2, s2, n2 = blocks.pop()
            w1, s1, n1 = blocks.pop()
            blocks.append([w1 + w2, s1 + s2, n1 + n2])
    out = []
    for w, s, n in blocks:
        out.extend([s / w] * n)
    return out


def tidied(points):
    """Monotone response knots from raw measurements, or None if under 2 levels.

    Duplicates are averaged (repetition kept as weight), dips pooled by the
    isotonic fit; the curve is then anchored at (0,0) when no zero level was
    measured and extended to full input with the last fitted slope, clamped
    to [0,1]. xs strictly increase, ys never decrease.
    """
    xs, groups = merge(points)
    if len(xs) < 2:
        return None
    means = [sum(g) / len(g) for g in groups]
    fitted = isotonic(means, [len(g) for g in groups])
    if xs[0] > 0:
        xs = [0.] + xs
        fitted = [0.] + fitted
    if xs[-1] < 1:
        slope = (fitted[-1] - fitted[-2]) / (xs[-1] - xs[-2])
        fitted = fitted + [min(1., fitted[-1] + max(0., slope) * (1 - xs[-1]))]
        xs = xs + [1.]
    return xs, fitted


def invert(knots, q):
    """Nominal ink to send for desired coverage q; flats map to their midpoint."""
    xs, ys = knots
    if q <= ys[0]:
        return xs[0]
    if q >= ys[-1]:
        return xs[-1]
    hits = [i for i, y in enumerate(ys) if y == q]
    if hits:
        return (xs[hits[0]] + xs[hits[-1]]) / 2
    for i in range(len(xs) - 1):
        if ys[i] < q < ys[i + 1]:
            t = (q - ys[i]) / (ys[i + 1] - ys[i])
            return xs[i] + t * (xs[i + 1] - xs[i])
    return xs[-1]


def compensation_lut(knots, n=LUT):
    grid = np.linspace(0, 1, n)
    return grid, np.array([invert(knots, float(q)) for q in grid])


def compensate(cmyk, calibration):
    """Remap nominal tones through each plate's inverse response curve."""
    if not calibration or not calibration.get('enabled'):
        return cmyk
    out = cmyk.copy()
    for i, name in enumerate(INKS):
        knots = tidied(calibration.get('inks', {}).get(name, []))
        if knots is None:
            continue
        grid, lut = compensation_lut(knots)
        out[:, :, i] = np.interp(cmyk[:, :, i], grid, lut)
    return out


def fingerprint(calibration):
    clean = validate(calibration)
    canonical = json.dumps({'enabled': clean['enabled'], 'inks': clean['inks']},
                           sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


def snapshot(calibration):
    """Self-contained calibration identity stored on each rendered result."""
    clean = validate(calibration)
    return {'enabled': clean['enabled'], 'label': clean['label'],
            'fingerprint': fingerprint(clean), 'inks': clean['inks']}


def summary(calibration):
    clean = validate(calibration)
    return {'enabled': clean['enabled'], 'label': clean['label'],
            'fingerprint': fingerprint(clean),
            'points': {name: len(clean['inks'][name]) for name in INKS}}


def derive(calibration):
    """Tidied response, compensation curve and before/after samples for display."""
    clean = validate(calibration)
    grid = np.linspace(0, 1, GRID)
    result = {'enabled': clean['enabled'], 'label': clean['label'],
              'fingerprint': fingerprint(clean),
              'grid': [round(float(g), 4) for g in grid], 'inks': {}}
    for name in INKS:
        points = clean['inks'][name]
        entry = {'raw': points, 'merged': [], 'knots': None, 'response': None,
                 'compensation': None, 'residual_max': None, 'residual_mean': None,
                 'samples': []}
        knots = tidied(points)
        if knots is not None:
            xs, ys = knots
            entry['knots'] = [[float(x), float(y)] for x, y in zip(xs, ys)]
            ux, groups = merge(points)
            entry['merged'] = [{'x': x, 'mean': sum(g) / len(g), 'n': len(g),
                                'min': min(g), 'max': max(g),
                                'fitted': float(np.interp(x, xs, ys))}
                               for x, g in zip(ux, groups)]
            response = np.interp(grid, xs, ys)
            entry['response'] = [round(float(v), 6) for v in response]
            entry['compensation'] = [round(invert(knots, float(q)), 6) for q in grid]
            dev = [abs(y - float(np.interp(x, xs, ys))) for x, y in points]
            entry['residual_max'] = max(dev)
            entry['residual_mean'] = sum(dev) / len(dev)
            for desired in (.1, .25, .5, .75, .9):
                send = invert(knots, desired)
                entry['samples'].append({
                    'desired': desired,
                    'before': float(np.interp(desired, xs, ys)),
                    'send': send,
                    'after': float(np.interp(send, xs, ys))})
        result['inks'][name] = entry
    return result
