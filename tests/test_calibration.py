import io
import json
import os
import sys
import tempfile
import unittest
import zipfile

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from image_lab import create_app
from image_lab.validation import Invalid
from dotpress import install
from dotpress import calibration
from dotpress.calibration import tidied, invert, merge, isotonic, validate, default, fingerprint


class TidiedTest(unittest.TestCase):
    def test_dips_pooled_into_monotone_blocks(self):
        points = [[0, 0], [.2, .3], [.4, .28], [.6, .55], [.8, .9], [1, 1]]
        xs, ys = tidied(points)
        self.assertEqual(xs[0], 0)
        self.assertEqual(xs[-1], 1)
        self.assertTrue(all(b >= a - 1e-12 for a, b in zip(ys, ys[1:])))
        self.assertAlmostEqual(float(np.interp(.4, xs, ys)), float(np.interp(.2, xs, ys)) * 0 + .29, places=2)

    def test_duplicates_merged_not_deleted(self):
        points = [[.5, .4], [.5, .5], [.5, .6], [0, 0], [1, 1]]
        xs, groups = merge(points)
        self.assertEqual(groups[xs.index(.5)], [.4, .5, .6])
        kx, ky = tidied(points)
        self.assertAlmostEqual(float(np.interp(.5, kx, ky)), .5)

    def test_isotonic_weighted(self):
        fitted = isotonic([.5, .2, .2], [1, 1, 1])
        self.assertTrue(all(b >= a for a, b in zip(fitted, fitted[1:])))
        self.assertAlmostEqual(fitted[0], .3)
        heavier = isotonic([.5, .2], [3, 1])
        self.assertAlmostEqual(heavier[0], .425)

    def test_inverse_monotone_bounded_no_reversal(self):
        points = [[0, 0], [.1, .2], [.2, .2], [.3, .45], [.5, .6], [.7, .62], [.9, .95], [1, 1]]
        knots = tidied(points)
        lut = [invert(knots, float(q)) for q in np.linspace(0, 1, 501)]
        self.assertTrue(all(0 <= v <= 1 for v in lut))
        self.assertTrue(all(b >= a - 1e-9 for a, b in zip(lut, lut[1:])))

    def test_roundtrip_through_response(self):
        knots = tidied([[0, 0], [.25, .4], [.5, .62], [.75, .85], [1, 1]])
        for q in (.1, .3, .5, .7, .9):
            send = invert(knots, q)
            self.assertAlmostEqual(float(np.interp(send, knots[0], knots[1])), q, places=6)

    def test_flat_segment_inverts_to_midpoint(self):
        self.assertAlmostEqual(invert(([0, .4, .6, 1], [0, .5, .5, 1]), .5), .5)

    def test_single_level_is_identity(self):
        self.assertIsNone(tidied([[.5, .5], [.5, .6]]))
        self.assertIsNone(tidied([]))

    def test_anchors_and_extrapolation(self):
        xs, ys = tidied([[.2, .3], [.8, .9]])
        self.assertEqual((xs[0], ys[0]), (0, 0))
        self.assertEqual(xs[-1], 1)
        self.assertGreaterEqual(ys[-1], ys[-2])
        self.assertLessEqual(ys[-1], 1)

    def test_validate_rejects_bad_data(self):
        with self.assertRaises(Invalid):
            validate({'inks': {'C': [[2, .5]]}})
        with self.assertRaises(Invalid):
            validate({'inks': {'C': [[.1, .2]] * 65}})
        with self.assertRaises(Invalid):
            validate({'inks': {'C': [[.1]]}})
        self.assertEqual(validate(None), default())

    def test_fingerprint_stable_and_data_driven(self):
        cal = {'enabled': True, 'label': 'a', 'inks': {'C': [[0, 0], [1, 1]], 'M': [], 'Y': [], 'K': []}}
        same = {'enabled': True, 'label': 'renamed', 'inks': {'C': [[0., 0.], [1., 1.]], 'M': [], 'Y': [], 'K': []}}
        self.assertEqual(fingerprint(cal), fingerprint(same))
        other = {'enabled': False, 'label': 'a', 'inks': {'C': [[0, 0], [1, 1]], 'M': [], 'Y': [], 'K': []}}
        self.assertNotEqual(fingerprint(cal), fingerprint(other))


class PressTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        app = create_app(os.path.join(self.tmp.name, 'preparation'))
        install(app, os.path.join(self.tmp.name, 'press'))
        app.config['TESTING'] = True
        self.client = app.test_client()
        project = self.client.post('/press/demo').get_json()
        self.pid = project['id']
        self.rev = project['revision']

    def get(self, path):
        reply = self.client.get(path)
        self.assertEqual(reply.status_code, 200, reply.get_json())
        return reply

    def patch_calibration(self, cal):
        reply = self.client.patch(f'/press/projects/{self.pid}',
                                  json={'revision': self.rev, 'calibration': cal})
        self.assertEqual(reply.status_code, 200, reply.get_json())
        self.rev = reply.get_json()['revision']

    def render(self):
        reply = self.client.post(f'/press/projects/{self.pid}/render', json={'revision': self.rev})
        self.assertEqual(reply.status_code, 200, reply.get_json())
        return reply.get_json()

    def heavy_calibration(self, enabled=True):
        return {'enabled': enabled, 'label': '小机铜版纸',
                'inks': {'C': [[0, 0], [.25, .42], [.5, .5], [.5, .66], [.5, .7], [.75, .84], [1, 1]],
                         'M': [[0, 0], [.5, .68], [1, 1]],
                         'Y': [],
                         'K': [[0, 0], [.3, .3], [.3, .5], [1, 1]]}}


class RenderCalibrationTest(PressTestCase):
    def test_compensation_reduces_midtone_and_keeps_ink_limit(self):
        self.patch_calibration(self.heavy_calibration())
        record = self.render()
        report = record['report']
        self.assertTrue(report['calibration']['enabled'])
        self.assertLessEqual(report['total_ink_max'], record['settings']['ink_limit'] + 1e-6)
        cyan = report['channels']['C']
        self.assertLess(cyan['mean_tone'], cyan['nominal_mean'])
        self.assertIn('nominal_total_max', report)

    def test_ink_hungry_calibration_still_respects_total_limit(self):
        hungry = {'enabled': True, 'label': '反相',
                  'inks': {name: [[0, 0], [1, .4]] for name in 'CMYK'}}
        self.patch_calibration(hungry)
        record = self.render()
        self.assertLessEqual(record['report']['total_ink_max'],
                             record['settings']['ink_limit'] + 1e-6)

    def test_disabled_calibration_is_identity(self):
        before = self.render()
        self.patch_calibration(self.heavy_calibration(enabled=False))
        after = self.render()
        self.assertAlmostEqual(before['report']['total_ink_mean'], after['report']['total_ink_mean'])
        self.assertNotIn('calibration', after['report'])

    def test_result_record_and_delivery_identify_calibration(self):
        cal = self.heavy_calibration()
        self.patch_calibration(cal)
        record = self.render()
        self.assertEqual(record['calibration']['fingerprint'], fingerprint(cal))
        self.assertEqual(record['calibration']['inks']['C'], cal['inks']['C'])
        blob = self.get(f'/press/projects/{self.pid}/plates.zip').data
        with zipfile.ZipFile(io.BytesIO(blob)) as z:
            manifest = json.loads(z.read('manifest.json'))
            readme = z.read('README.txt').decode()
        self.assertEqual(manifest['calibration']['fingerprint'], fingerprint(cal))
        self.assertIn(fingerprint(cal), readme)

    def test_measure_reports_sent_tones(self):
        self.patch_calibration(self.heavy_calibration())
        reply = self.client.post(f'/press/projects/{self.pid}/measure',
                                 json={'revision': self.rev, 'rect': [0, 0, 1, 1]})
        self.assertEqual(reply.status_code, 200)
        body = reply.get_json()
        self.assertIn('sent_mean', body)
        self.assertIn('nominal_mean', body)


class PersistenceTest(PressTestCase):
    def test_versions_restore_same_calibration(self):
        self.patch_calibration(self.heavy_calibration())
        saved = fingerprint(self.heavy_calibration())
        reply = self.client.post(f'/press/projects/{self.pid}/versions',
                                 json={'revision': self.rev, 'label': '重墨机'})
        self.rev = reply.get_json()['revision']
        vid = reply.get_json()['versions'][-1]['id']
        self.patch_calibration({'enabled': False, 'label': '', 'inks': {n: [] for n in 'CMYK'}})
        reply = self.client.post(f'/press/projects/{self.pid}/versions/{vid}/restore',
                                 json={'revision': self.rev})
        self.assertEqual(reply.status_code, 200)
        restored = reply.get_json()
        self.assertEqual(fingerprint(restored['calibration']), saved)
        self.assertEqual(restored['calibration']['label'], '小机铜版纸')

    def test_project_export_import_keeps_calibration(self):
        self.patch_calibration(self.heavy_calibration())
        blob = self.get(f'/press/projects/{self.pid}/project.zip').data
        reply = self.client.post('/press/import',
                                 data={'file': (io.BytesIO(blob), 'job.zip')},
                                 content_type='multipart/form-data')
        self.assertEqual(reply.status_code, 201, reply.get_json())
        imported = reply.get_json()
        self.assertEqual(fingerprint(imported['calibration']), fingerprint(self.heavy_calibration()))
        self.assertEqual(imported['calibration']['inks']['C'], self.heavy_calibration()['inks']['C'])

    def test_reopen_reads_same_calibration(self):
        self.patch_calibration(self.heavy_calibration())
        reopened = self.get(f'/press/projects/{self.pid}').get_json()
        self.assertEqual(fingerprint(reopened['calibration']), fingerprint(self.heavy_calibration()))

    def test_toggle_keeps_measurements(self):
        cal = self.heavy_calibration()
        self.patch_calibration(cal)
        cal['enabled'] = False
        self.patch_calibration(cal)
        cal['enabled'] = True
        self.patch_calibration(cal)
        reopened = self.get(f'/press/projects/{self.pid}').get_json()
        self.assertEqual(reopened['calibration']['inks']['C'], self.heavy_calibration()['inks']['C'])


class DeriveEndpointTest(PressTestCase):
    def test_derive_returns_response_compensation_and_samples(self):
        reply = self.client.post('/press/calibration/derive',
                                 json={'calibration': self.heavy_calibration()})
        self.assertEqual(reply.status_code, 200)
        body = reply.get_json()
        cyan = body['inks']['C']
        self.assertEqual(len(body['grid']), 65)
        self.assertEqual(len(cyan['response']), 65)
        self.assertEqual(len(cyan['compensation']), 65)
        merged = {m['x']: m for m in cyan['merged']}
        self.assertEqual(merged[.5]['n'], 3)
        self.assertAlmostEqual(merged[.5]['mean'], (.5 + .66 + .7) / 3)
        self.assertEqual(len(cyan['samples']), 5)
        for sample in cyan['samples']:
            self.assertAlmostEqual(sample['after'], sample['desired'], delta=.03)
        comp = cyan['compensation']
        self.assertTrue(all(b >= a - 1e-9 for a, b in zip(comp, comp[1:])))
        self.assertTrue(all(0 <= v <= 1 for v in comp))

    def test_derive_rejects_invalid(self):
        reply = self.client.post('/press/calibration/derive',
                                 json={'calibration': {'inks': {'C': [[5, 1]]}}})
        self.assertEqual(reply.status_code, 400)


if __name__ == '__main__':
    unittest.main()
