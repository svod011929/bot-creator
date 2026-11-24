# Правила для ИИ / AI rules

Перед любой правкой прочитайте этот файл целиком. Базируйтесь только на актуальном `main`.
Возвращайте только изменённые и новые файлы или единый diff. Не удаляйте существующие тесты.
Не меняйте цены, проценты, финансовые правила, рассылки и схему базы без явного разрешения владельца.
Секреты, токены, ключи, пароли, `.env`, базы, логи, `creator.config` и данные сервера в архив и в ответы не кладите.

Проверка: `pytest` (плюс `ruff check` по затронутым файлам). Число тестов не должно упасть, прежние тесты обязаны остаться.

## Что нельзя возвращать (регрессии)

- Убирать скрытую реферальную ссылку [LavePay](https://t.me/laveshka?startapp=ref_p8lzxfvu) или показывать её как голый URL. В текстах это текстовая гиперссылка (`<a href="…">LavePay</a>`), не открытый адрес. Тесты: `tests/test_help_contacts.py`, `tests/test_lavepay.py`, `tests/test_child_lavepay_payouts.py`. Константа: `creator/lavepay_signup.py` (`LAVEPAY_SIGNUP_URL`).
- Удалять тесты PDF-документов (`test_legal_pdf_files_present`, `test_documents_menu_and_pdf` в `tests/test_help_contacts.py`) или сами PDF в `creator/legal_files/`.
- Удалять проверку, что ключи MGRAM не попадают в дочерний процесс (`MGRAM_PARTNER_API_KEY` и `MGRAM_HUB_API_KEY` не входят в `CHILD_ENV_KEYS`; тесты `tests/test_mgram_factory.py`, `tests/test_child_lavepay_payouts.py`, `tests/test_supervisor.py`, `tests/test_mgram_microtasks.py`, также `tests/test_mgram.py`, `tests/test_shared_worker.py`).
- Передавать дочернему процессу секреты конструктора: токен конструктора, ключи LavePay, ключ выводов, секреты вебхуков, `creator.config`, `MGRAM_PARTNER_API_KEY`, `MGRAM_HUB_API_KEY`. Для заданий MGRAM в микрозадачах дочерний бот получает только путь к локальному сокету и свой прокси-токен. Дочернему процессу разрешён только белый список `CHILD_ENV_KEYS`.
- Начислять награду за задание MGRAM, если нет положительного подтверждения `POST /tasks/check` со статусом задания `completed`. Исчезновение задания из списка, `skip: true` без этой записи, ошибки API, таймауты и пустые ответы начислением не считаются. Начисление — один раз, атомарно, через `credit_mgram_task`. Тесты: `tests/test_mgram_microtasks.py`.
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
- Менять цены и финансовые правила без разрешения владельца.

Кнопка «Перезапустить все боты» в админке («Инструменты») уже есть и требует подтверждения — не удаляйте её.

## English (short)

Base work on current `main` only. Return a diff or the changed/new files. Keep every existing test, and keep the suite green. Do not put secrets, `.env`, databases, logs or `creator.config` in your return.

Do **not** reintroduce these regressions: drop or bare-URL the hidden [LavePay](https://t.me/laveshka?startapp=ref_p8lzxfvu) referral hyperlink (or delete its tests); delete PDF/legal tests or the test that MGRAM keys never enter the child (`MGRAM_PARTNER_API_KEY` and `MGRAM_HUB_API_KEY`); credit a MGRAM microtask without a positive `/tasks/check` `completed` status, or on a disappeared task / error / timeout / empty response; put «📚 Контент ботов» back on the main menu (the section stays reachable by typing the name); break the `bot_stats` empty-state strings or remove `disable_web_page_preview`; replace `worker` / `supervisor` / webhook tests with stale copies; add a default-off news/rewards flag; turn the subscription gate on when unset; credit idea rewards with a raw `UPDATE` instead of atomic `idea_accept` + `change_balance`; send news only to a public username (use `channel_dest`); overwrite the README with old handoff files; weaken the child command menu (`/admin` is owner-only); remove `setMySettings` autoconfig; leak creator secrets into the child process; change prices or financial rules without the owner’s approval.
