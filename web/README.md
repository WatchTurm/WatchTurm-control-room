# Web UI – WatchTurm Control Room

Frontend for the release management dashboard. Static HTML/CSS/JS, no build step. Data from snapshot (`data/latest.json`, `data/release_history/`). Part of WatchTurm Control Room – release visibility for DevOps.

**Developed by Mateusz Zadrożny** • [WatchTurm on LinkedIn](https://www.linkedin.com/company/watchturm)

## Structure

- `index.html` – main app
- `app.js` – UI logic
- `admin-config.js` – project/groups config (edit to match your setup)
- `styles.css` – styles
- `changelog.html` – release history
- `assets/` – logos, favicons

## Local dev

```bash
python start_local_server.py --port 8080
```

Then open `http://localhost:8080/web/index.html`. Ensure `data/latest.json` exists (run snapshot first).
