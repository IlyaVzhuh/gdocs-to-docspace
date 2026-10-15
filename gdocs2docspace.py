#!/usr/bin/env python3
"""Download Google Docs / Sheets / Slides by URL and upload them to DocSpace.

    gdocs2docspace.py URL [URL ...]
    gdocs2docspace.py -f urls.txt --folder 1234
    cat urls.txt | gdocs2docspace.py -f -

Documents are exported as docx / xlsx / pptx. The Google file has to be
readable by "anyone with the link"; for private files pass --cookies with a
Netscape cookies.txt exported from a logged-in browser.

DocSpace auth, first match wins:
    DOCSPACE_API_KEY env  ->  --key-file  ->  DOCSPACE_USER + DOCSPACE_PASSWORD env
"""
import argparse
import http.cookiejar
import json
import mimetypes
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

DOCSPACE = os.environ.get("DOCSPACE_URL", "https://docspace.example.com").rstrip("/")
KEY_FILE = Path(__file__).resolve().parent / "docspace-api-key"

# google url kind -> export extension
KINDS = {"document": "docx", "spreadsheets": "xlsx", "presentation": "pptx"}
GDOC_RE = re.compile(
    r"docs\.google\.com/(document|spreadsheets|presentation)/(?:u/\d+/)?d/([\w-]+)"
)
UA = "Mozilla/5.0 (X11; Linux x86_64) gdocs2docspace"


class Fail(Exception):
    def __init__(self, msg, code="error"):
        super().__init__(msg)
        self.code = code  # machine-readable, used by the web front end


# ---------------------------------------------------------------- google

def google_opener(cookies):
    jar = http.cookiejar.MozillaCookieJar()
    if cookies:
        jar.load(cookies, ignore_discard=True, ignore_expires=True)
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    opener.addheaders = [("User-Agent", UA)]
    return opener


def filename_from(headers, fallback):
    cd = headers.get("Content-Disposition", "")
    m = re.search(r"filename\*=(?:UTF-8|utf-8)''([^;]+)", cd)
    if m:
        name = urllib.parse.unquote(m.group(1))
    else:
        m = re.search(r'filename="([^"]+)"', cd)
        name = m.group(1) if m else fallback
    return name.replace("/", "_").strip() or fallback


def download(opener, url):
    """Return (filename, bytes) for a Google Docs editor url."""
    m = GDOC_RE.search(url)
    if not m:
        raise Fail("not a Google Docs/Sheets/Slides url", "bad_url")
    kind, doc_id = m.groups()
    if doc_id == "e":
        raise Fail("this is a 'published to web' link, use the normal share link", "bad_url")
    ext = KINDS[kind]
    export = f"https://docs.google.com/{kind}/d/{doc_id}/export?format={ext}"
    try:
        with opener.open(export, timeout=120) as r:
            ctype = r.headers.get("Content-Type", "")
            data = r.read()
            name = filename_from(r.headers, f"{doc_id}.{ext}")
    except urllib.error.HTTPError as e:
        hint = {
            401: "not shared by link (use --cookies)",
            403: "access denied or export disabled by the owner",
            404: "not found",
        }.get(e.code, e.reason)
        code = {401: "private", 403: "denied", 404: "not_found"}.get(e.code, "google")
        raise Fail(f"google: HTTP {e.code} {hint}", code) from None
    except OSError as e:
        raise Fail(f"google: {e}", "google") from None
    # a login page instead of the file means the document is private
    if ctype.startswith("text/html"):
        raise Fail("google: got a login page, document is private (use --cookies)", "private")
    if not name.lower().endswith("." + ext):
        name += "." + ext
    return name, data


# -------------------------------------------------------------- docspace

class DocSpace:
    def __init__(self, base, auth, headers=None):
        self.base = base
        self.auth = auth
        self.headers = headers or {}

    def call(self, method, path, body=None, ctype="application/json"):
        headers = {"Accept": "application/json", "User-Agent": UA, **self.headers}
        if self.auth:
            headers["Authorization"] = self.auth
        if body is not None:
            headers["Content-Type"] = ctype
        req = urllib.request.Request(
            self.base + path, data=body, method=method, headers=headers
        )
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                return json.load(r)["response"]
        except urllib.error.HTTPError as e:
            text = e.read().decode("utf-8", "replace")
            try:
                text = json.loads(text)["error"]["message"]
            except (ValueError, KeyError, TypeError):
                text = text[:200]
            code = {401: "auth", 403: "forbidden"}.get(e.code, "docspace")
            raise Fail(f"docspace: HTTP {e.code} {method} {path}: {text}", code) from None
        except OSError as e:
            raise Fail(f"docspace: {e}", "docspace") from None

    def login(self, user, password):
        body = json.dumps({"userName": user, "password": password}).encode()
        self.auth = self.call("POST", "/api/2.0/authentication", body)["token"]

    def folder_id(self, folder):
        """Resolve @my / @common style aliases to a numeric id."""
        if str(folder).isdigit():
            return folder
        return self.call("GET", f"/api/2.0/files/{folder}?count=1")["current"]["id"]

    def upload(self, folder_id, name, data, overwrite):
        boundary = uuid.uuid4().hex
        mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
        quoted = name.replace("\\", "\\\\").replace('"', '\\"')
        body = b"".join([
            f"--{boundary}\r\n".encode(),
            f'Content-Disposition: form-data; name="file"; filename="{quoted}"\r\n'.encode(),
            f"Content-Type: {mime}\r\n\r\n".encode(),
            data,
            f"\r\n--{boundary}--\r\n".encode(),
        ])
        # createNewIfExist=false overwrites as a new version of the same file
        query = urllib.parse.urlencode(
            {"createNewIfExist": "false" if overwrite else "true"}
        )
        res = self.call(
            "POST",
            f"/api/2.0/files/{folder_id}/upload?{query}",
            body,
            f"multipart/form-data; boundary={boundary}",
        )
        # the endpoint answers with a list, one entry per uploaded file
        if isinstance(res, list):
            if not res:
                raise Fail("docspace: upload returned no file")
            res = res[0]
        return res


def connect(key_file):
    key = os.environ.get("DOCSPACE_API_KEY")
    if not key and key_file.is_file():
        key = key_file.read_text().strip()
    if key:
        return DocSpace(DOCSPACE, f"Bearer {key}")
    user, password = os.environ.get("DOCSPACE_USER"), os.environ.get("DOCSPACE_PASSWORD")
    if user and password:
        ds = DocSpace(DOCSPACE, None)
        ds.login(user, password)
        return ds
    raise Fail(
        f"no DocSpace credentials: set DOCSPACE_API_KEY, put the key in {key_file}, "
        "or set DOCSPACE_USER and DOCSPACE_PASSWORD"
    )


# ------------------------------------------------------------------ main

def read_urls(args):
    urls = list(args.urls)
    if args.file:
        src = sys.stdin if args.file == "-" else open(args.file)
        with src:
            urls += [l.strip() for l in src if l.strip() and not l.startswith("#")]
    return urls


def main():
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("urls", nargs="*", metavar="URL")
    p.add_argument("-f", "--file", help="file with one url per line, '-' for stdin")
    p.add_argument("--folder", default="@my",
                   help="DocSpace folder or room id (default: @my, My documents)")
    p.add_argument("--overwrite", action="store_true",
                   help="add a new version when a file with the same name exists")
    p.add_argument("--cookies", help="Netscape cookies.txt for private Google files")
    p.add_argument("--save-dir", help="also keep the downloaded files in this directory")
    p.add_argument("--no-upload", action="store_true", help="download only")
    p.add_argument("--key-file", type=Path, default=KEY_FILE,
                   help=f"file with a DocSpace API key (default: {KEY_FILE})")
    args = p.parse_args()

    urls = read_urls(args)
    if not urls:
        p.error("no urls given")
    if args.no_upload and not args.save_dir:
        p.error("--no-upload needs --save-dir")

    try:
        opener = google_opener(args.cookies)
        ds = folder = None
        if not args.no_upload:
            ds = connect(args.key_file)
            folder = ds.folder_id(args.folder)
    except (Fail, OSError) as e:
        sys.exit(f"error: {e}")
    if args.save_dir:
        os.makedirs(args.save_dir, exist_ok=True)

    failed = 0
    for url in urls:
        try:
            name, data = download(opener, url)
            if args.save_dir:
                Path(args.save_dir, name).write_bytes(data)
            if ds:
                f = ds.upload(folder, name, data, args.overwrite)
                where = f.get("webUrl") or f"id {f.get('id')}"
            else:
                where = str(Path(args.save_dir, name))
            print(f"ok    {name} ({len(data)} bytes) -> {where}")
        except (Fail, OSError) as e:
            failed += 1
            print(f"FAIL  {url}: {e}", file=sys.stderr)

    print(f"{len(urls) - failed}/{len(urls)} done", file=sys.stderr)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
