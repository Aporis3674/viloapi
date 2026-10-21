"""Approval-gated Vilo operator. No model-generated shell execution."""
import ipaddress
import json
import os
from pathlib import Path
import re
import sqlite3
import time
import uuid

from cryptography.fernet import Fernet
import paramiko

ROOT = '/etc/vilo-node'
IDENT = re.compile(r'[A-Za-z0-9_-]{1,64}\Z')
NAME = re.compile(r'[a-z0-9_-]{2,48}\Z')


def validate_host(host, port, username, fingerprint):
    ip = ipaddress.ip_address(host)
    if not ip.is_global or not 1 <= port <= 65535 or not IDENT.fullmatch(username):
        raise ValueError('Invalid public IP, SSH port or username')
    if not re.fullmatch(r'SHA256:[A-Za-z0-9+/]{43}', fingerprint):
        raise ValueError('Obtain the SSH host key fingerprint from the VPS provider')
    return str(ip)


def validate_name(value):
    if not isinstance(value, str) or not NAME.fullmatch(value):
        raise ValueError('Invalid subscription name')
    return value


class Store:
    def __init__(self, db_path, key_path):
        key_path = Path(key_path)
        if not key_path.exists():
            key_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'wb') as out:
                out.write(Fernet.generate_key())
        if os.name != 'nt' and key_path.stat().st_mode & 0o077:
            raise PermissionError('Master key must not be accessible to other users')
        self.cipher = Fernet(key_path.read_bytes())
        self.db = sqlite3.connect(db_path)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS servers (
                id INTEGER PRIMARY KEY, host TEXT NOT NULL UNIQUE, port INTEGER NOT NULL,
                username TEXT NOT NULL, fingerprint TEXT NOT NULL, credential BLOB NOT NULL
            );
            CREATE TABLE IF NOT EXISTS proposals (
                id TEXT PRIMARY KEY, admin_id INTEGER NOT NULL, server_id INTEGER NOT NULL,
                name TEXT NOT NULL, action TEXT NOT NULL, value TEXT NOT NULL,
                previous TEXT, expires INTEGER NOT NULL, state TEXT NOT NULL DEFAULT 'pending'
            );
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY, timestamp INTEGER NOT NULL, admin_id INTEGER NOT NULL,
                server_id INTEGER, action TEXT NOT NULL, outcome TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS memory (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        ''')
        self.db.commit()

    def add_server(self, host, port, username, fingerprint, pem):
        host = validate_host(host, port, username, fingerprint)
        if 'PRIVATE KEY-----' not in pem or len(pem) > 20000:
            raise ValueError('Invalid PEM private key')
        # Parsing before storage rejects malformed credentials. No credentials in logs or replies.
        from io import StringIO
        parsed = None
        for kind in (paramiko.Ed25519Key, paramiko.RSAKey, paramiko.ECDSAKey):
            try:
                parsed = kind.from_private_key(StringIO(pem))
                break
            except (paramiko.SSHException, ValueError):
                continue
        if parsed is None:
            raise ValueError('Unsupported or encrypted PEM; provide a valid unencrypted SSH key privately')
        blob = self.cipher.encrypt(pem.encode())
        cur = self.db.execute('INSERT INTO servers(host,port,username,fingerprint,credential) VALUES(?,?,?,?,?)',
                              (host, port, username, fingerprint, blob))
        self.db.commit()
        return cur.lastrowid

    def server(self, server_id):
        row = self.db.execute('SELECT id,host,port,username,fingerprint,credential FROM servers WHERE id=?', (server_id,)).fetchone()
        if not row:
            raise ValueError('Unknown server')
        return row

    def list_servers(self):
        return [dict(id=i, ip=host, port=port, username=user) for i, host, port, user in
                self.db.execute('SELECT id,host,port,username FROM servers ORDER BY id')]

    def log(self, admin_id, server_id, action, outcome):
        self.db.execute('INSERT INTO events(timestamp,admin_id,server_id,action,outcome) VALUES(?,?,?,?,?)',
                        (int(time.time()), admin_id, server_id, action, outcome[:120]))
        self.db.commit()

    def remember(self, key, value):
        if len(key) > 80 or len(value) > 2000:
            raise ValueError('Memory too large')
        self.db.execute('INSERT INTO memory(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, value))
        self.db.commit()

    def memories(self, limit=5):
        return self.db.execute('SELECT key,value FROM memory ORDER BY key LIMIT ?', (limit,)).fetchall()


class Remote:
    """Pinned host key. Only fixed local API commands; no arbitrary shell input."""
    def __init__(self, store):
        self.store = store

    def request(self, server_id, path, payload=None):
        ident, host, port, username, fingerprint, encrypted = self.store.server(server_id)
        from io import StringIO
        pem = self.store.cipher.decrypt(encrypted).decode()
        key = None
        for kind in (paramiko.Ed25519Key, paramiko.RSAKey, paramiko.ECDSAKey):
            try:
                key = kind.from_private_key(StringIO(pem))
                break
            except (paramiko.SSHException, ValueError):
                continue
        if key is None:
            raise ValueError('SSH key unavailable')
        from base64 import b64encode
        body = b64encode(json.dumps(payload or {}, separators=(',', ':')).encode()).decode()
        if path not in ('status', 'client', 'delete'):
            raise ValueError('Operation forbidden')
        # Escape hatch prevented: path fixed; base64 strictly validated by remote CLI.
        command = f'python3 -I {ROOT}/agent_bridge.py {path} {body}'
        import hashlib, socket
        raw_socket = socket.create_connection((host, port), timeout=10)
        transport = paramiko.Transport(raw_socket)
        try:
            transport.start_client(timeout=10)
            actual = 'SHA256:' + b64encode(hashlib.sha256(transport.get_remote_server_key().asbytes()).digest()).decode().rstrip('=')
            if actual != fingerprint:
                raise ValueError('SSH host key mismatch')
            transport.auth_publickey(username, key)
            if not transport.is_authenticated():
                raise PermissionError('SSH authentication failed')
            channel = transport.open_session(timeout=10)
            channel.settimeout(45)
            channel.exec_command(command)
            output = channel.makefile('r', 65536).read(65537)
            code = channel.recv_exit_status()
            if code or len(output) > 65536:
                raise RuntimeError('Node command failed')
            result = json.loads(output)
            if not result.get('ok'):
                raise RuntimeError('Node rejected command')
            return result
        finally:
            transport.close()


class Operator:
    def __init__(self, store, remote, admins):
        self.store, self.remote, self.admins = store, remote, set(admins)

    def require_admin(self, user_id):
        if user_id not in self.admins:
            raise PermissionError('Administrator only')

    def inspect(self, user_id, server_id):
        self.require_admin(user_id)
        return self.remote.request(server_id, 'status')

    def propose(self, user_id, server_id, action, name, value, impact):
        self.require_admin(user_id)
        name = validate_name(name)
        if action not in ('disable', 'enable', 'extend'):
            raise ValueError('Operation not supported')
        if not isinstance(impact, str) or not impact.strip() or len(impact) > 500:
            raise ValueError('Explain impact before approval')
        status = self.remote.request(server_id, 'status')
        old = next((c for c in status['clients'] if c['name'] == name), None)
        if old is None:
            raise ValueError('Subscription missing on target server')
        before = {k: old[k] for k in ('name', 'uuid', 'trafficLimitGb', 'expiryDays', 'expiryTimestamp', 'userLimit', 'isActive')}
        after = dict(before)
        if action in ('enable', 'disable'):
            after['isActive'] = int(action == 'enable')
        elif action == 'extend':
            if not value.isdigit() or not 1 <= int(value) <= 365:
                raise ValueError('Days must be 1..365')
            after['expiryTimestamp'] += int(value) * 86400
        proposal_id = uuid.uuid4().hex
        self.store.db.execute('INSERT INTO proposals(id,admin_id,server_id,name,action,value,previous,expires) VALUES(?,?,?,?,?,?,?,?)',
                              (proposal_id, user_id, server_id, name, action, json.dumps(after),
                               self.store.cipher.encrypt(json.dumps(before).encode()).decode(), int(time.time()) + 300))
        self.store.db.commit()
        self.store.log(user_id, server_id, 'propose_' + action, 'pending')
        return proposal_id, before, after, impact

    def decide(self, user_id, proposal_id, approve):
        self.require_admin(user_id)
        row = self.store.db.execute('SELECT admin_id,server_id,name,action,value,previous,expires,state FROM proposals WHERE id=?', (proposal_id,)).fetchone()
        if not row or row[0] != user_id or row[7] != 'pending' or time.time() >= row[6]:
            raise ValueError('Proposal unavailable, expired or already handled')
        state = 'executing' if approve else 'rejected'
        updated = self.store.db.execute('UPDATE proposals SET state=? WHERE id=? AND state=?', (state, proposal_id, 'pending')).rowcount
        self.store.db.commit()
        if updated != 1:
            raise ValueError('Proposal already handled')
        if not approve:
            self.store.log(user_id, row[1], row[3], 'rejected')
            return 'rejected'
        try:
            self.remote.request(row[1], 'client', {'client': json.loads(row[4])})
            self.store.db.execute('UPDATE proposals SET state=? WHERE id=?', ('applied', proposal_id))
            self.store.db.commit()
            self.store.log(user_id, row[1], row[3], 'applied')
            return 'applied'
        except Exception:
            self.store.db.execute('UPDATE proposals SET state=? WHERE id=?', ('unknown', proposal_id))
            self.store.db.commit()
            self.store.log(user_id, row[1], row[3], 'outcome_unknown')
            raise RuntimeError('Outcome unknown: inspect server before retry or rollback')

    def prepare_rollback(self, user_id, proposal_id):
        self.require_admin(user_id)
        row = self.store.db.execute('SELECT server_id,name,value,state FROM proposals WHERE id=?', (proposal_id,)).fetchone()
        if not row or row[3] != 'applied':
            raise ValueError('No applied change to roll back')
        current = self.remote.request(row[0], 'status')
        actual = next((c for c in current['clients'] if c['name'] == row[1]), None)
        if not actual or any(actual.get(k) != v for k, v in json.loads(row[2]).items()):
            raise ValueError('Server changed since approval; manual review required')
        self.store.db.execute("UPDATE proposals SET state='rollback_pending', expires=? WHERE id=? AND state='applied'", (int(time.time()) + 300, proposal_id))
        self.store.db.commit()
        return {'server_id': row[0], 'name': row[1]}

    def rollback(self, user_id, proposal_id):
        self.require_admin(user_id)
        row = self.store.db.execute('SELECT server_id,name,value,previous,state,expires FROM proposals WHERE id=?', (proposal_id,)).fetchone()
        if not row or row[4] != 'rollback_pending' or time.time() >= row[5]:
            raise ValueError('No approved rollback request')
        current = self.remote.request(row[0], 'status')
        actual = next((c for c in current['clients'] if c['name'] == row[1]), None)
        if not actual or any(actual.get(k) != v for k, v in json.loads(row[2]).items()):
            raise ValueError('Server changed since approval; manual review required')
        updated = self.store.db.execute("UPDATE proposals SET state='rollback_executing' WHERE id=? AND state='rollback_pending'", (proposal_id,)).rowcount
        self.store.db.commit()
        if updated != 1:
            raise ValueError('Already handled')
        try:
            before = json.loads(self.store.cipher.decrypt(row[3].encode()))
            self.remote.request(row[0], 'client', {'client': before})
            self.store.db.execute("UPDATE proposals SET state='rolled_back' WHERE id=?", (proposal_id,))
            self.store.db.commit()
            self.store.log(user_id, row[0], 'rollback', 'applied')
            return 'rolled_back'
        except Exception:
            self.store.db.execute("UPDATE proposals SET state='rollback_unknown' WHERE id=?", (proposal_id,))
            self.store.db.commit()
            self.store.log(user_id, row[0], 'rollback', 'outcome_unknown')
            raise RuntimeError('Rollback outcome unknown; inspect server')

    def reject_rollback(self, user_id, proposal_id):
        self.require_admin(user_id)
        row = self.store.db.execute('SELECT server_id,state FROM proposals WHERE id=?', (proposal_id,)).fetchone()
        if not row or row[1] != 'rollback_pending':
            raise ValueError('Rollback not pending')
        self.store.db.execute("UPDATE proposals SET state='applied' WHERE id=?", (proposal_id,))
        self.store.db.commit()
        self.store.log(user_id, row[0], 'rollback', 'rejected')
        return 'rejected'


def summarize_config(url):
    """No network fetch or untrusted file execution: identify link syntax only."""
    if len(url) > 4096:
        raise ValueError('Link too long')
    from urllib.parse import urlsplit
    parsed = urlsplit(url)
    if parsed.scheme == 'vless':
        fields = dict(item.split('=', 1) for item in parsed.query.split('&') if '=' in item)
        return {'kind': 'VLESS', 'host': parsed.hostname, 'port': parsed.port,
                'security': fields.get('security', 'none'), 'transport': fields.get('type', 'tcp')}
    if parsed.scheme in ('https', 'http') and parsed.hostname == 'github.com':
        return {'kind': 'GitHub', 'path': parsed.path[:150]}
    if parsed.scheme in ('https', 'http'):
        return {'kind': 'subscription', 'host': parsed.hostname, 'note': 'Credentials not fetched; manual review required'}
    raise ValueError('Unsupported link')
