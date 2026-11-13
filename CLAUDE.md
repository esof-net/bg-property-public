# Working in this repo (esof-net/bg-property-public)

Read this before touching GitHub Actions or the shared working directory - both have caused real, user-visible problems this session.

## Never iterate by repeatedly dispatching a live GitHub Actions workflow

Every failed `workflow_dispatch` run sends the repo owner a failure email. A "dispatch it, watch it fail, patch one line, dispatch again" debugging loop is genuinely disruptive to them even when each individual run is harmless - this happened for real (5 failed runs of one diagnostic script in ~35 minutes, all from iterating live instead of validating first) and the owner had to tell us directly to stop.

Before any live dispatch of a new or changed script:
- Read it end to end for the failure modes you can already see. The specific bugs that caused real failures this session were all things a careful read would have caught: an unhandled HTTP error with no response body logged (so the first failure taught nothing), a slow aggregate query (`count=exact`) with no statement-timeout fallback, a column referenced that doesn't exist in production yet.
- Dry-run or syntax-check whatever you can locally first.
- Get it right before it touches production Actions - one live dispatch that works beats five that iterate you there.

If a live dispatch still fails despite that care, fix the *specific* confirmed cause from its actual logs before dispatching again - don't treat redispatch itself as a debugging step.

This applies to every agent working here, not just one role - general-purpose builders have caused this as often as named agents.

## This working directory is shared with other concurrently-running agents

More than one agent session can be operating in `/home/user/bg-property-public` (or wherever this repo is checked out) at the same time, each able to see the others' in-progress, uncommitted changes. Before committing or discarding anything:
- Run `git status`/`git log --oneline -5` first. If you see uncommitted changes you didn't make, leave them alone unless they're clearly unrelated and you're only committing your own specific files.
- For anything beyond a trivial docs edit, prefer building in an isolated `git worktree` off `origin/main` (or the relevant branch) rather than working directly in the shared checkout - this has already prevented real collisions this session.
- Never `git add -A` in a shared checkout.

## Nothing ships without Missy's review

Every code change - however small, however confident you are - goes through Missy's review before merging, per each agent's own standing rules. Self-merging is a real mistake that's happened this session (caught and corrected each time, but avoid it in the first place).

## Repo layout

This repo runs the scrape/backfill/sync Actions. The website is not here: it is built from the private repo (`kirilbp/bg-property-tracker`) and pushed to this repo's `gh-pages` branch.

- `apps/scraper/` - all Python: scrapers, backfills, `sync_to_supabase.py`, `data_store.py`, `geo_utils.py`, `commit_and_push.sh`. Workflows call `python apps/scraper/<script>.py`. Run everything from the repo root and use `apps/scraper/paths.py` (`DATA_DIR`) for the data folder.
- `.github/workflows/` - all workflows.
- `data/` - only the static `bg_oblast_boundaries.json` and `bg_settlements_to_oblast.json` are tracked. Everything else in `data/` is git-ignored scraper state.
- Python code and workflow changes come from the private repo by a manual, selective copy by the owner.

## Scraper data lives in the `data-store` release, not in git

Committing `data/` pushed the old repo (`esof-net/bg-property-collector`) over GitHub's size quota. Now:
- `history*`, `leads*`, `*_grid_state.json`, `geocode_cache.json`, `home_stats.json`, `area_index.json` are assets of the GitHub Release tagged `data-store` in this repo, moved by `python apps/scraper/data_store.py pull|push` (aliases like `@homes`; see its docstring).
- Every workflow pulls what it needs, runs, uploads a `data-backup-<run id>` Actions artifact (7-30 days), then pushes. If another workflow changed a file meanwhile, `push` merges history files and keeps the remote copy of derived ones.
- Never `git add data/` and never commit generated data. Locally, `pull --all` is safe (public, no token); do not run `push` locally against production.
- Needs: Actions workflow permissions "Read and write", and the release created once (`gh release create data-store ...`).
