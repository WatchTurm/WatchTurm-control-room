# Snapshot – WatchTurm Control Room

Produces `data/latest.json` and `data/release_history/*.jsonl` from GitHub, TeamCity, and Jira APIs. Used by the release management dashboard.

**Developed by Mateusz Zadrożny** • [WatchTurm on LinkedIn](https://www.linkedin.com/company/watchturm)

## Setup

1. Copy `.env.example` from repo root to `MVP1/snapshot/.env`.
2. Fill in `GITHUB_TOKEN`, `JIRA_*`, `TEAMCITY_*`.
3. `pip install -r requirements.txt`
4. Run: `python snapshot.py` or use `snapshot_api_server.py` for live API.

## Output

- `../data/latest.json` – current state
- `../data/release_history.jsonl` – append-only history (if configured)
