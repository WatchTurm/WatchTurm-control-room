import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

import requests


@dataclass
class DatadogConfig:
    site: str
    api_key: str
    app_key: str


def _normalize_site(site: str) -> str:
    s = (site or "").strip().lower()
    s = s.replace("https://", "").replace("http://", "").strip("/")
    if not s:
        s = "datadoghq.com"
    return s


def _base_url(site: str) -> str:
    return f"https://api.{_normalize_site(site)}"


def load_config_from_env() -> DatadogConfig:
    site = os.getenv("DD_SITE", "datadoghq.com")
    api_key = os.getenv("DD_API_KEY", "").strip()
    app_key = os.getenv("DD_APP_KEY", "").strip()
    return DatadogConfig(site=_normalize_site(site), api_key=api_key, app_key=app_key)


def _headers(cfg: DatadogConfig) -> Dict[str, str]:
    h: Dict[str, str] = {}
    if cfg.api_key:
        h["DD-API-KEY"] = cfg.api_key
    if cfg.app_key:
        h["DD-APPLICATION-KEY"] = cfg.app_key
    return h


def validate(cfg: DatadogConfig, timeout: float = 10.0) -> Tuple[bool, str, Dict[str, Any]]:
    """Validate keys by calling Datadog validate endpoint.

    Notes:
    - /api/v1/validate validates the API key.
    - To validate the APP key we call /api/v1/metrics (list metrics). This endpoint normally
      requires a valid application key with `metrics_read` permission.
    """

    if not cfg.api_key:
        return False, "Missing DD_API_KEY", {}
    if not cfg.app_key:
        return False, "Missing DD_APP_KEY", {}

    base = _base_url(cfg.site)
    meta: Dict[str, Any] = {"site": cfg.site, "baseUrl": base}

    try:
        r = requests.get(f"{base}/api/v1/validate", headers=_headers(cfg), timeout=timeout)
        if r.status_code != 200:
            return False, f"validate failed: HTTP {r.status_code}", {**meta, "body": safe_json(r)}
        body = safe_json(r)
        if not body.get("valid"):
            return False, "API key invalid", {**meta, "body": body}
    except requests.RequestException as e:
        return False, f"validate request failed: {e.__class__.__name__}", {**meta, "error": str(e)}

    try:
        r2 = requests.get(f"{base}/api/v1/metrics", headers=_headers(cfg), timeout=timeout)
        if r2.status_code != 200:
            hint = ""
            if r2.status_code == 403:
                hint = " (Forbidden: check DD_SITE matches your org and APP key has metrics_read)"
            return False, f"app key check failed: HTTP {r2.status_code}{hint}", {**meta, "body": safe_json(r2)}
        meta["appKeyOk"] = True
    except requests.RequestException as e:
        return False, f"app key request failed: {e.__class__.__name__}", {**meta, "error": str(e)}

    return True, "ok", meta


def query_timeseries(
    cfg: DatadogConfig,
    query: str,
    window_seconds: int = 900,
    timeout: float = 10.0,
) -> Tuple[bool, str, Dict[str, Any]]:
    if not query:
        return False, "missing query", {}

    now = int(datetime.now(tz=timezone.utc).timestamp())
    _from = now - int(window_seconds)

    base = _base_url(cfg.site)

    try:
        r = requests.get(
            f"{base}/api/v1/query",
            headers=_headers(cfg),
            params={"from": _from, "to": now, "query": query},
            timeout=timeout,
        )
        if r.status_code != 200:
            return False, f"query failed: HTTP {r.status_code}", {"body": safe_json(r)}
        body = safe_json(r)

        series = (body.get("series") or [])
        last_val: Optional[float] = None
        last_ts: Optional[int] = None
        if series:
            pts = series[0].get("pointlist") or []
            for ts, val in reversed(pts):
                if val is None:
                    continue
                last_val = float(val)
                last_ts = int(ts / 1000) if ts and ts > 10_000_000_000 else int(ts)
                break

        return True, "ok", {
            "query": query,
            "from": _from,
            "to": now,
            "last": {"ts": last_ts, "value": last_val},
            "raw": body,
        }
    except requests.RequestException as e:
        return False, f"query request failed: {e.__class__.__name__}", {"error": str(e)}


def safe_json(resp: requests.Response) -> Dict[str, Any]:
    try:
        return resp.json() if resp.content else {}
    except Exception:
        return {"_raw": resp.text}
