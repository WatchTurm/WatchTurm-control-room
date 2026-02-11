# WatchTurm Control Room – Backend (Datadog proxy)

This small FastAPI service exists for one reason in MVP1:

- **avoid CORS** (UI running in browser cannot call Datadog API directly)
- **keep API/App keys out of the browser**

It only exposes read-only endpoints.

## Configure

Create a `.env` file in the project root **or** export env vars:

- `DD_SITE` – e.g. `datadoghq.com`
- `DD_API_KEY`
- `DD_APP_KEY`

## Run

From the repo root:

```bash
python -m venv .venv
. .venv/bin/activate  # (Windows: .venv\Scripts\activate)

pip install -r backend/requirements.txt
uvicorn backend.app:app --reload --port 8001
```

Then open the UI (default: `http://localhost:8000/web/`).

## Endpoints

- `GET /api/health`
- `GET /api/datadog/health` – validates keys (`/api/v1/validate` + `/api/v1/org`)
- `POST /api/datadog/query` – runs metric queries via `/api/v1/query`

Example query payload:

```json
{
  "items": [
    {"name": "errors", "query": "sum:trace.http.request.errors{env:qa}.as_count()", "windowSeconds": 900}
  ]
}
```
