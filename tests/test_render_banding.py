#!/usr/bin/env python3
"""Regression tests for DotPress banded rendering.

Runs standalone (``python3 tests/test_render_banding.py``) and under pytest.
Each case is compared against the pre-banding reference kept in
``scripts/render_equivalence.py``: every PNG channel, the whole-page report and
the region measurement must match exactly across screening, gain, rotation,
registration, alpha edges and fit modes.  The peak-memory check for a
1200x1200 page in isolated processes lives in ``scripts/render_equivalence.py``.
"""
import importlib.util
import math
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotpress import core  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    'render_equivalence', ROOT / 'scripts' / 'render_equivalence.py')
harness = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(harness)

CASES = [
    ('screened', {}),
    ('unscreened', {'screened': False}),
    ('rotation+registration', {'angles': [-33., 12., 61., 7.],
                               'phase_x': [.2, -.3, .45, .1],
                               'offset_x': [.2, -.15, .05, .25],
                               'offset_y': [-.18, .3, -.22, .11]}),
    ('gain+ink limit', {'gain': .35, 'ink_limit': 1.4, 'black': .3,
                        'saturation': 1.6, 'gamma': .7}),
    ('cover crop', {'fit': 'cover', 'crop': [.1, .05, .9, .95]}),
    ('stretch crop', {'fit': 'stretch', 'crop': [.15, .2, .7, .9]}),
    ('contain crop', {'fit': 'contain', 'crop': [.2, .1, .8, .95]}),
]


def almost(a,b,tolerance=1e-12):
    return len(a)==len(b) and all(math.isclose(x,y,rel_tol=tolerance,abs_tol=tolerance)
                                  for x,y in zip(a,b))


def settings(**changes):
    base = harness.settings_for(320)
    base.update(changes)
    return base


def test_banded_matches_reference(name='screened', changes=None):
    changes = changes or dict(CASES)[name]
    source = harness.synthesis(320)
    raw = settings(**changes)
    rect = [.25, .25, .75, .75]
    views_r, arrays_r = harness.reference_render(source, raw)
    views_c, report, measure = core.render_page(source, raw, rect=rect)
    p = core.settings(raw)
    assert core.statistics(arrays_r, p) == report
    assert core.measurement(arrays_r, rect) == measure
    for channel, picture in views_r.items():
        assert harness.sha(picture) == harness.sha(views_c[channel]), channel


def test_band_size_does_not_change_output():
    source = harness.synthesis(200)
    raw = settings(offset_x=[.2, -.15, .05, .25], offset_y=[-.18, .3, -.22, .11])
    original = core.BAND_ROWS
    try:
        outputs = []
        for band in (1, 7, 64, 199):
            core.BAND_ROWS = band
            outputs.append(core.render_page(source, raw, rect=[.1, .1, .9, .9]))
    finally:
        core.BAND_ROWS = original
    reference = outputs[0]
    for views, report, measure in outputs[1:]:
        assert report == reference[1]
        assert measure == reference[2]
        for channel, picture in reference[0].items():
            assert harness.sha(picture) == harness.sha(views[channel]), channel


def test_region_measurement_matches_full_page_report():
    source = harness.synthesis(240)
    raw = settings()
    views, report, measure = core.render_page(source, raw, rect=[0., 0., 1., 1.])
    nominal = [report['channels'][name]['mean_tone'] for name in core.INKS]
    coverage = [report['channels'][name]['area_coverage'] for name in core.INKS]
    # A full-page region reports the same pixels as the whole-page report;
    # only the summation shape (mean over a crop vs over the page) differs.
    assert almost(measure["nominal_mean"], nominal)
    assert almost(measure["coverage"], coverage)


def main():
    failures = 0
    checks = [('band size independence', test_band_size_does_not_change_output),
              ('region equals full page', test_region_measurement_matches_full_page_report)]
    for name, changes in CASES:
        checks.append((name, (lambda n=name, c=changes: test_banded_matches_reference(n, c))))
    for name, check in checks:
        try:
            check()
            print(f'ok   {name}')
        except Exception:
            failures += 1
            print(f'FAIL {name}')
            traceback.print_exc()
    print(f'{len(checks)-failures}/{len(checks)} checks passed')
    return 1 if failures else 0


if __name__ == '__main__':
    sys.exit(main())
