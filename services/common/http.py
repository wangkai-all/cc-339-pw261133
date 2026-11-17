"""Tiny HTTP JSON framework + urllib client helpers (stdlib only)."""

import json
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class HttpError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


class Router:
    """Pattern router: list of (method, regex, handler)."""

    def __init__(self):
        import re
        self._re = re
        self.routes = []

    def add(self, method, pattern):
        def deco(fn):
            self.routes.append((method, self._re.compile(pattern), fn))
            return fn
        return deco

    def match(self, method, path):
        for m, rx, fn in self.routes:
            if m != method:
                continue
            hit = rx.match(path)
            if hit:
                return fn, hit.groupdict()
        return None, None


def make_handler(router, static_dir=None):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # quieter logs
            pass

        def _send(self, status, payload, ctype="application/json"):
            if ctype == "application/json":
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            else:
                body = payload if isinstance(payload, bytes) else \
                    str(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", ctype + "; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_json(self):
            n = int(self.headers.get("Content-Length", 0) or 0)
            if not n:
                return {}
            raw = self.rfile.read(n)
            try:
                return json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                raise HttpError(400, "invalid JSON body")

        def _serve_static(self, rel):
            import os
            base = os.path.normpath(os.path.join(static_dir, rel))
            if not base.startswith(os.path.abspath(static_dir)):
                self._send(403, {"error": "forbidden"})
                return
            if os.path.isdir(base) or rel.endswith("/"):
                base = os.path.join(base, "index.html")
            if not os.path.isfile(base):
                self._send(404, {"error": "not found"})
                return
            ctype = "text/html" if base.endswith(".html") else \
                "application/javascript" if base.endswith(".js") else \
                "text/css" if base.endswith(".css") else \
                "image/png" if base.endswith(".png") else "text/plain"
            with open(base, "rb") as fh:
                self._send(200, fh.read(), ctype)

        def _handle(self, method):
            from urllib.parse import parse_qs
            if "?" in self.path:
                path, qs = self.path.split("?", 1)
                query = {k: v[0] for k, v in parse_qs(qs).items()}
            else:
                path, query = self.path, {}
            try:
                if static_dir and method == "GET" and path.startswith("/ui/"):
                    self._serve_static(path[4:] or "index.html")
                    return
                fn, kwargs = router.match(method, path)
                if fn is None:
                    self._send(404, {"error": "no route for %s %s"
                                              % (method, path)})
                    return
                if method in ("POST", "PUT", "PATCH"):
                    result = fn(self._read_json(), **kwargs)
                else:
                    result = fn(query, **kwargs)
                if result is None:
                    result = {"ok": True}
                if isinstance(result, tuple) and len(result) == 3 and \
                        isinstance(result[1], (bytes, bytearray)):
                    status, body, ctype = result
                    self._send(status, bytes(body), ctype)
                else:
                    status, body = result if isinstance(result, tuple) \
                        else (200, result)
                    self._send(status, body)
            except HttpError as exc:
                self._send(exc.status, {"error": exc.message})
            except Exception as exc:  # noqa: BLE001 - service must stay up
                self._send(500, {"error": "internal: %s" % exc})

        def do_GET(self):
            self._handle("GET")

        def do_POST(self):
            self._handle("POST")

        def do_PUT(self):
            self._handle("PUT")

        def do_PATCH(self):
            self._handle("PATCH")
    return Handler


def serve(name, port, router, static_dir=None):
    httpd = ThreadingHTTPServer(
        ("127.0.0.1", port), make_handler(router, static_dir))
    print("%s listening on http://127.0.0.1:%d" % (name, port), flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


# ----------------------------- client side --------------------------------

def request(method, url, payload=None, timeout=30):
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers,
                                 method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            ctype = resp.headers.get("Content-Type", "")
            if "json" in ctype:
                return json.loads(raw.decode("utf-8"))
            return raw
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            detail = json.loads(raw).get("error", raw)
        except ValueError:
            detail = raw
        raise HttpError(exc.code, detail)


def get(url, **kw):
    return request("GET", url, **kw)


def post(url, payload=None, **kw):
    return request("POST", url, payload or {}, **kw)


def put(url, payload=None, **kw):
    return request("PUT", url, payload or {}, **kw)


def download(url, dest, timeout=30):
    data = request("GET", url, timeout=timeout)
    if not isinstance(data, bytes):
        raise HttpError(502, "expected binary response")
    with open(dest, "wb") as fh:
        fh.write(data)


class LockedDB:
    """SQLite connection guarded by a lock (ThreadingHTTPServer is threaded)."""

    def __init__(self, path):
        import sqlite3
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.lock = threading.RLock()

    def query(self, sql, args=()):
        with self.lock:
            return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def one(self, sql, args=()):
        rows = self.query(sql, args)
        return rows[0] if rows else None

    def execute(self, sql, args=()):
        with self.lock:
            cur = self.conn.execute(sql, args)
            self.conn.commit()
            return cur.lastrowid

    def close(self):
        with self.lock:
            self.conn.close()
