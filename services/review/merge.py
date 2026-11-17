"""Per-cell merge rules between an old reviewed extraction and a new one.

When the source uploads a revised scan and extraction is re-run, we must
not copy the whole old page, and must not erase human corrections.  The
match is content-based, not coordinate-based, because inserted pages shift
coordinates.  Each old cell is matched to a new cell by structural position
(row/col) plus its content fingerprint; the decision then depends on
whether content changed and whether a human had edited the value.
"""

from services.common import config


def _index(cells):
    by_pos, by_fp = {}, {}
    for c in cells:
        by_pos[(c["row"], c["col"])] = c
        # fingerprints may repeat; keep the first at each position
        by_fp.setdefault(c.get("fingerprint"), []).append(c)
    return by_pos, by_fp


def _same_cell(old, new):
    """Two cells describe the same content when their normalized content
    fingerprints match (geometry-independent)."""
    return old.get("fingerprint") and old.get("fingerprint") == \
        new.get("fingerprint")


def merge_extraction(old_cells, old_verdicts, new_cells):
    """Return per-new-cell merge records.

    Parameters
    ----------
    old_cells : list of cell dicts from the previous extraction
    old_verdicts : dict cell_id -> {"value", "human_edited", "status", ...}
        human review state from the previous version
    new_cells : list of cell dicts from the freshly run extraction

    Returns list aligned with new_cells, each carrying a decision in
    config.CELL_* plus the value the review field should start from and
    whether human confirmation is required.
    """
    old_by_pos, old_by_fp = _index(old_cells)
    used_old = set()
    results = []

    for new in new_cells:
        pos = (new["row"], new["col"])
        old = old_by_pos.get(pos)
        verdict = old_verdicts.get(old["cell_id"]) if old else None

        # fallback: positional cell changed content, try fingerprint match
        # elsewhere (page shift can reorder structural rows only by insert,
        # which is handled at page level; here we still guard defensively)
        if old and not _same_cell(old, new):
            for cand in old_by_fp.get(new.get("fingerprint"), []):
                if cand["cell_id"] not in used_old and \
                        cand["col"] == new["col"]:
                    old, verdict = cand, old_verdicts.get(cand["cell_id"])
                    break

        rec = {
            "cell_id": new["cell_id"],
            "row": new["row"],
            "col": new["col"],
            "new_text": new["text"],
            "new_confidence": new["confidence"],
            "bbox": new["bbox"],
            "fingerprint": new.get("fingerprint"),
            "old_text": old["text"] if old else None,
            "decision": None,
            "proposed_value": new["text"],
            "needs_confirmation": False,
            "reason": "",
        }

        if old is None or verdict is None:
            rec["decision"] = config.CELL_ADDED if old is None \
                else config.CELL_KEEP_OCR
            rec["reason"] = "no prior review for this cell"
            results.append(rec)
            continue

        used_old.add(old["cell_id"])
        unchanged = _same_cell(old, new)
        human_edited = bool(verdict.get("human_edited"))
        human_value = verdict.get("value")

        if unchanged:
            # Content pixel-identical after normalization: the prior verdict
            # is still valid regardless of coordinate movement.
            if human_edited:
                rec["decision"] = config.CELL_REUSE
                rec["proposed_value"] = human_value
                rec["reason"] = "content unchanged; human edit preserved"
            else:
                rec["decision"] = config.CELL_REUSE
                rec["proposed_value"] = human_value or new["text"]
                rec["reason"] = "content unchanged; prior confirmation reused"
        else:
            if human_edited:
                # Content drifted but a person had corrected this cell:
                # keep their edit as a DRAFT rather than silently wiping it,
                # and require explicit confirmation against the new scan.
                rec["decision"] = config.CELL_DRAFT
                rec["proposed_value"] = human_value
                rec["needs_confirmation"] = True
                rec["reason"] = "content changed; human edit retained for " \
                                "re-confirmation, not applied blindly"
            else:
                # No human opinion and the content changed: re-review.
                rec["decision"] = config.CELL_RECHECK
                rec["proposed_value"] = new["text"]
                rec["needs_confirmation"] = True
                rec["reason"] = "content changed; new extraction must be " \
                                "reviewed"
        results.append(rec)

    # cells that disappeared in the new version
    removed = []
    for old in old_cells:
        if old["cell_id"] in used_old:
            continue
        removed.append({
            "cell_id": old["cell_id"],
            "row": old["row"],
            "col": old["col"],
            "old_text": old["text"],
            "new_text": None,
            "decision": config.CELL_REMOVED,
            "proposed_value": None,
            "needs_confirmation": True,
            "reason": "cell absent from the new version",
        })

    return results, removed


def page_requires_review(merge_records, removed):
    """A merged page needs human review if any cell is not a clean reuse."""
    clean = {config.CELL_REUSE}
    for r in merge_records:
        if r["decision"] not in clean or r["needs_confirmation"]:
            return True
    return bool(removed)
