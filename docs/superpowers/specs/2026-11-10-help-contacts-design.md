# B4: help and support contacts in the creator — design

## Scope
Creator bot only. Child bots are unchanged. No messages are sent to anyone; links are shown only when a user opens the screens.

## Configuration
Existing `CHAT_URL`, `CHANNEL_URL`, `ADMIN_URL` in the server `.env` stay the source of truth (no hardcoded production links). Values are validated and normalized at load: `t.me/…`, `https://t.me/…`, `https://telegram.me/…` become `https://t.me/<path>`; only a public username or a `+invite` path is accepted. Anything else is dropped (logged by variable name only), so a malformed value cannot break the whole Information message. Buttons apply the same check defensively.

## UI
- «📰 Информация» gains «❓ Помощь».
- Help screen: short instructions — launch, hosting, token, payouts in your bots (manual, owner-marked), how to back out of an input — plus contact buttons «💬 Чат», «📢 Канал», «👑 Администратор» when configured, «⛔ Оповестить о проблеме» always, «⬅️ Назад» to Information.
- No link configured: the existing report flow is offered; nothing invented.
- Welcome text points to «Информация» → «❓ Помощь».

## Not changed
Prices, payments, report flow, admin ids, child bots, DB schema.
