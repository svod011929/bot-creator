# D1: consistent child-bot wording for balance unit and manual payouts — design

## Scope
Text-only consistency in child bots so users and owners understand what was credited in the bot's own unit, what can be requested, and that payouts are manual. Amounts, rewards, formulas, gates, withdrawal logic and callbacks are unchanged; no new messages are sent (existing notifications only change wording).

## Changes
- Owner new-request message and request detail: button `✅ Выплачено` → `✅ Отметить выплаченной` (same `wd:ok:…` callback).
- User notification after the owner's decision: "Владелец бота отметил заявку на вывод №N как выплаченную." / "Заявка на вывод №N отклонена владельцем, средства возвращены на баланс." — no claim that money was transferred.
- Request accepted: says the owner pays out manually and status is in «📜 Мои заявки».
- Amount out of range: message now includes the bot's unit.
- Owner statistics: "Заблокировали бота" → "Недоступны для рассылки" (that is what the flag means); "Сегодня" labelled as since 00:00 server time; "Выплачено: <sum> <current unit>" replaced by count of requests marked paid plus sums grouped by the unit stored with each request (unknown unit shown as such), so different units are never added together. Only an additive read query.

## Not changed
Balances, rewards, minimum withdrawal, request creation/resolution/refund, history screens' logic, creator-side rubles, schema.

## Tests
Fake session: button labels and unchanged callbacks, user notification wording on paid/rejected, accepted and out-of-range messages, statistics with mixed/unknown units and none paid, HTML-escaping of units, existing balance/refund behaviour.
