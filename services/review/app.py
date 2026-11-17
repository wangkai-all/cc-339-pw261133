"""Review / proofreading service.

Maintains its own database of reviewed pages and cell-level verdicts.
It pulls extractions from the receiver, applies the per-cell merge rules
when a new extraction supersedes an old one, enforces optimistic
concurrency on edits (two reviewers cannot silently overwrite each other),
and only forwards fully approved pages to the warehouse.
"""

import json
import os
import time

from services.common import config
from services.common.http import HttpError, LockedDB, Router, get, post, serve
from services.review.merge import merge_extraction, page_requires_review

router = Router()
DB = None
ROOT = None


def init(root):
    global DB, ROOT
    ROOT = root
    config.ensure_dirs()
    DB = LockedDB(os.path.join(ROOT, "review", "review.db"))
    DB.conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS review_pages (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            doc_id          TEXT NOT NULL,
            version         INTEGER NOT NULL,
            receiver_page_id INTEGER NOT NULL,
            seq             INTEGER NOT NULL,
            table_fingerprint TEXT,
            status          TEXT NOT NULL DEFAULT 'PENDING',
            revision        INTEGER NOT NULL DEFAULT 1,
            created_at      REAL NOT NULL,
            updated_at      REAL NOT NULL,
            UNIQUE(doc_id, version, receiver_page_id)
        );
        CREATE TABLE IF NOT EXISTS review_cells (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            review_page_id  INTEGER NOT NULL,
            cell_id         TEXT NOT NULL,
            row_idx         INTEGER NOT NULL,
            col_idx         INTEGER NOT NULL,
            bbox            TEXT NOT NULL,
            ocr_text        TEXT NOT NULL,
            ocr_confidence  REAL NOT NULL,
            fingerprint     TEXT,
            current_value   TEXT NOT NULL,
            human_edited    INTEGER NOT NULL DEFAULT 0,
            decision        TEXT NOT NULL DEFAULT 'KEEP_OCR',
            confirmed       INTEGER NOT NULL DEFAULT 0,
            editor          TEXT,
            updated_at      REAL NOT NULL,
            UNIQUE(review_page_id, cell_id)
        );
        CREATE TABLE IF NOT EXISTS cell_history (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            review_page_id  INTEGER NOT NULL,
            cell_id         TEXT NOT NULL,
            old_value       TEXT,
            new_value       TEXT,
            editor          TEXT,
            reason          TEXT,
            at              REAL NOT NULL
        );
        """)


# -------------------------- pulling from receiver ---------------------------

def _receiver(path):
    return get(config.RECEIVER_URL + path)


@router.add("POST", r"^/sync$")
def sync(body):
    """Pull all extracted pages for a document/version from the receiver
    and create local review pages.  Each new page is matched to a page of
    the previous version by its geometry-independent table fingerprint, so
    inserted pages (which shift every later page number) never get matched
    to the wrong predecessor.  Unmatched pages are genuinely new."""
    doc_id = body["doc_id"]
    version = int(body["version"])
    pages = _receiver("/documents/%s/pages?version=%d" % (doc_id, version))
    created, merged = [], []

    # all pages of the immediately preceding version, indexed by fingerprint
    prev_version_row = DB.one(
        "SELECT MAX(version) AS v FROM review_pages WHERE doc_id=? AND "
        "version<?", (doc_id, version))
    prev_pages = []
    if prev_version_row and prev_version_row["v"] is not None:
        prev_pages = DB.query(
            "SELECT * FROM review_pages WHERE doc_id=? AND version=?",
            (doc_id, prev_version_row["v"]))
    prev_by_fp = {}
    for pp in prev_pages:
        prev_by_fp.setdefault(pp["table_fingerprint"], pp)
    # structural fallback preserves page order when fingerprints differ
    # because some cells (not the whole page) changed.
    prev_unmatched = list(prev_pages)

    for pg in pages["pages"]:
        if pg["status"] != "EXTRACTED":
            continue
        existing = DB.one(
            "SELECT * FROM review_pages WHERE doc_id=? AND version=? "
            "AND receiver_page_id=?", (doc_id, version, pg["id"]))
        if existing:
            continue
        ext = _receiver("/pages/%d/extraction" % pg["id"])
        extraction = ext["extraction"]

        prev = prev_by_fp.get(extraction.get("table_fingerprint"))
        structural = False
        if prev is None and prev_unmatched:
            # no whole-page fingerprint match: use the next not-yet-used
            # predecessor in document order and let the cell-level rules
            # decide REUSE / RECHECK / DRAFT per cell.
            prev = prev_unmatched.pop(0)
            structural = True
        elif prev is not None and prev in prev_unmatched:
            prev_unmatched.remove(prev)

        page_id = DB.execute(
            "INSERT INTO review_pages(doc_id, version, receiver_page_id, "
            "seq, table_fingerprint, status, revision, created_at, "
            "updated_at) VALUES(?,?,?,?,?,'PENDING',1,?,?)",
            (doc_id, version, pg["id"], pg["seq"],
             extraction.get("table_fingerprint"), time.time(), time.time()))
        _persist_cells(page_id, extraction["cells"], fresh=prev is None)

        if prev is None:
            created.append(page_id)
        else:
            _apply_merge(prev["id"], page_id, extraction["cells"],
                         structural=structural)
            merged.append(page_id)

    return {"synced": len(created) + len(merged), "created": created,
            "merged": merged, "previous_version":
            prev_version_row["v"] if prev_version_row else None}


def _persist_cells(review_page_id, cells, fresh):
    for c in cells:
        DB.execute(
            "INSERT INTO review_cells(review_page_id, cell_id, row_idx, "
            "col_idx, bbox, ocr_text, ocr_confidence, fingerprint, "
            "current_value, human_edited, decision, confirmed, updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,0,?,0,?)",
            (review_page_id, c["cell_id"], c["row"], c["col"],
             json.dumps(c["bbox"]), c["text"], c["confidence"],
             c.get("fingerprint"), c["text"],
             config.CELL_KEEP_OCR if fresh else config.CELL_RECHECK,
             time.time()))


def _apply_merge(prev_page_id, new_page_id, new_cells, structural=False):
    old_rows = DB.query(
        "SELECT * FROM review_cells WHERE review_page_id=?", (prev_page_id,))
    old_cells = [{"cell_id": r["cell_id"], "row": r["row_idx"],
                  "col": r["col_idx"], "text": r["ocr_text"],
                  "fingerprint": r["fingerprint"], "bbox": json.loads(r["bbox"])}
                 for r in old_rows]
    verdicts = {r["cell_id"]: {"value": r["current_value"],
                               "human_edited": bool(r["human_edited"]),
                               "confirmed": bool(r["confirmed"])}
                for r in old_rows}

    if structural:
        # A structural fallback means whole-page fingerprints did not match.
        # If almost none of the new cell fingerprints exist in the old page,
        # this is really an inserted/new page, not a revision: keep all new
        # OCR values and do not transplant prior review onto it.
        old_fps = {c["fingerprint"] for c in old_cells if c["fingerprint"]}
        hits = sum(1 for c in new_cells
                   if c.get("fingerprint") in old_fps)
        ratio = hits / max(1, len(new_cells))
        if ratio < 0.5:
            for c in new_cells:
                DB.execute(
                    "UPDATE review_cells SET decision=?, confirmed=0, "
                    "human_edited=0, updated_at=? WHERE review_page_id=? "
                    "AND cell_id=?",
                    (config.CELL_ADDED, time.time(), new_page_id,
                     c["cell_id"]))
            DB.execute(
                "UPDATE review_pages SET status=? WHERE id=?",
                (config.REVIEW_PENDING, new_page_id))
            return [], []

    records, removed = merge_extraction(old_cells, verdicts, new_cells)

    by_id = {r["cell_id"]: r for r in records}
    for r in records:
        DB.execute(
            "UPDATE review_cells SET current_value=?, decision=?, "
            "confirmed=?, human_edited=?, updated_at=? WHERE review_page_id=? "
            "AND cell_id=?",
            (r["proposed_value"], r["decision"],
             0 if r["needs_confirmation"] else 1,
             1 if r["decision"] == config.CELL_DRAFT else 0,
             time.time(), new_page_id, r["cell_id"]))
        DB.execute(
            "INSERT INTO cell_history(review_page_id, cell_id, old_value, "
            "new_value, editor, reason, at) VALUES(?,?,?,?,?,?,?)",
            (new_page_id, r["cell_id"], r["old_text"], r["proposed_value"],
             "merge-engine", r["reason"], time.time()))

    needs = page_requires_review(records, removed)
    DB.execute("UPDATE review_pages SET status=? WHERE id=?",
               (config.REVIEW_PENDING if needs else config.REVIEW_IN_PROGRESS,
                new_page_id))
    return records, removed


# -------------------------------- review API --------------------------------

@router.add("GET", r"^/pages/(?P<page_id>[0-9]+)/image$")
def proxy_image(body, page_id):
    """Proxy the original scan PNG from the receiver so the UI can show the
    evidence image without talking to two origins."""
    page = DB.one("SELECT receiver_page_id FROM review_pages WHERE id=?",
                  (page_id,))
    if not page:
        raise HttpError(404, "unknown review page")
    raw = get(config.RECEIVER_URL +
              "/pages/%d/image" % page["receiver_page_id"])
    if not isinstance(raw, bytes):
        raise HttpError(502, "receiver did not return an image")
    return 200, raw, "image/png"


@router.add("GET", r"^/documents/(?P<doc_id>[^/]+)/pages$")
def list_pages(body, doc_id):
    rows = DB.query(
        "SELECT id, version, receiver_page_id, seq, status, revision, "
        "table_fingerprint FROM review_pages WHERE doc_id=? "
        "ORDER BY version, seq", (doc_id,))
    return {"doc_id": doc_id, "pages": rows}


@router.add("GET", r"^/pages/(?P<page_id>[0-9]+)$")
def get_page(body, page_id):
    page = DB.one("SELECT * FROM review_pages WHERE id=?", (page_id,))
    if not page:
        raise HttpError(404, "unknown review page")
    cells = DB.query(
        "SELECT cell_id, row_idx, col_idx, bbox, ocr_text, ocr_confidence, "
        "fingerprint, current_value, human_edited, decision, confirmed, "
        "editor FROM review_cells WHERE review_page_id=? ORDER BY row_idx, "
        "col_idx", (page_id,))
    for c in cells:
        c["bbox"] = json.loads(c["bbox"])
    history = DB.query(
        "SELECT cell_id, old_value, new_value, editor, reason, at "
        "FROM cell_history WHERE review_page_id=? ORDER BY id DESC LIMIT 30",
        (page_id,))
    return {"page": page, "cells": cells, "history": history}


@router.add("PUT", r"^/pages/(?P<page_id>[0-9]+)/cells/(?P<cell_id>[^/]+)$")
def edit_cell(body, page_id, cell_id):
    """Edit one cell.  Optimistic concurrency via expected_revision: if the
    page revision moved (another reviewer saved first), reject with 409."""
    page = DB.one("SELECT * FROM review_pages WHERE id=?", (page_id,))
    if not page:
        raise HttpError(404, "unknown review page")
    expected = body.get("expected_revision")
    if expected is not None and int(expected) != page["revision"]:
        raise HttpError(409, "page was modified by another reviewer "
                            "(revision %d, you had %d)"
                            % (page["revision"], expected))
    cell = DB.one(
        "SELECT * FROM review_cells WHERE review_page_id=? AND cell_id=?",
        (page_id, cell_id))
    if not cell:
        raise HttpError(404, "unknown cell")

    new_value = body.get("value", cell["current_value"])
    editor = body.get("editor", "anonymous")
    DB.execute(
        "UPDATE review_cells SET current_value=?, human_edited=1, "
        "confirmed=1, editor=?, updated_at=? WHERE id=?",
        (new_value, editor, time.time(), cell["id"]))
    DB.execute(
        "INSERT INTO cell_history(review_page_id, cell_id, old_value, "
        "new_value, editor, reason, at) VALUES(?,?,?,?,?,?,?)",
        (page_id, cell_id, cell["current_value"], new_value, editor,
        body.get("reason", "manual edit"), time.time()))
    new_rev = page["revision"] + 1
    DB.execute(
        "UPDATE review_pages SET revision=?, status=?, updated_at=? WHERE id=?",
        (new_rev, config.REVIEW_IN_PROGRESS, time.time(), page_id))
    return {"cell_id": cell_id, "value": new_value, "revision": new_rev}


@router.add("POST", r"^/pages/(?P<page_id>[0-9]+)/approve$")
def approve(body, page_id):
    page = DB.one("SELECT * FROM review_pages WHERE id=?", (page_id,))
    if not page:
        raise HttpError(404, "unknown review page")
    unconfirmed = DB.query(
        "SELECT cell_id, current_value FROM review_cells "
        "WHERE review_page_id=? AND confirmed=0", (page_id,))
    if unconfirmed:
        raise HttpError(409, "%d cell(s) still need confirmation: %s"
                            % (len(unconfirmed),
                               [u["cell_id"] for u in unconfirmed]))
    cells = DB.query(
        "SELECT cell_id, row_idx, col_idx, current_value, ocr_text, "
        "human_edited FROM review_cells WHERE review_page_id=? "
        "ORDER BY row_idx, col_idx", (page_id,))
    payload = {"doc_id": page["doc_id"], "version": page["version"],
               "review_page_id": page["id"], "seq": page["seq"],
               "approved_by": body.get("editor", "reviewer"),
               "cells": [{"cell_id": c["cell_id"], "row": c["row_idx"],
                          "col": c["col_idx"], "value": c["current_value"],
                          "ocr_text": c["ocr_text"],
                          "human_edited": bool(c["human_edited"])}
                         for c in cells]}
    result = post(config.WAREHOUSE_URL + "/records", payload)
    DB.execute("UPDATE review_pages SET status=?, updated_at=? WHERE id=?",
               (config.REVIEW_STORED, time.time(), page_id))
    return {"approved": True, "warehouse": result}


@router.add("GET", r"^/documents/(?P<doc_id>[^/]+)/compare$")
def compare(body, doc_id):
    """Compare the newest two review versions page by page for the UI."""
    versions = [r["version"] for r in DB.query(
        "SELECT DISTINCT version FROM review_pages WHERE doc_id=? "
        "ORDER BY version", (doc_id,))]
    if len(versions) < 2:
        return {"doc_id": doc_id, "comparisons": [],
                "note": "need two versions to compare"}
    va, vb = versions[-2], versions[-1]
    pa = DB.query("SELECT * FROM review_pages WHERE doc_id=? AND version=? "
                  "ORDER BY seq", (doc_id, va))
    pb = DB.query("SELECT * FROM review_pages WHERE doc_id=? AND version=? "
                  "ORDER BY seq", (doc_id, vb))
    comps = []
    # Match pages by table fingerprint first (page numbers shift on
    # insertion); fall back to document order for pages whose content
    # partially changed, so their per-cell old values still show.
    fp_map = {}
    for p in pa:
        fp_map.setdefault(p["table_fingerprint"], p)
    used_old = set()
    for newp in pb:
        oldp = fp_map.get(newp["table_fingerprint"])
        if oldp is None:
            for cand in pa:
                if cand["id"] not in used_old:
                    oldp = cand
                    break
        if oldp is not None:
            used_old.add(oldp["id"])
        entry = {"new_page_id": newp["id"], "new_seq": newp["seq"],
                 "old_seq": oldp["seq"] if oldp else None,
                 "matched": oldp is not None,
                 "fingerprint_match":
                     newp["table_fingerprint"] ==
                     (oldp["table_fingerprint"] if oldp else None),
                 "cells": []}
        new_cells = DB.query(
            "SELECT * FROM review_cells WHERE review_page_id=?",
            (newp["id"],))
        old_cells = {c["cell_id"]: c for c in DB.query(
            "SELECT * FROM review_cells WHERE review_page_id=?",
            (oldp["id"],))} if oldp else {}
        for nc in new_cells:
            oc = old_cells.get(nc["cell_id"])
            entry["cells"].append({
                "cell_id": nc["cell_id"],
                "old_value": oc["current_value"] if oc else None,
                "new_value": nc["current_value"],
                "ocr_value": nc["ocr_text"],
                "decision": nc["decision"],
                "changed": (not oc) or
                oc["current_value"] != nc["current_value"],
            })
        comps.append(entry)
    return {"doc_id": doc_id, "old_version": va, "new_version": vb,
            "comparisons": comps}


@router.add("GET", r"^/sync-versions$")
def sync_versions(body):
    doc_id = body.get("doc_id")
    if not doc_id:
        raise HttpError(400, "doc_id required")
    data = _receiver("/documents/%s/versions" % doc_id)
    return {"doc_id": doc_id,
            "versions": [v["version"] for v in data["versions"]]}


@router.add("GET", r"^/documents/(?P<doc_id>[^/]+)/warehouse-records$")
def warehouse_proxy(body, doc_id):
    return get(config.WAREHOUSE_URL +
               "/documents/%s/records" % doc_id)


@router.add("GET", r"^/health$")
def health(body):
    return {"service": "review", "ok": True}


def main():
    root = config.ensure_dirs()
    init(root)
    serve("review", config.REVIEW_PORT, router,
          static_dir=os.path.join(os.path.dirname(__file__), "static"))


if __name__ == "__main__":
    main()
