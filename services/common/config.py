"""Shared service configuration and constants."""

import os

ROOT = os.environ.get("PD_ROOT", os.path.join(os.getcwd(), "data"))

RECEIVER_PORT = int(os.environ.get("PD_RECEIVER_PORT", "8011"))
EXTRACTOR_PORT = int(os.environ.get("PD_EXTRACTOR_PORT", "8012"))
REVIEW_PORT = int(os.environ.get("PD_REVIEW_PORT", "8013"))
WAREHOUSE_PORT = int(os.environ.get("PD_WAREHOUSE_PORT", "8014"))

RECEIVER_URL = os.environ.get("PD_RECEIVER_URL",
                              "http://127.0.0.1:%d" % RECEIVER_PORT)
WAREHOUSE_URL = os.environ.get("PD_WAREHOUSE_URL",
                               "http://127.0.0.1:%d" % WAREHOUSE_PORT)

# Extraction job lifecycle (receiver side).
JOB_PENDING = "PENDING"
JOB_RUNNING = "RUNNING"
JOB_DONE = "DONE"
JOB_SUPERSEDED = "SUPERSEDED"   # a newer version made the job stale
JOB_REJECTED = "REJECTED"       # extractor result came back too late

# Extraction page status.
PAGE_NEW = "NEW"
PAGE_EXTRACTED = "EXTRACTED"
PAGE_FAILED = "FAILED"

# Review page lifecycle.
REVIEW_PENDING = "PENDING"
REVIEW_IN_PROGRESS = "IN_PROGRESS"
REVIEW_APPROVED = "APPROVED"
REVIEW_STORED = "STORED"

# Per-cell merge decisions when a new extraction is merged with prior review.
CELL_KEEP_OCR = "KEEP_OCR"          # no prior opinion, take new extraction
CELL_REUSE = "REUSE"                # cell unchanged, human verdict still valid
CELL_DRAFT = "DRAFT"                # human edited, content drifted -> keep edit,
                                    # flag for confirmation
CELL_RECHECK = "RECHECK"            # content changed and no safe reuse
CELL_ADDED = "ADDED"                # new cell that did not exist before
CELL_REMOVED = "REMOVED"            # cell disappeared in the new version


def ensure_dirs():
    for sub in ("receiver", "review", "warehouse", "downloads"):
        os.makedirs(os.path.join(ROOT, sub), exist_ok=True)
    return ROOT
