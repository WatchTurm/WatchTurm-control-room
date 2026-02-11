# Deployment Guide - WatchTurm Control Room

## Co to jest "dokumentacja deploymentu"?

To instrukcje jak **wdrożyć aplikację WatchTurm Control Room na serwer produkcyjny** (nie deploymenty aplikacji, które aplikacja śledzi, tylko deployment samej aplikacji).

---

## Wymagania

- Python 3.8+
- Node.js (opcjonalnie, dla frontend build tools)
- Reverse proxy (nginx/Apache) - opcjonalnie
- Systemd lub PM2 (dla uruchamiania jako service)

---

## Krok 1: Przygotowanie serwera

### 1.1 Instalacja zależności

```bash
# Backend dependencies
cd MVP1/snapshot
pip install -r requirements.txt
```

### 1.2 Konfiguracja zmiennych środowiskowych

Utwórz plik `.env` w `MVP1/snapshot/`:

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
```

(Open-source edition uses GitHub, TeamCity, Jira only. See `.env.example` in repo root.)

---

## Krok 2: Uruchomienie API Server

### 2.1 Bezpośrednie uruchomienie (development/testing)

```bash
cd MVP1/snapshot
python snapshot_api_server.py --port 8001 --interval 30
```

### 2.2 Jako systemd service (produkcja)

Utwórz plik `/etc/systemd/system/release-ops-api.service`:

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

Aktywacja:

```bash
sudo systemctl enable release-ops-api
sudo systemctl start release-ops-api
sudo systemctl status release-ops-api
```

### 2.3 Z PM2 (alternatywa)

```bash
npm install -g pm2
cd MVP1/snapshot
pm2 start snapshot_api_server.py --name release-ops-api --interpreter python3 -- --port 8001 --interval 30
pm2 save
pm2 startup
```

---

## Krok 3: Frontend

### 3.1 Statyczne pliki

Frontend to statyczne pliki HTML/JS/CSS. Możesz:

**Opcja A: Prosty HTTP server (Python)**

```bash
cd web
python start_local_server.py --port 8080
```

**Opcja B: Nginx (produkcja)**

Konfiguracja `/etc/nginx/sites-available/release-ops`:

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

    # Serve data files
    location /data {
        alias /path/to/release-ops-control-room-main/data;
    }
}
```

Aktywacja:

```bash
sudo ln -s /etc/nginx/sites-available/release-ops /etc/nginx/sites-enabled/
sudo nginx -t
sudo systemctl reload nginx
```

**Opcja C: Apache**

Konfiguracja wirtualnego hosta:

```apache
<VirtualHost *:80>
    ServerName your-domain.com
    DocumentRoot /path/to/release-ops-control-room-main/web

    <Directory /path/to/release-ops-control-room-main/web>
        Options Indexes FollowSymLinks
        AllowOverride All
        Require all granted
    </Directory>

    # Proxy API
    ProxyPass /api http://localhost:8001/api
    ProxyPassReverse /api http://localhost:8001/api
</VirtualHost>
```

---

## Krok 4: Konfiguracja API Base URL

W produkcji, jeśli frontend i API są na różnych domenach, ustaw w `web/index.html`:

```html
<script>
  window.SNAPSHOT_API_BASE = "https://api.your-domain.com";
</script>
<script src="app.js"></script>
```

Lub skonfiguruj reverse proxy (jak wyżej) żeby `/api` proxy'owało do backendu.

---

## Krok 5: Pierwszy snapshot

Po uruchomieniu API server, pierwszy snapshot uruchomi się automatycznie po interwale (domyślnie 30 minut).

Możesz też uruchomić ręcznie:

```bash
cd MVP1/snapshot
python snapshot.py
```

Lub przez API:

```bash
curl -X POST http://localhost:8001/api/snapshot/trigger
```

---

## Krok 6: Monitoring

### Logi

- API server: sprawdź output systemd/PM2
- Snapshot: logi w `MVP1/snapshot/` (jeśli skonfigurowane)

### Health check

```bash
curl http://localhost:8001/health
```

### Status snapshot

```bash
curl http://localhost:8001/api/snapshot/status
```

---

## Troubleshooting

### API server nie startuje

- Sprawdź czy port 8001 jest wolny: `netstat -tuln | grep 8001`
- Sprawdź logi: `journalctl -u release-ops-api -f`
- Sprawdź `.env` - czy wszystkie wymagane zmienne są ustawione

### Frontend nie łączy się z API

- Sprawdź CORS w `snapshot_api_server.py` (powinno być `Access-Control-Allow-Origin: *`)
- Sprawdź czy API server działa: `curl http://localhost:8001/health`
- Sprawdź konfigurację `SNAPSHOT_API_BASE` w frontend

### Snapshot nie działa

- Sprawdź czy wszystkie integracje są skonfigurowane (GitHub, TeamCity, etc.)
- Sprawdź logi snapshot: `python snapshot.py` (uruchom ręcznie)
- Sprawdź czy `data/` directory istnieje i jest zapisywalne

---

## Bezpieczeństwo

### Produkcja

1. **Nie używaj `Access-Control-Allow-Origin: *` w produkcji**
   - Skonfiguruj konkretne domeny w `snapshot_api_server.py`

2. **Chroń `.env`**
   - Upewnij się że `.env` nie jest w git
   - Ustaw odpowiednie uprawnienia: `chmod 600 .env`

3. **HTTPS**
   - Użyj Let's Encrypt dla SSL
   - Skonfiguruj nginx/Apache do HTTPS

4. **Firewall**
   - Otwórz tylko port 80/443 (frontend)
   - API (8001) powinno być dostępne tylko przez reverse proxy

5. **SSO / uwierzytelnianie**
   - Aplikacja nie ma wbudowanego logowania. Postaw ją za reverse proxy z SSO (oauth2-proxy, Keycloak Gate).
   - Szablon konfiguracji: [SSO_SETUP.md](SSO_SETUP.md)

---

## Backup

### Dane

Backup katalogu `data/`:

```bash
tar -czf release-ops-backup-$(date +%Y%m%d).tar.gz data/
```

### Konfiguracja

Backup plików konfiguracyjnych:

```bash
tar -czf release-ops-config-$(date +%Y%m%d).tar.gz MVP1/snapshot/.env MVP1/snapshot/configs/
```

---

## Aktualizacja

1. Zatrzymaj service: `sudo systemctl stop release-ops-api`
2. Zrób backup (jak wyżej)
3. Pobierz nową wersję kodu
4. Zaktualizuj zależności: `pip install -r requirements.txt`
5. Uruchom ponownie: `sudo systemctl start release-ops-api`

---

## Wsparcie

W razie problemów:
- Sprawdź logi systemd/PM2
- Sprawdź logi nginx/Apache
- Uruchom snapshot ręcznie dla debugowania
- Sprawdź `PRODUCTION_READINESS_AUDIT.md` dla znanych problemów
