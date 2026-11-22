# E2 (display only): resale showcase and partner accruals

Read-only over existing data; no change to percentages, resale fee, prices, purchase or ownership transfer.

- «🛒 Перепродажа»: same visibility as before (not deleted, on sale, not own), newest first, 10 per page, filters «Все» + each template, ⬅️/➡️, «Стр. x/y · всего N». Header states the current resale fee and that the buyer pays exactly the price.
- Listing card: existing facts (link, template, users, data size, hosting, VIP, price) + honest notes: user count = records in the bot DB incl. inactive; hosting balance and VIP stay with the bot; fee is taken from the seller; buyer's balance and shortfall. Own listing: no buy button.
- «📤 Продать своего бота»: seller net income example using the exact purchase formula (price − floor(price·fee/100)).
- «👥 Партнёры»: accrual count/sum, last 30 days, last accrual (own `ref` ledger rows only, no referral IDs), note that the percent is the current setting and past accruals are not recalculated, button «📜 Партнёрские начисления» → E1 history filter.
- No schema changes, no new events, no sends.
