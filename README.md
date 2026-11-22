# Telegram Bot Creator

Python/aiogram constructor hosting clicker, referral and star-themed Telegram bots.
Creator accounting uses RUB; child units are independently configurable. Withdrawals are manual requests, not automatic transfers.

## Local development
Python 3.11+. Install `requirements-dev.txt`; copy `config.example` to a private `.env` and configure only when intentionally connecting services.
Run `pytest`, `ruff check .`, `python -m compileall -q creator child`, `python -m creator --dry-run`. Tests use temporary SQLite and fake Telegram/provider sessions.

No production data, private deployment instructions, credentials or previous private Git history are included. This is a clean source snapshot. Integration code does not prove provider end-to-end health.

## Creator source sponsors (admin only)

`/admin` → **📡 Источники ОП** → **TGrass / PiarFlow** → **🔑 Задать / заменить ключ**.
Enter the source's API key, then press **💾 Сохранить**. The bot attempts to delete the incoming message, but Telegram chats are not a vault or end-to-end encrypted; deletion cannot be guaranteed. Never paste real keys in issues, pull requests or this repository. Use private server-side configuration if chat entry is unsuitable.

Precedence: server `TGRASS_HUB_API_KEY` / `PIARFLOW_HUB_API_KEY` → manually saved private source → existing legacy autohub cache → legacy autohub registration with factory credentials. Server keys are read-only in the bot, so a manual entry is never silently ignored. Save/remove requires confirmation; unreadable configuration fails closed. Removing a manual key returns to an older source/autohub possibility, not revocation or global disable.

A manually configured source also works without factory credentials. New approvals still try child factory registration when possible and fall back to the creator source. Existing approved bots keep their stored keys; changing/removing this setting does not mass-rotate children or provider credentials. A disabled relay bot takes the current source when explicitly re-approved through the existing management workflow. No live validation is implied by a saved key. The feed consists of whatever sponsors the network assigns to the source, **not exclusively your own advertising campaigns**.

Settings/status views never contact providers, create invoices, alter rewards, or update child configurations. No new real key/provider registration is required to install this code. Configure only intentionally after installation. Public PiarFlow contract: https://piarflow.com/api-docs and https://piarflow.com/llms.txt.
