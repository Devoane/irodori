"""Keep the local server local.

The server listens on 127.0.0.1 only, but any web page open in the browser can
still send requests to it. Requests are refused unless they come from this app:
the Host header must name the loopback address (stops DNS rebinding), and
cross-site requests are rejected by Origin and Sec-Fetch-Site (stops CSRF).
Responses carry a strict Content Security Policy; the app needs no outside
resources at all.
"""
from __future__ import annotations

import base64
import hashlib
import re
from pathlib import Path, PurePath
from urllib.parse import urlsplit

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import PlainTextResponse

LOOPBACK = {"127.0.0.1", "localhost", "::1"}
MAX_UPLOAD = 1 << 30            # 1 GiB per request


def _host(value: str) -> str | None:
    try:
        return urlsplit("//" + value).hostname
    except ValueError:
        return None


def csp(index_html: Path) -> str:
    """The import map is an inline script: allow exactly that one by its hash."""
    m = re.search(r'<script type="importmap">(.*?)</script>', index_html.read_text(encoding="utf-8"), re.S)
    sha = base64.b64encode(hashlib.sha256(m.group(1).encode()).digest()).decode() if m else ""
    return ("default-src 'self'; "
            f"script-src 'self' 'sha256-{sha}'; "
            "style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; connect-src 'self' data: blob:; "
            "font-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")


class LocalOnly(BaseHTTPMiddleware):
    def __init__(self, app, policy: str):
        super().__init__(app)
        self.headers = {
            "Content-Security-Policy": policy,
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
            "Cross-Origin-Opener-Policy": "same-origin",
            "Cross-Origin-Resource-Policy": "same-origin",
            "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
        }

    async def dispatch(self, request, call_next):
        h = request.headers
        if _host(h.get("host", "")) not in LOOPBACK:
            return PlainTextResponse("Bad Host header", status_code=421)
        origin = h.get("origin")
        if origin and origin != "null" and _host(urlsplit(origin).netloc) not in LOOPBACK:
            return PlainTextResponse("Request from another site", status_code=403)
        if h.get("sec-fetch-site") == "cross-site" or (origin == "null" and request.method not in ("GET", "HEAD")):
            return PlainTextResponse("Request from another site", status_code=403)
        length = h.get("content-length") or "0"
        if not length.isdigit() or int(length) > MAX_UPLOAD:
            return PlainTextResponse("The file is too large (limit 1 GiB)", status_code=413)
        response = await call_next(request)
        for k, v in self.headers.items():
            response.headers.setdefault(k, v)
        # pages and scripts revalidate (ETag), so a new index.html never meets an old app.js
        response.headers["Cache-Control"] = "no-store" if request.url.path.startswith("/api/") else "no-cache"
        return response


def safe_name(name: str | None) -> str:
    """A bare file name from an upload: no folders, no '..', no hidden files."""
    base = PurePath((name or "").replace("\\", "/")).name.strip()
    if base in ("", ".", "..") or base.startswith("."):
        raise ValueError(f"Forbidden file name: {name!r}")
    return base[:200]
