"""Low-frequency moiré diagnostics measured from the real rasterized plates.

The diagnosis never subtracts two screen angles.  It renders the actual binary
plates with the active settings, takes windowed 2-D FFTs of each plate and of
the overlap/preview signals, and matches measured low-frequency peaks against
an analytic catalogue of diamond-lattice harmonics, pixel-grid aliasing and
between-channel beat frequencies.  When the measurement cannot support a
reliable verdict, the reasons are reported instead of a guess.
"""
import hashlib
import json
import math
import numpy as np
from PIL import Image
from scipy.ndimage import maximum_filter

from .core import (INKS, encode, finite, proof, screen_plates, separate,
                   source_canvas)
from image_lab.validation import Invalid

F0_BAND = 0.5          # low frequency band: below half the screen frequency
SELF_ORDER = 2         # lattice harmonics kept for self-aliasing catalogue
PAIR_ORDER = 2         # harmonics combined when enumerating pair beats
PEAK_LIMIT = 10
MIN_SIDE_PX = 64
MIN_PERIODS = 6.0
PATCH_PX = 288         # synthetic flat-tone patch used to verify candidates


# ---------------------------------------------------------------------------
# input identity: diagnosis, advice and later delivery must share one revision
# ---------------------------------------------------------------------------

def fingerprint(source_name, p):
    payload = json.dumps({'source': source_name, 'settings': p},
                         sort_keys=True, ensure_ascii=False, default=float)
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()[:16]


def natural_angle(angle):
    """Screen angles are periodic at 180 degrees; keep them in (-90, 90]."""
    return ((float(angle) + 90.0) % 180.0) - 90.0


# ---------------------------------------------------------------------------
# analytic frequency catalogue (cycles per mm, then sampled-grid aliasing)
# ---------------------------------------------------------------------------

def _basis(angle, lpi):
    theta = math.radians(angle)
    f = lpi / 25.4
    e1 = f * np.array([math.cos(theta), math.sin(theta)])
    e2 = f * np.array([-math.sin(theta), math.cos(theta)])
    return e1, e2


def _orders(max_order):
    return [(m, n) for m in range(-max_order, max_order + 1)
            for n in range(-max_order, max_order + 1)
            if 0 < abs(m) + abs(n) <= max_order]


def _harmonic(basis, order):
    e1, e2 = basis
    m, n = order
    return m * e1 + n * e2


def _alias(freq, sample_rate):
    """Wrap a continuous spatial frequency onto the discrete pixel grid."""
    return np.mod(freq + sample_rate / 2.0, sample_rate) - sample_rate / 2.0


def _catalogue(p, enabled):
    """All low-order aliased harmonics and between-channel beat vectors."""
    fs = p['dpi'] / 25.4
    f0 = p['lpi'] / 25.4
    bases = [_basis(p['angles'][i], p['lpi']) for i in range(4)]
    aliased = {}
    entries = []
    for i in range(4):
        if not enabled[i]:
            continue
        vecs = []
        for order in _orders(SELF_ORDER):
            k = _alias(_harmonic(bases[i], order), fs)
            vecs.append((order, k))
            entries.append({'kind': 'self_alias', 'channels': [INKS[i]],
                            'order': list(order), 'freq': k})
        aliased[i] = vecs
    for i in range(4):
        if not enabled[i]:
            continue
        for j in range(i + 1, 4):
            if not enabled[j]:
                continue
            for oi, ki in aliased[i]:
                for oj, kj in aliased[j]:
                    if abs(oi[0]) + abs(oi[1]) > PAIR_ORDER or abs(oj[0]) + abs(oj[1]) > PAIR_ORDER:
                        continue
                    beat = ki - kj
                    entries.append({'kind': 'pair_beat',
                                    'channels': [INKS[i], INKS[j]],
                                    'order': [list(oi), list(oj)],
                                    'freq': beat, 'ratio': float(np.hypot(*beat) / f0)})
    return entries, bases, aliased


# ---------------------------------------------------------------------------
# windowed FFT and peak picking
# ---------------------------------------------------------------------------

def _spectrum(values, mm_per_px):
    h, w = values.shape
    window = np.hanning(h)[:, None] * np.hanning(w)[None, :]
    signal = (values - values.mean()) * window
    spec = np.abs(np.fft.rfft2(signal))
    fx = np.fft.rfftfreq(w, mm_per_px)
    fy = np.fft.fftfreq(h, mm_per_px)
    grid_x = fx[None, :]
    grid_y = fy[:, None]
    floor = np.median(spec[spec > 0]) if np.any(spec > 0) else 1e-12
    prominence = spec / max(floor, 1e-12)
    return spec, prominence, grid_x, grid_y


def _pick_peaks(prominence, grid_x, grid_y, f0, limit=PEAK_LIMIT):
    """Local maxima inside the low-frequency band, conjugate half-plane only."""
    h, w = prominence.shape
    local = maximum_filter(prominence, size=3, mode='nearest')
    candidates = np.argwhere((prominence == local) & (prominence >= 8.0))
    peaks = []
    for row, col in candidates:
        fx = float(grid_x[0, col])
        fy = float(grid_y[row, 0])
        if row == 0 and col == 0:
            continue
        if fy < 0 or (abs(fy) < 1e-12 and fx < 0):
            continue
        freq = math.hypot(fx, fy)
        if freq >= F0_BAND * f0:
            continue
        peaks.append({'freq': np.array([fx, fy]), 'prominence': float(prominence[row, col])})
    peaks.sort(key=lambda item: item['prominence'], reverse=True)
    kept = []
    for peak in peaks:
        if all(np.hypot(*(peak['freq'] - other['freq'])) > 1.5 * f0 / 40.0 for other in kept):
            kept.append(peak)
        if len(kept) >= limit:
            break
    return kept


def _low_band_ratio(spec, grid_x, grid_y, f0):
    radius = np.hypot(np.broadcast_to(grid_x, spec.shape),
                      np.broadcast_to(grid_y, spec.shape))
    energy = spec ** 2
    total = energy.sum() - energy[0, 0]
    if total <= 0:
        return 0.0
    low = energy[(radius < F0_BAND * f0) & (radius > 0)].sum()
    return float(low / total)


def _match(peak, entries, tolerance, prefer=None):
    best = None
    for entry in entries:
        if prefer and entry['kind'] != prefer:
            continue
        distance = float(np.hypot(*(peak['freq'] - entry['freq'])))
        if distance <= tolerance and (best is None or distance < best[0]):
            best = (distance, entry)
    return best[1] if best else None


def _peak_payload(peak, f0, mm_per_px, entry, content_related):
    fx, fy = peak['freq']
    freq_hz = math.hypot(fx, fy)
    angle = math.degrees(math.atan2(fy, fx))
    return {
        'freq_cycles_mm': [round(fx, 5), round(fy, 5)],
        'period_mm': round(1.0 / freq_hz, 2) if freq_hz > 0 else None,
        'angle_deg': round(angle, 1),
        'period_cells': round(f0 / freq_hz, 1) if freq_hz > 0 else None,
        'prominence': round(peak['prominence'], 1),
        'kind': entry['kind'] if entry else ('content' if content_related else 'unexplained'),
        'channels': entry['channels'] if entry else None,
        'order': entry['order'] if entry else None,
        'content_related': bool(content_related),
    }


# ---------------------------------------------------------------------------
# region crops
# ---------------------------------------------------------------------------

def _region_bounds(p, rect):
    h, w = (max(1, round(p['height_mm'] / 25.4 * p['dpi'])),
            max(1, round(p['width_mm'] / 25.4 * p['dpi'])))
    if rect is None:
        rect = [0.2, 0.2, 0.8, 0.8]
    if not isinstance(rect, list) or len(rect) != 4:
        raise Invalid('诊断区域须为四项归一化坐标')
    l, t, r, b = [finite(v, '诊断区域', 0, 1) for v in rect]
    if not l < r or not t < b:
        raise Invalid('诊断区域为空')
    x0, x1 = max(0, int(l * w)), min(w, math.ceil(r * w))
    y0, y1 = max(0, int(t * h)), min(h, math.ceil(b * h))
    x1 = max(x1, x0 + 1)
    y1 = max(y1, y0 + 1)
    return [y0, x0, y1, x1], [x0 / w, y0 / h, x1 / w, y1 / h]


def _luma(rgb, p):
    from .core import linear
    return (linear(rgb) * np.array([0.2126, 0.7152, 0.0722])).sum(axis=2)


# ---------------------------------------------------------------------------
# candidate angle sets, verified with the real rasterizer on flat patches
# ---------------------------------------------------------------------------

FAMILIES = [
    ('经典四分色 15/75/0/45', (15.0, 75.0, 0.0, 45.0)),
    ('偏转四分色 22.5/82.5/7.5/52.5', (22.5, 82.5, 7.5, 52.5)),
    ('三等分网角 15/75/30/45', (15.0, 75.0, 30.0, 45.0)),
]
ANCHORS = (0.0, 7.5, 15.0, 22.5, 30.0)


def _candidate_sets(p, enabled):
    current = [natural_angle(a) for a in p['angles']]
    seen = {tuple(round(a, 2) for a in current)}
    sets = [('当前手动网角', current)]
    for anchor in ANCHORS:
        for label, offsets in FAMILIES:
            angles = []
            for i in range(4):
                value = natural_angle(anchor + offsets[i]) if enabled[i] else current[i]
                angles.append(value)
            key = tuple(round(a, 2) for a in angles)
            if key not in seen:
                seen.add(key)
                sets.append((f'{label}（整体锚点 {anchor:g}°）', angles))
    return sets


def _analytic_score(p, angles, enabled):
    """Penalty for aliased harmonics and pair beats landing near zero."""
    trial = {**p, 'angles': angles}
    entries, _, _ = _catalogue(trial, enabled)
    f0 = p['lpi'] / 25.4
    self_penalty = 0.0
    pair_penalty = 0.0
    per_self = {}
    per_pair = {}
    for entry in entries:
        ratio = float(np.hypot(*entry['freq']) / f0)
        if entry['kind'] == 'self_alias':
            weight = 1.0 / (abs(entry['order'][0]) + abs(entry['order'][1]))
            penalty = weight * max(0.0, 1.0 - ratio / F0_BAND) ** 2
            self_penalty += penalty
            name = entry['channels'][0]
            per_self[name] = min(per_self.get(name, 1e9), ratio)
        else:
            weight = 1.0
            penalty = weight * max(0.0, 1.0 - ratio / 0.35) ** 2
            pair_penalty += penalty
            name = tuple(entry['channels'])
            per_pair[name] = min(per_pair.get(name, 1e9), ratio)
    separation = 0.0
    active = [i for i in range(4) if enabled[i]]
    for a in range(len(active)):
        for b in range(a + 1, len(active)):
            i, j = active[a], active[b]
            delta = abs(natural_angle(angles[i] - angles[j]))
            delta = min(delta, 180.0 - delta)
            if delta < 15.0:
                separation += (15.0 - delta) / 15.0
    score = self_penalty + 2.0 * pair_penalty + separation
    return score, per_self, per_pair


def _flat_patch(p, angles, enabled):
    n = PATCH_PX
    cmyk = np.zeros((n, n, 4))
    for i in range(4):
        cmyk[:, :, i] = 0.45 if enabled[i] else 0.0
    trial = {**p, 'angles': angles}
    plates = screen_plates(cmyk, trial)
    overlay = plates[:, :, enabled].mean(axis=2)
    return overlay


def _sim_metric(p, angles, enabled):
    mm_per_px = 25.4 / p['dpi']
    f0 = p['lpi'] / 25.4
    overlay = _flat_patch(p, angles, enabled)
    spec, prominence, gx, gy = _spectrum(overlay, mm_per_px)
    ratio = _low_band_ratio(spec, gx, gy, f0)
    peaks = _pick_peaks(prominence, gx, gy, f0)
    return ratio, len(peaks)


def _recommend(p, enabled, current_angles):
    evaluated = []
    current_sim, current_peaks = _sim_metric(p, current_angles, enabled)
    for label, angles in _candidate_sets(p, enabled):
        analytic, per_self, per_pair = _analytic_score(p, angles, enabled)
        sim_ratio, sim_peaks = _sim_metric(p, angles, enabled)
        improvement = 0.0 if current_sim <= 0 else (current_sim - sim_ratio) / current_sim
        evaluated.append({'label': label, 'angles': [round(a, 2) for a in angles],
                          'analytic_score': round(analytic, 4),
                          'sim_low_band': round(sim_ratio, 5),
                          'sim_low_peaks': sim_peaks,
                          'expected_improvement': round(improvement, 3),
                          'is_current': label == '当前手动网角'})
    alternatives = sorted((c for c in evaluated if not c['is_current']),
                          key=lambda c: (c['sim_low_band'], c['analytic_score']))
    current = next(c for c in evaluated if c['is_current'])
    candidates = alternatives[:3]
    return current, candidates, current_sim, current_peaks


# ---------------------------------------------------------------------------
# main diagnosis
# ---------------------------------------------------------------------------

def diagnose(source, p, rect=None):
    p = dict(p)
    enabled = list(p['enabled'])
    reasons = []
    notes = []
    if not p['screened']:
        return {'reliable': False,
                'reasons': ['当前设置关闭了网点（连续调输出），不存在调幅加网网花可诊断。'],
                'notes': [], 'candidates': []}
    active = [i for i in range(4) if enabled[i]]
    if len(active) < 2:
        reasons = ['启用通道少于两个，通道间拍频网花无法评估，仅能检查单通道采样混叠。']
    if not active:
        return {'reliable': False,
                'reasons': ['没有启用的印版通道，无法进行网花诊断。'],
                'notes': [], 'candidates': []}

    (y0, x0, y1, x1), norm_rect = _region_bounds(p, rect)
    rgb, _ = source_canvas(source, p)
    cmyk = separate(rgb, p)
    plates = screen_plates(cmyk, p)
    proof_continuous = proof(cmyk, p)

    mm_per_px = 25.4 / p['dpi']
    f0 = p['lpi'] / 25.4
    width_mm = (x1 - x0) * mm_per_px
    height_mm = (y1 - y0) * mm_per_px
    periods = [width_mm * f0, height_mm * f0]
    if min(x1 - x0, y1 - y0) < MIN_SIDE_PX:
        reasons.append(f'诊断区域不足 {MIN_SIDE_PX} 像素，频率分辨率过低。')
    if min(periods) < MIN_PERIODS:
        reasons.append(f'诊断区域仅覆盖约 {min(periods):.1f} 个网胞（建议≥{MIN_PERIODS:g}），低频周期估计不稳定。')

    entries, bases, aliased = _catalogue(p, enabled)
    tolerance = max(1.5 / min(width_mm, height_mm), 0.08 * f0)

    crop_plates = plates[y0:y1, x0:x1]
    overlay = crop_plates[:, :, active].mean(axis=2)
    if overlay.mean() < 0.02:
        reasons.append('该区域几乎没有落墨，缺少可分析的网点信号。')
    elif overlay.mean() > 0.98:
        reasons.append('该区域近乎全墨，网点纹理信息不足。')

    spec, prominence, gx, gy = _spectrum(overlay, mm_per_px)
    cont_luma = _luma(proof_continuous[y0:y1, x0:x1], p)
    cont_spec, cont_prom, cgx, cgy = _spectrum(cont_luma, mm_per_px)
    low_ratio = _low_band_ratio(spec, gx, gy, f0)
    cont_ratio = _low_band_ratio(cont_spec, cgx, cgy, f0)
    screen_excess = max(0.0, low_ratio - cont_ratio)

    def content_near(peak):
        distance = np.hypot(cgx - peak['freq'][0], cgy - peak['freq'][1])
        idx = np.unravel_index(np.argmin(distance), distance.shape)
        return float(distance[idx]) <= tolerance and float(cont_prom[idx]) >= 8.0

    raw_peaks = _pick_peaks(prominence, gx, gy, f0)
    peaks = []
    unexplained = 0
    pair_weight = {}
    for peak in raw_peaks:
        entry = _match(peak, entries, tolerance, prefer='pair_beat')
        if entry is None:
            entry = _match(peak, entries, tolerance)
        related = content_near(peak)
        if entry is not None and entry['kind'] == 'pair_beat':
            key = ''.join(entry['channels'])
            pair_weight[key] = pair_weight.get(key, 0.0) + peak['prominence']
        elif entry is None and not related:
            unexplained += 1
        payload = _peak_payload(peak, f0, mm_per_px, entry, related)
        peaks.append(payload)
    if raw_peaks and unexplained / len(raw_peaks) > 0.5:
        reasons.append('超过一半的低频峰无法由加网谐波/混叠目录或图像内容解释，可能受裁切边缘与窗口伪影影响，结论置信度下降。')

    # per-channel measured self-aliasing
    channel_alias = []
    for i in active:
        band = [e for e in entries if e['kind'] == 'self_alias' and e['channels'] == [INKS[i]]]
        ratios = sorted(float(np.hypot(*e['freq']) / f0) for e in band)
        min_ratio = ratios[0] if ratios else None
        cspec, cprom, cgx2, cgy2 = _spectrum(crop_plates[:, :, i], mm_per_px)
        cpeaks = _pick_peaks(cprom, cgx2, cgy2, f0, limit=3)
        observed = [_peak_payload(pk, f0, mm_per_px,
                                  _match(pk, band, tolerance, prefer='self_alias'),
                                  False) for pk in cpeaks]
        channel_alias.append({'channel': INKS[i],
                              'min_alias_ratio': round(min_ratio, 3) if min_ratio is not None else None,
                              'alias_period_cells': round(1.0 / min_ratio, 1) if min_ratio else None,
                              'observed_low_peaks': observed})

    pair_contributions = []
    for i in active:
        for j in active:
            if j <= i:
                continue
            pair_entries = [e for e in entries if e['kind'] == 'pair_beat'
                            and e['channels'] == [INKS[i], INKS[j]]]
            if not pair_entries:
                continue
            min_beat = min(e['ratio'] for e in pair_entries)
            pair_contributions.append(
                {'channels': [INKS[i], INKS[j]],
                 'score': round(pair_weight.get(INKS[i] + INKS[j], 0.0), 1),
                 'min_beat_ratio': round(min_beat, 3),
                 'beat_period_cells': round(1.0 / min_beat, 1) if min_beat > 0 else None})
    pair_contributions.sort(key=lambda item: item['score'], reverse=True)

    beat_prominence = sum(item['score'] for item in pair_contributions)
    alias_prominence = sum(
        pk['prominence'] for info in channel_alias for pk in info['observed_low_peaks']
        if pk['kind'] == 'self_alias')
    score = 100.0 * (0.5 * min(beat_prominence / 100.0, 1.0)
                    + 0.3 * min(alias_prominence / 100.0, 1.0)
                    + 0.2 * min(screen_excess / 0.15, 1.0))
    score = round(float(np.clip(score, 0, 100)), 1)
    level = ('明显' if score >= 60 else '中等' if score >= 35 else
             '轻微' if score >= 15 else '未检出')

    pixels_per_cell = p['dpi'] / p['lpi']
    if pixels_per_cell < 4.0:
        notes.append(f'当前每网胞仅 {pixels_per_cell:.2f} 像素，采样混叠无法仅靠网角消除，建议提高DPI或降低LPI。')
    elif pixels_per_cell < 8.0:
        notes.append(f'每网胞 {pixels_per_cell:.2f} 像素，混叠风险偏高，网角调整只能缓解。')

    current_angles = [natural_angle(a) for a in p['angles']]
    candidates = []
    current_sim = None
    if len(active) >= 2:
        _, candidates, current_sim, _ = _recommend(p, enabled, current_angles)
        if current_sim is not None:
            for cand in candidates:
                if cand['expected_improvement'] < 0.05:
                    cand['warnings'] = ['仿真改善不足5%，当前手动网角可能已接近该DPI/LPI下的可用极限。']
                else:
                    cand['warnings'] = []
        if pixels_per_cell < 4.0:
            notes.append('候选网角已按当前DPI/LPI仿真排序，但严重欠采样条件下不承诺消除网花。')

    reliable = not reasons
    return {
        'reliable': reliable,
        'reasons': reasons,
        'notes': notes,
        'context': {
            'dpi': p['dpi'], 'lpi': p['lpi'],
            'pixels_per_cell': round(pixels_per_cell, 2),
            'paper': p['paper'],
            'pixel_rect': [x0, y0, x1, y1],
            'rect': [round(v, 4) for v in norm_rect],
            'region_mm': [round(width_mm, 2), round(height_mm, 2)],
            'periods_covered': [round(periods[0], 1), round(periods[1], 1)],
            'active_channels': [INKS[i] for i in active],
        },
        'current': {
            'angles': [round(a, 2) for a in current_angles],
            'moire_score': score,
            'level': level,
            'low_band_ratio': round(low_ratio, 5),
            'content_low_band_ratio': round(cont_ratio, 5),
            'screen_excess': round(screen_excess, 5),
            'beat_prominence': round(beat_prominence, 1),
            'peaks': peaks,
            'pair_contributions': pair_contributions,
            'channel_alias': channel_alias,
            'sim_low_band': round(current_sim, 5) if current_sim is not None else None,
        },
        'candidates': candidates,
    }


# ---------------------------------------------------------------------------
# same-region before/after preview, rendered on demand (nothing is persisted)
# ---------------------------------------------------------------------------

def preview(source, p, rect, angles, view):
    p = dict(p)
    (y0, x0, y1, x1), _ = _region_bounds(p, rect)
    rgb, _ = source_canvas(source, p)
    cmyk = separate(rgb, p)
    # render only the diagnosed crop, keeping physical mm coordinates via origin
    crop = cmyk[y0:y1, x0:x1]
    before = screen_plates(crop, p, origin=(x0, y0))
    trial = {**p, 'angles': [float(a) for a in angles]}
    after = screen_plates(crop, trial, origin=(x0, y0))
    if view == 'proof':
        image_before = proof(before, p)
        image_after = proof(after, p)
        gap = np.full((image_before.shape[0],
                       max(4, image_before.shape[1] // 40),
                       image_before.shape[2]), 0.75)
    else:
        if view not in INKS:
            raise Invalid('预览通道无效')
        i = INKS.index(view)
        image_before = (1.0 - before[:, :, i])[:, :, None]
        image_after = (1.0 - after[:, :, i])[:, :, None]
        gap = np.full((image_before.shape[0],
                       max(4, image_before.shape[1] // 40), 1), 0.75)
    combined = np.concatenate([image_before, gap, image_after], axis=1)
    if combined.shape[2] == 1:
        combined = combined[:, :, 0]
    pic = Image.fromarray(np.rint(np.clip(combined, 0, 1) * 255).astype('uint8'))
    return encode(pic)
