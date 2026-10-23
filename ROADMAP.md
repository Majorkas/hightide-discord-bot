# Roadmap — Discord Events Bot

Working memory for the build. Plan: `docs/superpowers/plans/2026-10-23-discord-events-bot.md`. Spec: `docs/superpowers/specs/2026-10-23-discord-events-bot-design.md`.

## Milestone 1 — Foundation ✅ (complete 2026-10-23)

- [x] Task 1: Project scaffold + config
- [x] Task 2: Database models + session (incl. tz-aware `TZDateTime` round-trip on SQLite)
- [x] Task 3: Repositories
- [x] Task 4: Bot entry point + `/config` and `/source` commands
- [x] 4-agent council + fixes (guild_only, session lifecycle, input validation, board uniqueness, coverage: 56 tests green, 0 warnings)

## Milestone 2 — Scraper ⬜

- [ ] Task 5: Normalization helpers (dates, price, fingerprint)
- [ ] Task 6: WP REST fetcher + product parser
- [ ] Task 7: Event service (upsert/dedupe/stale) + `/board preview`
- [ ] 4-agent council + fixes

## Milestone 3 — Board system ⬜

- [ ] Task 8: Board service (windowing, hashing, create/edit)
- [ ] Task 9: `/board refresh`, `/board recreate` + daily scheduler loop
- [ ] 4-agent council + fixes

## Milestone 4 — Month rollover ⬜

- [ ] Task 10: Month rollover + previous-month deletion
- [ ] 4-agent council + fixes

## Milestone 5 — Hardening ⬜

- [ ] Task 11: `/events upcoming` user command + full test pass (incl. deferred setup_hook startup smoke test)
- [ ] Task 12: README + .env.example + roadmap polish
- [ ] 4-agent council + final whole-branch review
