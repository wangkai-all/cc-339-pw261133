"""End-to-end service test on isolated ports and a temp data directory.

Boots the four real services in background threads (same code paths as the
separate-process deployment), then drives upload -> extraction -> review ->
warehouse, including the stale-version guard and the optimistic-lock conflict.
"""

import base64
import importlib
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.common import config, http as httpc
from services.common.http import HttpError, get, post, put
from services.extractor import engine
import importlib.util

_spec = importlib.util.spec_from_file_location(
    "generate_scans",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                 "tools", "generate_scans.py"))
generate_scans = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(generate_scans)


def _server(mod_name, port, static=False):
    mod = importlib.import_module(mod_name)
    handler = httpc.make_handler(
        mod.router,
        os.path.join(os.path.dirname(mod.__file__), "static") if static
        else None)
    httpd = ThreadingHTTPServer(("127.0.0.1", port), handler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    return httpd


class ServiceFlowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="pd-e2e-")
        os.environ["PD_ROOT"] = cls.tmp
        # reload config to pick up temp root and free ports
        importlib.reload(config)
        httpc  # noqa
        cls.ports = dict(rec=8111, ext=8112, rev=8113, wh=8114)
        config.RECEIVER_PORT = cls.ports["rec"]
        config.EXTRACTOR_PORT = cls.ports["ext"]
        config.REVIEW_PORT = cls.ports["rev"]
        config.WAREHOUSE_PORT = cls.ports["wh"]
        config.RECEIVER_URL = "http://127.0.0.1:%d" % cls.ports["rec"]
        config.WAREHOUSE_URL = "http://127.0.0.1:%d" % cls.ports["wh"]

        from services.receiver import app as recv
        from services.warehouse import app as wh
        from services.review import app as rev
        from services.extractor import app as ext
        recv.init(cls.tmp)
        wh.init(cls.tmp)
        rev.init(cls.tmp)
        cls.servers = [
            _server("services.receiver.app", cls.ports["rec"]),
            _server("services.warehouse.app", cls.ports["wh"]),
            _server("services.review.app", cls.ports["rev"], static=True),
            _server("services.extractor.app", cls.ports["ext"]),
        ]
        # extractor background worker (HTTP control server runs above)
        cls.ext = ext
        cls.ext.STATE.update(running=True, hold=False, held=[], processed=0)
        cls.worker = threading.Thread(target=ext._worker, daemon=True)
        cls.worker.start()

        cls.R = config.RECEIVER_URL
        cls.REV = "http://127.0.0.1:%d" % cls.ports["rev"]
        cls.WH = config.WAREHOUSE_URL
        cls.DOC = "doc-e2e"

        # generate scans
        cls.gen = os.path.join(cls.tmp, "scans")
        old = sys.argv
        sys.argv = ["g", cls.gen]
        generate_scans.main()
        sys.argv = old

    @classmethod
    def tearDownClass(cls):
        cls.ext.STATE["running"] = False
        for s in cls.servers:
            s.shutdown()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _upload(self, ver_tag, ver_num):
        import glob
        for seq, path in enumerate(sorted(glob.glob(
                os.path.join(self.gen, "receipt-1042", ver_tag, "*.png")))):
            with open(path, "rb") as fh:
                b64 = base64.b64encode(fh.read()).decode()
            post(self.R + "/documents/%s/pages" % self.DOC,
                 {"version": ver_num, "seq": seq, "image": b64})

    def _wait(self, n, timeout=30):
        end = time.time() + timeout
        while time.time() < end:
            jobs = get(self.R + "/jobs")["jobs"]
            if len([j for j in jobs if j["status"] == "DONE"]) >= n and \
                    not [j for j in jobs if j["status"] in
                         ("PENDING", "RUNNING")]:
                return jobs
            time.sleep(0.3)
        raise AssertionError("jobs did not complete: %s"
                             % get(self.R + "/jobs"))

    def test_full_flow_with_guards(self):
        post(self.R + "/documents", {"doc_id": self.DOC})
        self._upload("v1", 1)
        jobs = self._wait(3)
        self.assertTrue(all(j["status"] == "DONE" for j in jobs))

        # extraction really produced cells with evidence
        pages = get(self.R + "/documents/%s/pages?version=1"
                    % self.DOC)["pages"]
        ext0 = get(self.R + "/pages/%d/extraction" % pages[0]["id"])
        self.assertTrue(ext0["extraction"]["cells"][0]["bbox"])

        # sync and review
        post(self.REV + "/sync", {"doc_id": self.DOC, "version": 1})
        rpages = get(self.REV + "/documents/%s/pages" % self.DOC)["pages"]
        self.assertEqual(len(rpages), 3)
        detail = get(self.REV + "/pages/%d" % rpages[0]["id"])
        cell = detail["cells"][4]
        rev0 = detail["page"]["revision"]

        # optimistic lock: stale revision rejected
        put(self.REV + "/pages/%d/cells/%s"
             % (rpages[0]["id"], cell["cell_id"]),
             {"value": "A-EDIT", "expected_revision": rev0,
              "editor": "u1"})
        with self.assertRaises(HttpError) as cm:
            put(self.REV + "/pages/%d/cells/%s"
                 % (rpages[0]["id"], cell["cell_id"]),
                 {"value": "B-STALE", "expected_revision": rev0,
                  "editor": "u2"})
        self.assertEqual(cm.exception.status, 409)

        # cannot approve while any cell unconfirmed on another page
        target = rpages[1]
        with self.assertRaises(HttpError):
            post(self.REV + "/pages/%d/approve" % target["id"], {})

        # confirm all cells on all pages, then approve
        for p in rpages:
            d = get(self.REV + "/pages/%d" % p["id"])
            for c in d["cells"]:
                if not c["confirmed"]:
                    put(self.REV + "/pages/%d/cells/%s"
                         % (p["id"], c["cell_id"]),
                         {"value": c["current_value"],
                          "expected_revision": d["page"]["revision"],
                          "editor": "approver"})
                    d["page"]["revision"] += 1
            post(self.REV + "/pages/%d/approve" % p["id"],
                 {"editor": "approver"})
        wh = get(self.WH + "/documents/%s/records" % self.DOC)["records"]
        self.assertEqual(len(wh), 3)

        # ---- new version, with a held (late) extraction ----
        post(self.R + "/documents/%s/new-version" % self.DOC, {})
        post("http://127.0.0.1:%d/control/hold" % self.ports["ext"], {})
        self._upload("v2", 2)
        time.sleep(2.5)
        # nothing submitted while held
        self.assertGreaterEqual(len(self.ext.STATE["held"]), 1)
        post("http://127.0.0.1:%d/control/release" % self.ports["ext"], {})
        time.sleep(1.5)

        v2pages = get(self.R + "/documents/%s/pages?version=2"
                      % self.DOC)["pages"]
        self.assertEqual(len(v2pages), 4)
        self.assertTrue(all(p["status"] == "EXTRACTED" for p in v2pages))

        # merge into review and verify reuse + preserved human edit
        post(self.REV + "/sync", {"doc_id": self.DOC, "version": 2})
        cmp_data = get(self.REV + "/documents/%s/compare" % self.DOC)
        self.assertEqual(cmp_data["old_version"], 1)
        self.assertEqual(cmp_data["new_version"], 2)
        all_decisions = [c["decision"] for cmp_ in cmp_data["comparisons"]
                         for c in cmp_["cells"]]
        self.assertIn("REUSE", all_decisions)   # unchanged cells reused
        # inserted page: structurally present but with no fingerprint match
        # and every cell decided ADDED (no prior review transplanted)
        inserted = [c for c in cmp_data["comparisons"]
                    if not c["fingerprint_match"]
                    and all(x["decision"] == "ADDED" for x in c["cells"])]
        self.assertTrue(inserted, "inserted page must be detected as ADDED")
        # a partially changed page keeps REUSE for unchanged cells and asks
        # for re-confirmation (DRAFT) on the cells whose content changed
        self.assertIn("DRAFT", all_decisions)

        # warehouse still holds exactly the v1 records (append-only)
        wh2 = get(self.WH + "/documents/%s/records" % self.DOC)["records"]
        self.assertEqual(len(wh2), 3)
        self.assertTrue(all(r["record"]["version"] == 1 for r in wh2))

    def test_late_v1_result_cannot_overwrite_v2(self):
        """An old extraction task that finishes after a new version exists
        must be rejected and never touch the current version's data."""
        doc = "doc-stale"
        post(self.R + "/documents", {"doc_id": doc})
        # hold all extractor results, then upload v1
        post("http://127.0.0.1:%d/control/hold" % self.ports["ext"], {})
        import glob
        v1 = sorted(glob.glob(os.path.join(
            self.gen, "receipt-1042", "v1", "*.png")))
        with open(v1[0], "rb") as fh:
            b64 = base64.b64encode(fh.read()).decode()
        post(self.R + "/documents/%s/pages" % doc,
             {"version": 1, "seq": 0, "image": b64})
        time.sleep(2.0)
        held = list(self.ext.STATE["held"])
        self.assertTrue(held, "extractor should have held the v1 result")

        # a new version is declared BEFORE the old task reports back
        post(self.R + "/documents/%s/new-version" % doc, {})
        post("http://127.0.0.1:%d/control/release" % self.ports["ext"], {})
        time.sleep(1.0)

        jobs = get(self.R + "/jobs")["jobs"]
        v1_jobs = [j for j in jobs if j["doc_id"] == doc]
        self.assertTrue(any(j["status"] == "REJECTED" for j in v1_jobs),
                        "late v1 job must be REJECTED: %s" % v1_jobs)
        # the v1 page must not have been marked EXTRACTED by the stale write
        pages = get(self.R + "/documents/%s/pages?version=1" % doc)["pages"]
        self.assertTrue(all(p["status"] != "EXTRACTED" for p in pages))


if __name__ == "__main__":
    unittest.main()
