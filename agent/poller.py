"""Independent agent bot: Telegram long polling, no shop webhook changes."""
import os
from pathlib import Path
import time
import urllib.error

from bot import App, Telegram
from core import Store


def main():
    root = Path(os.environ.get('VILO_AGENT_DATA', '/var/lib/vilo-agent'))
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.name != 'nt' and root.stat().st_mode & 0o077:
        raise PermissionError('Agent data directory must be private')
    admins = {int(s) for s in os.environ['VILO_AGENT_ADMIN_IDS'].split(',') if s.strip()}
    if not admins:
        raise ValueError('At least one administrator required')
    bot = Telegram(os.environ['VILO_AGENT_BOT_TOKEN'])
    app = App(bot, Store(root / 'agent.sqlite', root / 'master.key'), admins)
    me = bot.call('getMe', {})
    if not me.get('is_bot'):
        raise ValueError('Invalid agent bot token')
    offset = 0
    while True:
        try:
            updates = bot.call('getUpdates', {'offset': offset, 'timeout': 45, 'allowed_updates': ['message', 'callback_query']})
            for update in updates:
                app.handle(update)
                offset = update['update_id'] + 1
        except (urllib.error.URLError, TimeoutError, RuntimeError):
            time.sleep(3)


if __name__ == '__main__':
    main()
