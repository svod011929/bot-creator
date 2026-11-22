# D2: step-by-step own tasks in the Star bot template — design

## Scope
Only the Star bot template has the bot's own tasks («⚙️ Админка» → «📋 Задания»). Replace the single-line prompt with a wizard collecting exactly the same fields and validation: title, link, reward, optional channel. Not the required-subscription wizard, not TGrass/PiarFlow. Reward semantics, crediting, balances and units are unchanged.

## Flow (owner only, private chat)
1. «➕ Добавить задание» → «Шаг 1/4: название» (the old one-line `Название | ссылка | награда | @канал` is still accepted here and jumps to the preview).
2. «Шаг 2/4: ссылка» — must start with http://, https:// or tg://.
3. «Шаг 3/4: награда в <unit>» — number > 0 (comma allowed); non-finite values rejected.
4. «Шаг 4/4: канал для автопроверки» — @username or numeric id, or «⏭ Без проверки подписки».
5. Preview with «✅ Сохранить» / «❌ Отмена». Nothing is written before «Сохранить».

Invalid input repeats the step with a hint. /cancel, «❌ Отмена» or a menu button cancels without saving. Draft lives only in FSM; a stale «Сохранить» saves nothing. Ownership re-read from config right before the insert. Preview escapes HTML.

## Not changed
Existing tasks, task check/credit logic, deletion, DB schema, other templates.
