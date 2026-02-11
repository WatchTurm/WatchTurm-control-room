# Data directory

Snapshot generator writes here:

- `latest.json` – main payload (projects, environments, components, integrations)
- `release_history.json` or `release_history/index.json` + `events.jsonl` – tag-change events
- `deployment_history/` – optional deployment events
- `snapshot_progress.json` – in-progress snapshot status

**These files are gitignored.** Run `python MVP1/snapshot/snapshot.py` to generate them.  
See the Setup guide in the app for configuration steps.
