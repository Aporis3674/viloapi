"""Agent worker for updates forwarded by the existing shop bot webhook."""
import json
import time
import re
import urllib.parse
import urllib.request

from core import Operator, Remote, Store, summarize_config

GROUP_ID = -1003925413574
TOPIC_ID = 2464
SKILLS = {'status': 'Read server inventory and recent traffic', 'diagnose': 'Analyze a connection link without downloading it',
          'propose': 'Prepare a reversible change requiring administrator approval', 'rollback': 'Restore the previous server configuration'}


def url_allowed(raw):
    """Model URLs cannot target loopback, local networks, credentials or redirects."""
    from ipaddress import ip_address
    u = urllib.parse.urlsplit(raw)
    if u.scheme != 'https' or u.username or u.password or not u.hostname or u.port not in (None, 443):
        raise ValueError('Model endpoint must be public HTTPS on port 443')
    if u.hostname.lower() in ('localhost', 'cloud.telegram.org') or u.hostname.endswith(('.local', '.internal', '.test')):
        raise ValueError('Private model endpoint not allowed')
    try:
        if not ip_address(u.hostname).is_global:
            raise ValueError('Private model endpoint not allowed')
    except ValueError as e:
        if str(e) == 'Private model endpoint not allowed':
            raise
        if not re.fullmatch(r'(?:[a-zA-Z0-9-]+\.)+[a-zA-Z]{2,63}', u.hostname):
            raise ValueError('Invalid model hostname')
    return raw.rstrip('/')


class Telegram:
    def __init__(self, token):
        self.token = token
        self.base = f'https://api.telegram.org/bot{token}/'

    def call(self, method, data):
        if method not in ('sendMessage', 'answerCallbackQuery', 'deleteMessage', 'getFile', 'getMe', 'getUpdates'):
            raise ValueError('Telegram method not allowed')
        req = urllib.request.Request(self.base + method, json.dumps(data).encode(),
                                     headers={'Content-Type': 'application/json'}, method='POST')
        with urllib.request.urlopen(req, timeout=70) as response:
            result = json.load(response)
        if not result.get('ok'):
            raise RuntimeError('Telegram API failed')
        return result['result']

    def send(self, text, *, chat=GROUP_ID, topic=TOPIC_ID, buttons=None):
        args = {'chat_id': chat, 'text': text[:3900]}
        if topic and chat == GROUP_ID:
            args['message_thread_id'] = topic
        if buttons:
            args['reply_markup'] = {'inline_keyboard': buttons}
        return self.call('sendMessage', args)


def ask_model(store, question):
    if not question or len(question) > 1500:
        raise ValueError('Question must be 1..1500 characters')
    selected = store.db.execute("SELECT value FROM memory WHERE key='model_choice'").fetchone()
    if not selected:
        raise ValueError('Select a model via /models and /model first')
    lessons = [{'key': k, 'value': v} for k, v in store.memories(5) if k.startswith('lesson:')]
    response = model_request(store, '/chat/completions', {
        'model': selected[0], 'max_tokens': 500,
        'messages': [
            {'role': 'system', 'content': 'You are a read-only VPN diagnosis assistant. Do not claim to have executed commands. Recommend only; all server changes require explicit human approval through the operator workflow.'},
            {'role': 'user', 'content': json.dumps({'question': question, 'lessons': lessons})}
        ]
    })
    answer = response['choices'][0]['message']['content']
    if not isinstance(answer, str):
        raise ValueError('Invalid model response')
    return answer[:3500]


def model_request(store, route, body=None):
    row = store.db.execute("SELECT value FROM memory WHERE key='model_config'").fetchone()
    if not row:
        raise ValueError('Configure the model first in a private chat')
    data = json.loads(store.cipher.decrypt(row[0].encode()))
    base = url_allowed(data['endpoint'])
    # Explicit model endpoints still require network controls at the host firewall to resist DNS rebinding.
    url = base + route
    req = urllib.request.Request(url, json.dumps(body).encode() if body is not None else None,
          headers={'Authorization': 'Bearer ' + data['api_key'], 'Content-Type': 'application/json'},
          method='POST' if body is not None else 'GET')
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args):
            raise ValueError('Model endpoint redirect refused')
    with urllib.request.build_opener(NoRedirect()).open(req, timeout=25) as response:
        if response.status != 200:
            raise ValueError('Model API failed')
        raw = response.read(32769)
    if len(raw) > 32768:
        raise ValueError('Model response too large')
    return json.loads(raw)


class App:
    def __init__(self, telegram, store, admins):
        self.tg, self.store, self.admins = telegram, store, set(admins)
        self.operator = Operator(store, Remote(store), self.admins)

    def private(self, message):
        user = message['from']['id']
        self.operator.require_admin(user)
        chat = message['chat']['id']
        text = message.get('text', '').strip()
        document = message.get('document')
        setup_row = self.store.db.execute('SELECT value FROM memory WHERE key=?', (f'setup:{user}',)).fetchone()
        setup = json.loads(setup_row[0]) if setup_row else {}
        if setup and (document or (not text.startswith('/') and text)) and not message.get('reply_to_message', {}).get('text', '').startswith('Agent setup:'):
            raise ValueError('Reply to the Agent setup message so credentials are handled by the agent')
        if setup and time.time() - setup['createdAt'] > 600:
            setup = {}
            self.store.db.execute('DELETE FROM memory WHERE key=?', (f'setup:{user}',))
            self.store.db.commit()
        if text.startswith('/addserver '):
            parts = text.split()
            if len(parts) != 5:
                raise ValueError('Usage: /addserver IP SSH_PORT USERNAME SHA256:HOST_FINGERPRINT')
            from core import validate_host
            ip, port, username, fingerprint = parts[1:]
            validate_host(ip, int(port), username, fingerprint)
            self.store.remember(f'setup:{user}', json.dumps({'kind': 'server', 'details': [ip, int(port), username, fingerprint], 'createdAt': time.time()}))
            self.tg.send('Agent setup: reply to this message with the .pem file in this PRIVATE chat. It will be encrypted locally; the message will be deleted.', chat=chat, topic=None)
        elif text.startswith('/endpoint '):
            endpoint = url_allowed(text.split(' ', 1)[1])
            self.store.remember(f'setup:{user}', json.dumps({'kind': 'endpoint', 'details': endpoint, 'createdAt': time.time()}))
            self.tg.send('Agent setup: reply to this message with the model API key in this PRIVATE chat within 10 minutes.', chat=chat, topic=None)
        elif text == '/models':
            models = model_request(self.store, '/models')
            ids = [m.get('id') for m in models.get('data', []) if isinstance(m, dict) and isinstance(m.get('id'), str)]
            self.store.remember('model_list', json.dumps(ids[:100]))
            self.tg.send('Available models:\n' + '\n'.join(ids[:30]) + '\nChoose: /model MODEL_ID', chat=chat, topic=None)
        elif text.startswith('/model '):
            name = text[7:].strip()
            listed = json.loads(self.store.db.execute("SELECT value FROM memory WHERE key='model_list'").fetchone()[0])
            if name not in listed:
                raise ValueError('Select a model from /models')
            self.store.remember('model_choice', name)
            self.tg.send('Model selected.', chat=chat, topic=None)
        elif text.startswith('/remember '):
            key, sep, value = text[10:].partition('=')
            if not sep:
                raise ValueError('Usage: /remember KEY=lesson')
            self.store.remember('lesson:' + key.strip(), value.strip())
            self.tg.send('Feedback saved.', chat=chat, topic=None)
        elif document and setup.get('kind') == 'server':
            if not document.get('file_name', '').endswith('.pem') or document.get('file_size', 0) > 20000:
                raise ValueError('Send a PEM file under 20 KB')
            details = self.tg.call('getFile', {'file_id': document['file_id']})
            if not re.fullmatch(r'[a-zA-Z0-9_./-]+', details.get('file_path', '')):
                raise ValueError('Invalid Telegram file path')
            # Telegram file endpoint is trusted; limit content before storage.
            with urllib.request.urlopen(f'https://api.telegram.org/file/bot{self.tg.token}/{details["file_path"]}', timeout=15) as res:
                raw = res.read(20001)
            if len(raw) > 20000:
                raise ValueError('PEM too large')
            self.tg.call('deleteMessage', {'chat_id': chat, 'message_id': message['message_id']})
            server_id = self.store.add_server(*setup['details'], raw.decode())
            self.store.db.execute('DELETE FROM memory WHERE key=?', (f'setup:{user}',))
            self.store.db.commit()
            self.store.log(user, server_id, 'register_server', 'stored')
            self.tg.send(f'Server #{server_id} stored with encrypted SSH credentials. Use /servers in the agent topic.', chat=chat, topic=None)
        elif setup.get('kind') == 'endpoint' and text:
            self.tg.call('deleteMessage', {'chat_id': chat, 'message_id': message['message_id']})
            endpoint = setup['details']
            self.store.db.execute('DELETE FROM memory WHERE key=?', (f'setup:{user}',))
            self.store.db.commit()
            secret = self.store.cipher.encrypt(json.dumps({'endpoint': endpoint, 'api_key': text}).encode()).decode()
            self.store.remember('model_config', secret)
            self.tg.send('Endpoint and API key encrypted. Run /models.', chat=chat, topic=None)
        else:
            self.tg.send('Commands: /addserver IP PORT USERNAME SHA256:FINGERPRINT, /endpoint https://api.example.com/v1, /models, /model ID, /remember KEY=lesson', chat=chat, topic=None)

    def group(self, message):
        if message['chat']['id'] != GROUP_ID or message.get('message_thread_id') != TOPIC_ID:
            return
        user = message.get('from', {}).get('id')
        if user not in self.admins:
            return
        text = message.get('text', '').strip()
        if text == '/servers':
            servers = self.store.list_servers()
            self.tg.send('\n'.join(f"#{s['id']}: {s['ip']} SSH:{s['port']}" for s in servers) or 'No servers. Add privately with /addserver.')
        elif text.startswith('/status '):
            sid = int(text.split()[1]); info = self.operator.inspect(user, sid)
            self.tg.send(f"Server #{sid}: {'online' if info.get('xray') else 'offline'}; clients: {len(info.get('clients', []))}; observedAt: {info.get('observedAt')}")
        elif text.startswith('/diagnose '):
            summary = summarize_config(text.split(' ', 1)[1])
            self.tg.send('Link analysis: ' + json.dumps(summary, ensure_ascii=False))
        elif text == '/skills':
            self.tg.send('\n'.join(f'{name}: {description}' for name, description in SKILLS.items()))
        elif text.startswith('/propose '):
            parts = text.split(' ', 5)
            if len(parts) < 6:
                raise ValueError('Usage: /propose SERVER_ID disable|enable|extend NAME VALUE Reason/impact')
            _, sid, action, name, value, impact = parts
            proposal_id, before, after, reason = self.operator.propose(user, int(sid), action, name, value, impact)
            changed = {k: [before[k], v] for k, v in after.items() if before[k] != v}
            # Do not publish UUIDs or credentials in the group.
            self.tg.send(f'Change request {proposal_id[:12]}\nWhere: server #{sid}, config {name}\nWhat: {action}\nWhy/impact: {reason[:300]}\nChanged fields: {", ".join(changed)}\nExpires in 5 minutes.',
                buttons=[[{'text': 'Approve', 'icon_custom_emoji_id': '6267225207560214192', 'callback_data': 'approve:' + proposal_id}, {'text': 'Reject', 'icon_custom_emoji_id': '6052869226677410910', 'callback_data': 'reject:' + proposal_id}]])
        elif text.startswith('/propose_rollback '):
            proposal_id = text.split()[1]
            if not re.fullmatch(r'[a-f0-9]{32}', proposal_id):
                raise ValueError('Invalid proposal ID')
            details = self.operator.prepare_rollback(user, proposal_id)
            self.tg.send(f"Rollback request {proposal_id[:12]}\nWhere: server #{details['server_id']}, config {details['name']}\nWhat: restore previous client settings\nImpact: current connection may restart. Expires in 5 minutes.",
                buttons=[[{'text': 'Approve rollback', 'icon_custom_emoji_id': '6267225207560214192', 'callback_data': 'rollback:' + proposal_id}, {'text': 'Reject', 'icon_custom_emoji_id': '6052869226677410910', 'callback_data': 'rejectrollback:' + proposal_id}]])
        elif text.startswith('/ask '):
            answer = ask_model(self.store, text[5:])
            self.tg.send(answer) # Model cannot invoke tools, modify servers or issue approvals.
        elif text.startswith('/help'):
            self.tg.send('/servers, /status ID, /diagnose LINK, /skills, /ask QUESTION, /propose ID ACTION NAME VALUE WHY, /propose_rollback ID')

    def callback(self, query):
        if query.get('message', {}).get('chat', {}).get('id') != GROUP_ID or query['message'].get('message_thread_id') != TOPIC_ID:
            return
        user = query['from']['id']; self.operator.require_admin(user)
        cmd, sep, proposal_id = query.get('data', '').partition(':')
        if cmd not in ('approve', 'reject', 'rollback', 'rejectrollback') or not re.fullmatch(r'[a-f0-9]{32}', proposal_id):
            return
        if cmd == 'rollback':
            result = self.operator.rollback(user, proposal_id)
        elif cmd == 'rejectrollback':
            result = self.operator.reject_rollback(user, proposal_id)
        else:
            result = self.operator.decide(user, proposal_id, cmd == 'approve')
        self.tg.call('answerCallbackQuery', {'callback_query_id': query['id'], 'text': result})
        self.tg.send(f'Proposal {proposal_id[:12]}: {result}.')

    def handle(self, update):
        try:
            if 'callback_query' in update:
                self.callback(update['callback_query']); return
            msg = update.get('message', {})
            if not msg.get('from') or msg['from'].get('is_bot'):
                return
            if msg.get('chat', {}).get('type') == 'private':
                self.private(msg)
            else:
                self.group(msg)
        except Exception as exc:
            user = update.get('message', {}).get('from', {}).get('id') or update.get('callback_query', {}).get('from', {}).get('id', 0)
            if user in self.admins:
                self.tg.send(f'Operation failed: {type(exc).__name__}. Check details privately; no secret was logged.', chat=user, topic=None)
