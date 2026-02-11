# Release History: 60-Day Data & 3-Day Limitation

## Where the "~3 days" limitation came from

1. **Generator (snapshot)**
   - Bootstrap previously scanned only the last **60 commits** per kustomization file (`BOOTSTRAP_COMMITS_TO_SCAN_PER_FILE`), not 60 days.
   - 60 commits often span only a few days when deployments are frequent.
   - Incremental collection only adds events when snapshot runs and sees tag changes; we only have data since snapshot was first run (e.g. a few days of history).
   - **Fix**: Bootstrap now fetches commits until we span **60 days** (`RELEASE_HISTORY_BOOTSTRAP_DAYS`), via paginated GitHub API. We also keep all deduped events in that window (no per-env cap).

2. **UI**
   - Filters (platform, date range) already operated on the full event list.
   - Pagination previously capped at 10 or 30; "Show more" jumped to 30 rather than appending 10.
   - **Fix**: Real "Load more" pagination: show 10 by default, add 10 each time. Filters apply first to the full dataset, then we paginate over the filtered result. Reset to 10 when filters change.

## Changes made

- **Snapshot**: `RELEASE_HISTORY_BOOTSTRAP_DAYS` (default 60), `BOOTSTRAP_MAX_PAGES`, `_commits_spanning_days()`, bootstrap returns events spanning ~60 days. Warnings stored in `index.bootstrapWarnings` if GitHub limits hit.
- **UI**: Pagination over full filtered set (10 default, +10 on "Load more"), date comparison using date-only (YYYY-MM-DD), reset `visibleLimit` to 10 when filters change.

## Backfill for existing data

If you already have release history (e.g. only ~4 days), the 60-day bootstrap runs only when the index is **empty**. To get 60 days without clearing data:

- **One-time backfill**: When `totalEvents > 0` but `oldestEvent` is within the last 60 days, the snapshot runs a **backfill** step: it fetches 60 days of bootstrap events, keeps only those **older** than your current `oldestEvent` and not already in `events.jsonl`, appends them, and updates the index. Then it sets `index["backfill60DaysRun"] = true` so it doesn’t run again.
- Disable backfill: `RELEASE_HISTORY_BACKFILL_60_DAYS=0`.

## Verifying 60 days

- Inspect `data/release_history/index.json`: `stats.oldestEvent` and `stats.newestEvent` should span up to ~60 days after bootstrap or backfill.
- In Release History, set date range to "last 30 days" (or 60): you should see events across that range when present in the JSON.
