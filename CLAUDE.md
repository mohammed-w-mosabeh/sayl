# CLAUDE.md — Sayl

Sayl is an open-source, IDM-class download manager for Linux (X11 + Wayland) that captures browser downloads WITHOUT a browser extension. The full specification is in `docs/SPEC.md` and the phase checklist is in `docs/ROADMAP.md`. Read both at the start of every session.

## Communication rules (mandatory)

- ALWAYS write to the user in English: replies, questions, summaries, commit messages, PR text, code comments, docs. Do this even when the user writes in Arabic. Understand the Arabic, answer in English.
- The only Arabic allowed is user-facing UI strings in the i18n files (`ar.json`) and the Arabic half of the bilingual README.
- The user prefers that you make opinionated decisions instead of asking. Pick the most robust option, state it in one line, and continue. Ask only before something irreversible (deleting user data, force-pushing, publishing a release, changing the public API after release).
- Keep progress reports short: what was done, decisions, test results, what is next.

## Git workflow (you own all of it)

You handle every git and GitHub operation yourself. The user should never have to type a git command.

1. **First session:** if the folder is not a repo, run `git init -b main`, add `.gitignore`, make the first commit `chore: initial project scaffold`. Do NOT change the user's global `user.name`/`user.email`; if they are unset, stop and tell the user the two commands to set them.
2. **Remote:** if `gh auth status` succeeds and there is no `origin`, create the repo with `gh repo create sayl --private --source=. --remote=origin` and push `main`. If `gh` is not authenticated, keep working locally and remind the user once at the end of the session.
3. **Branches:** one branch per phase: `phase/<n>-<slug>` (e.g. `phase/1-daemon-core`). Small fixes outside a phase: `fix/<slug>`. Never commit directly to `main` after the initial commit.
4. **Commits:** Conventional Commits (`feat:`, `fix:`, `refactor:`, `test:`, `docs:`, `chore:`, `ci:`, `build:`), with an optional scope (`feat(aria2): ...`). Commit after every logical unit of work that passes lint and tests. Small, focused commits. Imperative mood, subject ≤ 72 chars, body explains *why* when not obvious.
5. **Before every commit:** run `ruff check`, `ruff format --check`, `mypy`, and the relevant `pytest` (and `pnpm lint` / `pnpm typecheck` for the desktop app). Do not commit failing code. Never use `--no-verify`.
6. **End of a phase:** update `docs/ROADMAP.md` checkboxes and `CHANGELOG.md`, push the branch, open a PR with `gh pr create` (title = phase name, body = summary, decisions, test results, known limitations). Merge with `gh pr merge --squash --delete-branch` only after all checks pass, then `git switch main && git pull`. Tag milestones: `v0.1.0` after Phase 2, `v0.2.0` after Phase 5, `v1.0.0` after Phase 7.
7. **Never:** force-push `main`, rewrite published history, commit secrets (`.env`, tokens, CA keys, cookies, `*.session`), commit downloaded test files or large binaries, or commit generated build output.
8. If a commit or push fails, diagnose and fix the cause; do not bypass hooks or protections.

## Engineering rules

- Python 3.12, managed with `uv`. Desktop app with `pnpm`.
- Type hints everywhere, `mypy --strict`, `ruff` clean, >80% coverage on daemon logic.
- No blocking calls on the asyncio event loop. Graceful shutdown on SIGTERM.
- Every subprocess uses an argument list, never `shell=True`.
- All local services bind to `127.0.0.1` only. The API requires the bearer token.
- Never log request/response bodies, cookies, or auth headers.
- Never implement DRM circumvention.
- Work one phase at a time, in order. Do not start the next phase until the current one meets its acceptance criteria in `docs/SPEC.md`.

## Commands (keep this section updated as the project grows)

```bash
uv sync                      # install Python deps
uv run pytest                # tests
uv run ruff check . && uv run ruff format --check .
uv run mypy daemon cli
uv run sayld                 # run the daemon
uv run sayl doctor           # check aria2c, ffmpeg, JS runtime, permissions
cd desktop && pnpm install && pnpm tauri dev
```
