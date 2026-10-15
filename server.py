#!/usr/bin/env python3
"""Web front end for gdocs2docspace: paste Google links, get them in DocSpace.

Served under PREFIX on the DocSpace domain itself, so the browser sends the
DocSpace session cookie along and every upload is done as the logged-in user.
No credentials are stored here.
"""
import json
import os
import re
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from gdocs2docspace import DocSpace, Fail, download, google_opener

PREFIX = "/gdocs-import"
ROUTER = os.environ.get("DOCSPACE_INTERNAL", "http://onlyoffice-router:8092")
PORT = int(os.environ.get("PORT", "8080"))
INDEX = Path(__file__).resolve().parent / "index.html"
COOKIE_RE = re.compile(r"(?:^|;\s*)asc_auth_key=([^;]+)")

HTTP_STATUS = {"auth": 401, "forbidden": 403, "bad_url": 400, "bad_request": 400}


class Handler(BaseHTTPRequestHandler):
    server_version = "gdocs-import"
    protocol_version = "HTTP/1.1"
    timeout = 60

    def send(self, status, body, ctype="application/json; charset=utf-8", extra=None):
        if not isinstance(body, bytes):
            body = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def public_host(self):
        return self.headers.get("X-Forwarded-Host") or self.headers.get("Host", "")

    def do_GET(self):
        path = urllib.parse.urlsplit(self.path).path
        if path == PREFIX:
            self.send(301, b"", extra={"Location": PREFIX + "/"})
        elif path == PREFIX + "/":
            self.send(200, INDEX.read_bytes(), "text/html; charset=utf-8", {
                "Content-Security-Policy":
                    "default-src 'none'; style-src 'unsafe-inline'; "
                    "script-src 'unsafe-inline'; connect-src 'self'; "
                    "frame-ancestors 'none'; base-uri 'none'",
            })
        elif path == PREFIX + "/healthz":
            self.send(200, {"ok": True})
        else:
            self.send(404, {"ok": False, "code": "not_found", "error": "not found"})

    def do_POST(self):
        if urllib.parse.urlsplit(self.path).path != PREFIX + "/api/import":
            return self.send(404, {"ok": False, "code": "not_found", "error": "not found"})
        try:
            self.send(200, self.do_import())
        except Fail as e:
            self.send(HTTP_STATUS.get(e.code, 502),
                      {"ok": False, "code": e.code, "error": str(e)})

    def do_import(self):
        # the session cookie makes this endpoint a CSRF target: accept only
        # JSON posted by a page of our own origin
        origin = urllib.parse.urlsplit(self.headers.get("Origin", "")).netloc
        if not origin or origin != self.public_host():
            raise Fail("cross-origin request", "forbidden")
        if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
            raise Fail("json expected", "bad_request")
        try:
            length = int(self.headers.get("Content-Length", ""))
            if not 0 < length <= 8192:
                raise ValueError
            req = json.loads(self.rfile.read(length))
            url, folder = str(req["url"]).strip(), str(req["folder"])
        except (ValueError, KeyError, TypeError):
            raise Fail("bad request body", "bad_request") from None
        if not folder.isdigit():
            raise Fail("bad folder id", "bad_request")

        m = COOKIE_RE.search(self.headers.get("Cookie", ""))
        if not m:
            raise Fail("not logged in to DocSpace", "auth")
        ds = DocSpace(ROUTER, None, {
            "Cookie": f"asc_auth_key={m.group(1)}",
            "Host": self.public_host(),
            "X-Forwarded-Host": self.public_host(),
            "X-Forwarded-Proto": self.headers.get("X-Forwarded-Proto", "https"),
        })
        # check the session before fetching anything, so the service cannot be
        # used as an anonymous download proxy
        ds.call("GET", "/api/2.0/people/@self")

        name, data = download(google_opener(None), url)
        f = ds.upload(folder, name, data, overwrite=False)
        return {"ok": True, "name": f.get("title") or name, "size": len(data),
                "id": f.get("id"), "webUrl": f.get("webUrl")}

    def log_message(self, fmt, *args):
        print(f"{self.headers.get('X-Forwarded-For', self.client_address[0])} "
              + fmt % args, flush=True)


if __name__ == "__main__":
    ThreadingHTTPServer.daemon_threads = True
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
