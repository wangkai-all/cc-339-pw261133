"""Unit tests for the per-cell merge rules."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.common import config
from services.review.merge import merge_extraction, page_requires_review


def cell(cid, row, col, text, fp, bbox=(0, 0, 10, 10)):
    return {"cell_id": cid, "row": row, "col": col, "text": text,
            "fingerprint": fp, "bbox": list(bbox), "confidence": 0.9}


class MergeRulesTest(unittest.TestCase):
    def setUp(self):
        self.old = [
            cell("r1c0", 1, 0, "A-101", "fp-a"),
            cell("r1c1", 1, 1, "12", "fp-q"),
            cell("r1c2", 1, 2, "B-07", "fp-b"),
        ]
        self.verdicts = {
            "r1c0": {"value": "A-101", "human_edited": False},
            "r1c1": {"value": "13", "human_edited": True},
            "r1c2": {"value": "B-07", "human_edited": False},
        }

    def test_unchanged_content_reuses_verdict_even_if_coordinates_move(self):
        # identical fingerprints but shifted bboxes (page insertion offset)
        new = [
            cell("r1c0", 1, 0, "A-101", "fp-a", bbox=(0, 14, 10, 24)),
            cell("r1c1", 1, 1, "12", "fp-q", bbox=(20, 14, 30, 24)),
            cell("r1c2", 1, 2, "B-07", "fp-b", bbox=(40, 14, 50, 24)),
        ]
        recs, removed = merge_extraction(self.old, self.verdicts, new)
        self.assertEqual(removed, [])
        for r in recs:
            self.assertEqual(r["decision"], config.CELL_REUSE)
        # human edited value preserved on the unchanged cell
        qty = next(r for r in recs if r["cell_id"] == "r1c1")
        self.assertEqual(qty["proposed_value"], "13")
        self.assertFalse(page_requires_review(recs, removed))

    def test_changed_cell_without_human_edit_requires_recheck(self):
        # r1c0 here has no human edit (pure OCR confirmation)
        verdicts = dict(self.verdicts)
        verdicts["r1c0"] = {"value": "A-101", "human_edited": False}
        new = [
            cell("r1c0", 1, 0, "A-999", "fp-a2"),  # OCR content changed
            cell("r1c1", 1, 1, "12", "fp-q"),
            cell("r1c2", 1, 2, "B-07", "fp-b"),
        ]
        recs, _ = merge_extraction(self.old, verdicts, new)
        changed = next(r for r in recs if r["cell_id"] == "r1c0")
        self.assertEqual(changed["decision"], config.CELL_RECHECK)
        self.assertTrue(changed["needs_confirmation"])
        self.assertEqual(changed["proposed_value"], "A-999")  # new OCR
        self.assertTrue(page_requires_review(recs, []))

    def test_changed_cell_with_human_edit_keeps_edit_as_draft(self):
        # r1c1 was human-edited to 13; new scan shows different content.
        # The human edit must NOT be wiped, but must be re-confirmed.
        new = [
            cell("r1c0", 1, 0, "A-101", "fp-a"),
            cell("r1c1", 1, 1, "99", "fp-q3"),
            cell("r1c2", 1, 2, "B-07", "fp-b"),
        ]
        recs, _ = merge_extraction(self.old, self.verdicts, new)
        changed = next(r for r in recs if r["cell_id"] == "r1c1")
        self.assertEqual(changed["decision"], config.CELL_DRAFT)
        self.assertEqual(changed["proposed_value"], "13")  # human edit kept
        self.assertTrue(changed["needs_confirmation"])

    def test_new_cell_is_added_and_removed_cell_flagged(self):
        new = [
            cell("r1c0", 1, 0, "A-101", "fp-a"),
            cell("r1c1", 1, 1, "12", "fp-q"),
            # r1c2 dropped, r1c3 newly appears
            cell("r1c3", 1, 3, "NEW", "fp-n"),
        ]
        recs, removed = merge_extraction(self.old, self.verdicts, new)
        added = next(r for r in recs if r["cell_id"] == "r1c3")
        self.assertEqual(added["decision"], config.CELL_ADDED)
        self.assertEqual(len(removed), 1)
        self.assertEqual(removed[0]["decision"], config.CELL_REMOVED)
        self.assertEqual(removed[0]["cell_id"], "r1c2")

    def test_no_prior_version_keeps_all_new_ocr(self):
        recs, removed = merge_extraction([], {}, self.old)
        self.assertTrue(all(r["decision"] in
                            (config.CELL_ADDED, config.CELL_KEEP_OCR)
                            for r in recs))
        self.assertEqual(removed, [])


if __name__ == "__main__":
    unittest.main()
