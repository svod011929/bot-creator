# C4 minimal owner stats — plan
- [x] Task 1: failing tests `tests/test_bot_stats.py`.
- [x] Task 2: `creator/bot_stats.py` (read-only aggregate reader + renderer).
- [x] Task 3: `creator/handlers/bot_stats.py` router + card button.
- [x] Task 4: full regression, lint, dry-run, invariance, secret scan.
- [ ] Task 5: push, stacked PR, code-only release, two health checks, roadmap update.

Verification: 809 full tests passed (804 + 5 new); Ruff/compileall/offline dry-run (15 routers)/diff-check passed. Runtime delta: two new modules plus one card button and router registration; all other creator/child files byte-identical to 562d124.
