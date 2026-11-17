"""Extractor service (separate process).

Polls the receiver for extraction jobs, runs the real pixel-level table
extraction in :mod:`services.extractor.engine`, and posts results back.
The receiver decides whether a result is still current; this service never
writes anything itself.

A small control endpoint lets a test/demo pause result submission to model
an extraction task that finishes late (after a new version arrived).
"""

import threading
import time

from services.common import config
from services.common.http import HttpError, Router, get, post, serve
from services.extractor import engine

router = Router()

STATE = {
    "running": True,
    "hold": False,          # when True, finished results are held back
    "held": [],             # held results awaiting release
    "processed": 0,
}
_LOCK = threading.RLock()


def _poll_once():
    try:
        resp = post(config.RECEIVER_URL + "/extractor/claim", {})
    except HttpError as exc:
        print("claim failed: %s" % exc.message, flush=True)
        return False
    job = resp.get("job")
    if not job:
        return False

    print("extracting job %s page %s" % (job["job_id"], job["file_name"]),
          flush=True)
    try:
        extraction = engine.extract(job["file_path"])
    except Exception as exc:  # noqa: BLE001 - report failure, keep polling
        extraction = {"ok": False, "reason": str(exc)}

    payload = {"job_id": job["job_id"], "ok": extraction.get("ok", False),
               "extraction": extraction}
    with _LOCK:
        if STATE["hold"]:
            STATE["held"].append(payload)
            print("job %s result HELD (late-task simulation)"
                  % job["job_id"], flush=True)
            return True
    _submit(payload)
    return True


def _submit(payload):
    try:
        result = post(config.RECEIVER_URL + "/extractor/result", payload)
        with _LOCK:
            STATE["processed"] += 1
        print("job %s submitted -> %s"
              % (payload["job_id"], result), flush=True)
    except HttpError as exc:
        # 409 here is the stale-version guard working as designed.
        print("job %s rejected: %s" % (payload["job_id"], exc.message),
              flush=True)
        with _LOCK:
            STATE["processed"] += 1


def _worker():
    while STATE["running"]:
        try:
            did = _poll_once()
        except Exception as exc:  # noqa: BLE001 - never die in background
            print("poll error: %s" % exc, flush=True)
            did = False
        time.sleep(0.3 if did else 1.0)


@router.add("POST", r"^/control/hold$")
def hold(body):
    with _LOCK:
        STATE["hold"] = True
    return {"hold": True, "held_count": len(STATE["held"])}


@router.add("POST", r"^/control/release$")
def release(body):
    with _LOCK:
        STATE["hold"] = False
        held = list(STATE["held"])
        STATE["held"].clear()
    for payload in held:
        _submit(payload)
    return {"released": len(held)}


@router.add("POST", r"^/control/stop$")
def stop(body):
    STATE["running"] = False
    return {"stopping": True}


@router.add("GET", r"^/status$")
def status(body):
    with _LOCK:
        return {"service": "extractor", "running": STATE["running"],
                "hold": STATE["hold"], "held": len(STATE["held"]),
                "processed": STATE["processed"]}


@router.add("GET", r"^/health$")
def health(body):
    return {"service": "extractor", "ok": True}


def main():
    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    serve("extractor", config.EXTRACTOR_PORT, router)
    STATE["running"] = False


if __name__ == "__main__":
    main()
