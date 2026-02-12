 Deployment Guide – WatchTurm Control Room

This document explains how to **deploy the WatchTurm Control Room application itself** to a server (production or staging).  
It does **not** describe the deployments that WatchTurm monitors; it’s about deploying the Control Room.

---

## 1. Requirements

- **Python** 3.8+  
- **Node.js** (optional – only if you want to tweak/build frontend tooling)  
- **Reverse proxy** (nginx / Apache) – recommended for production  
- **Process manager** – `systemd` or `pm2` (to run the API server as a service)

Repository layout (simplified):

- `MVP1/snapshot/` – backend API and snapshot logic  
- `web/` – static frontend (HTML/JS/CSS) for the Control Room  
- `data/` – snapshot JSON files (e.g. `latest.json`)

---

## 2. Backend API Server

The API server is responsible for:

- talking to GitHub / TeamCity / Jira / Datadog / Rancher (where configured)  
- generating and updating snapshot files in `data/`  
- serving snapshot data to the frontend

### 2.1. Install Python dependencies

```bash
cd MVP1/snapshot
pip install -r requirements.txt
```

### 2.2. Environment variables

Create a `.env` file in `MVP1/snapshot/` with the integrations you actually use:

```bash
# GitHub
GITHUB_TOKEN=your_token_here
GITHUB_ORG=your_org

# TeamCity
TEAMCITY_API=https://your-teamcity.com
TEAMCITY_TOKEN=your_token

# Jira
JIRA_API=https://your-jira.atlassian.net
JIRA_EMAIL=your@email.com
JIRA_TOKEN=your_token

# Datadog
DD_API_KEY=your_key
DD_APP_KEY=your_key
DD_SITE=datadoghq.com

# Rancher (optional)
RANCHER_URL=https://your-rancher.com
RANCHER_TOKEN=your_token
```

> The open-source/demo setup typically uses GitHub, TeamCity and Jira.  
> Check any `.env.example` file in the repo for additional options.

### 2.3. Run the API server (development / testing)

```bash
cd MVP1/snapshot
python snapshot_api_server.py --port 8001 --interval 30
```

- `--port` – HTTP port for the API  
- `--interval` – how often to refresh snapshot data (seconds)

Stop with `Ctrl+C` when running in a foreground shell.

### 2.4. Run as a `systemd` service (recommended for production)

Create `/etc/systemd/system/release-ops-api.service`:

```ini
[Unit]
Description=WatchTurm Control Room API Server
After=network.target

[Service]
Type=simple
User=your_user
WorkingDirectory=/path/to/release-ops-control-room-main/MVP1/snapshot
Environment="PATH=/usr/bin:/usr/local/bin"
ExecStart=/usr/bin/python3 snapshot_api_server.py --port 8001 --interval 30
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Enable and start:

```bash
sudo systemctl enable release-ops-api
sudo systemctl start release-ops-api
sudo systemctl status release-ops-api
```

### 2.5. Run with PM2 (alternative)

```bash
npm install -g pm2
cd MVP1/snapshot
pm2 start snapshot_api_server.py --name release-ops-api --interpreter python3 -- --port 8001 --interval 30
pm2 save
pm2 startup
```

This keeps the API alive and restarts it automatically on failure / reboot (after `pm2 startup` is fully configured).

---

## 3. Frontend (web UI)

The UI is a **pure static frontend**:

- `web/index.html` – main Control Room UI  
- `web/app.js`, `web/styles.css` – logic and styles  
- It expects API endpoints (and snapshot JSON files) to be reachable under a predictable URL.

You can serve it in multiple ways.

### 3.1. Simple HTTP server (local / demo)

```bash
cd web
python start_local_server.py --port 8080
```

Then open:

- `http://localhost:8080/` – Control Room UI

Make sure the API server is running and accessible at the URL configured in `web/app.js` (or `data/latest.json` is present if the UI reads snapshots from disk).

### 3.2. nginx (production)

Example config: `/etc/nginx/sites-available/release-ops`:

```nginx
server {
    listen 80;
    server_name your-domain.com;

    root /path/to/release-ops-control-room-main/web;
    index index.html;

    location / {
        try_files $uri $uri/ /index.html;
    }

    # Proxy API requests to backend
    location /api {
        proxy_pass http://localhost:8001;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    }

    # Serve snapshot data if you want it directly
    location /data {
        alias /path/to/release-ops-control-room-main/data;
    }
}
```

Enable site and reload nginx:

```bash
sudo ln -s /etc/nginx/sites-available/release-ops /etc/nginx/sites-enabled/
sudo nginx -t
sudo systemctl reload nginx
```

### 3.3. Apache (alternative)

Virtual host example:

```apache
<VirtualHost *:80>
    ServerName your-domain.com

    DocumentRoot /path/to/release-ops-control-room-main/web

    <Directory /path/to/release-ops-control-room-main/web>
        AllowOverride All
        Require all granted
    </Directory>

    ProxyPass /api http://localhost:8001/api
    ProxyPassReverse /api http://localhost:8001/api

    Alias /data /path/to/release-ops-control-room-main/data
</VirtualHost>
```

Reload Apache:

```bash
sudo systemctl reload apache2
```

---

## 4. Health checks & validation

After deployment, validate:

1. **API health**
   - `curl http://localhost:8001/health` (or the configured health endpoint, if present)  
   - Check logs under `MVP1/snapshot/` or `journalctl -u release-ops-api`.

2. **Snapshot generation**
   - Confirm that `data/latest.json` is being updated periodically.  
   - Inspect a sample file to verify that projects / environments match your expectations.

3. **UI**
   - Open `http(s)://your-domain.com/` in a browser.  
   - Check Overview, Environment view, Release History, Ticket Tracker and Runbooks pages.

---

## 5. Upgrades & deployments

To roll out a new version of WatchTurm Control Room:

1. Pull new code:

```bash
cd /path/to/release-ops-control-room-main
git pull origin main   # or your branch
```

2. Reinstall backend dependencies if `requirements.txt` changed:

```bash
cd MVP1/snapshot
pip install -r requirements.txt
```

3. Restart the API:

```bash
sudo systemctl restart release-ops-api
# or:
pm2 restart release-ops-api
```

4. If frontend files changed, reload nginx/Apache (to clear any caches) and refresh the browser.

---

## 6. Troubleshooting

- **UI loads but shows “no data”**  
  - Check that `data/latest.json` exists and is readable by the web server.  
  - Verify the API can reach GitHub / CI / Jira (correct tokens and URLs).

- **API keeps crashing**  
  - Inspect logs: `journalctl -u release-ops-api` or `pm2 logs release-ops-api`.  
  - Verify all mandatory environment variables are set (especially tokens).

- **CORS / mixed content issues**  
  - Ensure the frontend and backend are served over the same scheme (HTTP/HTTPS) and host, or configure CORS headers on the API if you intentionally separate them.

If something in this guide does not match the current code layout, always trust the **actual repository structure and `snapshot.py` code** first, then adjust the paths/commands accordingly.
