"""Tiny webhook server for Instagram DMs (stdlib only).

Point your Meta app's Instagram webhook at https://<your-host>/webhook and
subscribe to the `messages` field.
"""

import hashlib
import hmac
import json
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

log = logging.getLogger("ig_agent.webhook")


def valid_signature(app_secret: str, body: bytes, header: str | None) -> bool:
    if not app_secret:
        return True  # signature check disabled; set IG_APP_SECRET in production
    if not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(app_secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.removeprefix("sha256="))


def make_handler(verify_token: str, app_secret: str, on_event):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, code: int, body: str = "") -> None:
            self.send_response(code)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(body.encode())

        def do_GET(self):  # subscription verification handshake
            query = parse_qs(urlparse(self.path).query)
            if (query.get("hub.mode", [""])[0] == "subscribe"
                    and verify_token
                    and query.get("hub.verify_token", [""])[0] == verify_token):
                self._send(200, query.get("hub.challenge", [""])[0])
            else:
                self._send(403, "forbidden")

        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            if not valid_signature(app_secret, body, self.headers.get("X-Hub-Signature-256")):
                self._send(401, "bad signature")
                return
            self._send(200, "ok")  # ack fast; Meta retries slow responses
            try:
                on_event(json.loads(body))
            except Exception:
                log.exception("failed to handle webhook event")

        def log_message(self, fmt, *args):
            log.info(fmt, *args)

    return Handler


def serve(host: str, port: int, verify_token: str, app_secret: str, on_event) -> None:
    server = ThreadingHTTPServer((host, port), make_handler(verify_token, app_secret, on_event))
    log.info("listening on %s:%s", host, port)
    server.serve_forever()
