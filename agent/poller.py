"""Pull agent updates over standard HTTPS from tgcloud; preserve shop webhook."""
import json
import os
from pathlib import Path
import time
import urllib.error
import urllib.request

from bot import App, Telegram
from core import Store

BASE = 'https://app8891620674.tgcloud.ai/api/'


def cloud_call(name, data):
    req = urllib.request.Request(BASE + name, json.dumps(data).encode(),
          headers={'Content-Type': 'application/json'}, method='POST')
    with urllib.request.urlopen(req, timeout=25) as response:
        body = json.loads(response.read(150000))
    if body.get('status') == 403 or body.get('body', {}).get('ok') is False:
        raise PermissionError('Cloud queue unauthorized')
    result = body.get('result', body)
    if isinstance(result, dict) and result.get('ok') is True:
        return result
    raise RuntimeError('Unexpected cloud queue response')


def main():
    root = Path(os.environ.get('VILO_AGENT_DATA', '/var/lib/vilo-agent'))
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    admins = {int(s) for s in os.environ['VILO_AGENT_ADMIN_IDS'].split(',') if s.strip()}
    secret = os.environ['VILO_AGENT_FORWARD_SECRET']
    app = App(Telegram(os.environ['VILO_AGENT_BOT_TOKEN']), Store(root / 'agent.sqlite', root / 'master.key'), admins)
    while True:
        try:
            response = cloud_call('pollAgentQueue', {'secret': secret})
            for item in response['updates']:
                app.handle(item['update'])
                cloud_call('ackAgentQueue', {'secret': secret, 'id': item['id']})
            time.sleep(3)
        except (urllib.error.URLError, TimeoutError, RuntimeError, PermissionError, KeyError, ValueError):
            time.sleep(10)


if __name__ == '__main__':
    main()
