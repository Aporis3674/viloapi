# Vilo agent worker

This optional worker uses the **existing ViloShop bot token**. Telegram keeps the original tgcloud webhook; no `getUpdates` or `setWebhook` calls are made. The shop forwards only admin updates from topic `-1003925413574 / 2464` plus explicit admin-private setup replies. The worker needs an always-on host with public HTTPS for the forwarding endpoint. It **cannot run on the serverless isolate** because it needs an SSH client and durable encrypted storage.

## Setup

- Python 3.10+ on a dedicated host, reachable at an HTTPS URL with a valid certificate (not the VPN node). Install `agent/requirements.txt` in a virtual environment.
- Set `VILO_AGENT_BOT_TOKEN` to the existing shop bot's token **only on this worker**, `VILO_AGENT_ADMIN_IDS` to the numeric admin IDs, `VILO_AGENT_DATA` to a private directory, `VILO_AGENT_FORWARD_SECRET` to 48+ random characters. Never add credentials to git or Telegram groups.
- Start `python3 -I agent/worker.py`; reverse-proxy public HTTPS to its loopback listener `127.0.0.1:8765/forward` (do not expose HTTP directly). Set tgcloud `settings.agent_forward_url` to the exact HTTPS URL and `settings.agent_forward_secret` to the same secret. Use a secure admin-controlled configuration path; no public configuration endpoint is included.
- The bot must be allowed to read messages in topic 2464. The agent accepts commands only from configured admin IDs; setup replies go only to private chat.
- Create encrypted local backups of the agent data directory **and its master key separately**. Losing the key makes encrypted SSH credentials and model keys unrecoverable.

## Commands

Private admin chat:

- `/addserver PUBLIC_IP SSH_PORT SSH_USERNAME SHA256:SSH_HOST_KEY_FINGERPRINT`, then reply to its setup prompt with the PEM document. Verify the fingerprint out of band from the VPS provider. Password-auth SSH is not supported.
- `/endpoint https://api.example.com/v1`, then reply with its API key; `/models` fetches the OpenAI-compatible model list; `/model MODEL_ID` selects a listed model.
- `/remember KEY=lesson` stores concise feedback.

Group topic `https://t.me/c/3925413574/2464`:

- `/servers`, `/status ID`, `/diagnose LINK`, `/ask QUESTION`, `/skills`.
- `/propose ID disable|enable|extend SUB_NAME VALUE Why/impact` shows Approve/Reject. Only an admin's fresh approval performs the change. For extend, VALUE is a day count. Other actions use `-`.
- `/propose_rollback PROPOSAL_ID` requests a separate approval to restore the previous client record. Server changes since approval block rollback.

The model is **read-only advice**; it cannot execute shell commands or invoke approval. Replacement configurations are not activated until synchronization with the shop subscription registry is implemented. SSH uses a pinned host key and a fixed bridge into the node's loopback API. All proposed changes, decisions, ambiguous outcomes and rollbacks are audited. Automatic search for unknown Xray layouts, automated network probes or arbitrary GitHub code execution are deliberately disabled until a reviewed adapter is available.

## Security notes

A PEM sent through Telegram has already traversed Telegram servers, even if the bot later deletes the message. For strongest protection, use a dedicated SSH key for the agent, restrict it to the node, and rotate it if compromised. Public HTTP subscriptions contain UUID credentials in the URL and are not confidential on the wire; do not share subscription URLs publicly. No PEM/API keys appear in logs or group replies.

The existing tgcloud server registry uses HTTPS management tokens and does not automatically register IP-only nodes. The agent's encrypted SSH registry is separate; automatic cross-registry synchronization requires a dedicated trusted integration. No deployment or VPS installation is performed by this repository alone.
