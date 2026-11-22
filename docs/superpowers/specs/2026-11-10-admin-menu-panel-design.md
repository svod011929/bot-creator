# Admin main-menu entry and bounded admin navigation — design

## Request
Show the admin-panel button in the creator main menu only to configured admins, and improve the admin panel within a bounded, non-financial scope.

## Alternatives
1. **Chosen:** conditional reply-keyboard row plus read-only navigation inside the existing panel (overview line, paged bot list, bot lookup, owner/bot cross-links).
2. Separate admin keyboard mode toggled by a command — extra state, no benefit.
3. New admin dashboards/analytics — overlaps paused owner analytics work and needs metric definitions; rejected.

## Behaviour
- `main_kb(admin)` appends one row `🛠 Админ-панель` only when `ctx.is_admin(uid)` is true at render time (`/start`, `/cancel`). Everyone else sees the unchanged six buttons.
- Visibility is never authorization. Every admin message, callback and admin FSM input is re-checked server-side by the router filter: current `ADMIN_IDS` membership **and** a private chat (callbacks without an accessible message are refused). A removed admin with a stale keyboard or stale FSM gets nothing from the admin router.
- Pressing the button resets the admin's own FSM like other menu buttons (also clearing pending source-key candidates). For non-admins the text is ordinary text: no state reset, no panel.
- Panel header shows recorded counts only: users, non-deleted bots, recorded `running`, recorded `error`, supervised processes. Recorded status is not live Telegram health. No money aggregates are added.
- `🤖 Боты`: 10 per page, newest first, `a:bots:<page>` with bounded LIMIT/OFFSET; out-of-range pages clamp.
- `🔎 Найти бота`: admin FSM accepts a numeric ID or exact @username (case-insensitive, `[A-Za-z0-9_]{1,64}`), parameterized SQL, opens the existing bot card. Not found keeps the input open; `/cancel` and Back exit.
- Bot card gains `👤 Владелец` → existing user card (read-only render). User card gains up to 10 `🤖 ID n` buttons → existing bot card.
- Malformed admin callback IDs are answered as stale instead of raising.

## Not changed
Prices editor, broadcast, ban/balance editing, bot start/stop/delete, `/reply`, TGrass/PiarFlow approvals, source settings, payments, rewards, schema, child code. No new sending, financial or mutating actions.

## Tests
Fake Telegram session: menu visibility for admin/non-admin/removed admin, button press and FSM reset, non-admin press, group-chat refusal, stale callbacks after admin removal, paging bounds, lookup by ID/username/not-found/invalid, owner/bot cross-links, malformed IDs, and DB snapshot unchanged by navigation.
