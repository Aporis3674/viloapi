"""Private webhook receiver for the existing bot; no Telegram getUpdates polling."""
import hashlib
import ssl
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import threading

from bot import App, Telegram
from core import Store


def build_app():
    root = Path(os.environ.get('VILO_AGENT_DATA', '/var/lib/vilo-agent'))
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if root.stat().st_mode & 0o077:
        raise PermissionError('Agent data directory must be private')
    admins = {int(s) for s in os.environ['VILO_AGENT_ADMIN_IDS'].split(',') if s.strip()}
    if not admins:
        raise ValueError('At least one administrator required')
    app = App(Telegram(os.environ['VILO_AGENT_BOT_TOKEN']), Store(root / 'agent.sqlite', root / 'master.key'), admins)
    secret = os.environ['VILO_AGENT_FORWARD_SECRET']
    if len(secret) < 48:
        raise ValueError('Forward secret too short')
    return app, secret.encode()


class Handler(BaseHTTPRequestHandler):
    app = None
    secret = None
    lock = threading.Lock()

    def log_message(self, *_):
        pass

    def do_POST(self):
        if self.path != '/forward':
            self.send_error(404)
            return
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if not 0 < size <= 65536:
                raise ValueError('Invalid payload size')
            body = self.rfile.read(size)
            signature = self.headers.get('X-Vilo-Signature', '')
            expected = hmac.new(self.secret, body, hashlib.sha256).hexdigest()
            if not hmac.compare_digest(signature, expected):
                self.send_error(403)
                return
            update = json.loads(body)
            if not isinstance(update, dict):
                raise ValueError('Invalid update')
            with self.lock:
                self.app.handle(update)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"ok":true}')
        except (ValueError, TypeError):
            self.send_error(400)
        except Exception:
            self.send_error(503)


def main():
    Handler.app, Handler.secret = build_app()
    server = ThreadingHTTPServer(('0.0.0.0', 8765), Handler)
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.minimum_version = ssl.TLSVersion.TLSv1_2
    tls.load_cert_chain(os.environ['VILO_AGENT_TLS_CERT'], os.environ['VILO_AGENT_TLS_KEY'])
    server.socket = tls.wrap_socket(server.socket, server_side=True)
    server.serve_forever()


if __name__ == '__main__':
    main()
