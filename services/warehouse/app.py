"""Warehouse / persistence service.

Accepts only fully approved pages from the review service.  Records are
append-only: a page can be stored more than once if it is re-approved in a
newer version, but existing rows are never mutated or overwritten.  Every
stored row keeps its provenance (document, version, reviewer, OCR vs human).
"""

import os
import time

from services.common import config
from services.common.http import HttpError, LockedDB, Router, serve

router = Router()
DB = None


def init(root):
    global DB
    config.ensure_dirs()
    DB = LockedDB(os.path.join(root, "warehouse", "warehouse.db"))
    DB.conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS records (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            doc_id          TEXT NOT NULL,
            version         INTEGER NOT NULL,
            review_page_id  INTEGER NOT NULL,
            seq             INTEGER NOT NULL,
            approved_by     TEXT NOT NULL,
            stored_at       REAL NOT NULL,
            UNIQUE(review_page_id)
        );
        CREATE TABLE IF NOT EXISTS record_items (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            record_id       INTEGER NOT NULL,
            cell_id         TEXT NOT NULL,
            row_idx         INTEGER NOT NULL,
            col_idx         INTEGER NOT NULL,
            value           TEXT NOT NULL,
            ocr_text        TEXT NOT NULL,
            human_edited    INTEGER NOT NULL,
            FOREIGN KEY(record_id) REFERENCES records(id)
        );
        """)


@router.add("POST", r"^/records$")
def add_record(body):
    required = ("doc_id", "version", "review_page_id", "cells")
    for k in required:
        if k not in body:
            raise HttpError(400, "missing field %s" % k)
    if not body["cells"]:
        raise HttpError(400, "refusing to store an empty approved page")

    # idempotent on review_page_id: never duplicate, never overwrite
    existing = DB.one("SELECT id FROM records WHERE review_page_id=?",
                      (body["review_page_id"],))
    if existing:
        return 409, {"stored": False,
                     "reason": "page already stored",
                     "record_id": existing["id"]}

    record_id = DB.execute(
        "INSERT INTO records(doc_id, version, review_page_id, seq, "
        "approved_by, stored_at) VALUES(?,?,?,?,?,?)",
        (body["doc_id"], body["version"], body["review_page_id"],
         body.get("seq"), body.get("approved_by", "reviewer"),
         time.time()))
    for c in body["cells"]:
        DB.execute(
            "INSERT INTO record_items(record_id, cell_id, row_idx, col_idx, "
            "value, ocr_text, human_edited) VALUES(?,?,?,?,?,?,?)",
            (record_id, c["cell_id"], c["row"], c["col"], c["value"],
             c.get("ocr_text", c["value"]), 1 if c.get("human_edited") else 0))
    return 201, {"stored": True, "record_id": record_id,
                 "items": len(body["cells"])}


@router.add("GET", r"^/records$")
def list_records(body):
    rows = DB.query(
        "SELECT r.*, COUNT(i.id) AS item_count FROM records r "
        "LEFT JOIN record_items i ON i.record_id=r.id "
        "GROUP BY r.id ORDER BY r.id")
    return {"records": rows}


@router.add("GET", r"^/records/(?P<record_id>[0-9]+)$")
def get_record(body, record_id):
    rec = DB.one("SELECT * FROM records WHERE id=?", (record_id,))
    if not rec:
        raise HttpError(404, "unknown record")
    items = DB.query(
        "SELECT cell_id, row_idx, col_idx, value, ocr_text, human_edited "
        "FROM record_items WHERE record_id=? ORDER BY row_idx, col_idx",
        (record_id,))
    return {"record": rec, "items": items}


@router.add("GET", r"^/documents/(?P<doc_id>[^/]+)/records$")
def doc_records(body, doc_id):
    rows = DB.query(
        "SELECT * FROM records WHERE doc_id=? ORDER BY version, seq",
        (doc_id,))
    out = []
    for r in rows:
        items = DB.query(
            "SELECT cell_id, row_idx, col_idx, value, human_edited "
            "FROM record_items WHERE record_id=? ORDER BY row_idx, col_idx",
            (r["id"],))
        out.append({"record": r, "items": items})
    return {"doc_id": doc_id, "records": out}


@router.add("GET", r"^/health$")
def health(body):
    return {"service": "warehouse", "ok": True}


def main():
    root = config.ensure_dirs()
    init(root)
    serve("warehouse", config.WAREHOUSE_PORT, router)


if __name__ == "__main__":
    main()
