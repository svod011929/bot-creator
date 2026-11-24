# Правила для ИИ / AI rules

Снимок исходников конструктора Telegram-ботов **bot-creator**.
Точный коммит этого архива указан в `SNAPSHOT.txt`. Кодовая база — `main` после [PR #46](https://github.com/svod011929/bot-creator/pull/46) и этих инструкций.

Перед любой правкой прочитайте этот файл целиком. Если вы проводите проверку и оптимизацию проекта, затем откройте [`AI_AUDIT_TASK.md`](./AI_AUDIT_TASK.md).

## Как работать

1. Базируйтесь **только на этом снимке**. Не берите код, тесты или настройки из более старых архивов или прошлых возвратов.
2. Возвращайте **только изменённые и новые файлы** или единый diff относительно этого снимка. Не присылайте полный проект «с нуля», если менялась лишь часть файлов.
3. Все существующие тесты должны продолжать проходить. **Не удаляйте и не вычищайте тесты**, даже если они кажутся лишними.
4. Не меняйте цены, проценты, финансовые правила, рассылки и схему базы без явного разрешения владельца.
5. Секреты, токены, ключи, пароли, `.env`, базы, логи, `creator.config` и данные сервера в архив и в ответы не кладите.

Проверка:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
pytest
ruff check creator child tests
```

В текущей версии `main` проходит **1861** тест. После ваших правок число не должно упасть, а все прежние тесты обязаны остаться. На каждый исправленный дефект добавьте тест.

## Что нельзя возвращать (регрессии)

Не делайте этого снова — такие правки будут отклонены:

- Убирать скрытую реферальную ссылку [LavePay](https://t.me/laveshka?startapp=ref_p8lzxfvu) или показывать её как голый URL. В текстах это текстовая гиперссылка (`<a href="…">LavePay</a>`), не открытый адрес. Тесты: `tests/test_help_contacts.py`, `tests/test_lavepay.py`, `tests/test_child_lavepay_payouts.py`. Константа: `creator/lavepay_signup.py` (`LAVEPAY_SIGNUP_URL`).
- Удалять тесты PDF-документов (`test_legal_pdf_files_present`, `test_documents_menu_and_pdf` в `tests/test_help_contacts.py`) или сами PDF в `creator/legal_files/`.
- Удалять проверку, что ключи MGRAM не попадают в дочерний процесс (`MGRAM_PARTNER_API_KEY` и `MGRAM_HUB_API_KEY` не входят в `CHILD_ENV_KEYS`; тесты `tests/test_mgram_factory.py`, `tests/test_child_lavepay_payouts.py`, `tests/test_supervisor.py`, `tests/test_mgram_microtasks.py`, также `tests/test_mgram.py`, `tests/test_shared_worker.py`).
- Передавать дочернему процессу секреты конструктора: токен конструктора, ключи LavePay, ключ выводов, секреты вебхуков, `creator.config`, `MGRAM_PARTNER_API_KEY`, `MGRAM_HUB_API_KEY`. Для заданий MGRAM в микрозадачах дочерний бот получает только путь к локальному сокету и свой прокси-токен. Дочернему процессу разрешён только белый список `CHILD_ENV_KEYS`. Не кладите `mgram_key` / `mgram_hub_api_key` / `mgram_partner_api_key` в `config.json` ребёнка.
- Начислять награду за задание MGRAM, если нет положительного подтверждения `POST /tasks/check` со статусом задания `completed`. Исчезновение задания из списка, `skip: true` без этой записи, ошибки API, таймауты и пустые ответы начислением не считаются. Начисление — один раз, атомарно, через `credit_mgram_task`. Тесты: `tests/test_mgram_microtasks.py`.
- Ломать очередь заявок владельца в шаблоне «Микрозадачи» (просмотр каждой заявки, одобрение и отклонение). Не вставляйте в неё посторонний код.
- Возвращать кнопку «📚 Контент ботов» в главное меню конструктора. Раздел по-прежнему открывается, если набрать это название вручную. Тесты меню в `tests/test_creator_flow.py`, `tests/test_creator_selection.py`, `tests/test_admin_panel.py`, `tests/test_creator_content_ui.py`.
- Ломать строки статистики в `creator/bot_stats.py` (пустые состояния должны склеиваться скобками, иначе вторая фраза не попадёт в ответ). Тесты: `tests/test_bot_stats.py`.
- Снимать `disable_web_page_preview=True` с сообщений, где оно уже стоит. Тесты в `tests/test_help_contacts.py`, `tests/test_lavepay.py`, `tests/test_child_lavepay_payouts.py`.
- Подменять актуальными более старыми копиями `child/worker.py`, `creator/supervisor.py`, `tests/test_shared_worker.py` и тесты вебхуков (`tests/test_unsubscribe_webhooks.py` и связанные).
- Вводить флаг вроде `ENABLE_NEWS_REWARDS` / `enable_news_rewards`, который **по умолчанию выключает** новости и награду за идеи. Новости и награды остаются включёнными; размер награды настраивается отдельно (по умолчанию 50 ₽).
- Включать обязательную подписку конструктора, если настройка не задана. `gate_is_on` истинен только при `gate_enabled == "1"`. Пока ключа нет — подписка выключена. Тесты: `tests/test_gate_news_ideas.py`, `tests/test_gate_private.py`.
- Начислять награду за идею прямым `UPDATE users SET balance = …`. Нужен атомарный `idea_accept` и `change_balance` с видом `idea_reward`, один раз. Тесты: `tests/test_gate_news_ideas.py`.
- Отправлять новости только в публичный канал по `@username`. Для закрытого канала используйте `channel_dest` (username или числовой id).
- Подменять актуальный README служебными файлами старых архивов (`CHANGES.md`, `HANDOFF.md`, `RETURN.md`, `TASK.md`) или откатывать публичный README к короткому английскому черновику.
- Делать меню команд слабее текущего: `/start`, `/profile`, `/withdraw` у всех, `/admin` только у владельца (`install_bot_commands` в `child/core.py`, вызовы из `child/__main__.py` и `child/worker.py`). Тесты: `tests/test_child_commands.py`.
- Убирать автонастройку `setMySettings` при запуске дочернего бота (`apply_bot_settings`). Тесты: `tests/test_child_commands.py`.
- Убирать или ломать админский раздел «⚙️ Настройки» → «🔑 Ключи интеграций» (PR #45). В чат, логи, журнал и ошибки выводятся только статус, источник (env / админка / прежний файл) и маска из последних 4 символов, если ключ достаточно длинный. Полные значения нигде не показываются. Токен конструктора, секрет вебхука LavePay и ключ шифрования выплат владельцев — только просмотр, не редактирование через бота. Приоритет: переменная окружения на сервере (из бота не меняется) → ключ, сохранённый в админке → прежний файл (например `data/mgram_hub.json`). Значения из файла не копируются в атрибуты env конфигурации. Тесты: `tests/test_integration_keys.py`, кнопка `ik:home` в `tests/test_admin_panel.py`.
- Класть ключ хаба MGRAM (или партнёрский ключ) в дочерний бот. Авторегистрация хаб-бота конструктора (PR #46) разрешена только если ключа ещё нет; уже заданный ключ не перезаписывается. Ключ дочернего бота, полученный при подключении к рассылкам, в слот хаба не записывается. Чтение ключа — через тот же резолвер, что и в «Ключи интеграций» (`value(cfg, "mgram_hub")` / `load_hub_key`). Тесты: `tests/test_mgram_hub.py`, `tests/test_mgram_microtasks.py`, `tests/test_mgram.py`.

- Менять цены и финансовые правила без разрешения владельца.
- Списывать хостинг только за один пропущенный день, если накопилось больше. `charge_due_bots` списывает все пропущенные дни за один проход по прежней дневной цене, не больше 30 за цикл, один период — один раз. Если денег не хватает, списываются только полные оплаченные дни, календарь сдвигается только на них, бот останавливается как раньше. Ноль или отрицательная дневная цена календарь двигает, деньги не берёт. В логе: `hosting charge bot=… days=… amount=… left=… ok=…` без секретов. Тесты: `tests/test_db.py`, `tests/test_billing_and_dryrun.py`.
- Откладывать права нового владельца до перезапуска бота. После передачи `apply_changes` сначала пишет `config.json`, фильтры админки читают владельца из файла (`LiveOwnerFilter`). Перезапуск по-прежнему делается, если бот должен работать. Тесты: `tests/test_child_bots.py`.
- Убирать фоновое авто-отклонение просроченных заявок микрозадач. Оно использует те же правила и комментарий «Авто-отклонение по сроку», что и при открытии очереди, раз в 600 секунд. Тесты: `tests/test_microtasks.py`.
- Хранить балансы и награды дочерних ботов дробными числами или менять их прямым `UPDATE users SET balance = <значение>`. Нужны целые минимальные единицы (1 единица = 100 долей), начисление через `add_balance` / `try_spend` / атомарные помощники, настройки и ввод пользователя по-прежнему в обычных единицах, отображение через `child.money.fmt`. Миграция идемпотентна, флаг `child_meta.money_unit`, при несовпадении сумм откатывается. Не запускайте её, пока есть счета LavePay в статусе `received` или открытые платёжные задания. Тесты: `tests/test_child_money.py`.
- Добавлять идею или менять её статус вне транзакции. `idea_add` и `idea_set_status` идут через тот же `tx()`, что и `idea_vote`. Тесты: `tests/test_db.py`.

Кнопка «Перезапустить все боты» в админке («Инструменты») уже есть и требует подтверждения — не удаляйте её.

## Ключи интеграций и хаб MGRAM

Ключи интеграций настраиваются в админке: «⚙️ Настройки» → «🔑 Ключи интеграций».
Не возвращайте ключи конструктора (LavePay, MGRAM, хабы и фабрики TGrass/PiarFlow, ключ выплат, токен бота, секрет вебхука) в дочерние процессы, чат, логи, журнал или архив.

Если у конструктора есть партнёрский ключ MGRAM, но нет ключа хаб-бота, при старте конструктор один раз регистрирует свой бот и сохраняет ключ в защищённое хранилище админки. Задания в шаблоне «Микрозадачи» идут только через локальный прокси конструктора. Если задания не пришли, в логе дочернего бота есть причина, владельцу можно показать короткую подсказку — без секретов.

## English (short)

This zip is a clean snapshot of `main` after [PR #46](https://github.com/svod011929/bot-creator/pull/46); the exact commit is in `SNAPSHOT.txt`. Base all work on **this** tree only, never on older archives. If you are auditing and fixing the project, also read [`AI_AUDIT_TASK.md`](./AI_AUDIT_TASK.md). Return only a diff or the changed/new files. Keep every existing test, and keep the suite green (`pytest`; **1861** tests pass on current `main`). Do not put secrets, `.env`, databases, logs or `creator.config` in your return.

Do **not** reintroduce these regressions: drop or bare-URL the hidden [LavePay](https://t.me/laveshka?startapp=ref_p8lzxfvu) referral hyperlink (or delete its tests); delete PDF/legal tests or the test that MGRAM keys never enter the child (`MGRAM_PARTNER_API_KEY` and `MGRAM_HUB_API_KEY`); credit a MGRAM microtask without a positive `/tasks/check` `completed` status, or on a disappeared task / error / timeout / empty response; put «📚 Контент ботов» back on the main menu (the section stays reachable by typing the name); break the `bot_stats` empty-state strings or remove `disable_web_page_preview`; replace `worker` / `supervisor` / webhook tests with stale copies; add a default-off news/rewards flag; turn the subscription gate on when unset; credit idea rewards with a raw `UPDATE` instead of atomic `idea_accept` + `change_balance`; send news only to a public username (use `channel_dest`); overwrite the README with old handoff files; weaken the child command menu (`/admin` is owner-only); remove `setMySettings` autoconfig; leak creator secrets into the child process; remove or weaken «🔑 Ключи интеграций» (masking, env > admin file > legacy, bot token / webhook / payout key stay read-only); put the MGRAM hub key into a child or overwrite an existing hub key; save a child mailing `api_key` as the constructor hub key; charge hosting only one missed day at a time when several are owed (catch up all missed days in one cycle, same per-day price, cap 30, never charge a period twice; partial pay stops the bot as before); leave the new owner locked out until a child restart (live `owner_id` / `LiveOwnerFilter`, and `write_files` before restart); remove the periodic auto-reject of stale microtask claims (same rule and «Авто-отклонение по сроку» comment as the queue, every 600s); store child balances as floats or use a non-atomic `UPDATE users SET balance = <value>` (integer minor units, scale 100, `add_balance` / `try_spend`, `child_meta.money_unit`; do not migrate while invoices are `received` or payment jobs are open); add an idea or change its status outside the same `tx()` as `idea_vote`; change prices or financial rules without the owner’s approval.
