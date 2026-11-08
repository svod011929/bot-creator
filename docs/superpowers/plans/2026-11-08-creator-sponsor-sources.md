# Creator Sponsor Sources Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans task-by-task. Execution delegated; no repeated document approval.

**Goal:** Управлять двумя ключами источника креатора и использовать их в прежнем автоматическом fallback.
**Architecture:** `creator/sponsor_sources.py` владеет bounded/atomic storage и precedence. `creator/handlers/sponsor_sources.py` — private admin FSM/confirmation/RAM. `creator/integrations.py` — lazy creation/lifecycle existing sync services; provider approve только выбирает источник.
**Tech Stack:** Python, aiogram3, SQLite, pytest, Linux atomic replace/flock.

## Global Constraints
Нет provider/live Telegram тестов, новых финансовых правил, миграции DB, массовой ротации, C4, секрета в FSM/тексте/логах. Новая публикация — только allowlist source snapshot без старой истории/данных/операционных заметок.

### Task 1: Source store и приоритет
Files: create `creator/sponsor_sources.py`, `tests/test_sponsor_sources.py`; modify `creator/config.py`.
Interfaces: `SourceStore(cfg, provider)`, `.resolve()->Source(key, origin)`, `.revision()->str`, `.save(key, expected)->None`, `.remove(expected)->None`, `SourceError`; `Source.key` repr=False; `.manual_path` public path for fixtures. `configured(cfg,provider)->bool`.
- [x] RED: parameterized tests both providers assert empty origin unset, manual save/resolution0600, env precedence, legacy cache fallback, invalid file failclosed and no mutation.
```python
store = SourceStore(cfg, 'tgrass')
revision = store.revision()
store.save('example-source-key', revision)
assert store.resolve().origin == 'manual'
assert store.manual_path.stat().st_mode & 0o777 == 0o600
```
- [x] Run `python -m pytest tests/test_sponsor_sources.py -q`, confirm missing store/API failure.
- [x] Implement bounded nofollow read, explicit safe SourceError, private directory/lock, CAS, temp/fsync/replace and unlink manual only. Env lock prevents ignored UI changes. Validate 8..512 ASCII token characters, reject Telegram bot tokens.
- [x] Run targeted tests including monkeypatched replace failure and stale revision. Commit store + tests.

### Task 2: Runtime selection without finance changes
Files: modify `creator/tgrass.py`, `creator/piarflow.py`, `creator/supervisor.py`, `creator/__main__.py`; create `creator/integrations.py`; extend store tests.
Interfaces: `ensure_services(ctx)->None` constructs enabled/matching saved-record services without network; `wait_and_sync(ctx, name)` waits until available then runs unchanged service.sync_loop. Services `.ensure_hub_key()` resolves source afresh under async lock, auto-registers only when credential exists, rechecks after await before writing old autohub cache atomically.
- [x] RED: pending record plus manual key without factory → approve relay without calling FakeClient registration; failed factory uses manual source; absent both remains pending with unchanged balance/config; rotation leaves approved snapshot untouched and re-enable uses next source.
```python
cfg.tgrass_factory_token = ''
store.save('example-source-key', store.revision())
await ctx.tgrass.approve(bot_id, admin_id)
assert db.tgrass(bot_id)['mode'] == 'relay'
```
- [x] Run test file; implement only this selection/lifecycle scope. Preserve sync/credit/reward/payment AST, no DB schema changes. Safe typed errors instead of raw provider texts in affected paths.
- [x] Run existing TGrass/PiarFlow/supervisor tests and new tests, commit runtime.

### Task 3: Admin settings and secret entry
Files: create `creator/handlers/sponsor_sources.py`; modify `creator/handlers/admin.py`, `creator/handlers/__init__.py`, `creator/handlers/common.py`, `creator/services.py`; add fake Dispatcher tests.
Interfaces: `SourceEdit.key/confirm/delete` states; `src:home`, `src:{tg|pf}:view|edit|remove|save:nonce|drop:nonce|back`; no keys in callbacks. RAM on ctx `source_inputs` repr=False, fixed cap/TTL; state only nonce/provider/revision. Handler guards admin/private/current state before write, recheck after Telegram await; store synchronous CAS final write.
- [x] RED: `/admin` shows sources even when no factories; opening read-only; edit then secret message deleted without echo; save needs nonce; stale/foreign/group/other FSM denied, cancel/start cleans candidate; delete confirmation; no DB/config financial changes.
- [x] Implement exact copy/spec and bounded callback parsing. On env key, refuse edit/remove. On failed save keep existing file, consume confirmation, show constant retry guidance. Candidate freed on success/cancel/expiry/eviction.
- [x] Run fake integration tests + full regression. Commit UI and README instructions.

### Task 4: Review, publication and optional safe release
- [ ] Run complete pytest/Ruff/compileall/offline dry-run/diff-check; compare unchanged financial AST and child source bytes to baseline.
- [ ] Gitleaks scan tree + all new Git history with redacted reports outside checkout; manual filename/PII/operational-identifier review; inspect staged/committed file list.
- [ ] One ordinary push main+feature; open requested PR via gh. No forced updates or private history upload.
- [ ] Fresh production drift/resource/time/process preflight; release only if safe. Private backups all actual SQLite/code/env/config/unit, quiescent pause, atomic narrow runtime update preserving config/secret/data; repeat checksum/data/process/fresh-log checks. Any genuine unsafe condition blocks deployment only.
- [ ] Record private release evidence, update same roadmap Page, keep C4 paused/A1+provider E2E unverified.

Self-review: all spec areas map to tasks1–4; runtime services and FSM signatures explicit, manual/env removal semantics consistent. No unknown provider filter, new finance work, live probes or implicit bulk rollout. Execution stays on isolated feature branch.

Verification checkpoint: 796 full tests and 46 focused tests passed; Ruff, compileall, all-three-template offline dry-run and diff-check passed. Financial/child source invariance checks passed. Production release and publication evidence are recorded separately; no live validation is implied.
