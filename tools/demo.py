#!/usr/bin/env python3 -I
"""End-to-end demonstration driven entirely through the service HTTP APIs.

Flow (run from an empty data/ directory):
  1. create a document and upload the fictional v1 scans to the receiver
  2. let the separate extractor service really OCR them, then sync review
  3. two reviewers edit the same cell -> optimistic lock conflict (409)
  4. approve pages -> warehouse stores append-only records
  5. upload a NEW version (v2) incl. an inserted page (coordinates shift)
  6. demonstrate a late extraction task that cannot overwrite the new version
  7. re-sync review and inspect the per-cell merge decisions (REUSE / DRAFT
     / RECHECK / ADDED), confirming human edits survive
  8. show version comparison and warehouse records

Run:  PYTHONPATH=/workspace python3 -I tools/demo.py
(The -I interpreter is given /workspace explicitly via sys.path below.)
"""

import base64
import glob
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.common import config, http as httpc
from services.common.http import HttpError, get, post, put

R = config.RECEIVER_URL
X = "http://127.0.0.1:%d" % config.EXTRACTOR_PORT
DOC = "receipt-1042"
SAMPLE = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "sample_input", DOC)


def step(n, title):
    print("\n=== [%d] %s ===" % (n, title))


def wait_jobs(expected_done, timeout=40):
    """Poll receiver job list until pending/running jobs drain."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        jobs = get(R + "/jobs")["jobs"]
        active = [j for j in jobs if j["status"] in
                  (config.JOB_PENDING, config.JOB_RUNNING)]
        done = [j for j in jobs if j["status"] == config.JOB_DONE]
        if not active and len(done) >= expected_done:
            return jobs
        time.sleep(0.5)
    raise RuntimeError("extraction jobs did not finish in time")


def upload_version(version_tag, version_num):
    files = sorted(glob.glob(os.path.join(SAMPLE, version_tag, "*.png")))
    for seq, path in enumerate(files):
        with open(path, "rb") as fh:
            b64 = base64.b64encode(fh.read()).decode("ascii")
        post(R + "/documents/%s/pages" % DOC,
             {"version": version_num, "seq": seq,
              "file_name": os.path.basename(path), "image": b64})
    print("uploaded %d pages for %s (version %d)"
          % (len(files), version_tag, version_num))


def main():
    # sanity: services must be up
    for url in (R, X, config.WAREHOUSE_URL and
                "http://127.0.0.1:%d" % config.REVIEW_PORT):
        get(url + "/health")

    step(1, "create document + upload v1 scans")
    try:
        post(R + "/documents", {"doc_id": DOC})
    except HttpError as e:
        if e.status != 409:
            raise
        print("document already exists; continuing")
    upload_version("v1", 1)

    step(2, "extractor service runs real OCR (separate process)")
    jobs = wait_jobs(3)
    for j in jobs:
        print(" job %s v%s status=%s" % (j["id"], j["version"], j["status"]))
    pages = get(R + "/documents/%s/pages?version=1" % DOC)["pages"]
    sample = get(R + "/pages/%d/extraction" % pages[0]["id"])
    vals = [c["text"] for c in sample["extraction"]["cells"][:6]]
    print("real OCR on page 1 first cells:", vals)

    step(3, "sync into review service")
    post("http://127.0.0.1:%d/sync" % config.REVIEW_PORT,
         {"doc_id": DOC, "version": 1})
    rpages = get("http://127.0.0.1:%d/documents/%s/pages"
                 % (config.REVIEW_PORT, DOC))["pages"]
    rp = rpages[0]
    detail = get("http://127.0.0.1:%d/pages/%d"
                 % (config.REVIEW_PORT, rp["id"]))
    target = detail["cells"][4]  # a data cell
    print("review page %s revision r%s; editing cell %s"
          % (rp["id"], detail["page"]["revision"], target["cell_id"]))

    step(4, "two reviewers race -> optimistic lock conflict")
    base = "http://127.0.0.1:%d/pages/%d/cells/%s" % (
        config.REVIEW_PORT, rp["id"], target["cell_id"])
    put(base, {"value": "REVIEWER-A-VALUE", "expected_revision":
                detail["page"]["revision"], "editor": "reviewer-a"})
    print("reviewer-a saved (revision advanced)")
    try:
        put(base, {"value": "REVIEWER-B-STALE", "expected_revision":
                    detail["page"]["revision"], "editor": "reviewer-b"})
        print("ERROR: stale write was accepted!")
    except HttpError as e:
        print("reviewer-b blocked as expected: HTTP %s %s"
              % (e.status, e.message))
    # reviewer B retries with the fresh revision and wins
    fresh = get("http://127.0.0.1:%d/pages/%d"
                % (config.REVIEW_PORT, rp["id"]))["page"]["revision"]
    put(base, {"value": "REVIEWER-B-FINAL", "expected_revision": fresh,
                "editor": "reviewer-b"})
    print("reviewer-b retried with fresh revision and saved")

    step(5, "approve all v1 pages -> warehouse")
    for p in rpages:
        d = get("http://127.0.0.1:%d/pages/%d"
                % (config.REVIEW_PORT, p["id"]))
        # confirm every cell so approval is allowed
        for c in d["cells"]:
            if not c["confirmed"]:
                put("http://127.0.0.1:%d/pages/%d/cells/%s"
                     % (config.REVIEW_PORT, p["id"], c["cell_id"]),
                     {"value": c["current_value"],
                      "expected_revision": d["page"]["revision"],
                      "editor": "approver"})
                d["page"]["revision"] += 1
        out = post("http://127.0.0.1:%d/pages/%d/approve"
                   % (config.REVIEW_PORT, p["id"]), {"editor": "approver"})
        print(" page %s stored as warehouse record %s"
              % (p["id"], out["warehouse"]["record_id"]))

    step(6, "upload NEW version v2 (inserted page shifts coordinates)")
    post(R + "/documents/%s/new-version" % DOC, {})
    # hold extractor results to simulate a slow/old task finishing late
    post(X + "/control/hold", {})
    upload_version("v2", 2)
    time.sleep(2)  # let extractor process and HOLD results
    st = get(X + "/status")
    print("extractor held results:", st)

    step(7, "release late results -> stale guard rejects old-version job")
    rel = post(X + "/control/release", {})
    print("released %d held results" % rel["released"])
    time.sleep(1)
    jobs = get(R + "/jobs")["jobs"]
    rejected = [j for j in jobs if j["status"] == config.JOB_REJECTED]
    print("rejected stale jobs:", [(j["id"], j["version"], j["reject_reason"])
                                   for j in rejected])
    v2 = get(R + "/documents/%s/pages?version=2" % DOC)["pages"]
    print("v2 extracted pages:", [(p["seq"], p["status"]) for p in v2])

    step(8, "re-sync review -> per-cell merge, human edit preserved")
    post("http://127.0.0.1:%d/sync" % config.REVIEW_PORT,
         {"doc_id": DOC, "version": 2})
    cmp_data = get("http://127.0.0.1:%d/documents/%s/compare"
                   % (config.REVIEW_PORT, DOC))
    for cmp_ in cmp_data["comparisons"]:
        decisions = {}
        for c in cmp_["cells"]:
            decisions.setdefault(c["decision"], 0)
            decisions[c["decision"]] += 1
        print(" new page seq=%s matched-old=%s decisions=%s"
              % (cmp_["new_seq"], cmp_["matched"], decisions))
        # show the specific changed cell and any preserved human edit
        for c in cmp_["cells"]:
            if c["changed"]:
                print("   changed %s: old=%r ocr=%r now=%r [%s]"
                      % (c["cell_id"], c["old_value"], c["ocr_value"],
                         c["new_value"], c["decision"]))

    step(9, "warehouse records (append-only, v1 untouched)")
    wh = get("http://127.0.0.1:%d/documents/%s/records"
             % (config.WAREHOUSE_PORT, DOC))["records"]
    for r in wh["records"] if isinstance(wh, dict) else wh:
        rec = r["record"]
        print(" record #%s v%s seq=%s items=%s"
              % (rec["id"], rec["version"], rec["seq"], len(r["items"])))

    print("\nDEMO COMPLETE. UI at http://127.0.0.1:8013/ui/")


if __name__ == "__main__":
    main()
