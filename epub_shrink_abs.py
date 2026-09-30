"""Audiobookshelf client for epub-shrink.

This is an independent client written against the server's HTTP API. It is not
copied from, linked to, or a derivative of Audiobookshelf's source, and it does
not import any of it. The wire format (endpoint shapes, field names, and two
server-specific quirks noted below) was established by reading the public
Audiobookshelf source and by observing a live server.

Implements exactly the flow the server exposes for adding a book:

    1. authenticate  ->  POST /login          (username/password)
       or present an API key as `Authorization: Bearer <key>`
    2. find library  ->  GET  /api/libraries   (mediaType "book", its folders)
    3. upload        ->  POST /api/upload      (multipart; lands in the folder
                          as <folder>/<author>/<series>/<title>/<filename>)
    4. scan          ->  POST /api/libraries/:id/scan   (background)
    5. find the item ->  GET  /api/libraries/:id/search?q=<title>
    6. set metadata  ->  PATCH /api/items/:id/media     (title, authors,
                          narrators, genres, publisher, description, language,
                          dates, ISBN/ASIN, and series with `sequence` as a
                          *string*; `tags` goes at the top level of the body)

Endpoint shapes were verified against the server source (advplyr/audiobookshelf
@ 2.37.1): see the packaging/README feature notes. Only the standard library is
used so the module travels inside the AppImage without extra wheels.

The upload itself is fire-and-forget on the server — it returns 200 immediately
and does NOT trigger a scan. If the library's folder watcher is enabled the
book may appear on its own; otherwise the explicit scan call is required, and
an admin/root account is needed for it. This is reported honestly rather than
guessed.
"""

import json
import os
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request


class AbsError(Exception):
    """An Audiobookshelf request failed in a way the caller should surface."""


# The field names the server accepts for a book, taken from `updateFromRequest`
# in server/models/Book.js of advplyr/audiobookshelf 2.37.1. Two details are
# easy to get wrong and are load-bearing here:
#
#   * `series[i].sequence` must be a JSON *string*. A number is accepted by the
#     request parser and then quietly discarded, so the book lands in the series
#     with no position in it.
#   * `tags` sits at the *top level* of the body, not inside `metadata`.
#     Everything else below belongs inside `metadata`.
_STRING_METADATA_FIELDS = {
    "title": "title",
    "subtitle": "subtitle",
    "published_year": "publishedYear",
    "published_date": "publishedDate",
    "publisher": "publisher",
    "description": "description",
    "isbn": "isbn",
    "asin": "asin",
    "language": "language",
}


def to_metadata_payload(meta: dict) -> dict:
    """Translate a read_book_meta() dict into a PATCH /media request body.

    Absent values are omitted rather than sent as null: the server treats a null
    as "clear this field", and a missing EPUB attribute should not be allowed to
    wipe metadata that Audiobookshelf matched from elsewhere.
    """
    metadata: dict = {}

    for source, target in _STRING_METADATA_FIELDS.items():
        value = meta.get(source)
        if isinstance(value, str) and value.strip():
            metadata[target] = value.strip()

    authors = [str(a).strip() for a in (meta.get("authors") or [])
               if str(a).strip()]
    if not authors and meta.get("author"):
        authors = [str(meta["author"]).strip()]
    if authors:
        metadata["authors"] = [{"name": name} for name in authors]

    series = str(meta.get("series") or "").strip()
    if series:
        entry: dict = {"name": series}
        index = str(meta.get("series_index") or "").strip()
        if index:
            entry["sequence"] = index          # string, on purpose
        metadata["series"] = [entry]

    for source in ("narrators", "genres"):
        values = [str(v).strip() for v in (meta.get(source) or [])
                  if str(v).strip()]
        if values:
            metadata[source] = values

    payload: dict = {"metadata": metadata}
    tags = [str(t).strip() for t in (meta.get("tags") or []) if str(t).strip()]
    if tags:
        payload["tags"] = tags                # top level, not under metadata
    return payload


class AbsClient:
    def __init__(self, url: str, api_key: str = "",
                 username: str = "", password: str = "",
                 insecure_tls: bool = False, timeout: float = 60.0):
        self.base = (url or "").rstrip("/")
        if not self.base:
            raise AbsError("audiobookshelf URL is not set")
        self.api_key = api_key or ""
        self.username = username or ""
        self.password = password or ""
        self.timeout = timeout
        ctx = ssl.create_default_context() if not insecure_tls \
            else ssl._create_unverified_context()  # noqa: S323 - explicit user opt-in
        self._ctx = ctx
        self._token: str | None = self.api_key or None

    # ------------------------------------------------------------------ http

    def _request(self, method: str, path: str, body: bytes | None = None,
                 headers: dict | None = None) -> tuple[int, dict | bytes]:
        url = f"{self.base}{path}"
        req = urllib.request.Request(url, data=body, method=method)
        if headers:
            for key, value in headers.items():
                req.add_header(key, value)
        if self._token:
            req.add_header("Authorization", f"Bearer {self._token}")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout,
                                        context=self._ctx) as resp:
                raw = resp.read()
                ctype = resp.headers.get("Content-Type", "")
                if "json" in ctype:
                    return resp.status, json.loads(raw or b"{}")
                return resp.status, raw
        except urllib.error.HTTPError as exc:
            detail = b""
            try:
                detail = exc.read()
                parsed = json.loads(detail or b"{}")
                detail = parsed.get("error") or parsed
            except Exception:  # noqa: BLE001 - non-JSON error body
                pass
            raise AbsError(f"{exc.code} {exc.reason}: {detail}".strip()) from None
        except urllib.error.URLError as exc:
            raise AbsError(f"cannot reach Audiobookshelf at {self.base}: {exc.reason}") from None

    def _ensure_auth(self) -> None:
        if self._token:
            return
        if not self.username:
            raise AbsError("no API key and no username configured; set one in "
                           "Settings -> Audiobookshelf")
        body = json.dumps({"username": self.username,
                           "password": self.password}).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        status, payload = self._request("POST", "/login", body, headers)
        try:
            self._token = payload["user"]["accessToken"]
        except (KeyError, TypeError):
            raise AbsError(f"login response had no accessToken (HTTP {status})") from None

    def _guard(self, payload: dict, *keys: str) -> dict:
        for key in keys:
            if key not in payload:
                raise AbsError(f"unexpected response shape: missing {key!r}")
        return payload

    # ---------------------------------------------------------------- auth

    def validate(self) -> str:
        """Verify the configured credentials and return the acting username."""
        self._ensure_auth()
        try:
            _status, payload = self._request("POST", "/api/authorize")
        except AbsError:
            self._token = None
            raise
        # _guard returns the whole body; the name lives inside the "user" object.
        user = self._guard(payload, "user").get("user") or {}
        name = user.get("username") or user.get("email") or ""
        if not name and self._token:
            # Some versions return only the user id from authorize; the token
            # itself is a JWT carrying the username claim.
            name = self._jwt_username(self._token)
        return name or ("api-key" if self.api_key else "unknown")

    @staticmethod
    def _jwt_username(token: str) -> str:
        """Best-effort `username` claim from a JWT's payload segment."""
        try:
            payload = token.split(".")[1]
            payload += "=" * (-len(payload) % 4)
            import base64
            claims = json.loads(base64.urlsafe_b64decode(payload))
            return claims.get("username") or claims.get("email") or ""
        except Exception:  # noqa: BLE001 - a JWT we cannot decode is not fatal
            return ""

    # ------------------------------------------------------------- libraries

    def fetch_libraries(self) -> list[dict]:
        """Return book libraries: {id, name, folders: [{id, fullPath}]}.

        A plain list rather than a generator, because callers pick a library by
        name and iterate the whole list while doing so.
        """
        self._ensure_auth()
        _status, payload = self._request("GET", "/api/libraries")
        return [
            {"id": lib["id"],
             "name": lib.get("name", "unnamed"),
             "folders": lib.get("folders", [])}
            for lib in self._guard(payload, "libraries")["libraries"]
            if lib.get("mediaType") == "book"
        ]

    # --------------------------------------------------------------- upload

    @staticmethod
    def _multipart(fields: dict[str, str],
                   files: list[tuple[str, str, str, bytes]],
                   boundary: str) -> bytes:
        """Build a multipart/form-data body. Field names are taken verbatim."""
        out = bytearray()
        for key, value in fields.items():
            out += (f"--{boundary}\r\n"
                    f'Content-Disposition: form-data; name="{key}"\r\n\r\n'
                    f"{value}\r\n").encode("utf-8")
        for field, filename, content_type, data in files:
            out += (f"--{boundary}\r\n"
                    f'Content-Disposition: form-data; name="{field}"; '
                    f'filename="{filename}"\r\n'
                    f"Content-Type: {content_type}\r\n\r\n").encode("utf-8")
            out += data
            out += b"\r\n"
        out += f"--{boundary}--\r\n".encode("utf-8")
        return bytes(out)

    def upload(self, library_id: str, folder_id: str, title: str,
               author: str, series: str, file_bytes: bytes,
               filename: str) -> None:
        """Upload one file into a library folder. Returns when the server
        accepted it (which the server does immediately, without scanning)."""
        self._ensure_auth()
        if not (filename or "").strip():
            # Without a filename the server stores the file under its internal
            # upload id, which is unrecoverable once it has landed.
            raise AbsError("refusing to upload with no filename")
        boundary = f"epub-shrink-{os.getpid()}-{int(time.time() * 1000)}"
        fields = {"library": library_id, "folder": folder_id,
                  "title": title, "author": author, "series": series}
        files = [("0", filename, "application/epub+zip", file_bytes)]
        body = self._multipart(fields, files, boundary)
        headers = {"Content-Type": f"multipart/form-data; boundary={boundary}"}
        self._request("POST", "/api/upload", body, headers)

    def request_scan(self, library_id: str) -> bool:
        """Ask the server to (re)scan a library. Fire-and-forget on the server.

        Returns True when the scan was accepted, False when the account lacks
        permission (the only realistic failure) — the folder watcher may still
        pick the file up on its own.
        """
        self._ensure_auth()
        try:
            self._request("POST", f"/api/libraries/{library_id}/scan")
            return True
        except AbsError as exc:
            if "403" in str(exc) or "Forbidden" in str(exc):
                return False
            raise

    # -------------------------------------------------------------- metadata

    def search_items(self, library_id: str, query: str,
                     limit: int = 10) -> list[dict]:
        """Find library items matching `query` by title/subtitle/isbn."""
        self._ensure_auth()
        q = urllib.parse.quote(query)
        _status, payload = self._request(
            "GET", f"/api/libraries/{library_id}/search?q={q}&limit={limit}")
        books = self._guard(payload, "book", "narrators")["book"]
        items = []
        for wrapper in books:
            item = wrapper.get("libraryItem") or {}
            if item.get("id"):
                items.append(item)
        return items

    def update_media(self, item_id: str, meta: dict) -> dict:
        """Set an item's metadata from a read_book_meta() dict.

        `meta` uses the engine's own field names and is translated to the
        server's here. Only fields that are actually present are sent, so a
        thin EPUB does not blank out metadata Audiobookshelf already had.
        """
        if not item_id:
            raise AbsError("no Audiobookshelf item id to update")
        body = json.dumps(to_metadata_payload(meta)).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        _status, payload = self._request(
            "PATCH", f"/api/items/{item_id}/media", body, headers)
        return self._guard(payload, "updated", "libraryItem")

    # ------------------------------------------------------------ high level

    @staticmethod
    def _identifies(candidate: dict, needles: list[str]) -> bool:
        """Whether a library item refers to the book we just uploaded.

        Matched against several things at once because the server does not
        present the item under the name we sent it: the scan re-reads the file
        and may title it from the book's own metadata instead, while the folder
        path keeps the author/series/title we supplied and the stored filename
        keeps our derived name.
        """
        media = candidate.get("media") or {}
        haystacks = [
            candidate.get("path") or "",
            str((media.get("metadata") or {}).get("title") or ""),
            str(((media.get("ebookFile") or {}).get("metadata") or {})
                .get("filename") or ""),
        ]
        blob = " \u0001 ".join(haystacks).lower()
        return any(needle in blob for needle in needles if needle)

    def _find_uploaded(self, library_id: str, meta: dict, title: str,
                       filename: str,
                       search_terms: list[str] | None = None) -> dict | None:
        """Locate the item the server created for an upload.

        `search_terms` carries the names the book is actually indexed under, as
        opposed to the title we asked for. Audiobookshelf's search only matches
        title, subtitle, ASIN and ISBN — not author, series, folder path or
        filename — and the item's title comes from the server parsing the EPUB,
        not from our request. So when the user corrects a title in the upload
        dialog, the title we sent is precisely the one thing the search will not
        find; the title the file itself declares is passed in here so the item
        can still be located and given the corrected metadata.

        Falls back to the ISBN, which the search does match by substring, before
        giving up and reporting the upload as unindexed.
        """
        stem = os.path.splitext(filename)[0]
        terms = [title, *(search_terms or ())]
        isbn = str(meta.get("isbn") or "").strip()
        if isbn:
            terms.append(isbn)

        seen: set[str] = set()
        for term in terms:
            if not term or not term.strip():
                continue
            try:
                matches = self.search_items(library_id, term, limit=8)
            except AbsError:
                continue
            # The stored filename is the one identifier the server echoes back
            # verbatim, so it is the authoritative check that a hit is our book
            # and not a different one that merely shares a word.
            for candidate in matches:
                item_id = candidate.get("id")
                if not item_id or item_id in seen:
                    continue
                seen.add(item_id)
                if self._identifies(candidate, [stem, *[t.lower() for t in terms]]):
                    return candidate
        return None

    def publish(self, library_id: str, folder_id: str, meta: dict,
                file_bytes: bytes, filename: str,
                scan_after: bool = True, poll_seconds: float = 60.0,
                progress=None,
                search_terms: list[str] | None = None) -> dict:
        """Upload, optionally scan, find the item and set its metadata.

        `meta` is a read_book_meta() dict; `filename` is the name the file is
        uploaded under, normally derived from that same metadata. The server
        stores the file under `filename` and files it into
        <author>/<series>/<title>/ itself, so both have to agree with the
        metadata that is then PATCHed on afterwards.

        `search_terms` is for callers that have edited the title: see
        _find_uploaded.

        Returns a status dict for the GUI to display. Never raises for a slow
        scan: if the item cannot be found within `poll_seconds` the file was
        still uploaded and the caller is told to check back manually.
        """
        def say(message: str) -> None:
            if progress:
                progress(message)

        title = str(meta.get("title") or "").strip()
        if not title:
            raise AbsError("cannot upload without a title: the book's metadata "
                           "has none and no fallback was supplied")
        author = str(meta.get("author") or "").strip()
        series = str(meta.get("series") or "").strip()

        say(f"uploading {filename} …")
        self.upload(library_id, folder_id, title, author, series,
                    file_bytes, filename)

        scanned = False
        if scan_after:
            scanned = self.request_scan(library_id)
            say("scan accepted" if scanned
                else "scan needs an admin account; relying on the folder watcher")

        poll_until = time.monotonic() + poll_seconds
        delay = 1.0
        item = None
        while time.monotonic() < poll_until:
            time.sleep(delay)
            delay = min(delay * 1.5, 5.0)
            item = self._find_uploaded(library_id, meta, title, filename,
                                       search_terms)
            if item:
                break

        if not item:
            return {"status": "uploaded_unindexed",
                    "message": f"{filename} uploaded to Audiobookshelf but did "
                               f"not appear after scanning; check the library "
                               f"manually (watcher may still be indexing)."}

        item_id = item["id"]
        say("setting metadata …")
        self.update_media(item_id, meta)
        return {"status": "ok", "item_id": item_id,
                "message": f"uploaded and indexed: {title} "
                           f"{('· ' + author) if author else ''}"
                           f"{(' · ' + series) if series else ''}"}


def build_client(settings: dict) -> AbsClient:
    """Construct a client from the settings dict's 'abs' section."""
    abs_conf = settings.get("abs") or {}
    return AbsClient(
        url=abs_conf.get("url", ""),
        api_key=abs_conf.get("api_key", ""),
        username=abs_conf.get("username", ""),
        password=abs_conf.get("password", ""),
        insecure_tls=bool(abs_conf.get("insecure_tls", False)),
    )