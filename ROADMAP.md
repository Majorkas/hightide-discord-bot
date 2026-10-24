# Roadmap — Discord Events Bot

Working memory for the build. Plan: `docs/superpowers/plans/2026-10-23-discord-events-bot.md`. Spec: `docs/superpowers/specs/2026-10-23-discord-events-bot-design.md`.

## Milestone 1 — Foundation ✅ (complete 2026-10-23)

- [x] Task 1: Project scaffold + config
- [x] Task 2: Database models + session (incl. tz-aware `TZDateTime` round-trip on SQLite)
- [x] Task 3: Repositories
- [x] Task 4: Bot entry point + `/config` and `/source` commands
- [x] 4-agent council + fixes (guild_only, session lifecycle, input validation, board uniqueness, coverage: 56 tests green, 0 warnings)

## Milestone 2 — Scraper ✅ (complete 2026-10-23)

- [x] Task 5: Normalization helpers (dates, price, fingerprint) — incl. invalid-date guard
- [x] Task 6: WP REST fetcher + product parser — incl. summary/title-dedupe extraction, From-prefix time overlay
- [x] Task 7: Event service (upsert/dedupe/stale) + `/board preview` — incl. embed limit guards (≤25 fields / ≤1024 / ≤6000)
- [x] 4-agent council + fixes (embed injection defense, guard collapse indicator, fetcher hardening, fingerprint change detection, per-product isolation; 163 tests green, 0 warnings)

## Milestone 3 — Board system ✅ (complete 2026-10-23)

- [x] Task 8: Board service (windowing, hashing, create/edit) — incl. deleted-message recovery on skip path, channel-move handling, fuller render hash
- [x] Task 9: `/board refresh`, `/board recreate` + daily scheduler loop — incl. off-loop fetch, error-isolated steps, per-board concurrency lock
- [x] 4-agent council + fixes (reliable trigger + bounded retry, Forbidden handling, recreate hardening; 227 tests green, 0 warnings)

## Milestone 4 — Month rollover ✅ (complete 2026-10-23)

- [x] Task 10: Month rollover + previous-month deletion — incl. stored-channel targeting, authorship + guild guards, save-before-delete ordering
- [x] 4-agent council + fixes (fetch-once-per-day retry semantics + backoff, rollover failure-path hardening; 265 tests green, 0 warnings)

## Milestone 5 — Hardening ✅ (complete 2026-10-23)

- [x] Task 11: `/events upcoming` user command + full test pass (incl. deferred setup_hook startup smoke test)
- [x] Task 12: README + .env.example + roadmap polish
- [x] 4-agent council + final whole-branch review + fix wave (289 tests green, 0 warnings)

**Status: COMPLETE — merge-ready.** 12/12 tasks, 5 milestone councils, final whole-branch review passed.

---

# v2 — Community Features

Plan: `docs/superpowers/plans/2026-10-24-community-features.md`. Spec: `docs/superpowers/specs/2026-10-24-community-features-design.md`.

## v2 Milestone 1 — Roles foundation ✅ (complete 2026-10-24)

- [x] Task 1: Role tables + DDL + repositories
- [x] Task 2: `/roles` admin commands + persistent panel + toggle
- [x] 4-agent council + fixes (role vetting, sqlite_autoincrement, defer-first, panel-id consistency; 622 tests green)

## v2 Milestone 2 — Event pings ✅ (complete 2026-10-24)

- [x] Task 3: Keyword matching + first-seen ping pipeline
- [x] 4-agent council + fixes (pending-pings scan, ping caps, bootstrap silence, whitespace-folded matching; 675 tests green)

## v2 Milestone 3 — Daily tonight post ⬜

- [ ] Task 4: "Today at Hightide" post + config
- [ ] 4-agent council + fixes

## v2 Milestone 4 — Sign-ups ⬜

- [ ] Task 5: Signup table + service
- [ ] Task 6: Signup buttons on week cards + admin list
- [ ] 4-agent council + fixes

## v2 Milestone 5 — Deck + card ⬜

- [ ] Task 7: `/card` Scryfall lookup
- [ ] Task 8: `/deck` Moxfield + Archidekt
- [ ] 4-agent council + fixes

## v2 Milestone 6 — Wants board ⬜

- [ ] Task 9: `/want` commands + board + browse
- [ ] 4-agent council + fixes

## v2 Milestone 7 — New-product alerts ⬜

- [ ] Task 10: Watched-category alerts
- [ ] 4-agent council + fixes

## v2 Milestone 8 — Hardening + deploy ⬜

- [ ] Task 11: Permissions/README/full pass
- [ ] Final whole-branch review
- [ ] Fly deploy
