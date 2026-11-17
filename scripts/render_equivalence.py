#!/usr/bin/env python3
"""Equivalence and peak-memory check for DotPress banded rendering.

The reference implementation is the pre-banding pipeline, preserved verbatim
below as ``reference_*`` so the comparison always runs under identical settings
in the same interpreter.  It is only exercised on a page small enough for the
reference to fit comfortably; the measured case uses a page at the 1200x1200
output budget.

Usage (from the repository root):

    python3 scripts/render_equivalence.py
    python3 scripts/render_equivalence.py --check          # assertions only
    python3 scripts/render_equivalence.py --source photo.png --settings job.json

Exit status is 0 when every view, the report and the region measurement match
the reference and the measured peak RSS is at most half the reference peak.
"""
import argparse
import hashlib
import io
import json
import math
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np
from PIL import Image, ImageOps
from scipy.ndimage import map_coordinates

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotpress import core as current  # noqa: E402


# --- Reference pipeline (pre-banding code, kept for the comparison) ---------

def reference_source_canvas(source, p):
    w, h = current.pixel_size(p)
    rgba = np.asarray(source, dtype=float) / 255
    sh, sw = rgba.shape[:2]
    l, t, r, b = p['crop']
    cw, ch = (r - l) * sw, (b - t) * sh
    yy, xx = np.mgrid[:h, :w].astype(float)
    if p['fit'] == 'stretch':
        sx = l * sw + (xx + .5) * cw / w - .5
        sy = t * sh + (yy + .5) * ch / h - .5
        inside = np.ones((h, w), dtype=bool)
    else:
        scale = min(w / cw, h / ch) if p['fit'] == 'contain' else max(w / cw, h / ch)
        sx = l * sw + ((xx + .5 - w / 2) / scale + cw / 2) - .5
        sy = t * sh + ((yy + .5 - h / 2) / scale + ch / 2) - .5
        inside = (sx >= l * sw - .5) & (sx < r * sw - .5) & (sy >= t * sh - .5) & (sy < b * sh - .5)
    alpha = rgba[:, :, 3]
    sampled_alpha = map_coordinates(alpha, [sy, sx], order=1, mode='nearest') * inside
    premult = current.linear(rgba[:, :, :3]) * alpha[:, :, None]
    sampled = np.stack([map_coordinates(premult[:, :, c], [sy, sx], order=1, mode='nearest')
                        for c in range(3)], 2)
    sampled *= inside[:, :, None]
    paper = current.linear(current.parse_color(p['paper']))
    composed = sampled + paper * (1 - sampled_alpha[:, :, None])
    return current.srgb(composed), sampled_alpha


def reference_screen_plates(cmyk, p):
    h, w = cmyk.shape[:2]
    yy, xx = np.mgrid[:h, :w].astype(float)
    x = (xx + .5) * 25.4 / p['dpi']
    y = (yy + .5) * 25.4 / p['dpi']
    out = []
    for i in range(4):
        shiftx = p['offset_x'][i]
        shifty = p['offset_y'][i]
        coords = [yy - shifty * p['dpi'] / 25.4, xx - shiftx * p['dpi'] / 25.4]
        coverage = map_coordinates(cmyk[:, :, i], coords, order=1, mode='constant', cval=0)
        effective = current.dot_gain(coverage, p['gain'])
        if p['screened']:
            rank = current.lattice_rank(x - shiftx, y - shifty, p['angles'][i], p['lpi'],
                                        p['phase_x'][i], p['phase_y'][i])
            plate = (effective >= rank).astype(float)
        else:
            plate = effective
        out.append(plate)
    return np.stack(out, axis=2)


def reference_render(source, raw):
    p = current.settings(raw)
    rgb, alpha = reference_source_canvas(source, p)
    cmyk = current.separate(rgb, p)
    plates = reference_screen_plates(cmyk, p)
    views = {'source': current.image(rgb), 'proof': current.image(current.proof(plates, p)),
             'continuous': current.image(current.proof(cmyk, p))}
    for i, name in enumerate(current.INKS):
        views[name] = current.image(1 - plates[:, :, i])
        views[name + '-tone'] = current.image(1 - cmyk[:, :, i])
    return views, {'nominal': cmyk, 'plates': plates, 'alpha': alpha}


# --- Helpers ---------------------------------------------------------------

def sha(image):
    stream = io.BytesIO()
    image.save(stream, 'PNG')
    return hashlib.sha256(stream.getvalue()).hexdigest()


def synthesis(size=640, seed=20261117):
    """Deterministic synthetic RGBA photo with gradients and a transparent edge."""
    y, x = np.mgrid[:size, :size].astype(float)
    a = np.empty((size, size, 4), dtype=np.uint8)
    a[:, :, 0] = ((x * 255) // (size - 1)).astype(np.uint8)
    a[:, :, 1] = ((y * 255) // (size - 1)).astype(np.uint8)
    a[:, :, 2] = (127 + 110 * np.sin(x / 31.0) * np.cos(y / 47.0)).astype(np.uint8)
    a[:, :, 3] = (np.clip((x + y - size * .55) / (size * .12), 0, 1) * 255).astype(np.uint8)
    a[:size // 8, :size // 8, 3] = 0
    return Image.fromarray(a)


def settings_for(px, width_mm=None, **changes):
    """Settings whose page is ``px`` pixels square at 288 DPI."""
    base = {'width_mm': 100., 'height_mm': 75., 'dpi': 96., 'lpi': 18.,
            'black': .7, 'ink_limit': 2.8, 'gamma': 1., 'saturation': 1.,
            'paper': '#fff8e8', 'gain': .12, 'proof_density': 1.,
            'angles': [15., 75., 0., 45.], 'phase_x': [0., 0., 0., 0.],
            'phase_y': [0., 0., 0., 0.], 'offset_x': [0., 0., 0., 0.],
            'offset_y': [0., 0., 0., 0.], 'enabled': [True] * 4,
            'crop': [.03, .05, .98, .97], 'fit': 'contain', 'screened': True}
    side = (width_mm if width_mm is not None else px * 25.4 / 288)
    base.update(width_mm=side, height_mm=side, dpi=288, lpi=24,
                phase_x=[.1, .2, .3, .4], phase_y=[.05, .15, .25, .35],
                offset_x=[.08, -.05, .12, 0.], offset_y=[-.03, .07, 0., .1])
    base.update(changes)
    return base


def compare(reference, candidate, settings, rect):
    views_r, arrays_r = reference
    views_c, report_c, measure_c = candidate
    p = current.settings(settings)
    report_r = current.statistics(arrays_r, p)
    measure_r = current.measurement(arrays_r, rect)
    identical = {name: sha(views_r[name]) == sha(views_c[name]) for name in views_r}
    return {'png_identical': identical,
            'all_png_identical': all(identical.values()),
            'report_identical': report_r == report_c,
            'measurement_identical': measure_r == measure_c,
            'report_reference': report_r, 'report_candidate': report_c,
            'measurement_reference': measure_r, 'measurement_candidate': measure_c}


# --- Measured case in an isolated process ----------------------------------

MEASURED = r'''
import json, sys
sys.path.insert(0, sys.argv[4])
sys.path.insert(0, sys.argv[5])
import numpy as np
from PIL import Image
mode = sys.argv[3]
source = Image.open(sys.argv[1]).convert('RGBA')
settings = json.load(open(sys.argv[2]))
import importlib.util
if mode == 'reference':
    spec = importlib.util.spec_from_file_location('reference', sys.argv[6])
    reference = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(reference)
    views, arrays = reference.reference_render(source, settings)
    report = reference.current.statistics(arrays, reference.current.settings(settings))
    measure = reference.current.measurement(arrays, [.25, .25, .75, .75])
else:
    spec = importlib.util.spec_from_file_location('contract', sys.argv[4] + '/dotpress/core.py')
    core = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(core)
    views, report, measure = core.render_page(source, settings, rect=[.25, .25, .75, .75])
def high_water_kb():
    status = open('/proc/self/status').read()
    return int(status.split('VmHWM:')[1].split()[0])

peak = high_water_kb()
import hashlib, io
hashes = {}
for name, pic in views.items():
    stream = io.BytesIO()
    pic.save(stream, 'PNG')
    hashes[name] = hashlib.sha256(stream.getvalue()).hexdigest()
print(json.dumps({'peak_rss_kb': peak, 'hashes': hashes,
                  'report': report, 'measurement': measure}))
'''


def isolated_peak(source_path, settings_path, mode, reference_path):
    folder = Path(tempfile.mkdtemp(prefix='dotpress-measure-'))
    script = folder / 'measure.py'
    script.write_text(MEASURED)
    proc = subprocess.run(
        [sys.executable, str(script), str(source_path), str(settings_path), mode,
         str(ROOT), str(ROOT), str(reference_path)],
        capture_output=True, text=True)
    if proc.returncode != 0:
        raise SystemExit(f'{mode} measurement failed:\n{proc.stderr}')
    return json.loads(proc.stdout)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true', help='fail on any mismatch')
    parser.add_argument('--source', type=Path, help='RGBA source image to use')
    parser.add_argument('--settings', type=Path, help='JSON settings for the measured case')
    parser.add_argument('--json', type=Path, help='write the full result report here')
    args = parser.parse_args()

    source = Image.open(args.source).convert('RGBA') if args.source else synthesis(4000)
    settings = json.loads(args.settings.read_text()) if args.settings else settings_for(1200)
    if source.width * source.height > 16_000_000:
        raise SystemExit('source exceeds the 16 MP upload budget')

    # Correctness: reference vs candidate over several semantics-preserving cases.
    cases = []
    rect = [.25, .25, .75, .75]
    small_source = synthesis(320)
    small = settings_for(320)
    for name, changes in (
            ('screened', {}),
            ('unscreened', {'screened': False}),
            ('rotation+registration', {'angles': [-33., 12., 61., 7.],
                                       'phase_x': [.2, -.3, .45, .1],
                                       'offset_x': [.2, -.15, .05, .25],
                                       'offset_y': [-.18, .3, -.22, .11]}),
            ('gain+ink limit', {'gain': .35, 'ink_limit': 1.4, 'black': .3,
                                'saturation': 1.6, 'gamma': .7}),
            ('cover crop', {'fit': 'cover', 'crop': [.1, .05, .9, .95]}),
            ('contain crop', {'fit': 'contain', 'crop': [.2, .1, .8, .95]}),
            ('stretch crop', {'fit': 'stretch', 'crop': [.15, .2, .7, .9]})):
        candidate = current.render_page(small_source, {**small, **changes}, rect=rect)
        result = compare(reference_render(small_source, {**small, **changes}), candidate,
                         {**small, **changes}, rect)
        cases.append({'case': name, **result})
    correctness_ok = all(c['all_png_identical'] and c['report_identical']
                         and c['measurement_identical'] for c in cases)

    # Memory: same page (1200x1200 output cap) and identical settings in
    # isolated processes for the reference and the shipped pipeline.
    with tempfile.TemporaryDirectory(prefix='dotpress-bench-') as folder:
        folder = Path(folder)
        source_path = folder / 'source.png'
        source.save(source_path)
        settings_path = folder / 'settings.json'
        settings_path.write_text(json.dumps(settings))
        reference_path = Path(__file__).resolve()
        reference = isolated_peak(source_path, settings_path, 'reference', reference_path)
        candidate = isolated_peak(source_path, settings_path, 'candidate', reference_path)
        page = current.pixel_size(current.settings(settings))
        memory = {'page_pixels': list(page),
                  'reference_peak_kb': reference['peak_rss_kb'],
                  'candidate_peak_kb': candidate['peak_rss_kb'],
                  'ratio': candidate['peak_rss_kb'] / reference['peak_rss_kb'],
                  'report_identical': reference['report'] == candidate['report'],
                  'measurement_identical': reference['measurement'] == candidate['measurement'],
                  'at_most_half': candidate['peak_rss_kb'] <= reference['peak_rss_kb'] / 2}
        memory_ok = (memory['at_most_half'] and memory['report_identical']
                     and memory['measurement_identical'])

    result = {'source': [source.width, source.height],
              'correctness_cases': cases,
              'correctness_ok': correctness_ok,
              'memory': memory,
              'memory_ok': memory_ok}
    if args.json:
        args.json.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'correctness_ok': correctness_ok,
                      'cases': {c['case']: c['all_png_identical'] and c['report_identical']
                                and c['measurement_identical'] for c in cases},
                      'memory': {k: memory[k] for k in
                                 ('page_pixels', 'reference_peak_kb', 'candidate_peak_kb',
                                  'ratio', 'at_most_half', 'report_identical',
                                  'measurement_identical')}}, indent=1))
    if args.check and not (correctness_ok and memory_ok):
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
