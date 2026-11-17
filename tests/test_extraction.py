"""Tests that the extractor really reads pixels (no hard-coded results).

Regenerates the fictional scans into a temp directory, runs the actual
engine, and asserts grid detection, plausible OCR, coordinate evidence and
fingerprint stability across the coordinate shift.
"""

import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.extractor import engine
import importlib.util
_spec = importlib.util.spec_from_file_location(
    "generate_scans",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                 "tools", "generate_scans.py"))
generate_scans = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(generate_scans)


class ExtractionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="pd-scans-")
        gen = os.path.join(cls.tmp, "in")
        old_argv = sys.argv
        sys.argv = ["generate_scans.py", gen]
        generate_scans.main()
        sys.argv = old_argv
        cls.gen = gen

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _path(self, ver, page):
        return os.path.join(self.gen, generate_scans_manifest_doc(), ver,
                            "page-%03d.png" % page)

    def test_detects_grid_and_real_cells(self):
        r = engine.extract(self._path("v1", 1))
        self.assertTrue(r["ok"], r.get("reason"))
        # 4 columns + outer edges => 5 vertical lines; header + 4 rows
        self.assertGreaterEqual(len(r["grid"]["vlines"]), 5)
        self.assertGreaterEqual(len(r["grid"]["hlines"]), 5)
        # 4 columns * (header + 4 data rows)
        self.assertEqual(len(r["cells"]), 20)
        for c in r["cells"]:
            self.assertEqual(len(c["bbox"]), 4)
            self.assertTrue(0 <= c["confidence"] <= 1)
            self.assertTrue(c["fingerprint"])

    def test_ocr_reads_known_content_from_pixels(self):
        r = engine.extract(self._path("v1", 1))
        texts = [c["text"] for c in r["cells"] if c["row"] >= 1]
        # the OCR must recover most recognizable tokens from the actual image
        joined = " ".join(texts)
        for token in ("101", "12", "205", "DAMAGED", "OK"):
            self.assertIn(token, joined)

    def test_fingerprint_stable_under_coordinate_shift(self):
        # v1 page 2 and v2 page 3 carry identical content, but v2's table is
        # shifted down (page-number insertion moved the print frame).
        a = engine.extract(self._path("v1", 2))
        b = engine.extract(self._path("v2", 3))
        self.assertEqual(a["table_fingerprint"], b["table_fingerprint"])
        # while the actual coordinates differ
        self.assertNotEqual(a["cells"][0]["bbox"], b["cells"][0]["bbox"])

    def test_changed_content_has_different_fingerprint(self):
        a = engine.extract(self._path("v1", 1))
        b = engine.extract(self._path("v2", 1))  # qty 30->31, note changed
        self.assertNotEqual(a["table_fingerprint"], b["table_fingerprint"])


def generate_scans_manifest_doc():
    return generate_scans.__dict__.get("DOC", "receipt-1042") if False \
        else "receipt-1042"


if __name__ == "__main__":
    unittest.main()
