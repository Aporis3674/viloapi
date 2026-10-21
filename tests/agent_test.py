import json
from pathlib import Path
import sys
import tempfile
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'agent'))
from core import Operator, Store, summarize_config, validate_host
from bot import App, url_allowed

client = {'name': 'alice', 'uuid': str(uuid.uuid4()), 'trafficLimitGb': 10, 'expiryDays': 30,
          'expiryTimestamp': int(time.time()) + 2592000, 'userLimit': 1, 'isActive': 1}
class Remote:
    def __init__(self):
        self.clients = {'alice': client.copy()}
        self.writes = 0
    def request(self, server_id, path, payload=None):
        if path == 'status':
            return {'ok': True, 'clients': list(self.clients.values()), 'xray': True}
        if path == 'client':
            self.writes += 1
            value = payload['client'].copy()
            self.clients[value['name']] = value
            return {'ok': True}
        raise AssertionError('Unexpected method')
class Telegram:
    def __init__(self):
        self.sent = []
    def send(self, text, **kwargs):
        self.sent.append((text, kwargs))
    def call(self, method, data):
        return True

with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
    store = Store(root / 'agent.sqlite', root / 'master.key')
    remote = Remote()
    operator = Operator(store, remote, {42})
    try:
        operator.propose(99, 1, 'disable', 'alice', '-', 'test')
        raise AssertionError('Non-admin proposed a change')
    except PermissionError:
        pass
    proposal, before, after, why = operator.propose(42, 1, 'disable', 'alice', '-', 'test impact')
    assert remote.writes == 0 and after['isActive'] == 0
    assert operator.decide(42, proposal, False) == 'rejected'
    assert remote.writes == 0
    proposal, _, _, _ = operator.propose(42, 1, 'disable', 'alice', '-', 'test impact')
    try:
        operator.decide(99, proposal, True)
        raise AssertionError('Non-admin approval accepted')
    except PermissionError:
        pass
    assert remote.writes == 0
    assert operator.decide(42, proposal, True) == 'applied'
    assert remote.writes == 1 and remote.clients['alice']['isActive'] == 0
    try:
        operator.decide(42, proposal, True)
        raise AssertionError('Second approval applied')
    except ValueError:
        pass
    details = operator.prepare_rollback(42, proposal)
    assert details['server_id'] == 1 and remote.writes == 1
    operator.reject_rollback(42, proposal)
    assert remote.writes == 1
    operator.prepare_rollback(42, proposal)
    assert operator.rollback(42, proposal) == 'rolled_back'
    assert remote.clients['alice']['isActive'] == 1
    assert remote.writes == 2
    assert store.db.execute('SELECT COUNT(*) FROM events').fetchone()[0] >= 5
    store.remember('lesson:test', 'Test network first')
    assert store.memories()
    cipher = store.cipher.encrypt(b'private')
    assert b'private' not in cipher and store.cipher.decrypt(cipher) == b'private'
    app = App(Telegram(), store, {42})
    app.operator.remote = remote
    app.handle({'message': {'from': {'id': 42}, 'chat': {'id': -1003925413574}, 'message_thread_id': 1, 'text': '/servers'}})
    assert not app.tg.sent, 'Agent must ignore other topics'
    app.handle({'message': {'from': {'id': 99}, 'chat': {'id': -1003925413574}, 'message_thread_id': 2464, 'text': '/servers'}})
    assert not app.tg.sent, 'Agent must ignore non-admins'
    app.handle({'message': {'from': {'id': 42}, 'chat': {'id': -1003925413574}, 'message_thread_id': 2464, 'text': '/skills'}})
    assert app.tg.sent and 'diagnose' in app.tg.sent[-1][0]
    store.db.close()

for bad in ['127.0.0.1', '169.254.169.254', '10.0.0.1']:
    try:
        validate_host(bad, 22, 'root', 'SHA256:' + 'A'*43)
        raise AssertionError('Private host accepted')
    except ValueError:
        pass
for bad in ['http://api.example.com/v1', 'https://localhost/v1', 'https://127.0.0.1/v1', 'https://admin:pass@api.example.com/v1']:
    try:
        url_allowed(bad)
        raise AssertionError('Unsafe model URL accepted')
    except ValueError:
        pass
assert summarize_config('vless://id@1.1.1.1:443?security=reality&type=tcp')['kind'] == 'VLESS'
print('Agent authorization, approvals, rollback, encryption, topic gating and URL validation: PASS')
