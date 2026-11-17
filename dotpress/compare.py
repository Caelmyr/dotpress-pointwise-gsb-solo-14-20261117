"""Physical-area comparison of two immutable rendered results.

Geometry contract (identical to core.py):
- page origin is the top-left corner, x points right and y points down, mm
- pixel centers sit at (column+.5, row+.5)*25.4/dpi, so pixel (c, r) spans
  the mm rectangle [c*pitch, (c+1)*pitch] x [r*pitch, (r+1)*pitch]

The two renders may use different page sizes and DPIs. Only the intersection
of their physical pages (the common coverage domain) is compared. Both
renders are resampled onto one shared rectangular grid whose pitch is the
coarser of the two output pixel pitches; resampling uses area-weighted
overlap integrals, never a whole-image stretch, so a DPI change cannot be
hidden as a zero difference. Regions outside the common domain are reported
and drawn separately instead of being treated as equal paper.
"""
import io
import json
import zipfile

import numpy as np
from PIL import Image
from scipy.sparse import csr_matrix

from image_lab.validation import Invalid
from .core import INKS, encode

CHANNELS = ('source', 'proof', 'continuous', *INKS, *[i + '-tone' for i in INKS])
RGB_CHANNELS = ('source', 'proof', 'continuous')

# Ordered heat ramp: no difference -> large difference.
_HEAT_STOPS = ((0.0, (12, 0, 32)), (0.45, (122, 24, 124)),
               (0.75, (224, 74, 32)), (1.0, (255, 236, 64)))


def _result_pair(store, key, id_a, id_b):
    if not id_a or not id_b or id_a == id_b:
        raise Invalid('请选择两份不同的固定结果进行对照')
    p = store.read(key)
    known = {r['id']: r for r in p['results']}
    if id_a not in known or id_b not in known:
        raise Invalid('对照结果不存在或已不属于本工程')
    rec_a = known[id_a]
    rec_b = known[id_b]
    return rec_a, store.directory(key) / id_a, rec_b, store.directory(key) / id_b


def page_geometry(record):
    """Physical page geometry reconstructed from the frozen result settings."""
    s = record['settings']
    dpi = float(s['dpi'])
    pitch = 25.4 / dpi
    w_px, h_px = (int(record['report']['size'][0]), int(record['report']['size'][1]))
    # Declared page size is rounded to a whole pixel count, so the raster only
    # covers w_px*pitch x h_px*pitch; both rectangles are anchored at (0, 0).
    return {'mm': (float(s['width_mm']), float(s['height_mm'])),
            'dpi': dpi, 'pitch_mm': pitch, 'px': (w_px, h_px),
            'extent_mm': (w_px * pitch, h_px * pitch)}


def common_domain(geom_a, geom_b):
    """Intersection of the declared pages and of their actual raster extents.

    Pages share the top-left physical origin, so the result always starts at
    (0, 0); intersecting the raster extents as well guarantees every grid
    cell lies inside both pixel arrays even when DPI rounding shortens a page
    by a fraction of a pixel.
    """
    x1 = min(geom_a['mm'][0], geom_b['mm'][0],
             geom_a['extent_mm'][0], geom_b['extent_mm'][0])
    y1 = min(geom_a['mm'][1], geom_b['mm'][1],
             geom_a['extent_mm'][1], geom_b['extent_mm'][1])
    return 0.0, 0.0, x1, y1


def outside_rects(geom, common):
    """Non-overlapping mm rectangles of the page outside the common domain."""
    w, h = geom['mm']
    x0, y0, x1, y1 = common
    rects = []
    if x1 < w:
        rects.append([x1, y0, w, h])          # right strip
    if y1 < h:
        rects.append([x0, y1, x1, h])         # bottom strip (excludes the corner)
    if x0 > 0:
        rects.append([0.0, 0.0, x0, h])
    if y0 > 0:
        rects.append([x0, 0.0, x1, y0])
    return rects


def comparison_grid(geom_a, geom_b, common):
    """Shared grid: pitch = coarser output pixel pitch, cells cover common."""
    x0, y0, x1, y1 = common
    sx = max(geom_a['pitch_mm'], geom_b['pitch_mm'])
    sy = sx
    nx = max(1, int(np.ceil((x1 - x0) / sx - 1e-12)))
    ny = max(1, int(np.ceil((y1 - y0) / sy - 1e-12)))

    def edges(start, end, step, count):
        out = np.empty(count + 1)
        out[0] = start
        out[1:-1] = start + np.arange(1, count) * step
        out[-1] = end
        return out

    return {'pitch_mm': (sx, sy), 'size': (nx, ny), 'origin_mm': (x0, y0),
            'edges_x': edges(x0, x1, sx, nx), 'edges_y': edges(y0, y1, sy, ny)}


def _overlap_matrix(edges, pixel_pitch, pixel_count):
    """Area-overlap weights from source pixels onto comparison grid cells.

    Row k holds the fractions of grid cell k covered by each source pixel;
    every row sums to 1 because the grid stays inside the source page.
    """
    all_indices, all_data, indptr = [], [], [0]
    max_extent = pixel_count * pixel_pitch
    for lo, hi in zip(edges[:-1], edges[1:]):
        # the common domain is clipped to the raster extent; guard against
        # sub-pixel float drift so every grid cell intersects a real pixel
        lo = min(max(lo, 0.0), max_extent)
        hi = min(max(hi, 0.0), max_extent)
        first = max(0, int(np.floor(lo / pixel_pitch - 1e-12)))
        last = min(pixel_count - 1, int(np.ceil(hi / pixel_pitch + 1e-12)) - 1)
        row_indices, row_overlap = [], []
        for i in range(first, last + 1):
            overlap = min(hi, (i + 1) * pixel_pitch) - max(lo, i * pixel_pitch)
            if overlap > 0:
                row_indices.append(i)
                row_overlap.append(overlap)
        row_total = sum(row_overlap)
        if row_total <= 0:
            raise Invalid('共同覆盖域采样失败')
        all_indices.extend(row_indices)
        all_data.extend(v / row_total for v in row_overlap)
        indptr.append(len(all_data))
    return csr_matrix((np.asarray(all_data, dtype=float),
                       np.asarray(all_indices, dtype=np.int64),
                       np.asarray(indptr, dtype=np.int64)),
                      shape=(len(edges) - 1, pixel_count))


def _resample(array, matrix_y, matrix_x):
    """Area-weighted resample of a 2-D value image or HxWx3 RGB image.

    My is ny_grid x n_src_rows and Mx is nx_grid x n_src_cols, both with
    rows normalized to unit coverage, so the separable integral is
    My @ array @ Mx.T.
    """
    if array.ndim == 2:
        return matrix_y @ array @ matrix_x.T
    bands = []
    for c in range(array.shape[2]):
        bands.append((matrix_y @ array[:, :, c]) @ matrix_x.T)
    return np.stack(bands, axis=2)


def load_semantic(folder, channel):
    """Load a stored result PNG as physical values in 0..1.

    Plates and continuous tones are stored white=paper/black=ink, so they are
    converted back to ink coverage. RGB views stay in sRGB display units.
    """
    path = f'{folder}/{channel}.png'
    with Image.open(path) as pic:
        if channel in RGB_CHANNELS:
            return np.asarray(pic.convert('RGB'), dtype=np.float64) / 255.0
        return 1.0 - np.asarray(pic.convert('L'), dtype=np.float64) / 255.0


def _resample_pair(geom_a, geom_b, grid, common, folder_a, folder_b, channel):
    """Area-weighted resampling of one channel of both results."""
    mx_a = _overlap_matrix(grid['edges_x'], geom_a['pitch_mm'], geom_a['px'][0])
    my_a = _overlap_matrix(grid['edges_y'], geom_a['pitch_mm'], geom_a['px'][1])
    mx_b = _overlap_matrix(grid['edges_x'], geom_b['pitch_mm'], geom_b['px'][0])
    my_b = _overlap_matrix(grid['edges_y'], geom_b['pitch_mm'], geom_b['px'][1])
    ga = _resample(load_semantic(folder_a, channel), my_a, mx_a)
    gb = _resample(load_semantic(folder_b, channel), my_b, mx_b)
    return ga.astype(np.float64), gb.astype(np.float64)


def grid_arrays(geom_a, geom_b, grid, common, folder_a, folder_b, channel):
    if channel not in CHANNELS:
        raise Invalid('通道无效')
    return _resample_pair(geom_a, geom_b, grid, common, folder_a, folder_b, channel)


def _axis_fractions(lo, hi, pitch, count):
    """Normalized area weights of pixels overlapped by one grid cell interval."""
    extent = count * pitch
    lo, hi = min(max(lo, 0.0), extent), min(max(hi, 0.0), extent)
    first = max(0, int(np.floor(lo / pitch - 1e-12)))
    last = min(count - 1, int(np.ceil(hi / pitch + 1e-12)) - 1)
    indices, weights = [], []
    for i in range(first, last + 1):
        overlap = min(hi, (i + 1) * pitch) - max(lo, i * pitch)
        if overlap > 0:
            indices.append(i)
            weights.append(overlap)
    total = sum(weights)
    if total <= 0:
        raise Invalid('共同覆盖域采样失败')
    return indices, [w / total for w in weights]


def cell_value(raw, geom, cell_rect):
    """Area-weighted mean over one grid cell, identical to the grid integral.

    Uses only the few pixels the cell overlaps instead of resampling the page.
    """
    xlo, ylo, xhi, yhi = cell_rect
    cols, wx = _axis_fractions(xlo, xhi, geom['pitch_mm'], geom['px'][0])
    rows, wy = _axis_fractions(ylo, yhi, geom['pitch_mm'], geom['px'][1])
    patch = raw[np.ix_(rows, cols)]
    weights = np.outer(np.asarray(wy), np.asarray(wx))
    if patch.ndim == 2:
        return float((patch * weights).sum())
    return (patch * weights[:, :, None]).sum(axis=(0, 1))


def _channel_stats(ga, gb):
    diff = ga - gb
    magnitude = np.abs(diff)
    grid_cells = magnitude.shape[0] * magnitude.shape[1]
    if magnitude.ndim == 3:
        identical_cells = np.all(magnitude == 0.0, axis=2)
    else:
        identical_cells = magnitude == 0.0
    stats = {'cell_count': int(grid_cells),
             'mean_abs': float(magnitude.mean()),
             'rmse': float(np.sqrt((diff ** 2).mean())),
             'max_abs': float(magnitude.max()),
             'identical_fraction': float(identical_cells.mean())}
    if ga.ndim == 3:
        stats['per_channel'] = [
            {'mean_abs': float(magnitude[:, :, c].mean()),
             'rmse': float(np.sqrt((diff[:, :, c] ** 2).mean())),
             'max_abs': float(magnitude[:, :, c].max())}
            for c in range(ga.shape[2])]
    return stats


def _settings_diff(sa, sb):
    return [{'key': k, 'a': sa.get(k), 'b': sb.get(k)}
            for k in sorted(set(sa) | set(sb)) if sa.get(k) != sb.get(k)]


def build_comparison(store, key, id_a, id_b):
    rec_a, folder_a, rec_b, folder_b = _result_pair(store, key, id_a, id_b)
    payload, _ = _comparison_payload(rec_a, folder_a, rec_b, folder_b)
    return payload


def _comparison_payload(rec_a, folder_a, rec_b, folder_b):
    """Stats payload plus the resampled grid arrays reused by the archive."""
    geom_a = page_geometry(rec_a)
    geom_b = page_geometry(rec_b)
    common = common_domain(geom_a, geom_b)
    grid = comparison_grid(geom_a, geom_b, common)
    channels, grids = {}, {}
    for channel in CHANNELS:
        ga, gb = grid_arrays(geom_a, geom_b, grid, common, folder_a, folder_b, channel)
        grids[channel] = (ga, gb)
        channels[channel] = _channel_stats(ga, gb)
        channels[channel]['kind'] = 'rgb' if channel in RGB_CHANNELS else 'ink'
    sx, sy = grid['pitch_mm']
    payload = {
        'a': _result_summary(rec_a), 'b': _result_summary(rec_b),
        'pages': {'a': _page_summary(geom_a), 'b': _page_summary(geom_b)},
        'common_mm': [common[0], common[1], common[2], common[3]],
        'outside_mm': {'a': outside_rects(geom_a, common),
                       'b': outside_rects(geom_b, common)},
        'grid': {'pitch_mm': [sx, sy], 'size': list(grid['size']),
                 'origin_mm': list(grid['origin_mm']),
                 'cell_count': grid['size'][0] * grid['size'][1]},
        'sampling': {
            'rule': 'area_weighted_overlap',
            'description': '只比较两份页面物理覆盖域的交集；网格间距取两份输出像素间距的较大值（较粗一方），'
                           '两份结果按像素与网格单元的毫米重叠面积加权积分到同一网格，不做整图拉伸或单点对拍；'
                           '差异统计与点位读数均以该网格单元为采样尺度。',
            'pixel_pitch_mm': {'a': geom_a['pitch_mm'], 'b': geom_b['pitch_mm']},
            'grid_pitch_mm': sx},
        'channels': channels,
        'settings_diff': _settings_diff(rec_a['settings'], rec_b['settings'])}
    return payload, grids


def _result_summary(record):
    return {'id': record['id'], 'revision': record['revision'],
            'created': record['created'], 'source': record['source'],
            'settings': record['settings'], 'size_px': record['report']['size']}


def _page_summary(geom):
    return {'mm': list(geom['mm']), 'dpi': geom['dpi'],
            'pitch_mm': geom['pitch_mm'], 'px': list(geom['px']),
            'raster_extent_mm': list(geom['extent_mm'])}


def _heat_lut():
    lut = np.empty((256, 3), dtype=np.uint8)
    for i in range(256):
        t = i / 255.0
        for k in range(len(_HEAT_STOPS) - 1):
            t0, c0 = _HEAT_STOPS[k]
            t1, c1 = _HEAT_STOPS[k + 1]
            if t0 <= t <= t1:
                u = 0.0 if t1 == t0 else (t - t0) / (t1 - t0)
                lut[i] = [round(c0[j] + (c1[j] - c0[j]) * u) for j in range(3)]
                break
    return lut


_HEAT_LUT = _heat_lut()


def diff_image(ga, gb):
    """Heat map of per-cell absolute difference; RGB uses the mean channel gap."""
    magnitude = np.abs(ga - gb)
    if magnitude.ndim == 3:
        magnitude = magnitude.mean(axis=2)
    idx = np.rint(np.clip(magnitude, 0, 1) * 255).astype(np.uint8)
    return Image.fromarray(_HEAT_LUT[idx])


def grid_image(channel, grid_values):
    """Render resampled grid values back with the original view convention."""
    if channel in RGB_CHANNELS:
        rgb = np.rint(np.clip(grid_values, 0, 1) * 255).astype(np.uint8)
        return Image.fromarray(rgb)
    paper = np.rint(np.clip(1 - grid_values, 0, 1) * 255).astype(np.uint8)
    return Image.fromarray(paper, mode='L')


def outside_mask(geom, common):
    """Full-page RGBA mask: transparent inside common, red outside it."""
    w, h = geom['px']
    mm_w, mm_h = geom['mm']
    x0, y0, x1, y1 = common
    mask = np.zeros((h, w, 4), dtype=np.uint8)
    px0 = max(0, int(round(x0 / mm_w * w)))
    py0 = max(0, int(round(y0 / mm_h * h)))
    px1 = min(w, int(round(x1 / mm_w * w)))
    py1 = min(h, int(round(y1 / mm_h * h)))
    mask[:, :, :3] = (220, 48, 48)
    mask[:, :, 3] = 150
    mask[py0:py1, px0:px1, 3] = 0
    return Image.fromarray(mask, mode='RGBA')


def probe(store, key, id_a, id_b, x_mm, y_mm):
    rec_a, folder_a, rec_b, folder_b = _result_pair(store, key, id_a, id_b)
    geom_a = page_geometry(rec_a)
    geom_b = page_geometry(rec_b)
    common = common_domain(geom_a, geom_b)
    x0, y0, x1, y1 = common
    if not (x0 <= x_mm < x1 and y0 <= y_mm < y1):
        raise Invalid(f'点位({x_mm:.3f},{y_mm:.3f} mm)不在共同覆盖域内')
    grid = comparison_grid(geom_a, geom_b, common)
    sx, sy = grid['pitch_mm']
    nx, ny = grid['size']
    gx = min(nx - 1, int((x_mm - x0) // sx))
    gy = min(ny - 1, int((y_mm - y0) // sy))
    cell = [x0 + gx * sx, y0 + gy * sy, min(x0 + (gx + 1) * sx, x1),
            min(y0 + (gy + 1) * sy, y1)]
    sides = {}
    for tag, rec, geom, folder in (('a', rec_a, geom_a, folder_a),
                                   ('b', rec_b, geom_b, folder_b)):
        col = min(int(x_mm // geom['pitch_mm']), geom['px'][0] - 1)
        row = min(int(y_mm // geom['pitch_mm']), geom['px'][1] - 1)
        pixel_rect = [col * geom['pitch_mm'], row * geom['pitch_mm'],
                      (col + 1) * geom['pitch_mm'], (row + 1) * geom['pitch_mm']]
        values = {}
        for channel in CHANNELS:
            raw = load_semantic(folder, channel)
            # O(1) area integral over the single clicked cell; mathematically
            # identical to the corresponding entry of the full resampled grid
            grid_value = cell_value(raw, geom, cell)
            values[channel] = {
                'grid': _json_value(grid_value),
                'native_pixel': _json_value(raw[row, col]),
                'native_pixel_xy': [col, row]}
        sides[tag] = {'id': rec['id'], 'pixel_rect_mm': pixel_rect, 'values': values}
    return {'point_mm': [x_mm, y_mm], 'grid_cell_mm': cell,
            'grid_cell_xy': [gx, gy], 'common_mm': list(common),
            'sampling_grid_pitch_mm': sx, 'sides': sides}


def _json_value(value):
    if np.ndim(value) == 0:
        return float(value)
    return [float(v) for v in np.asarray(value).reshape(-1)]


def export_comparison(store, key, id_a, id_b):
    rec_a, folder_a, rec_b, folder_b = _result_pair(store, key, id_a, id_b)
    geom_a = page_geometry(rec_a)
    geom_b = page_geometry(rec_b)
    common = common_domain(geom_a, geom_b)
    grid = comparison_grid(geom_a, geom_b, common)
    payload, grids = _comparison_payload(rec_a, folder_a, rec_b, folder_b)
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w', zipfile.ZIP_DEFLATED) as z:
        for tag, rec, folder in (('A', rec_a, folder_a), ('B', rec_b, folder_b)):
            for channel in CHANNELS:
                z.writestr(f'{tag}/{channel}.png', (folder / f'{channel}.png').read_bytes())
            z.writestr(f'{tag}/result.json',
                       json.dumps(rec, ensure_ascii=False, indent=2))
            z.writestr(f'compare/outside-{tag.lower()}.png',
                       encode(outside_mask(geom_a if tag == 'A' else geom_b, common)))
        for channel in CHANNELS:
            ga, gb = grids[channel]
            z.writestr(f'compare/grid-a/{channel}.png', encode(grid_image(channel, ga)))
            z.writestr(f'compare/grid-b/{channel}.png', encode(grid_image(channel, gb)))
            z.writestr(f'compare/diff/{channel}.png', encode(diff_image(ga, gb)))
        manifest = {
            'format': 'dotpress-comparison-1',
            'result_a': {'id': rec_a['id'], 'revision': rec_a['revision'],
                         'source': rec_a['source']},
            'result_b': {'id': rec_b['id'], 'revision': rec_b['revision'],
                         'source': rec_b['source']},
            'comparison': payload,
            'convention': 'plate/tone values are ink coverage 0..1 (white=paper, black=ink); '
                          'RGB views keep sRGB display units',
            'diff_colormap': 'heat ramp dark purple -> red -> yellow, magnitude 0 -> 1',
            'coordinates': 'physical page top-left, mm; x right, y down',
            'immutability': 'both referenced results are frozen rendered revisions; editing the '
                            'current project never replaces the files in this package'}
        z.writestr('compare/manifest.json',
                   json.dumps(manifest, ensure_ascii=False, indent=2))
        readme = (
            'DotPress 历史结果物理对照包\n\n'
            f'对照结果 A：{rec_a["id"]}（修订 {rec_a["revision"]}）\n'
            f'对照结果 B：{rec_b["id"]}（修订 {rec_b["revision"]}）\n'
            '两份结果均为不可变渲染修订；继续编辑当前工程不会替换本包引用的图像。\n\n'
            '采样尺度：只比较两份页面物理覆盖域的交集（common_mm，单位 mm）。\n'
            f'对照网格间距 {grid["pitch_mm"][0]:.4f} mm，取两份输出像素间距的较大值；\n'
            '两份结果按像素与网格单元的毫米重叠面积加权积分到同一网格，未做整图拉伸。\n'
            'A/ 与 B/ 保存两份固定结果的原始全页 PNG；compare/grid-a、compare/grid-b 为\n'
            '共同域内同网格重采样图；compare/diff 为逐单元绝对差异热力图（0 深紫 → 1 黄）；\n'
            'compare/outside-*.png 标注该页面超出共同覆盖域、未参与比较的区域。\n')
        z.writestr('README.txt', readme)
    return stream.getvalue()
