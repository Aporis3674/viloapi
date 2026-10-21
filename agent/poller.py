"""Pull queued agent updates using existing tgcloud CLI authorization."""
import json
import os
from pathlib import Path
import subprocess
import time

from bot import App, Telegram
from core import Store

CLI = '/root/viloshop_bot/node_modules/@tgcloud/cli/src/cli.js'
ROOT = '/root/viloshop_bot'


def cloud_call(name, data):
    args = ['node', CLI, 'run', 'endpoints/' + name, json.dumps(data)]
    result = subprocess.run(args, cwd=ROOT, capture_output=True, text=True, timeout=35, check=False)
    if result.returncode:
        raise RuntimeError('Cloud queue command failed')
    # CLI output is JS object notation, not JSON; read only the one-line result object.
    from json5 import loads
    import re
    found = re.search(r'^<\s*(\{.*\})\s+\d+ms\s*$', result.stdout, re.MULTILINE)
    if not found:
        raise RuntimeError('Unexpected cloud queue response')
    body = loads(found.group(1))
    if not isinstance(body, dict) or body.get('ok') is not True:
        raise PermissionError('Cloud queue unauthorized')
    return body


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
        except (subprocess.TimeoutExpired, RuntimeError, PermissionError, KeyError, ValueError):
            time.sleep(10)


if __name__ == '__main__':
    main()
