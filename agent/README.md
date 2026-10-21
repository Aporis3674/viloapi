# Vilo agent — independent VPS bot

Runs on an always-on VPS with a **separate Telegram bot token**. Telegram `getUpdates` polling is used only for this agent bot. ViloShop's tgcloud webhook and VPN services remain unchanged. The agent responds only to configured admin IDs inside topic `https://t.me/c/3925413574/2464` (private setup commands go to the agent bot's own DM).

## Setup

1. Create a new bot in BotFather, add it to the group, grant topic/message access. A second bot token is necessary: Telegram cannot use `getUpdates` and a webhook simultaneously for the same token.
2. Install `requirements.txt` in a Python 3.10+ virtual environment on the VPS.
3. Set `VILO_AGENT_BOT_TOKEN` to the **new agent bot token**, `VILO_AGENT_ADMIN_IDS` to admin numeric IDs, `VILO_AGENT_DATA` to a private directory. Keep token and master key outside git and Telegram chat.
4. Start `python3 agent/poller.py` as a systemd service. No inbound port or tgcloud connection required.

## Commands

Private admin DM with the agent bot: `/addserver PUBLIC_IP SSH_PORT USERNAME SHA256:HOST_FINGERPRINT` followed by a reply containing a PEM document. `/endpoint https://api.example.com/v1` followed by a private API key reply; `/models`; `/model ID`; `/remember KEY=lesson`.

Agent topic: `/servers`, `/status ID`, `/diagnose LINK`, `/skills`, `/ask QUESTION`, `/propose ID disable|enable|extend NAME VALUE Why/impact`, `/propose_rollback PROPOSAL_ID`. Mutations require fresh admin approval; rollback requires separate approval. Model responses cannot execute shell or approve a change.

The agent's SSH registry is separate from ViloShop's subscription database. Do not run write proposals against the legacy German VPS: its panel has different paths and synchronization logic. The agent supports only VPS nodes installed with the `viloapi` bridge at `/etc/vilo-node/agent_bridge.py`. Automatic conversion, replacement configs, and cross-registry synchronization are not implemented.

## Security

SSH credentials and model API keys are encrypted at rest. Back up the master key separately. Telegram has already processed any PEM uploaded to it, even if the message is deleted later. Use a dedicated limited SSH key; rotate leaked bot tokens. Public HTTP subscription URLs expose UUID credentials on the wire. A valid HTTPS certificate for IP or a domain is needed for confidential subscription delivery.
