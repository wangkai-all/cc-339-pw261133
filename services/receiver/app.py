"""Receiver / ingestion service.

Owns the canonical scanned files, document versioning and the extraction
job queue.  The extractor is a separate process that claims jobs and posts
results back; every job carries its (document, version) so a result that
arrives after a newer version was uploaded is rejected and never overwrites
current data.
"""

import hashlib
import os
import time

from services.common import config
from services.common.http import HttpError, LockedDB, Router, get, post, serve

router = Router()
DB = None
ROOT = None


def _db_path():
    return os.path.join(ROOT, "receiver", "receiver.db")


def _file_dir(doc_id, version):
    d = os.path.join(ROOT, "receiver", "files", doc_id, version)
    os.makedirs(d, exist_ok=True)
    return d


def _sha256_bytes(b):
    return hashlib.sha256(b).hexdigest()


def init(root):
    global DB, ROOT
    ROOT = root
    config.ensure_dirs()
    DB = LockedDB(_db_path())
    DB.conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS documents (
            doc_id        TEXT PRIMARY KEY,
            current_ver   INTEGER NOT NULL DEFAULT 0,
            created_at    REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS pages (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            doc_id        TEXT NOT NULL,
            version       INTEGER NOT NULL,
            seq           INTEGER NOT NULL,
            file_name     TEXT NOT NULL,
            file_path     TEXT NOT NULL,
            sha256        TEXT NOT NULL,
            width         INTEGER,
            height        INTEGER,
            status        TEXT NOT NULL DEFAULT 'NEW',
            table_fingerprint TEXT,
            extraction    TEXT,
            extracted_at  REAL,
            UNIQUE(doc_id, version, seq)
        );
        CREATE TABLE IF NOT EXISTS jobs (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            doc_id        TEXT NOT NULL,
            version       INTEGER NOT NULL,
            page_id       INTEGER NOT NULL,
            status        TEXT NOT NULL DEFAULT 'PENDING',
            attempts      INTEGER NOT NULL DEFAULT 0,
            created_at    REAL NOT NULL,
            claimed_at    REAL,
            finished_at   REAL,
            reject_reason TEXT
        );
        """)


# ------------------------------- upload API --------------------------------

@router.add("POST", r"^/documents$")
def create_document(body):
    doc_id = body.get("doc_id")
    if not doc_id:
        raise HttpError(400, "doc_id required")
    existing = DB.one("SELECT doc_id FROM documents WHERE doc_id=?", (doc_id,))
    if existing:
        raise HttpError(409, "document %s already exists" % doc_id)
    DB.execute("INSERT INTO documents(doc_id, current_ver, created_at) "
               "VALUES(?, 1, ?)", (doc_id, time.time()))
    return 201, {"doc_id": doc_id, "version": 1}


@router.add("GET", r"^/documents/(?P<doc_id>[^/]+)$")
def get_document(body, doc_id):
    doc = DB.one("SELECT * FROM documents WHERE doc_id=?", (doc_id,))
    if not doc:
        raise HttpError(404, "unknown document")
    pages = DB.query(
        "SELECT id, seq, file_name, status, table_fingerprint, width, height "
        "FROM pages WHERE doc_id=? AND version=? ORDER BY seq",
        (doc_id, doc["current_ver"]))
    return {"document": doc, "pages": pages}


@router.add("POST", r"^/documents/(?P<doc_id>[^/]+)/pages$")
def upload_page(body, doc_id):
    """Register one scanned page for the document's current (or explicitly
    given) version.  image is base64 PNG bytes."""
    import base64
    doc = DB.one("SELECT * FROM documents WHERE doc_id=?", (doc_id,))
    if not doc:
        raise HttpError(404, "unknown document")
    version = int(body.get("version", doc["current_ver"]))
    if version != doc["current_ver"]:
        raise HttpError(409, "version %d is not current (%d)"
                            % (version, doc["current_ver"]))
    seq = body.get("seq")
    name = body.get("file_name") or ("page-%03d.png" % seq)
    raw = base64.b64decode(body["image"])
    sha = _sha256_bytes(raw)
    fdir = _file_dir(doc_id, "v%d" % version)
    fpath = os.path.join(fdir, name)
    with open(fpath, "wb") as fh:
        fh.write(raw)
    page_id = DB.execute(
        "INSERT INTO pages(doc_id, version, seq, file_name, file_path, "
        "sha256, status) VALUES(?,?,?,?,?,?,'NEW')",
        (doc_id, version, seq, name, fpath, sha))
    job_id = DB.execute(
        "INSERT INTO jobs(doc_id, version, page_id, status, created_at) "
        "VALUES(?,?,?,'PENDING',?)",
        (doc_id, version, page_id, time.time()))
    return 201, {"page_id": page_id, "job_id": job_id, "version": version}


@router.add("POST", r"^/documents/(?P<doc_id>[^/]+)/new-version$")
def new_version(body, doc_id):
    """Declare that a new revision of the scans exists.  Bumps current_ver;
    all in-flight jobs for older versions become stale from this moment."""
    doc = DB.one("SELECT * FROM documents WHERE doc_id=?", (doc_id,))
    if not doc:
        raise HttpError(404, "unknown document")
    new_ver = doc["current_ver"] + 1
    DB.execute("UPDATE documents SET current_ver=? WHERE doc_id=?",
               (new_ver, doc_id))
    # mark still-pending/running old jobs superseded
    DB.execute(
        "UPDATE jobs SET status=? WHERE doc_id=? AND version<? "
        "AND status IN ('PENDING','RUNNING')",
        (config.JOB_SUPERSEDED, doc_id, new_ver))
    return 201, {"doc_id": doc_id, "version": new_ver}


@router.add("GET", r"^/documents/(?P<doc_id>[^/]+)/versions$")
def list_versions(body, doc_id):
    rows = DB.query(
        "SELECT version, COUNT(*) AS n, "
        "SUM(status='EXTRACTED') AS extracted FROM pages "
        "WHERE doc_id=? GROUP BY version ORDER BY version", (doc_id,))
    return {"versions": rows}


# ----------------------------- extractor queue ------------------------------

@router.add("POST", r"^/extractor/claim$")
def claim_job(body):
    """Extractor asks for the next pending job for the newest version."""
    row = DB.one(
        "SELECT j.*, p.file_path, p.file_name FROM jobs j JOIN pages p "
        "ON p.id=j.page_id WHERE j.status='PENDING' "
        "ORDER BY j.doc_id, j.version DESC, j.id LIMIT 1")
    if not row:
        return {"job": None}
    # never run a job whose version is no longer current
    doc = DB.one("SELECT current_ver FROM documents WHERE doc_id=?",
                 (row["doc_id"],))
    if row["version"] < doc["current_ver"]:
        DB.execute("UPDATE jobs SET status=?, reject_reason=? WHERE id=?",
                   (config.JOB_SUPERSEDED, "newer version uploaded",
                    row["id"]))
        return claim_job(body)
    DB.execute("UPDATE jobs SET status='RUNNING', attempts=attempts+1, "
               "claimed_at=? WHERE id=?", (time.time(), row["id"]))
    return {"job": {"job_id": row["id"], "doc_id": row["doc_id"],
                    "version": row["version"], "page_id": row["page_id"],
                    "seq": None,
                    "file_path": row["file_path"],
                    "file_name": row["file_name"]}}


@router.add("POST", r"^/extractor/result$")
def submit_result(body):
    job = DB.one("SELECT * FROM jobs WHERE id=?", (body["job_id"],))
    if not job:
        raise HttpError(404, "unknown job")
    doc = DB.one("SELECT current_ver FROM documents WHERE doc_id=?",
                 (job["doc_id"],))
    # LATE RESULT GUARD: a result for an old version can never overwrite
    # current extraction, even if the extractor finished slowly.
    if job["version"] < doc["current_ver"]:
        DB.execute("UPDATE jobs SET status=?, reject_reason=?, finished_at=? "
                   "WHERE id=?",
                   (config.JOB_REJECTED, "stale: newer version exists",
                    time.time(), job["id"]))
        return 409, {"accepted": False,
                     "reason": "stale result for superseded version",
                     "current_version": doc["current_ver"]}
    if not body.get("ok"):
        DB.execute("UPDATE jobs SET status='PENDING', finished_at=? WHERE id=?",
                   (time.time(), job["id"]))
        DB.execute("UPDATE pages SET status='FAILED' WHERE id=?",
                   (job["page_id"],))
        return {"accepted": True, "retried": True}

    import json
    extraction = body["extraction"]
    DB.execute(
        "UPDATE pages SET status='EXTRACTED', extraction=?, "
        "table_fingerprint=?, width=?, height=?, extracted_at=? WHERE id=?",
        (json.dumps(extraction), extraction.get("table_fingerprint"),
         extraction.get("width"), extraction.get("height"),
         time.time(), job["page_id"]))
    DB.execute("UPDATE jobs SET status='DONE', finished_at=? WHERE id=?",
               (time.time(), job["id"]))
    return {"accepted": True, "page_id": job["page_id"]}


@router.add("GET", r"^/pages/(?P<page_id>[0-9]+)/image$")
def page_image(body, page_id):
    page = DB.one("SELECT * FROM pages WHERE id=?", (page_id,))
    if not page:
        raise HttpError(404, "unknown page")
    with open(page["file_path"], "rb") as fh:
        raw = fh.read()
    return 200, raw, "image/png"


@router.add("GET", r"^/pages/(?P<page_id>[0-9]+)/extraction$")
def page_extraction(body, page_id):
    import json
    page = DB.one("SELECT * FROM pages WHERE id=?", (page_id,))
    if not page:
        raise HttpError(404, "unknown page")
    if not page["extraction"]:
        raise HttpError(409, "page not extracted yet")
    out = dict(page)
    out["extraction"] = json.loads(page["extraction"])
    return out


@router.add("GET", r"^/documents/(?P<doc_id>[^/]+)/pages$")
def doc_pages(body, doc_id):
    version = body.get("version")
    doc = DB.one("SELECT current_ver FROM documents WHERE doc_id=?", (doc_id,))
    if not doc:
        raise HttpError(404, "unknown document")
    ver = int(version) if version else doc["current_ver"]
    rows = DB.query(
        "SELECT id, version, seq, file_name, status, table_fingerprint, "
        "width, height FROM pages WHERE doc_id=? AND version=? ORDER BY seq",
        (doc_id, ver))
    return {"doc_id": doc_id, "version": ver, "pages": rows}


@router.add("GET", r"^/jobs$")
def list_jobs(body):
    return {"jobs": DB.query(
        "SELECT id, doc_id, version, page_id, status, reject_reason "
        "FROM jobs ORDER BY id")}


@router.add("GET", r"^/health$")
def health(body):
    return {"service": "receiver", "ok": True}


def main():
    root = config.ensure_dirs()
    init(root)
    serve("receiver", config.RECEIVER_PORT, router)


if __name__ == "__main__":
    main()
