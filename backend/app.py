import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from .datadog_client import DatadogConfig, load_config_from_env, query_timeseries, validate
from .admin_routes import router as admin_router


# Load .env if present (local dev). In SaaS we'll replace this with a config store.
load_dotenv()

APP_NAME = "release-ops-control-room-backend"


class HealthResponse(BaseModel):
    ok: bool
    site: str
    reason: Optional[str] = None
    checkedAt: str
    meta: Dict[str, Any] = Field(default_factory=dict)


class QueryItem(BaseModel):
    name: str
    query: str
    windowSeconds: int = 900


class QueryRequest(BaseModel):
    items: List[QueryItem]


class QueryResult(BaseModel):
    name: str
    ok: bool
    reason: Optional[str] = None
    last: Optional[Dict[str, Any]] = None


class QueryResponse(BaseModel):
    ok: bool
    site: str
    checkedAt: str
    results: List[QueryResult]


def get_cfg() -> DatadogConfig:
    cfg = load_config_from_env()
    return cfg


app = FastAPI(title=APP_NAME)

# CORS for local UI (http://localhost:8000)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:8000",
        "http://127.0.0.1:8000",
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ],
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"]
)

app.include_router(admin_router)


@app.get("/api/health")
def health() -> Dict[str, Any]:
    return {"ok": True, "service": APP_NAME}


@app.get("/api/datadog/health", response_model=HealthResponse)
def datadog_health() -> HealthResponse:
    cfg = get_cfg()
    ts = datetime.now(tz=timezone.utc).isoformat()

    ok, reason, meta = validate(cfg)

    return HealthResponse(
        ok=ok,
        site=cfg.site,
        reason=None if ok else reason,
        checkedAt=ts,
        meta=meta or {},
    )


@app.post("/api/datadog/query", response_model=QueryResponse)
def datadog_query(req: QueryRequest) -> QueryResponse:
    cfg = get_cfg()
    ts = datetime.now(tz=timezone.utc).isoformat()

    # Ensure credentials look present
    if not cfg.api_key or not cfg.app_key:
        raise HTTPException(status_code=400, detail="Missing DD_API_KEY/DD_APP_KEY")

    results: List[QueryResult] = []

    for item in req.items:
        ok, reason, payload = query_timeseries(cfg, item.query, window_seconds=item.windowSeconds)
        last = None
        if ok:
            last = payload.get("last")
        results.append(
            QueryResult(
                name=item.name,
                ok=ok,
                reason=None if ok else reason,
                last=last,
            )
        )

    overall_ok = all(r.ok for r in results) if results else True

    return QueryResponse(
        ok=overall_ok,
        site=cfg.site,
        checkedAt=ts,
        results=results,
    )
