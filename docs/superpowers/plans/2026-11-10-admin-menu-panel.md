# Admin menu/panel — plan

- [x] Task 1: failing tests `tests/test_admin_panel.py` (visibility, authorization, navigation, read-only).
- [x] Task 2: `main_kb(admin)` + `BTN_ADMIN`; `/start` and `/cancel` pass admin flag; middleware resets state for admin button only for admins.
- [x] Task 3: admin filter requires private chat; button handler; overview; paging; bot lookup; cross-links; malformed-ID guards.
- [x] Task 4: full regression, Ruff, compileall, dry-run, diff-check, financial/child invariance, secret scan.
- [ ] Task 5: push branch, focused PR, code-only release with backups and two health checks.

Verification: 804 full tests passed (796 + 8 new), Ruff/compileall/offline dry-run/diff-check passed, 40 invariance checks (child, DB, billing, payments, provider services, existing admin mutation handlers) unchanged. Release evidence is kept privately; process health is not live user validation.
