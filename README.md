# Telegram Bot Creator

Python/aiogram constructor hosting clicker, referral and star-themed Telegram bots.
Creator accounting uses RUB; child units are independently configurable. Withdrawals are manual requests, not automatic transfers.

## Local development
Python 3.11+. Install `requirements-dev.txt`; copy `config.example` to a private `.env` and configure only when intentionally connecting services.
Run `pytest`, `ruff check .`, `python -m compileall -q creator child`, `python -m creator --dry-run`. Tests use temporary SQLite and fake Telegram/provider sessions.

No production data, private deployment instructions, credentials or previous private Git history are included. This is a clean source snapshot. Integration code does not prove provider end-to-end health.
