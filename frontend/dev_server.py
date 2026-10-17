"""Local dev server for the console.

Serves this folder on http://localhost:8081 and forwards API calls to the team's backend,
so the browser sees one origin and the backend needs no CORS change.

    python3 dev_server.py                         # backend at http://127.0.0.1:8080
    QG_BACKEND=http://127.0.0.1:9000 python3 dev_server.py
    QG_PORT=8082 python3 dev_server.py

Standard library only. Binds to 127.0.0.1, so nothing is reachable from the network.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BACKEND = os.environ.get("QG_BACKEND", "http://127.0.0.1:8080").rstrip("/")
PORT = int(os.environ.get("QG_PORT", "8081"))
# The pipeline may take up to 75 s and the demo's database run up to 120 s.
PROXY_TIMEOUT = float(os.environ.get("QG_PROXY_TIMEOUT", "240"))
PROXIED = ("/nl-to-sparql", "/demo/", "/agents", "/health")


class Handler(SimpleHTTPRequestHandler):
    def _proxied(self) -> bool:
        return self.path.startswith(PROXIED)

    def _hidden(self) -> bool:
        parts = self.path.split("?", 1)[0].split("/")
        return any(part.startswith(".") for part in parts if part) or self.path.endswith(".py")

    def do_GET(self):
        if self._proxied():
            return self._forward("GET")
        if self._hidden():
            return self.send_error(404)
        if self.path in ("/", ""):
            self.send_response(302)
            self.send_header("Location", "/cli/")
            return self.end_headers()
        return super().do_GET()

    def do_POST(self):
        if self._proxied():
            return self._forward("POST")
        return self.send_error(405)

    def _forward(self, method: str) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else None
        request = urllib.request.Request(BACKEND + self.path, data=body, method=method)
        if self.headers.get("Content-Type"):
            request.add_header("Content-Type", self.headers["Content-Type"])
        try:
            with urllib.request.urlopen(request, timeout=PROXY_TIMEOUT) as response:
                self._reply(response.status, response.read(), response.headers.get("Content-Type"))
        except urllib.error.HTTPError as error:  # the backend answered with an error status
            self._reply(error.code, error.read(), error.headers.get("Content-Type"))
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            reason = getattr(error, "reason", error)
            payload = {"error": f"The backend at {BACKEND} did not answer ({reason}). Is it running?"}
            self._reply(502, json.dumps(payload).encode(), "application/json")

    def _reply(self, status: int, body: bytes, content_type: str | None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type or "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def end_headers(self):
        if not self._proxied():
            self.send_header("Cache-Control", "no-store")  # always serve the latest frontend files
        super().end_headers()


def main() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", PORT), partial(Handler, directory=str(ROOT)))
    print(f"Console:  http://localhost:{PORT}/cli/")
    print(f"Backend:  {BACKEND}  (forwarding {', '.join(PROXIED)})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
