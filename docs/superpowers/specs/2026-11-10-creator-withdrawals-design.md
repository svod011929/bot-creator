# Creator withdrawals of earned money — design (rules approved by the owner 10.11.2026)

## Rules (exactly as approved)
- Earned kinds: `ref`, `resale_sell`, `tgrass`, `piarflow` (positive amounts). Not earned: `topup`, `refund`, positive `admin`.
- Spending is attributed to earned money first (conservative).
- Formula (kopecks, per user, from the ledger):
  - E = Σ amount of earned kinds (amount > 0)
  - S = −Σ amount of spending: `create`, `hosting`, `vip`, `resale_buy`, negative `admin` (amount < 0)
  - W = −Σ `withdraw` − Σ `withdraw_refund` (net withdrawn, includes pending: the request debits at creation)
  - available = min(balance, max(0, E − S − W)). Refunds are ignored (never raise the withdrawable amount).
- Payout: manual by the approver. LavePay payout API not used; extension point `PayoutProvider` only.
- Methods: `WITHDRAW_METHODS` (default `card,sbp,usdt_trc20`). Card: 16–19 digits + Luhn. SBP: RU phone +7/8 + bank name. USDT TRC-20: `T` + 33 base58.
- Minimum `WITHDRAW_MIN` = 100 ₽; fee `WITHDRAW_FEE_PERCENT` = 5 % (both admin-editable price settings), fee = ceil(amount·p/100) kopecks, net = amount − fee > 0. Ledger debits the full amount; request stores amount, fee, net.
- No hold; one pending request per user (DB partial unique index + check in the same transaction).
- Approver: `WITHDRAW_APPROVER_ID`; if unset and exactly one ADMIN_IDS entry, that admin; otherwise unconfigured (requests are created, nobody can resolve, warning logged without details).

## Data (additive)
`withdrawal_requests(id, user_id, amount, fee, net, method, details, status pending|paid|rejected, reason, created_at, resolved_at, resolved_by)`; unique partial index on (user_id) WHERE status='pending'. Ledger kinds `withdraw` (−amount at creation) and `withdraw_refund` (+amount on reject). No change to existing tables.

## Flows
- «💼 Мой кабинет» → «💸 Вывести заработанное»: available + breakdown → method → details → amount → preview (amount, fee, to receive) → «✅ Подтвердить» / «❌ Отмена». «📄 Мои заявки на вывод»: paginated, masked details.
- Approver: notification with «✅ Отметить выплаченной» / «❌ Отклонить»; reject asks optional reason («Без причины»). Admin panel «💸 Заявки на вывод» (approver only). Status transitions are single `UPDATE … WHERE status='pending'` in a transaction; identity + private chat re-checked on every callback/message.
- Requester notified on paid («отмечена выплаченной», honest wording) and on reject (refund to balance).

## Privacy
Full details only to the requester (own preview) and the approver (pending request). After resolution details are replaced by a mask keeping the last 4 characters. Never logged. Lists show masks.
