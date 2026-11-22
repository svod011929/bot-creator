# C4 (minimal): owner bot statistics from already stored facts — design

## Scope
Smallest useful slice of roadmap C4: a read-only statistics screen per bot for its owner, computed on open from data the bot already stores. No new tables, events, tracking, personal data, schema changes or financial logic. Full funnel, activity and retention metrics stay out of scope because no history for them is recorded.

## Alternatives
1. **Chosen:** per-bot screen reading only that bot's `bot.db` (owner checked first), one aggregate query per table.
2. Summary across all owner bots — N child databases per open on a 1 vCPU VPS; rejected.
3. New event logging for DAU/retention — needs a separate storage/retention decision; rejected.

## Entry and guards
Bot card → `📊 Статистика` (`bst:<id>`); `🔄 Обновить` re-reads; `⬅️ К боту` returns to `b:<id>:open`. Private chat, current owner, strict callback regex, foreign active FSM is not interrupted (alert only), bot re-checked after the read. Deleted/transferred bots are refused.

## Numbers (definitions shown in the UI)
- Users total = rows in `users`.
- New in the last 24 h / 7 d / 30 d = `created_at` within rolling windows ending now (UTC epoch, no calendar or timezone assumptions).
- Came via referral link = `referrer_id IS NOT NULL`.
- Marked unreachable = `blocked = 1` (set when a broadcast delivery was refused; cleared when the user returns). Not a measure of who left.
- Own tasks: active tasks; completions and distinct users from `task_done` (template tasks only, not TGrass/PiarFlow, which C3 already shows).
- Withdrawal requests by status: pending / marked paid / rejected — counts only. No sums: amounts are in the child's own unit and "marked paid" is not proof of transfer.

## Honesty rules
Missing `bot.db` = "no data yet"; unreadable DB or missing table = "not available", never 0. Explicit "not measured": activity/returning users, retention, conversion, revenue, live Telegram health. No identifiers or rows are shown or logged.

## Tests
Owner/other owner/admin-not-owner/group/stale/malformed/transferred, foreign FSM kept, windows boundaries, missing/corrupt DB and missing tables, read-only (child DB and creator DB bytes unchanged), HTML-safe username, text length, refresh and back.
