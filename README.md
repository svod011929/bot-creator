# Telegram Bot Creator

Python/aiogram constructor hosting clicker, referral and star-themed Telegram bots.
Creator accounting uses RUB; child units are independently configurable. Withdrawals are manual requests, not automatic transfers.

## Stage 5 Creator content

Owners can manage referral-club campaigns and star-journey cards from the constructor:
catalog, drafts, a participant preview that awards nothing, calendar, pause/resume and copy.
Creator forms are stored separately in an additive `creator_content_forms` table in the child
database and survive constructor restarts. Viewing does not migrate, start a child, pay for
hosting or send messages. Clicker catalogs are unchanged. Money, withdrawal and reward rules
are unchanged. See `STAGE5_CHANGES.md` and `docs/CREATOR_CONTENT.md`. Stacked on deployed
`0010d45` and keeps the Stage 1 post-review fixes.

## Stage 4 Star journey

Star bot now has daily routes, 24 starter knowledge cards, memory exercises, check-in streaks, nine missions,
four collectible constellations and spark rankings. Owners can draft, edit, schedule and publish their own cards.
Game sparks use an independent ledger and do not change the existing wallet or withdrawal rules.
See `STAGE4_CHANGES.md` and `docs/STAR_JOURNEY.md`. This package is stacked on deployed `472cc14` and keeps the
Stage 1 post-review fixes (draft rollback and child environment whitelist).

## Stage 3 referral club

The referral template now includes owner-managed campaigns, persistent draft editing, onboarding, campaign quizzes,
qualified invitations, goals, badges and a club ranking. Club points have their own ledger and are separate from
the existing withdrawal balance. New users who open a campaign link (`c_TOKEN`) get the same first-touch cash
referrer as `r<id>` links; existing `ref_reward` is credited after the same eligibility check. See
`STAGE3_CHANGES.md` and `docs/REFERRAL_CLUB.md`. This package is stacked on deployed `542b6f8` and keeps the
Stage 1 post-review fixes (draft rollback and child environment whitelist).

## Stage 2 clicker adventure

The clicker now includes four timed expeditions, eleven missions, three skill branches, a collection,
achievements and XP rankings. Adventure XP is separate from the existing bot wallet. See `STAGE2_CHANGES.md`
and `docs/CLICKER_ADVENTURE.md` for schema, compatibility, player flow and owner settings.
This package is stacked on the deployed Stage 1 release and keeps the post-review fixes
(draft rollback and child environment whitelist).

## Stage 1 reliability package

This tree includes local fixes over snapshot `9d485a7`; see `STAGE1_CHANGES.md` for scope, additive schema changes,
compatibility notes and validation. It has not been deployed. Development dependencies tested on CPython 3.12.14
are pinned in `requirements-dev.lock.txt` (`pip install -r requirements-dev.lock.txt`). No production dependency
upgrade or payment-policy change is implied by that lockfile.

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
