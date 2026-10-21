#!/usr/bin/env python3
"""Vilo node API. stdlib only; loopback HTTP behind nginx TLS."""
import argparse
import base64
import copy
import datetime
import hashlib
import hmac
import http.server
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import subprocess
import threading
import time
import urllib.parse
import uuid

NAME = re.compile(r"[a-z0-9_-]{2,48}\Z")
LOCK = threading.RLock()


def atomic_json(path, value):
    path = Path(path)
    temp = path.with_suffix('.new')
    with open(temp, 'w', encoding='utf-8') as out:
        os.chmod(temp, 0o600)
        json.dump(value, out, ensure_ascii=True)
        out.flush()
        os.fsync(out.fileno())
    os.replace(temp, path)


def validate_client(c):
    if not isinstance(c, dict) or not isinstance(c.get('name'), str) or not NAME.fullmatch(c['name']):
        raise ValueError('INVALID_CLIENT')
    if str(uuid.UUID(c.get('uuid', ''))) != c['uuid']:
        raise ValueError('INVALID_UUID')
    for key, minimum, maximum in [('trafficLimitGb', 0, 100000), ('expiryDays', 1, 36500),
                                   ('expiryTimestamp', 1, 2**53 - 1), ('userLimit', 1, 100), ('isActive', 0, 1)]:
        if type(c.get(key)) is not int or not minimum <= c[key] <= maximum:
            raise ValueError('INVALID_CLIENT_FIELD')
    epoch = c.get('trafficEpoch', '')
    if not isinstance(epoch, str) or len(epoch) > 80 or not re.fullmatch(r'[a-zA-Z0-9_-]*', epoch):
        raise ValueError('INVALID_TRAFFIC_EPOCH')
    return {key: c[key] for key in ['name', 'uuid', 'trafficLimitGb', 'expiryDays', 'expiryTimestamp', 'userLimit', 'isActive']} | {'trafficEpoch': epoch}


def connection_links(client, cfg):
    c = cfg['connection']
    query = urllib.parse.urlencode({'security': 'reality', 'encryption': 'none', 'pbk': c['pbk'],
        'fp': 'chrome', 'type': 'tcp', 'flow': 'xtls-rprx-vision', 'sni': c['sni'], 'sid': c['sid']})
    reality = f"vless://{client['uuid']}@{cfg['ip']}:443?{query}#{client['name']}-Reality"
    ws = f"vless://{client['uuid']}@{cfg['ip']}:80?security=none&encryption=none&type=ws&path=%2Fvilo-ws#{client['name']}-WS80"
    return reality, ws


class Node:
    def __init__(self, root, runner=subprocess.run):
        self.root = Path(root)
        self.cfg = json.loads((self.root / 'node.json').read_text())
        self.runner = runner
        self.db = sqlite3.connect(self.root / 'state.sqlite', check_same_thread=False)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('CREATE TABLE IF NOT EXISTS clients (name TEXT PRIMARY KEY, data TEXT NOT NULL)')
        self.db.execute('CREATE TABLE IF NOT EXISTS traffic (name TEXT PRIMARY KEY, up INTEGER NOT NULL DEFAULT 0, down INTEGER NOT NULL DEFAULT 0, last_up INTEGER NOT NULL DEFAULT 0, last_down INTEGER NOT NULL DEFAULT 0, seen REAL NOT NULL DEFAULT 0)')
        self.db.execute('CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
        self.db.commit()
        self.observed_at = 0
        self.xray_ok = False

    def command(self, args):
        return self.runner(args, check=True, capture_output=True, text=True, timeout=15).stdout

    def clients(self):
        return [json.loads(row[0]) for row in self.db.execute('SELECT data FROM clients ORDER BY name')]

    def traffic(self):
        return {name: {'uplink': up, 'downlink': down, 'lastSeen': seen}
                for name, up, down, seen in self.db.execute('SELECT name,up,down,seen FROM traffic')}

    def allowed(self, c, traffic=None):
        used = (traffic if traffic is not None else self.traffic()).get(c['name'], {})
        cap = 200 * 1024**2 if c['name'].startswith('test_') else c['trafficLimitGb'] * 1024**3
        return c['isActive'] == 1 and c['expiryTimestamp'] > time.time() and (not cap or used.get('uplink', 0) + used.get('downlink', 0) < cap)

    def sample(self):
        output = self.command(['/usr/local/bin/xray', 'api', 'statsquery', '--server=127.0.0.1:10085', '-pattern', 'user>>>'])
        data = json.loads(output)
        current = {}
        for stat in data.get('stat', []):
            match = re.fullmatch(r'user>>>([a-z0-9_-]+)>>>traffic>>>(uplink|downlink)', stat.get('name', ''))
            if match:
                value = int(stat.get('value', 0))
                if value < 0:
                    raise ValueError('INVALID_STATS')
                current.setdefault(match[1], {})[match[2]] = value
        now = time.time()
        for name, values in current.items():
            if not self.db.execute('SELECT 1 FROM clients WHERE name=?', (name,)).fetchone():
                continue
            self.db.execute('INSERT OR IGNORE INTO traffic(name) VALUES (?)', (name,))
            up, down, last_up, last_down, seen = self.db.execute('SELECT up,down,last_up,last_down,seen FROM traffic WHERE name=?', (name,)).fetchone()
            new_up, new_down = values.get('uplink', last_up), values.get('downlink', last_down)
            du = new_up - last_up if new_up >= last_up else new_up
            dd = new_down - last_down if new_down >= last_down else new_down
            self.db.execute('UPDATE traffic SET up=?,down=?,last_up=?,last_down=?,seen=? WHERE name=?',
                            (up + du, down + dd, new_up, new_down, now if du or dd else seen, name))
        self.db.commit()
        self.observed_at, self.xray_ok = now, True

    def apply(self):
        config_path = self.root / 'xray.json'
        old = json.loads(config_path.read_text())
        config = copy.deepcopy(old)
        traffic = self.traffic()
        active = [c for c in self.clients() if self.allowed(c, traffic)]
        for inbound in config['inbounds']:
            if inbound.get('tag') in ('reality', 'ws'):
                inbound['settings']['clients'] = [dict(id=c['uuid'], email=c['name'], level=0,
                    **({'flow': 'xtls-rprx-vision'} if inbound['tag'] == 'reality' else {})) for c in active]
        if config == old:
            return
        candidate = self.root / 'candidate.json'
        atomic_json(candidate, config)
        self.command(['/usr/local/bin/xray', 'run', '-test', '-config', str(candidate)])
        atomic_json(self.root / 'xray.backup.json', old)
        os.replace(candidate, config_path)
        try:
            self.command(['systemctl', 'restart', 'vilo-xray'])
            self.command(['systemctl', 'is-active', '--quiet', 'vilo-xray'])
        except Exception:
            atomic_json(config_path, old)
            self.command(['systemctl', 'restart', 'vilo-xray'])
            raise
        # Counters restart with Xray. Persist the baseline before the next sample.
        self.db.execute('UPDATE traffic SET last_up=0,last_down=0')
        self.db.commit()

    def put(self, raw):
        c = validate_client(raw)
        with LOCK:
            self.sample()  # Preserve counters before changing/restarting Xray; fail closed on stats failure.
            previous = self.db.execute('SELECT data FROM clients WHERE name=?', (c['name'],)).fetchone()
            previous_traffic = self.db.execute('SELECT * FROM traffic WHERE name=?', (c['name'],)).fetchone()
            old = json.loads(previous[0]) if previous else None
            if old and old['uuid'] == c['uuid'] and old.get('trafficEpoch', '') == c['trafficEpoch']:
                pass
            else:
                # Keep live counter baselines when resetting quota without restarting Xray.
                self.db.execute('UPDATE traffic SET up=0,down=0,seen=0 WHERE name=?', (c['name'],))
            self.db.execute('INSERT INTO clients VALUES (?,?) ON CONFLICT(name) DO UPDATE SET data=excluded.data', (c['name'], json.dumps(c)))
            self.db.commit()
            try:
                self.apply()
            except Exception:
                if previous:
                    self.db.execute('UPDATE clients SET data=? WHERE name=?', (previous[0], c['name']))
                else:
                    self.db.execute('DELETE FROM clients WHERE name=?', (c['name'],))
                self.db.execute('DELETE FROM traffic WHERE name=?', (c['name'],))
                if previous_traffic:
                    self.db.execute('INSERT INTO traffic VALUES (?,?,?,?,?,?)', previous_traffic)
                self.db.commit()
                raise
        return {'ok': True}

    def remove(self, name):
        if not isinstance(name, str) or not NAME.fullmatch(name):
            raise ValueError('INVALID_NAME')
        with LOCK:
            self.sample()
            previous = self.db.execute('SELECT data FROM clients WHERE name=?', (name,)).fetchone()
            self.db.execute('DELETE FROM clients WHERE name=?', (name,))
            self.db.commit()
            try:
                self.apply()
            except Exception:
                if previous:
                    self.db.execute('INSERT OR REPLACE INTO clients VALUES (?,?)', (name, previous[0]))
                    self.db.commit()
                raise
            self.db.execute('DELETE FROM traffic WHERE name=?', (name,))
            self.db.commit()
        return {'ok': True}

    def status(self):
        with LOCK:
            traffic = self.traffic()
            radar = {name: {'last_handshake': datetime.datetime.fromtimestamp(v['lastSeen'], datetime.timezone.utc).isoformat(),
                            'protocol': 'VLESS', 'isp': 'Unknown'} for name, v in traffic.items() if v['lastSeen']}
            return {'ok': True, 'ip': self.cfg['ip'], 'clients': self.clients(), 'traffic': traffic,
                    'radar': radar, 'observedAt': self.observed_at, 'xray': self.xray_ok}

    def monitor(self):
        while True:
            try:
                with LOCK:
                    self.sample()
                    self.apply()
            except Exception as exc:
                self.xray_ok = False
                print('Monitor failure:', type(exc).__name__, flush=True)
            time.sleep(10)


class Handler(http.server.BaseHTTPRequestHandler):
    node = None

    def log_message(self, *_):
        pass  # URLs contain subscription credentials; never write access logs.

    def send(self, status, body, content_type='application/json'):
        content = json.dumps(body).encode() if content_type == 'application/json' else body.encode()
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(content)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(content)

    def do_POST(self):
        expected = 'Bearer ' + self.node.cfg['syncSecret']
        if not hmac.compare_digest(self.headers.get('Authorization', '').encode(), expected.encode()):
            self.send(403, {'ok': False})
            return
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if not 0 < size <= 65536:
                raise ValueError('INVALID_SIZE')
            self.connection.settimeout(10)
            body = json.loads(self.rfile.read(size))
            if not isinstance(body, dict):
                raise ValueError('INVALID_BODY')
            if self.path == '/v1/status':
                result = self.node.status()
            elif self.path == '/v1/client':
                result = self.node.put(body.get('client'))
            elif self.path == '/v1/delete':
                result = self.node.remove(body.get('name'))
            else:
                self.send(404, {'ok': False})
                return
            self.send(200, result)
        except (ValueError, TypeError, KeyError):
            self.send(400, {'ok': False, 'error': 'INVALID_INPUT'})
        except Exception as exc:
            print('API failure:', type(exc).__name__, flush=True)
            self.send(503, {'ok': False, 'error': 'APPLY_FAILED'})

    def do_GET(self):
        url = urllib.parse.urlsplit(self.path)
        query = urllib.parse.parse_qs(url.query)
        if url.path != '/sub':
            self.send(404, {'ok': False})
            return
        with LOCK:
            name, credential = query.get('name', [''])[0], query.get('uuid', [''])[0]
            row = self.node.db.execute('SELECT data FROM clients WHERE name=?', (name,)).fetchone()
            client = json.loads(row[0]) if row else None
            if not client or not hmac.compare_digest(client['uuid'].encode(), credential.encode()) or not self.node.allowed(client):
                self.send(404, {'ok': False})
                return
            reality, ws = connection_links(client, self.node.cfg)
            fmt = query.get('format', [''])[0]
            c = self.node.cfg['connection']
            if fmt == 'singbox':
                out = {'outbounds': [
                    {'type': 'vless', 'tag': name + '-Reality', 'server': self.node.cfg['ip'], 'server_port': 443, 'uuid': client['uuid'],
                     'flow': 'xtls-rprx-vision', 'tls': {'enabled': True, 'server_name': c['sni'], 'utls': {'enabled': True, 'fingerprint': 'chrome'},
                     'reality': {'enabled': True, 'public_key': c['pbk'], 'short_id': c['sid']}}},
                    {'type': 'vless', 'tag': name + '-WS80', 'server': self.node.cfg['ip'], 'server_port': 80, 'uuid': client['uuid'],
                     'transport': {'type': 'ws', 'path': c['wsPath']}}]}
                self.send(200, out)
            elif fmt == 'clash':
                # JSON is valid YAML and safely quotes every dynamic value.
                out = {'proxies': [
                    {'name': name + '-Reality', 'type': 'vless', 'server': self.node.cfg['ip'], 'port': 443, 'uuid': client['uuid'],
                     'network': 'tcp', 'tls': True, 'flow': 'xtls-rprx-vision', 'servername': c['sni'], 'client-fingerprint': 'chrome',
                     'reality-opts': {'public-key': c['pbk'], 'short-id': c['sid']}},
                    {'name': name + '-WS80', 'type': 'vless', 'server': self.node.cfg['ip'], 'port': 80, 'uuid': client['uuid'],
                     'network': 'ws', 'tls': False, 'ws-opts': {'path': c['wsPath']}}]}
                self.send(200, json.dumps(out), 'text/yaml; charset=utf-8')
            elif not fmt:
                self.send(200, base64.b64encode(f'{reality}\n{ws}'.encode()).decode(), 'text/plain; charset=utf-8')
            else:
                self.send(400, {'ok': False, 'error': 'UNSUPPORTED_FORMAT'})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', default='/etc/vilo-node')
    args = parser.parse_args()
    node = Node(args.root)
    Handler.node = node
    threading.Thread(target=node.monitor, daemon=True).start()
    # ponytail: serialized API requests suit small fleets; use a bounded worker pool if throughput requires it.
    http.server.HTTPServer(('127.0.0.1', 10086), Handler).serve_forever()


if __name__ == '__main__':
    main()
