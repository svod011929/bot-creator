# E1: full history of the creator's operations — design

Read-only view over the existing `transactions` table (rubles, internal balance incl. LavePay top-ups). No schema change, no new events.

## UI
«💼 Мой кабинет» → «📜 История операций»:
- Filters: «Все», «Пополнения» (topup), «Боты» (create/hosting/vip), «Партнёрские» (ref), «Перепродажа» (resale_buy/resale_sell), «Прочее» (refund/admin/tgrass/piarflow and anything else).
- 10 per page, newest first, «Стр. x/y · всего N», ⬅️/➡️, number buttons to open an operation, «⬅️ В кабинет».
- Detail: №, date/time UTC, amount, type, description, «⛔ Оповестить о проблеме» (existing report flow), «⬅️ К списку».

## Privacy and safety
Every query is scoped to the caller's user id; detail by (id, user id). Private chat only. Descriptions never reveal other people's Telegram IDs: referral payer IDs and administrator IDs are replaced by neutral text, and any remaining 7+ digit number is masked. HTML escaped. Strict callback parsing; out-of-range page clamps. Admin views out of scope.

## Not changed
Balances, prices, payments, crediting, the report flow, the DB schema.
