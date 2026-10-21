import importlib.util
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace

spec = importlib.util.spec_from_file_location('vilo_agent', Path(__file__).resolve().parents[1] / 'scripts/node_agent.py')
agent = importlib.util.module_from_spec(spec)
spec.loader.exec_module(agent)

with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
    cfg = {'ip': '8.8.4.4', 'syncSecret': 'a' * 64, 'connection': {'pbk': 'p' * 43, 'sid': '12345678', 'sni': 'dl.google.com', 'wsPath': '/vilo-ws'}}
    (root / 'node.json').write_text(json.dumps(cfg))
    (root / 'xray.json').write_text(json.dumps({'inbounds': [
        {'tag': 'reality', 'settings': {'clients': []}}, {'tag': 'ws', 'settings': {'clients': []}}]}))
    counters = {'up': 100, 'down': 200}
    commands = []
    fail = {'test': False}
    def runner(args, **kwargs):
        commands.append(args)
        if 'statsquery' in args:
            return SimpleNamespace(stdout=json.dumps({'stat': [
                {'name': 'user>>>alice>>>traffic>>>uplink', 'value': counters['up']},
                {'name': 'user>>>alice>>>traffic>>>downlink', 'value': counters['down']}]}))
        if '-test' in args and fail['test']:
            raise RuntimeError('invalid xray config')
        return SimpleNamespace(stdout='')
    node = agent.Node(root, runner)
    client = {'name': 'alice', 'uuid': '12345678-1234-4234-8234-123456789abc', 'trafficLimitGb': 1,
              'expiryDays': 30, 'expiryTimestamp': int(time.time()) + 86400, 'isActive': 1, 'userLimit': 1}
    node.put(client)
    config = json.loads((root / 'xray.json').read_text())
    assert config['inbounds'][0]['settings']['clients'][0]['email'] == 'alice'
    node.sample()
    assert node.traffic()['alice']['uplink'] == 100
    node.sample()
    assert node.traffic()['alice']['uplink'] == 100, 'same counters must not double count'
    counters['up'] = 150
    node.sample()
    assert node.traffic()['alice']['uplink'] == 150
    counters['up'] = 20
    node.sample()
    assert node.traffic()['alice']['uplink'] == 170, 'external restart retains totals'
    assert node.status()['radar']['alice']['last_handshake']
    original = (root / 'xray.json').read_text()
    fail['test'] = True
    try:
        node.put({**client, 'isActive': 0})
        raise AssertionError('validation failure must propagate')
    except RuntimeError:
        pass
    assert (root / 'xray.json').read_text() == original
    assert node.clients()[0]['isActive'] == 1, 'rollback preserves prior client'
    fail['test'] = False
    node.put({**client, 'trafficEpoch': 'reset-1'})
    assert node.traffic().get('alice', {}).get('uplink', 0) == 0
    node.db.execute("UPDATE clients SET data=? WHERE name='alice'", (json.dumps({**client, 'expiryTimestamp': 1}),))
    node.db.commit()
    node.apply()
    assert not json.loads((root / 'xray.json').read_text())['inbounds'][0]['settings']['clients']
    node.remove('alice')
    node.remove('alice')
    assert node.clients() == []
    for patch in [{'name': '../../etc/passwd'}, {'userLimit': 0}, {'isActive': True}, {'trafficLimitGb': -1}]:
        try:
            agent.validate_client({**client, **patch})
            raise AssertionError('invalid input accepted')
        except ValueError:
            pass
    node.db.close()
print('Node validation, stats persistence, quota/expiry, idempotent delete and rollback: PASS')
