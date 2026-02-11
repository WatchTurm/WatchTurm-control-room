# WatchTurm Control Room - Snapshot
# Copyright 2026 Mateusz Zadrożny. Licensed under Apache-2.0. See LICENSE.

import os
import sys
import json
import base64
import re
import urllib.parse
from pathlib import Path
from datetime import datetime, timezone, timedelta

import requests
import yaml
from dotenv import load_dotenv

# Import file utilities for atomic, thread-safe file operations
# Handle both module import and direct script execution
try:
    from .file_utils import atomic_write_json, safe_read_json, atomic_append_jsonl
    from .logging_utils import logger
except ImportError:
    # Fallback for direct script execution (python snapshot.py)
    from file_utils import atomic_write_json, safe_read_json, atomic_append_jsonl
    from logging_utils import logger


def _env_any(*names: str):
    """Return first non-empty environment variable value from given names."""
    for n in names:
        v = os.getenv(n)
        if v and str(v).strip():
            return str(v).strip()
    return None

from datetime import timedelta
import time


# ------------------------------------------------------------
# API Retry Helper with Exponential Backoff
# ------------------------------------------------------------

def _api_request_with_retry(
    method: str,
    url: str,
    *,
    max_retries: int = 3,
    initial_backoff: float = 1.0,
    max_backoff: float = 60.0,
    timeout: float = 30.0,
    **kwargs
) -> requests.Response:
    """
    Make an API request with automatic retry and exponential backoff.
    
    Handles:
    - Rate limiting (429 status code)
    - Transient network errors
    - Server errors (5xx)
    
    Returns the response or raises the last exception.
    """
    last_exception = None
    
    for attempt in range(max_retries):
        try:
            if method.upper() == "GET":
                response = requests.get(url, timeout=timeout, **kwargs)
            elif method.upper() == "POST":
                response = requests.post(url, timeout=timeout, **kwargs)
            else:
                raise ValueError(f"Unsupported HTTP method: {method}")
            
            # Check rate limit headers (GitHub, Datadog, etc.)
            rate_limit_remaining = response.headers.get("X-RateLimit-Remaining")
            if rate_limit_remaining:
                try:
                    remaining = int(rate_limit_remaining)
                    if remaining < 10:  # Low threshold - warn but don't block
                        logger.warn("rate_limit_low", remaining=remaining, url=url[:100])
                        # Add small delay before next request to avoid hitting limit
                        if remaining < 5:
                            time.sleep(1.0)
                        elif remaining < 10:
                            time.sleep(0.5)
                except (ValueError, TypeError):
                    pass  # Ignore invalid header values
            
            # Handle rate limiting (429)
            if response.status_code == 429:
                # Extract Retry-After header if present
                retry_after = response.headers.get("Retry-After")
                if retry_after:
                    try:
                        wait_time = float(retry_after)
                    except ValueError:
                        wait_time = initial_backoff * (2 ** attempt)
                else:
                    wait_time = min(initial_backoff * (2 ** attempt), max_backoff)
                
                if attempt < max_retries - 1:
                    logger.warn("rate_limit_hit", wait_seconds=wait_time, attempt=attempt+1, url=url[:100])
                    time.sleep(wait_time)
                    continue
                else:
                    # Last attempt - raise the error
                    response.raise_for_status()
            
            # Handle server errors (5xx) - retry
            if 500 <= response.status_code < 600:
                if attempt < max_retries - 1:
                    wait_time = min(initial_backoff * (2 ** attempt), max_backoff)
                    logger.warn("server_error_retry", status=response.status_code, wait_seconds=wait_time, attempt=attempt+1, url=url[:100])
                    time.sleep(wait_time)
                    continue
                else:
                    response.raise_for_status()
            
            # Success or client error (4xx except 429) - don't retry
            return response
            
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            last_exception = e
            if attempt < max_retries - 1:
                wait_time = min(initial_backoff * (2 ** attempt), max_backoff)
                logger.warn("network_error_retry", error=str(e), wait_seconds=wait_time, attempt=attempt+1, url=url[:100])
                time.sleep(wait_time)
                continue
            else:
                raise
        except Exception as e:
            last_exception = e
            if attempt < max_retries - 1:
                wait_time = min(initial_backoff * (2 ** attempt), max_backoff)
                logger.warn("request_error_retry", error=str(e), wait_seconds=wait_time, attempt=attempt+1, url=url[:100])
                time.sleep(wait_time)
                continue
            else:
                raise
    
    # Should never reach here, but just in case
    if last_exception:
        raise last_exception
    raise RuntimeError("API request failed after retries")


# ------------------------------------------------------------
# Data Validation Helpers
# ------------------------------------------------------------

def _validate_required_field(
    data: dict,
    field_name: str,
    *,
    context: str = "",
    warn_func: callable = None,
    payload_warnings: list = None,
) -> tuple[bool, str]:
    """
    Validate that a required field exists and is non-empty.
    
    Returns: (is_valid, error_message)
    """
    value = data.get(field_name)
    if not value or (isinstance(value, str) and not value.strip()):
        error_msg = f"Missing required field: {field_name}"
        if context:
            error_msg += f" (context: {context})"
        
        if warn_func and payload_warnings is not None:
            payload_warnings.append(warn_func(
                level="warning",
                scope="global",
                reason="missing_required_field",
                source="validation",
                message=error_msg,
            ))
        
        return False, error_msg
    return True, ""


def _validate_timestamp(
    timestamp: str,
    *,
    field_name: str = "timestamp",
    context: str = "",
    warn_func: callable = None,
    payload_warnings: list = None,
) -> tuple[bool, datetime | None, str]:
    """
    Validate and parse a timestamp.
    
    Returns: (is_valid, parsed_datetime, error_message)
    """
    if not timestamp or not isinstance(timestamp, str) or not timestamp.strip():
        error_msg = f"Missing timestamp: {field_name}"
        if context:
            error_msg += f" (context: {context})"
        
        if warn_func and payload_warnings is not None:
            payload_warnings.append(warn_func(
                level="warning",
                scope="global",
                reason="missing_timestamp",
                source="validation",
                message=error_msg,
            ))
        
        return False, None, error_msg
    
    parsed = _parse_iso_safe(timestamp)
    if not parsed:
        error_msg = f"Invalid timestamp format: {field_name} = {timestamp}"
        if context:
            error_msg += f" (context: {context})"
        
        if warn_func and payload_warnings is not None:
            payload_warnings.append(warn_func(
                level="warning",
                scope="global",
                reason="invalid_timestamp_format",
                source="validation",
                message=error_msg,
            ))
        
        return False, None, error_msg
    
    return True, parsed, ""


# ------------------------------------------------------------
# Data Normalization Helpers
# ------------------------------------------------------------

def _normalize_env_key(key: str | None) -> str | None:
    """Normalize environment key: empty string -> None, strip, lowercase.
    
    This ensures consistent handling of environment keys throughout the codebase.
    Empty strings are treated as None to prevent false positives in filtering/matching.
    
    Args:
        key: Environment key (can be None, empty string, or any string)
    
    Returns:
        Normalized key (lowercase, stripped) or None if empty
    """
    if not key:
        return None
    key = str(key).strip()
    if not key:
        return None
    return key.lower()


# ------------------------------------------------------------
# Global Warnings (MVP1 Stage 8.1)
# ------------------------------------------------------------

def _warning(*, level: str, scope: str, reason: str, source: str = "", message: str = "", project: str = "", env: str = "", component: str = "") -> dict:
    """Create a normalized warning entry for payload.warnings[].

    Notes:
    - We keep this intentionally small and stable (UI-friendly).
    - It does NOT replace component/env warnings yet; it complements them.
    """
    w = {
        "level": (level or "info").strip(),
        "scope": (scope or "global").strip(),
        "reason": (reason or "unknown").strip(),
        "ts": iso_now(),
    }
    if source:
        w["source"] = source
    if message:
        w["message"] = message
    if project:
        w["project"] = project
    if env:
        w["env"] = env
    if component:
        w["component"] = component
    return w


def datadog_api_base(site: str) -> str:
    """Return Datadog API base url for given site.

    Examples:
    - datadoghq.com -> https://api.datadoghq.com
    - datadoghq.eu  -> https://api.datadoghq.eu
    """
    s = (site or "").strip().lower()
    if not s:
        s = "datadoghq.com"
    if s.startswith("http://") or s.startswith("https://"):
        # allow user to pass full base
        return s.rstrip("/")
    return f"https://api.{s}".rstrip("/")


def datadog_validate(api_key: str, app_key: str, *, site: str) -> tuple[bool, str]:
    """Validate Datadog API + APP key pair.

    Returns: (connected, reason)
    """
    api_key = (api_key or "").strip()
    app_key = (app_key or "").strip()
    if not api_key or not app_key:
        return False, "missing_keys"

    base = datadog_api_base(site)
    url = f"{base}/api/v1/validate"
    try:
        r = requests.get(
            url,
            headers={
                "DD-API-KEY": api_key,
                "DD-APPLICATION-KEY": app_key,
            },
            timeout=20,
        )
        if r.status_code == 200:
            return True, "ok"
        if r.status_code in (401, 403):
            return False, f"auth_{r.status_code}"
        return False, f"http_{r.status_code}"
    except Exception as e:
        return False, f"exception:{type(e).__name__}"


def datadog_query_timeseries(api_key: str, app_key: str, *, site: str, query: str, minutes: int = 5) -> tuple[float | None, str]:
    """Run a Datadog timeseries query and return the latest numeric point.

    Returns: (value_or_none, reason)
    - value_or_none: float if datapoint exists, else None
    - reason:
        - ok
        - no_data
        - missing_keys
        - auth_401 / auth_403
        - http_<status>
        - exception:<Type>

    Notes:
    - We intentionally use /api/v1/query (classic metrics query) because it's
      broadly available across Datadog plans and works for APM-derived metrics too.
    - This is MVP logic: last datapoint across any returned series.
    """
    api_key = (api_key or "").strip()
    app_key = (app_key or "").strip()
    q = (query or "").strip()
    if not api_key or not app_key or not q:
        return None, "missing_keys"

    base = datadog_api_base(site)
    url = f"{base}/api/v1/query"

    now = datetime.now(timezone.utc)
    frm = int((now - timedelta(minutes=minutes)).timestamp())
    to = int(now.timestamp())

    try:
        r = requests.get(
            url,
            headers={
                "DD-API-KEY": api_key,
                "DD-APPLICATION-KEY": app_key,
            },
            params={"from": frm, "to": to, "query": q},
            timeout=25,
        )

        if r.status_code == 200:
            payload = r.json() or {}
            series = payload.get("series") or []
            latest: float | None = None
            for s in series:
                pts = s.get("pointlist") or []
                for p in pts:
                    # pointlist: [ [timestamp_ms, value], ... ]
                    if not isinstance(p, (list, tuple)) or len(p) < 2:
                        continue
                    v = p[1]
                    if v is None:
                        continue
                    try:
                        latest = float(v)
                    except Exception:
                        continue
            if latest is None:
                return None, "no_data"
            return latest, "ok"

        if r.status_code in (401, 403):
            return None, f"auth_{r.status_code}"
        return None, f"http_{r.status_code}"
    except Exception as e:
        return None, f"exception:{type(e).__name__}"


def _dd_subst(template: str, *, env: str, project: str = "") -> str:
    """Very small templating for Datadog queries.

    Supported tokens:
      - $env
      - $project
    """
    t = template or ""
    return t.replace("$env", env).replace("$project", project)


def _dd_norm_pct(v: float | None) -> float | None:
    """Normalize a metric to 0..100 percentage if it looks like 0..1."""
    if v is None:
        return None
    try:
        fv = float(v)
    except Exception:
        return None
    if 0 <= fv <= 1.5:
        return fv * 100.0
    return fv


def _dd_norm_duration_ms(v: float | None) -> float | None:
    """Normalize a duration metric to milliseconds.

    Datadog APM duration metrics are commonly in seconds. We use a heuristic:
    - if value <= 50, treat as seconds and convert to ms
    - otherwise assume ms already
    """
    if v is None:
        return None
    try:
        fv = float(v)
    except Exception:
        return None
    if 0 <= fv <= 50:
        return fv * 1000.0
    return fv


def _dd_join_tags(tags: list[str]) -> str:
    tags = [t.strip() for t in (tags or []) if str(t or "").strip()]
    return ",".join(tags)


def _dd_build_selector_tags(env_selector: dict[str, str] | None, component_selector: dict[str, str] | None = None) -> list[str]:
    """
    Build Datadog tag list from deterministic selectors.
    
    Args:
        env_selector: dict with keys like "namespace", "cluster" (from config envSelectors)
        component_selector: dict with keys like "service", "kube_deployment" (from config componentSelectors)
    
    Returns:
        List of Datadog tag strings (e.g., ["kube_namespace:qa", "kube_cluster_name:prod-cluster"])
    """
    tags = []
    
    if env_selector:
        if "namespace" in env_selector:
            tags.append(f"kube_namespace:{env_selector['namespace']}")
        if "cluster" in env_selector:
            tags.append(f"kube_cluster_name:{env_selector['cluster']}")
    
    if component_selector:
        if "service" in component_selector:
            tags.append(f"service:{component_selector['service']}")
        if "kube_deployment" in component_selector:
            tags.append(f"kube_deployment:{component_selector['kube_deployment']}")
    
    return tags


def _dd_selector_matches_monitor(monitor: dict, env_selector: dict[str, str] | None) -> bool:
    """
    Check if a Datadog monitor matches the environment selector scope.
    
    Returns True if monitor tags match the selector, or if selector is None (no filtering).
    """
    if not env_selector:
        return True  # No selector = match all
    
    monitor_tags = monitor.get("tags", [])
    if not monitor_tags:
        return False  # Monitor has no tags, can't match
    
    # Check namespace match
    if "namespace" in env_selector:
        namespace_tag = f"kube_namespace:{env_selector['namespace']}"
        if namespace_tag not in monitor_tags:
            # Also try without prefix (some monitors use just "namespace:...")
            if f"namespace:{env_selector['namespace']}" not in monitor_tags:
                return False
    
    # Check cluster match (if specified)
    if "cluster" in env_selector:
        cluster_tag = f"kube_cluster_name:{env_selector['cluster']}"
        if cluster_tag not in monitor_tags:
            return False
    
    return True


def datadog_collect_observability(
    api_key: str,
    app_key: str,
    *,
    site: str,
    project_key: str,
    env_key: str,
    env_value: str,
    base_tags: list[str],
    tag_candidates: list[str],
    minutes: int = 10,
    # Backwards/forwards compatibility: callers may pass these (ignored here)
    datadog_cfg: dict | None = None,
    project_name: str | None = None,
    dd_env_map: dict | None = None,
    # NEW: Deterministic selectors (MVP1-safe)
    env_selector: dict[str, str] | None = None,
    component_selector: dict[str, str] | None = None,
) -> tuple[dict, list[dict]]:
    """Collect a small set of popular signals for the Overview "Parameters & logs" tiles.

    If env_selector is provided, uses deterministic selectors (MVP1-safe).
    Otherwise falls back to tag_candidates (legacy behavior for backwards compatibility).
    """

    warnings: list[dict] = []

    # queries are intentionally simple and widely available; we normalize values for UI.
    metric_specs = {
        "cpuPct": {
            "query": "avg:system.cpu.user{$tags}",
            "norm": _dd_norm_pct,
        },
        "memPct": {
            "query": "avg:system.mem.used_pct{$tags}",
            "norm": _dd_norm_pct,
        },
        "pods": {
            "query": "sum:kubernetes.pods.running{$tags}",
            "norm": lambda x: x,
        },
        "errorRatePct": {
            "query": "100 * (sum:trace.http.request.errors{$tags}.as_count() / sum:trace.http.request.hits{$tags}.as_count())",
            "norm": lambda x: x,
        },
        "p95ms": {
            "query": "p95:trace.http.request.duration{$tags}",
            "norm": _dd_norm_duration_ms,
        },
    }

    out: dict = {
        "projectKey": project_key,
        "envKey": env_key,
        "usedTags": None,
        "metrics": {
            "cpuPct": None,
            "memPct": None,
            "pods": None,
            "errorRatePct": None,
            "p95ms": None,
        },
        "meta": {
            "minutes": minutes,
            "tagCandidates": tag_candidates,
            "baseTags": base_tags,
            "deterministic": env_selector is not None,
        },
    }

    # MVP1-safe: Use deterministic selector if provided
    if env_selector:
        selector_tags = _dd_build_selector_tags(env_selector, component_selector)
        if base_tags:
            selector_tags = base_tags + selector_tags
        tags_str = _dd_join_tags(selector_tags)
        
        tmp_metrics = {}
        tmp_reasons = {}
        for k, spec in metric_specs.items():
            q = spec["query"].replace("{$tags}", f"{{{tags_str}}}" if tags_str else "")
            v, reason = datadog_query_timeseries(api_key, app_key, site=site, query=q, minutes=minutes)
            tmp_reasons[k] = reason
            if reason == "ok" and v is not None:
                tmp_metrics[k] = spec["norm"](v)
            else:
                tmp_metrics[k] = None
        
        out["usedTags"] = selector_tags
        out["metrics"] = tmp_metrics
        out["meta"]["reasons"] = tmp_reasons
        
        # Warn if no data with deterministic selector
        if not any(v is not None for v in tmp_metrics.values()):
            warnings.append({
                "type": "datadog_observability_no_data",
                "severity": "warn",
                "projectKey": project_key,
                "envKey": env_key,
                "message": f"Datadog returned no observability data for {project_key}/{env_key} using deterministic selector.",
                "details": {
                    "envSelector": env_selector,
                    "componentSelector": component_selector,
                    "tags": selector_tags,
                },
            })
        
        return out, warnings

    # Legacy fallback: Try each candidate tag for the environment
    chosen_tags: list[str] | None = None
    for env_tag in tag_candidates:
        tagset = base_tags + [env_tag]
        tags_str = _dd_join_tags(tagset)
        ok_any = False

        tmp_metrics = {}
        tmp_reasons = {}
        for k, spec in metric_specs.items():
            q = spec["query"].replace("{$tags}", f"{{{tags_str}}}" if tags_str else "")
            v, reason = datadog_query_timeseries(api_key, app_key, site=site, query=q, minutes=minutes)
            tmp_reasons[k] = reason
            if reason == "ok" and v is not None:
                ok_any = True
                tmp_metrics[k] = spec["norm"](v)
            else:
                tmp_metrics[k] = None

        if ok_any:
            chosen_tags = tagset
            out["usedTags"] = tagset
            out["metrics"] = tmp_metrics
            out["meta"]["reasons"] = tmp_reasons
            break

    if chosen_tags is None:
        # Nothing returned data for any candidate.
        out["meta"]["reasons"] = {
            k: "no_data" for k in metric_specs.keys()
        }
        warnings.append(
            {
                "type": "datadog_observability_no_data",
                "severity": "warn",
                "projectKey": project_key,
                "envKey": env_key,
                "message": f"Datadog returned no observability data for {project_key}/{env_key} using common env tags (env/environment/k8s namespace).",
                "details": {
                    "envValue": env_value,
                    "tagCandidates": tag_candidates,
                    "baseTags": base_tags,
                },
            }
        )

    return out, warnings


def datadog_list_monitors(dd_api_key: str, dd_app_key: str, site: str = "datadoghq.com") -> list[dict]:
    """Return all monitors visible to the provided API+APP key.
    We keep this lightweight and filter later to avoid overconfig in MVP1.
    """
    base = f"https://api.{site}"
    url = f"{base}/api/v1/monitor"
    headers = {"DD-API-KEY": dd_api_key, "DD-APPLICATION-KEY": dd_app_key}
    r = requests.get(url, headers=headers, timeout=20)
    if r.status_code != 200:
        raise RuntimeError(f"datadog monitors http={r.status_code} body={r.text[:200]}")
    data = r.json()
    return data if isinstance(data, list) else []


def datadog_monitor_to_global_alert(m: dict, site: str) -> dict | None:
    """Convert a Datadog monitor object to our globalAlerts banner format."""
    state = (m.get("overall_state") or "").upper()
    # Datadog uses 'No Data' in API v1
    if state == "NO DATA":
        state_norm = "NO_DATA"
    else:
        state_norm = state

    if state_norm not in ("ALERT", "WARN", "NO_DATA"):
        return None

    severity = "error" if state_norm == "ALERT" else ("warn" if state_norm == "WARN" else "info")

    mid = m.get("id")
    # app domain matches site
    app_domain = "datadoghq.eu" if site.endswith(".eu") else "datadoghq.com"
    url = f"https://app.{app_domain}/monitors/{mid}" if mid else None

    # best-effort env extraction
    env_tag = None
    for t in (m.get("tags") or []):
        if isinstance(t, str) and t.lower().startswith("env:"):
            env_tag = t.split(":", 1)[1].strip().lower()
            break

    return {
        "id": f"dd-monitor-{mid}" if mid else f"dd-monitor-{hash(m.get('name',''))}",
        "source": "datadog",
        "severity": severity,
        "title": m.get("name") or "Datadog monitor alert",
        "message": f"Monitor state: {m.get('overall_state')}",
        "env": env_tag,
        "links": ([{"label": "Open in Datadog", "url": url}] if url else []),
        "meta": {
            "kind": "monitor",
            "monitorId": mid,
            "overall_state": m.get("overall_state"),
            "type": m.get("type"),
        },
        "tags": m.get("tags") or [],
        "ts": datetime.now(timezone.utc).isoformat(),
    }


def datadog_collect_alert_feed(
    dd_api_key: str,
    dd_app_key: str,
    site: str,
    env_keys: list[str],
    limit: int = 10,
    # NEW: MVP1-safe deterministic selectors
    env_selectors: dict[str, dict[str, str]] | None = None,
) -> tuple[list[dict], list[dict]]:
    """Build globalAlerts from Datadog monitors.

    If env_selectors is provided (dict mapping env_key -> selector), filters monitors
    by deterministic selector scope (MVP1-safe). Otherwise falls back to env tag matching.

    Returns: (alerts, warnings)
    """
    warns: list[dict] = []
    try:
        monitors = datadog_list_monitors(dd_api_key, dd_app_key, site=site)
    except Exception as e:
        warns.append({
            "code": "DATADOG_MONITORS_FETCH_FAILED",
            "severity": "warn",
            "message": f"Datadog monitors fetch failed: {e}",
            "source": {"integration": "datadog", "site": site},
        })
        return [], warns

    alerts: list[dict] = []
    for m in monitors:
        a = datadog_monitor_to_global_alert(m, site)
        if not a:
            continue

        # MVP1-safe: Use deterministic selector if available
        if env_selectors and a.get("env"):
            env_key_lower = a["env"].lower()
            env_selector = env_selectors.get(env_key_lower)
            if env_selector:
                if not _dd_selector_matches_monitor(m, env_selector):
                    continue
            elif env_key_lower not in [ek.lower() for ek in env_keys]:
                continue
        elif a.get("env") and env_keys:
            # Legacy: If monitor has env tag, keep only those matching our known env keys
            if a["env"] not in [ek.lower() for ek in env_keys]:
                continue

        alerts.append(a)

    # sort by severity then newest
    sev_rank = {"error": 0, "warn": 1, "info": 2}
    alerts.sort(key=lambda x: (sev_rank.get(x.get("severity","info"), 9), x.get("title","")))
    return alerts[:limit], warns


def datadog_collect_news_feed(
    dd_api_key: str,
    dd_app_key: str,
    site: str,
    limit: int = 10,
) -> tuple[list[dict], list[dict]]:
    """Collect Datadog Monitors as news items for the News feed.

    Returns: (news_items, warnings)
    
    News item shape:
    {
        "ts": "2026-01-22T12:00:00Z",  # ISO timestamp
        "title": "Monitor name",
        "msg": "Monitor state: ALERT",
        "level": "bad" | "warn" | "ok",  # based on monitor state
        "source": "datadog",
        "url": "https://app.datadoghq.com/monitors/123",  # optional
    }
    """
    warns: list[dict] = []
    try:
        monitors = datadog_list_monitors(dd_api_key, dd_app_key, site=site)
    except Exception as e:
        warns.append({
            "code": "DATADOG_MONITORS_FETCH_FAILED",
            "severity": "warn",
            "message": f"Datadog monitors fetch failed: {e}",
            "source": {"integration": "datadog", "site": site},
        })
        return [], warns

    news_items: list[dict] = []
    for m in monitors:
        state = (m.get("overall_state") or "").upper()
        if state == "NO DATA":
            state_norm = "NO_DATA"
        else:
            state_norm = state

        # Only include monitors in alert/warn states (skip OK/No Data for news feed)
        if state_norm not in ("ALERT", "WARN"):
            continue

        # Determine level for news item
        level = "bad" if state_norm == "ALERT" else "warn"

        mid = m.get("id")
        app_domain = "datadoghq.eu" if site.endswith(".eu") else "datadoghq.com"
        url = f"https://app.{app_domain}/monitors/{mid}" if mid else None

        # Extract env from tags if available
        env_tag = None
        for t in (m.get("tags") or []):
            if isinstance(t, str) and t.lower().startswith("env:"):
                env_tag = t.split(":", 1)[1].strip()
                break

        monitor_name = m.get("name") or "Datadog monitor"
        msg_parts = [f"Monitor state: {state_norm}"]
        if env_tag:
            msg_parts.append(f"Env: {env_tag}")

        news_items.append({
            "ts": datetime.now(timezone.utc).isoformat(),
            "title": monitor_name,
            "msg": " • ".join(msg_parts),
            "level": level,
            "source": "datadog",
            "url": url,
            "meta": {
                "monitorId": mid,
                "overall_state": state_norm,
                "env": env_tag,
            },
        })

    # Sort by level (bad first) then by timestamp (newest first)
    level_rank = {"bad": 0, "warn": 1, "ok": 2}
    def _ts_key(x):
        ts_str = x.get("ts", "")
        if not ts_str:
            return 0
        try:
            # Handle ISO format with or without timezone
            ts_str = ts_str.replace("Z", "+00:00")
            return -datetime.fromisoformat(ts_str).timestamp()
        except Exception:
            return 0
    news_items.sort(key=lambda x: (level_rank.get(x.get("level", "ok"), 9), _ts_key(x)))
    
    return news_items[:limit], warns



def _dd_pick_status(signals: dict, thresholds: dict) -> str:
    """Derive a coarse status from numeric signals.

    Status order: unhealthy > degraded > healthy > unknown
    This is MVP heuristic only; thresholds can be overridden per project.
    """
    # Defaults
    defaults = {
        "errorRate": {"degraded": 1.0, "unhealthy": 5.0},
        "p95": {"degraded": 1000.0, "unhealthy": 2000.0},
        "cpu": {"degraded": 70.0, "unhealthy": 85.0},
        "mem": {"degraded": 70.0, "unhealthy": 85.0},
    }

    worst = "unknown"

    def bump(new: str):
        nonlocal worst
        order = {"unknown": 0, "healthy": 1, "degraded": 2, "unhealthy": 3}
        if order.get(new, 0) > order.get(worst, 0):
            worst = new

    any_value = False
    for key, val in (signals or {}).items():
        if val is None:
            continue
        any_value = True
        th = (thresholds or {}).get(key) or defaults.get(key) or {}
        try:
            v = float(val)
        except Exception:
            continue
        if "unhealthy" in th and v >= float(th["unhealthy"]):
            bump("unhealthy")
        elif "degraded" in th and v >= float(th["degraded"]):
            bump("degraded")
        else:
            bump("healthy")

    return worst if any_value else "unknown"


# ============================================================
# Ticket Tracker (MVP1.1 / MVP2-lite)
#
# Design goals:
# - ticketIndex is derived primarily from GitHub (PR titles/bodies) = truth of "what changed"
# - Jira is enrichment only (summary/status/assignee/fixVersions)
# - Works today as a local snapshot, later as a hosted worker (no tokens in UI)
# ============================================================

TICKET_KEY_RE = re.compile(r"\b([A-Z][A-Z0-9]+-\d+)\b")

def _strip(v: str | None) -> str:
    return (v or "").strip()

def github_list_recent_merged_prs(owner: str, repo: str, token: str, *, days: int = 30, per_repo_limit: int = 120) -> list[dict]:
    """Return recently merged PRs for a repo.

    Strategy:
    - list closed PRs sorted by updated desc (fast, no search API)
    - filter merged_at within the last `days`
    """
    since_dt = datetime.now(timezone.utc) - timedelta(days=days)
    out: list[dict] = []

    url = f"{GITHUB_API}/repos/{owner}/{repo}/pulls"
    params = {"state": "closed", "sort": "updated", "direction": "desc", "per_page": 100, "page": 1}
    headers = github_api_headers(token)

    while len(out) < per_repo_limit:
        # Use retry helper for rate limiting and transient errors
        try:
            r = _api_request_with_retry("GET", url, headers=headers, params=params, timeout=30)
        except requests.exceptions.HTTPError as e:
            if e.response and e.response.status_code in (401, 403):
                break
            raise
        
        if r.status_code in (401, 403):
            break
        r.raise_for_status()
        arr = r.json() or []
        if not arr:
            break

        stop = False
        for pr in arr:
            merged_at = pr.get("merged_at")
            if not merged_at:
                continue
            # merged_at is ISO string; compare as datetime
            try:
                merged_dt = datetime.fromisoformat(merged_at.replace("Z", "+00:00"))
            except Exception:
                continue

            if merged_dt < since_dt:
                stop = True
                break

            # Get merge commit SHA for branch/tag tracking
            merge_sha = (pr.get("merge_commit_sha") or "").strip()

            out.append({
                "number": pr.get("number"),
                "title": pr.get("title") or "",
                "body": pr.get("body") or "",
                "htmlUrl": pr.get("html_url") or "",
                "apiUrl": pr.get("url") or "",
                "mergedAt": merged_at,
                "user": ((pr.get("user") or {}).get("login") or ""),
                "baseRef": ((pr.get("base") or {}).get("ref") or ""),
                "headRef": ((pr.get("head") or {}).get("ref") or ""),
                "mergeSha": merge_sha,
                "repo": repo,
            })
            if len(out) >= per_repo_limit:
                break

        if stop:
            break

        params["page"] += 1

    return out


def github_check_commit_in_branch(owner: str, repo: str, commit_sha: str, branch: str, token: str) -> bool:
    """Check if a commit is reachable from a branch (i.e., commit is in branch history)."""
    if not commit_sha or not branch:
        return False
    try:
        # Use GitHub API to check if commit is in branch
        # Strategy: compare commit dates - if commit is newer than branch tip, it's not in branch
        # More reliable: use compare API
        url = f"{GITHUB_API}/repos/{owner}/{repo}/compare/{encode_branch_for_github_url(branch)}...{commit_sha}"
        headers = github_api_headers(token)
        r = requests.get(url, headers=headers, timeout=15)
        if r.status_code == 404:
            # Branch or commit doesn't exist
            return False
        if r.status_code != 200:
            return False
        data = r.json() or {}
        # If status is "behind" or "identical", commit is in branch
        status = (data.get("status") or "").lower()
        return status in ("behind", "identical", "ahead")  # ahead means branch is behind commit (commit is newer)
    except Exception:
        return False


def github_list_branches(owner: str, repo: str, token: str, *, limit: int = 100) -> list[dict]:
    """List branches for a repository. Returns list of {name, sha, protected, createdAt}.
    
    Note: GitHub API doesn't directly provide branch creation date. We approximate it using
    the commit date of the branch tip, which is the best available proxy.
    """
    try:
        url = f"{GITHUB_API}/repos/{owner}/{repo}/branches"
        headers = github_api_headers(token)
        params = {"per_page": min(limit, 100), "page": 1}
        out: list[dict] = []
        
        while len(out) < limit:
            r = requests.get(url, headers=headers, params=params, timeout=20)
            if r.status_code in (401, 403, 404):
                break
            r.raise_for_status()
            arr = r.json() or []
            if not arr:
                break
            for b in arr:
                commit = b.get("commit") or {}
                commit_sha = (commit.get("sha") or "").strip()
                # Get commit date as proxy for branch creation (best available approximation)
                commit_date = ""
                if commit_sha:
                    try:
                        commit_url = f"{GITHUB_API}/repos/{owner}/{repo}/commits/{commit_sha}"
                        cr = requests.get(commit_url, headers=headers, timeout=10)
                        if cr.status_code == 200:
                            commit_data = cr.json() or {}
                            commit_info = commit_data.get("commit") or {}
                            commit_date = (commit_info.get("author") or {}).get("date") or ""
                    except Exception:
                        pass
                
                out.append({
                    "name": (b.get("name") or "").strip(),
                    "sha": commit_sha,
                    "protected": bool(b.get("protected")),
                    "createdAt": commit_date,  # Approximate: commit date of branch tip
                })
                if len(out) >= limit:
                    break
            if len(arr) < params["per_page"]:
                break
            params["page"] += 1
        return out
    except Exception:
        return []


def github_list_tags(owner: str, repo: str, token: str, *, limit: int = 100) -> list[dict]:
    """List tags for a repository. Returns list of {name, sha, commitDate}."""
    try:
        url = f"{GITHUB_API}/repos/{owner}/{repo}/tags"
        headers = github_api_headers(token)
        params = {"per_page": min(limit, 100), "page": 1}
        out: list[dict] = []
        
        while len(out) < limit:
            r = requests.get(url, headers=headers, params=params, timeout=20)
            if r.status_code in (401, 403, 404):
                break
            r.raise_for_status()
            arr = r.json() or []
            if not arr:
                break
            for t in arr:
                commit = t.get("commit") or {}
                commit_sha = (commit.get("sha") or "").strip()
                # Get commit date
                commit_date = ""
                if commit_sha:
                    try:
                        commit_url = f"{GITHUB_API}/repos/{owner}/{repo}/commits/{commit_sha}"
                        cr = requests.get(commit_url, headers=headers, timeout=10)
                        if cr.status_code == 200:
                            commit_data = cr.json() or {}
                            commit_info = commit_data.get("commit") or {}
                            commit_date = (commit_info.get("author") or {}).get("date") or ""
                    except Exception:
                        pass
                
                out.append({
                    "name": (t.get("name") or "").strip(),
                    "sha": commit_sha,
                    "commitDate": commit_date,
                })
                if len(out) >= limit:
                    break
            if len(arr) < params["per_page"]:
                break
            params["page"] += 1
        return out
    except Exception:
        return []


def build_ticket_index_from_github(projects_out: list[dict], github_org: str, github_token: str, *, days: int = 120) -> dict:
    """Build a ticketIndex from recent merged PRs across all repos seen in environments."""

    # Collect unique code repos currently visible in snapshots.
    repos: set[str] = set()
    env_branch_by_repo_env: dict[tuple[str, str, str], str] = {}
    # key: (projectKey, envKey, repo) -> deployed branch
    for proj in projects_out:
        pkey = proj.get("key") or ""
        for env in proj.get("environments") or []:
            ekey = _normalize_env_key(env.get("key")) or ""
            for c in env.get("components") or []:
                repo = (c.get("repo") or "").strip()
                if repo:
                    repos.add(repo)
                    env_branch_by_repo_env[(pkey, ekey, repo)] = (c.get("branch") or "").strip()

    ticket_index: dict[str, dict] = {}

    owner = github_org or GITHUB_ORG_DEFAULT
    for repo in sorted(repos):
        prs = github_list_recent_merged_prs(owner, repo, github_token, days=days)
        for pr in prs:
            text = f"{pr.get('title','')}\n{pr.get('body','')}"
            keys = sorted(set(m.group(1) for m in TICKET_KEY_RE.finditer(text)))
            if not keys:
                continue

            for k in keys:
                ent = ticket_index.setdefault(k, {
                    "key": k,
                    "repos": [],
                    "prs": [],
                    "envPresence": {},
                })

                if repo not in ent["repos"]:
                    ent["repos"].append(repo)

                ent["prs"].append({
                    "repo": repo,
                    "number": pr.get("number"),
                    "title": pr.get("title"),
                    "url": pr.get("htmlUrl"),
                    "mergedAt": pr.get("mergedAt"),
                    "author": pr.get("user"),
                    "baseRef": pr.get("baseRef"),
                    "headRef": pr.get("headRef"),
                    "mergeSha": pr.get("mergeSha"),  # Required for branch/tag correlation
                })

                # Heuristic env mapping: if PR merged into a branch that is currently deployed in an env for that repo.
                base_ref = (pr.get("baseRef") or "").strip()
                if base_ref:
                    for (pkey, ekey, rrepo), deployed_branch in env_branch_by_repo_env.items():
                        if rrepo != repo:
                            continue
                        if deployed_branch and deployed_branch == base_ref:
                            ent["envPresence"].setdefault(pkey, {})[ekey] = True

    # Sort PRs newest first per ticket
    for ent in ticket_index.values():
        ent["prs"].sort(key=lambda x: (x.get("mergedAt") or ""), reverse=True)
        ent["repos"].sort()
        # ensure all envPresence maps exist
        ent.setdefault("envPresence", {})

    return ticket_index


def enrich_ticket_index_with_branches_and_tags(
    ticket_index: dict,
    projects_out: list[dict],
    github_org: str,
    github_token: str,
    *,
    max_repos: int = 20,
    max_branches_per_repo: int = 50,
    max_tags_per_repo: int = 100,
) -> None:
    """Enrich ticket index with branch and tag information.
    
    For each PR, determines which branches and tags contain the PR's merge commit.
    This enables tracking ticket lifecycle across releases.
    """
    if not ticket_index or not github_token:
        return

    # Collect unique repos from ticket PRs
    repos: set[str] = set()
    for ticket in ticket_index.values():
        for pr in ticket.get("prs") or []:
            repo = (pr.get("repo") or "").strip()
            if repo:
                repos.add(repo)

    if not repos:
        return

    owner = github_org or GITHUB_ORG_DEFAULT
    repos_list = sorted(list(repos))[:max_repos]

    # Cache branches and tags per repo to avoid repeated API calls
    repo_branches_cache: dict[str, list[dict]] = {}
    repo_tags_cache: dict[str, list[dict]] = {}

    for repo in repos_list:
        try:
            branches = github_list_branches(owner, repo, github_token, limit=max_branches_per_repo)
            repo_branches_cache[repo] = branches
        except Exception:
            pass

        try:
            tags = github_list_tags(owner, repo, github_token, limit=max_tags_per_repo)
            repo_tags_cache[repo] = tags
        except Exception:
            pass

    # For each ticket, enrich PRs with branch/tag information
    for ticket_key, ticket in ticket_index.items():
        prs = ticket.get("prs") or []
        
        for pr in prs:
            repo = (pr.get("repo") or "").strip()
            merge_sha = (pr.get("mergeSha") or "").strip()
            
            if not repo:
                continue
            
            # If mergeSha is missing, try to fetch it from GitHub API
            if not merge_sha:
                pr_number = pr.get("number")
                if pr_number:
                    try:
                        # Fetch PR details to get merge_commit_sha
                        url = f"{GITHUB_API}/repos/{owner}/{repo}/pulls/{pr_number}"
                        headers = github_api_headers(github_token)
                        r = requests.get(url, headers=headers, timeout=15)
                        if r.status_code == 200:
                            pr_data = r.json()
                            merge_sha = (pr_data.get("merge_commit_sha") or "").strip()
                            if merge_sha:
                                pr["mergeSha"] = merge_sha
                    except Exception:
                        pass  # Continue without mergeSha
            
            if not merge_sha:
                continue  # Skip branch/tag correlation if we can't get merge SHA

            # Find branches containing this PR
            branches_containing: list[str] = []
            for branch_info in repo_branches_cache.get(repo, []):
                branch_name = (branch_info.get("name") or "").strip()
                if not branch_name:
                    continue
                # For efficiency, check if merge SHA matches branch SHA or is in branch history
                branch_sha = (branch_info.get("sha") or "").strip()
                if branch_sha == merge_sha:
                    branches_containing.append(branch_name)
                elif branch_name.startswith("release/") or branch_name == "main" or branch_name == "master":
                    # For important branches, do a full check
                    try:
                        if github_check_commit_in_branch(owner, repo, merge_sha, branch_name, github_token):
                            branches_containing.append(branch_name)
                    except Exception:
                        pass  # Skip this branch if check fails

            # Find tags containing this PR
            tags_containing: list[dict] = []
            for tag_info in repo_tags_cache.get(repo, []):
                tag_name = (tag_info.get("name") or "").strip()
                tag_sha = (tag_info.get("sha") or "").strip()
                if not tag_name or not tag_sha:
                    continue
                # Check if merge SHA matches tag SHA or is in tag history
                if tag_sha == merge_sha:
                    tags_containing.append({
                        "name": tag_name,
                        "sha": tag_sha,
                        "date": tag_info.get("commitDate") or "",
                    })
                elif tag_name.startswith("v") or "release" in tag_name.lower() or tag_name.startswith("release/"):
                    # For version-like tags, check if commit is in tag history
                    try:
                        if github_check_commit_in_branch(owner, repo, merge_sha, tag_name, github_token):
                            tags_containing.append({
                                "name": tag_name,
                                "sha": tag_sha,
                                "date": tag_info.get("commitDate") or "",
                            })
                    except Exception:
                        pass  # Skip this tag if check fails

            # Store branch and tag information in PR
            if branches_containing:
                pr["branches"] = sorted(branches_containing)
            if tags_containing:
                pr["tags"] = sorted(tags_containing, key=lambda x: (x.get("date") or ""), reverse=True)


def build_ticket_index_from_components(projects_out: list[dict]) -> dict:
    """Best-effort ticket index built from component metadata (tags, branches, names).
    
    This is a fallback when GitHub API returns no tickets. It extracts ticket keys from:
    - Component tags (e.g., "my-service-PROJ-1234-v0.0.112")
    - Branch names (e.g., "feature/TCBP-1234-fix-bug")
    - Component names
    - Build metadata
    
    Returns a minimal ticketIndex with evidence from component data.
    """
    ticket_index: dict[str, dict] = {}
    
    for proj in projects_out:
        pkey = proj.get("key") or ""
        for env in proj.get("environments") or []:
            ekey = _normalize_env_key(env.get("key")) or ""
            env_name = (env.get("name") or ekey.upper()).strip()
            
            for comp in env.get("components") or []:
                repo = (comp.get("repo") or comp.get("repository") or "").strip()
                comp_name = (comp.get("name") or "").strip()
                tag = (comp.get("tag") or "").strip()
                branch = (comp.get("branch") or comp.get("ref") or "").strip()
                build = (comp.get("build") or "").strip()
                deployed_at = (comp.get("deployedAt") or "").strip()
                build_url = (comp.get("buildUrl") or "").strip()
                
                # Extract ticket keys from various fields
                text_sources = [tag, branch, comp_name, build]
                all_keys: set[str] = set()
                
                for text in text_sources:
                    if not text:
                        continue
                    matches = TICKET_KEY_RE.finditer(text)
                    for m in matches:
                        all_keys.add(m.group(1))
                
                if not all_keys:
                    continue
                
                # Create or update ticket entries
                for key in all_keys:
                    ent = ticket_index.setdefault(key, {
                        "key": key,
                        "repos": [],
                        "prs": [],
                        "envPresence": {},
                    })
                    
                    # Add repo if not already present
                    if repo and repo not in ent["repos"]:
                        ent["repos"].append(repo)
                    
                    # Add evidence entry (component-based, not PR-based)
                    evidence = {
                        "repo": repo,
                        "component": comp_name,
                        "tag": tag,
                        "branch": branch,
                        "build": build,
                        "deployedAt": deployed_at,
                        "buildUrl": build_url,
                        "source": "component_metadata",  # Indicates this came from component data, not PRs
                    }
                    
                    # Only add if we don't already have this exact evidence
                    if "evidence" not in ent:
                        ent["evidence"] = []
                    # Check for duplicates (same repo, component, tag)
                    is_duplicate = any(
                        e.get("repo") == repo and 
                        e.get("component") == comp_name and 
                        e.get("tag") == tag
                        for e in ent["evidence"]
                    )
                    if not is_duplicate:
                        ent["evidence"].append(evidence)
                    
                    # Set env presence based on where this component is deployed
                    if pkey and ekey:
                        ent["envPresence"].setdefault(pkey, {})[ekey] = True
    
    # Sort repos and evidence
    for ent in ticket_index.values():
        ent["repos"].sort()
        if "evidence" in ent:
            # Sort evidence by deployedAt (newest first)
            ent["evidence"].sort(key=lambda x: (x.get("deployedAt") or ""), reverse=True)
        # Ensure envPresence maps exist
        ent.setdefault("envPresence", {})
    
    return ticket_index


# ------------------------------------------------------------
# Ticket -> Environment presence (heuristic)
# ------------------------------------------------------------

def _normalize_branch_name(branch: str) -> str:
    """Normalize a branch name coming from snapshot or GitHub (strip refs/heads/, origin/ etc.)."""
    if not branch:
        return ""
    b = str(branch).strip()
    # common prefixes
    for pref in ("refs/heads/", "refs/", "origin/", "heads/"):
        if b.startswith(pref):
            b = b[len(pref):]
    return b.strip()

def _env_to_stage(env_name: str) -> str:
    """Map environment name to one of DEV/QA/UAT/PROD for ticket tracker badges."""
    n = (env_name or "").strip().lower()
    if not n:
        return "DEV"
    if "prod" in n:
        return "PROD"
    if "uat" in n:
        return "UAT"
    # treat qa + common QA color
    if n == "qa" or "qa" in n or n in ("green",):
        return "QA"
    # everything else we treat as DEV-like (dev lanes / colors)
    return "DEV"

def add_env_presence_to_ticket_index(ticket_index: dict, projects_out: list, prev_snapshot: dict | None = None, *, warnings: list = None) -> None:
    """Add envPresence to each ticket.

    Human-like, deterministic rules (product decision):
    - Primary truth is deployment timing + environment. If a PR was merged and the repo was deployed
      to an environment AFTER that merge time, the ticket is considered deployed to that environment.
    - Provenance (commit reachability / exact branch match) is OPTIONAL enrichment, not a blocker.
      We prefer false positives with warnings over false negatives.
    - Persistence: once a ticket is marked deployed to an environment, it must not disappear in later
      snapshots (unless rollback logic is added later). We therefore carry forward `envPresence=true`
      from the previous snapshot as a floor.
    
    Args:
        warnings: Optional list to append validation warnings to.
    """
    if not ticket_index or not projects_out:
        return
    
    if warnings is None:
        warnings = []

    # Build tag change map: (project_key, env_key, component_name) -> {repo, fromTag, toTag, deployedAt, ...}
    # This uses the same logic as compute_tag_change_events() to ensure consistency with Release History
    tag_changes_by_key: dict[tuple[str, str, str], dict] = {}
    
    # Handle first-run case: if no prev_snapshot, use current snapshot as baseline
    # This means we won't detect deployments on first run, but we'll mark current state
    if prev_snapshot is None:
        logger.info("first_run_detected", message="No previous snapshot, using current state as baseline. Deployments will be detected on subsequent runs.")
        # Create empty baseline for tag change detection
        prev_snapshot = {"projects": []}
    
    if prev_snapshot:
        prev_map = _component_map(prev_snapshot)
        cur_map = _component_map({"projects": projects_out})
        for key, cur in cur_map.items():
            pkey, ekey, cname = key
            cur_comp = cur['comp']
            cur_tag = (cur_comp.get('tag') or '').strip()
            prev_tag = ''
            if key in prev_map:
                prev_tag = (prev_map[key]['comp'].get('tag') or '').strip()
            # Only track actual tag changes (same criteria as Release History: prev_tag != cur_tag)
            # On first run, prev_tag will be empty, so we won't detect changes, but that's OK
            if prev_tag and cur_tag and prev_tag != cur_tag:
                repo = (cur_comp.get('repo') or cur_comp.get('repository') or '').strip()
                if repo:
                    deployed_at = (cur_comp.get('deployedAt') or '').strip()
                    
                    # Validate deployedAt - warn if missing but don't fail
                    if not deployed_at:
                        warnings.append(_warning(
                            level="warning",
                            scope="component",
                            reason="missing_deployedAt_for_tag_change",
                            source="ticket_tracker",
                            project=pkey,
                            env=ekey,
                            component=cname,
                            message=f"Tag changed ({prev_tag} → {cur_tag}) but deployedAt is missing. Deployment detection may be incomplete.",
                        ))
                    
                    tag_changes_by_key[key] = {
                        'repo': repo,
                        'fromTag': prev_tag,
                        'toTag': cur_tag,
                        'deployedAt': deployed_at,
                        'branch': _normalize_branch_name(cur_comp.get('branch') or cur_comp.get('ref') or ''),
                        'tag': cur_tag,
                    }

    # stage -> repo -> {branch, deployedAt, tag}
    # Product decision: DO NOT require tag changes between snapshots to consider a deployment.
    stage_repo_info: dict[str, dict[str, dict]] = {"DEV": {}, "QA": {}, "UAT": {}, "PROD": {}}

    def _pick_better(existing: dict | None, candidate: dict) -> dict:
        """Pick the entry with the newest deployedAt; fallback to existing."""
        if not existing:
            return candidate
        e_ts = (existing.get("deployedAt") or "").strip()
        c_ts = (candidate.get("deployedAt") or "").strip()
        if c_ts and (not e_ts or c_ts > e_ts):
            return candidate
        return existing

    # Include all components with repo + deployment timestamp (best-effort).
    for proj in projects_out:
        pkey = (proj.get("key") or "").strip()
        for env in (proj.get("environments") or []):
            stage = _env_to_stage(env.get("name"))
            env_key = _normalize_env_key(env.get("key")) or ""
            for comp in (env.get("components") or []):
                comp_name = (comp.get("name") or "").strip()
                repo = (comp.get("repo") or comp.get("repository") or "").strip()
                if not repo:
                    continue
                
                entry = {
                    "branch": _normalize_branch_name(comp.get("branch") or comp.get("ref") or ""),
                    "deployedAt": (comp.get("deployedAt") or env.get("lastDeploy") or "").strip(),
                    "tag": (comp.get("tag") or "").strip(),
                }
                stage_repo_info[stage][repo] = _pick_better(stage_repo_info[stage].get(repo), entry)

    def _parse_iso(s: str) -> datetime | None:
        if not s:
            return None
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00"))
        except Exception:
            return None

    # Evaluate env presence + build a simple timeline
    stage_order = ["DEV", "QA", "UAT", "PROD"]
    for _, t in ticket_index.items():
        prs = t.get("pullRequests") or t.get("prs") or []
        presence = {"DEV": False, "QA": False, "UAT": False, "PROD": False}
        presence_meta = {"DEV": None, "QA": None, "UAT": None, "PROD": None}

        # PR merge events
        # UI expects: {stage, at, ref, source, url?}
        timeline: list[dict] = []

        for pr in prs:
            repo = (pr.get("repo") or pr.get("repository") or "").strip()
            base = _normalize_branch_name(
                pr.get("baseRef")
                or pr.get("base")
                or pr.get("baseBranch")
                or pr.get("targetBranch")
                or pr.get("mergedInto")
                or ""
            )
            merged_at = (pr.get("mergedAt") or pr.get("merged_at") or "").strip()
            merged_dt = _parse_iso(merged_at)

            if not repo:
                continue

            # Add PR merge line
            if merged_at:
                pr_no = pr.get("pr") or pr.get("number") or ""
                pr_url = pr.get("url") or pr.get("htmlUrl") or pr.get("html_url") or ""
                timeline.append(
                    {
                        "stage": "PR merged",
                        "at": merged_at,
                        "ref": base or "-",
                        "source": f"{repo}#{pr_no}" if pr_no else repo,
                        "url": pr_url,
                        "type": "pr_merge",  # Event type for UI
                    }
                )

            for stage, repo_map in stage_repo_info.items():
                info = repo_map.get(repo) or {}
                deployed_branch = (info.get("branch") or "").strip()
                deployed_at = (info.get("deployedAt") or "").strip()
                deployed_dt = _parse_iso(deployed_at)

                if not deployed_dt or not merged_dt:
                    # Not enough timing info -> cannot assert.
                    continue

                if deployed_dt < merged_dt:
                    continue

                # Product decision: timing is primary truth.
                # If deployment happened after merge, count it.
                presence[stage] = True

                # Keep the earliest deploy time per stage for the ticket.
                existing = presence_meta.get(stage)
                if not existing or (deployed_at and deployed_at < (existing.get("when") or "")):
                    # Confidence / warning are best-effort, non-blocking.
                    confidence = "high" if (base and deployed_branch and deployed_branch == base) else "heuristic"
                    warn_txt = ""
                    if base and deployed_branch and deployed_branch != base:
                        warn_txt = "Deployment inferred by timing despite branch mismatch."
                    else:
                        warn_txt = "Deployment inferred by timing (provenance not verified)."
                    presence_meta[stage] = {
                        "when": deployed_at,
                        "repo": repo,
                        "tag": info.get("tag") or "",
                        "branch": deployed_branch,
                        "confidence": confidence,
                        "inferred": True,
                        "warning": warn_txt,
                    }

        # PRIORITY: Use time-aware build-driven environment presence if available
        time_aware_branches = t.get("timeAwareBranches") or []
        time_aware_builds = t.get("timeAwareBuilds") or []
        time_aware_deployments = t.get("timeAwareDeployments") or []
        
        # Build component -> environment mapping (used for both time-aware and legacy logic)
        comp_to_env: dict[str, dict] = {}  # component_name -> {stage, env_key, project_key, repo}
        for proj in projects_out:
            pkey = (proj.get("key") or "").strip()
            for env in (proj.get("environments") or []):
                stage = _env_to_stage(env.get("name"))
                env_key = _normalize_env_key(env.get("key")) or ""
                for comp in (env.get("components") or []):
                    comp_name = (comp.get("name") or "").strip()
                    comp_repo = (comp.get("repo") or comp.get("repository") or "").strip()
                    if comp_name:
                        comp_to_env[comp_name] = {
                            "stage": stage,
                            "envKey": env_key,
                            "projectKey": pkey,
                            "repo": comp_repo,
                        }
        
        # Build-driven environment presence (time-aware)
        # Map time-aware deployments to environments based on component location
        if time_aware_deployments:
            
            # Process time-aware deployments (build-driven)
            for deploy_info in time_aware_deployments:
                deployed_at = deploy_info.get("deployedAt") or ""
                deploy_component = (deploy_info.get("component") or "").strip()
                deploy_build = deploy_info.get("build") or ""
                deploy_tag = deploy_info.get("tag") or ""
                
                if not deployed_at or not deploy_component:
                    continue
                
                # Find environment for this component
                env_info = comp_to_env.get(deploy_component)
                if not env_info:
                    continue
                
                stage = env_info["stage"]
                deployed_dt = _parse_iso(deployed_at)
                
                if not deployed_dt:
                    continue
                
                # Mark environment as present (build-driven)
                presence[stage] = True
                
                # Update metadata (keep earliest deployment)
                existing = presence_meta.get(stage)
                if not existing or (deployed_at and deployed_at < (existing.get("when") or "")):
                    presence_meta[stage] = {
                        "when": deployed_at,
                        "repo": env_info.get("repo") or "",
                        "tag": deploy_tag,
                        "branch": "",  # Build-driven, not branch-driven
                        "build": deploy_build,
                        "component": deploy_component,
                        "confidence": "high",  # High confidence: build-driven with time validation
                        "source": "time_aware_build",
                    }
        
        # Add branch/release events from time-aware branches (deterministic, time-validated)
        if time_aware_branches:
            for branch_info in time_aware_branches:
                branch_name = branch_info.get("branch") or ""
                branch_created_at = branch_info.get("createdAt") or ""
                branch_repo = ""  # Extract from PR context if needed
                
                # Find repo from PRs
                for pr in prs:
                    if branch_info.get("prMergedAt") == pr.get("mergedAt"):
                        branch_repo = (pr.get("repo") or "").strip()
                        break
                
                if branch_name and (branch_name.startswith("release/") or branch_name in ("main", "master")):
                    timeline.append({
                        "stage": f"Included in {branch_name}",
                        "at": branch_created_at,  # Use actual branch creation time
                        "ref": branch_name,
                        "source": branch_repo,
                        "type": "branch",
                        "timeAware": True,  # Mark as time-validated
                    })
        else:
            # Fallback to legacy branch correlation (if time-aware not available)
            for pr in prs:
                repo = (pr.get("repo") or "").strip()
                merged_at = (pr.get("mergedAt") or "").strip()
                branches = pr.get("branches") or []
                
                # Add branch events (focus on release branches)
                for branch_name in branches:
                    if branch_name.startswith("release/") or branch_name in ("main", "master"):
                        timeline.append({
                            "stage": f"Included in {branch_name}",
                            "at": merged_at,  # Use PR merge time as proxy
                            "ref": branch_name,
                            "source": repo,
                            "type": "branch",
                            "timeAware": False,  # Mark as legacy (no time validation)
                        })
        
        # Add tag/release events (use time-aware if available, otherwise legacy)
        for pr in prs:
            repo = (pr.get("repo") or "").strip()
            merged_at = (pr.get("mergedAt") or "").strip()
            tags = pr.get("tags") or []
            
            # Add tag/release events
            for tag_info in tags:
                tag_name = tag_info.get("name") or ""
                tag_date = tag_info.get("date") or merged_at  # Prefer tag date, fallback to merge date
                if tag_name:
                    timeline.append({
                        "stage": f"Tagged as {tag_name}",
                        "at": tag_date,
                        "ref": tag_name,
                        "source": repo,
                        "type": "tag",
                    })
        
        # Add build events from time-aware builds
        for build_info in time_aware_builds:
            build_no = build_info.get("buildNumber") or ""
            started_at = build_info.get("startedAt") or ""
            finished_at = build_info.get("finishedAt") or ""
            build_tag = build_info.get("tag") or ""
            build_repo = build_info.get("repo") or ""
            
            if build_no and started_at:
                timeline.append({
                    "stage": f"Build {build_no}",
                    "at": started_at,
                    "ref": build_tag or build_no,
                    "source": build_repo,
                    "type": "build",
                    "timeAware": True,
                    "finishedAt": finished_at,
                })
        
        # Add deployment events from time-aware deployments (build-driven)
        for deploy_info in time_aware_deployments:
            deployed_at = deploy_info.get("deployedAt") or ""
            deploy_tag = deploy_info.get("tag") or ""
            deploy_component = deploy_info.get("component") or ""
            deploy_build = deploy_info.get("build") or ""
            
            if deployed_at:
                # Find environment stage for this component
                env_info = comp_to_env.get(deploy_component) if deploy_component else None
                
                # Skip if component not found in any environment (fail closed)
                if not env_info:
                    continue
                
                stage_label = env_info.get("stage")
                
                timeline.append({
                    "stage": f"Deployed to {stage_label}",
                    "at": deployed_at,
                    "ref": deploy_tag or deploy_build or "",
                    "source": deploy_component,
                    "type": "deployment",
                    "timeAware": True,
                    "build": deploy_build,
                })

        # Persistence floor: once deployed stays deployed (carry forward from previous snapshot).
        # This prevents \"no new deploy\" snapshots or transient data gaps from wiping deployments.
        if prev_snapshot:
            try:
                prev_ticket_index = (prev_snapshot.get("ticketIndex") or {}) if isinstance(prev_snapshot, dict) else {}
                ticket_key = (t.get("key") or "").strip()
                prev_ticket = prev_ticket_index.get(ticket_key) if (ticket_key and isinstance(prev_ticket_index, dict)) else None
                if isinstance(prev_ticket, dict):
                    prev_presence = prev_ticket.get("envPresence") or {}
                    prev_meta = prev_ticket.get("envPresenceMeta") or {}
                    if isinstance(prev_presence, dict):
                        for stage in stage_order:
                            if prev_presence.get(stage) is True and presence.get(stage) is not True:
                                presence[stage] = True
                                # Prefer current meta when present; otherwise carry previous meta forward.
                                if not presence_meta.get(stage) and isinstance(prev_meta, dict):
                                    pm = prev_meta.get(stage)
                                    if isinstance(pm, dict) and pm:
                                        carried = dict(pm)
                                        carried.setdefault("source", "persisted_prev_snapshot")
                                        carried.setdefault("confidence", "persisted")
                                        carried.setdefault("warning", "Deployment carried forward from previous snapshot.")
                                        presence_meta[stage] = carried
            except Exception:
                pass

        # Add deployment events from legacy logic (only if time-aware didn't already add them)
        # This ensures backward compatibility when time-aware correlation is disabled or unavailable
        time_aware_deployment_stages = {
            ev.get("stage", "").replace("Deployed to ", "")
            for ev in timeline
            if ev.get("type") == "deployment" and ev.get("timeAware")
        }
        
        for stage in stage_order:
            # Skip if time-aware already added deployment event for this stage
            if stage in time_aware_deployment_stages:
                continue
            
            meta = presence_meta.get(stage)
            if meta and meta.get("when"):
                ver_bits = []
                if meta.get("tag"):
                    ver_bits.append(str(meta.get("tag")))
                if meta.get("branch"):
                    ver_bits.append(str(meta.get("branch")))
                version_txt = " • ".join(ver_bits) if ver_bits else "-"
                timeline.append(
                    {
                        "stage": f"Deployed to {stage}",
                        "at": meta.get("when"),
                        "ref": version_txt,
                        "source": meta.get("repo") or "",
                        "type": "deployment",
                        "timeAware": False,  # Legacy logic
                    }
                )

        # Sort timeline by time (when missing, keep last)
        def _when_key(ev: dict) -> str:
            return (ev.get("at") or "").strip()

        timeline_sorted = sorted(timeline, key=_when_key, reverse=True)  # Newest first

        t["envPresence"] = presence
        t["envPresenceMeta"] = presence_meta
        t["timeline"] = timeline_sorted


# ============================================================================
# TIME-AWARE, DETERMINISTIC TICKET → RELEASE → DEPLOYMENT CORRELATION
# ============================================================================
# This module implements time-aware correlation that respects real-world
# delivery timelines. All correlations require BOTH reachability AND time.
#
# Rules:
# 1. PR → Branch: branch.createdAt >= pr.mergedAt AND mergeSha ∈ branch
# 2. PR → Build: build.startedAt >= pr.mergedAt
# 3. Build → Deployment: deployment.at >= build.finishedAt
# 4. Environment presence is build-driven, NOT branch-driven
# ============================================================================

def _parse_iso_safe(s: str) -> datetime | None:
    """Parse ISO timestamp string to datetime, return None if invalid."""
    if not s or not isinstance(s, str):
        return None
    try:
        # Handle both Z and +00:00 timezone formats
        s_clean = s.replace("Z", "+00:00")
        return datetime.fromisoformat(s_clean)
    except Exception:
        return None


def correlate_prs_with_branches_time_aware(
    prs: list[dict],
    repo_branches: list[dict],
    owner: str,
    repo: str,
    github_token: str,
) -> list[dict]:
    """
    Time-aware PR → Branch correlation.
    
    Rule: branch.createdAt >= pr.mergedAt AND mergeSha ∈ branch
    
    Returns list of {branch, createdAt, sha} for branches that:
    - Contain the PR merge commit (reachability)
    - Were created at or after the PR merge time (time constraint)
    
    If branch creation date is missing, the branch is excluded (fail closed).
    """
    if not prs or not repo_branches:
        return []
    
    correlated: list[dict] = []
    
    for pr in prs:
        merge_sha = (pr.get("mergeSha") or "").strip()
        merged_at = (pr.get("mergedAt") or "").strip()
        merged_dt = _parse_iso_safe(merged_at)
        
        if not merge_sha or not merged_dt:
            continue  # Missing required data - fail closed
        
        for branch_info in repo_branches:
            branch_name = (branch_info.get("name") or "").strip()
            branch_created_at = (branch_info.get("createdAt") or "").strip()
            branch_created_dt = _parse_iso_safe(branch_created_at)
            
            # CRITICAL: If branch creation date is missing, exclude it (fail closed)
            if not branch_created_dt:
                continue
            
            # TIME CONSTRAINT: Branch must be created at or after PR merge
            if branch_created_dt < merged_dt:
                continue  # Branch created before PR merge - impossible to contain it
            
            # REACHABILITY: Check if merge commit is in branch
            branch_sha = (branch_info.get("sha") or "").strip()
            if branch_sha == merge_sha:
                # Direct match - branch tip is the merge commit
                correlated.append({
                    "branch": branch_name,
                    "createdAt": branch_created_at,
                    "sha": branch_sha,
                    "prMergedAt": merged_at,
                })
            elif branch_name.startswith("release/") or branch_name in ("main", "master"):
                # For important branches, do full reachability check
                try:
                    if github_check_commit_in_branch(owner, repo, merge_sha, branch_name, github_token):
                        correlated.append({
                            "branch": branch_name,
                            "createdAt": branch_created_at,
                            "sha": branch_sha,
                            "prMergedAt": merged_at,
                        })
                except Exception:
                    pass  # Skip on error - fail closed
    
    return correlated


def correlate_prs_with_builds_time_aware(
    prs: list[dict],
    components: list[dict],
    teamcity_rest_base: str | None,
    teamcity_token: str | None,
) -> list[dict]:
    """
    Time-aware PR → Build correlation.
    
    Rule: build.startedAt >= pr.mergedAt
    
    Returns list of {build, buildNumber, startedAt, finishedAt, branch, tag} for builds that:
    - Started at or after the PR merge time
    - Are associated with components that match the PR's repository
    
    If build start date is missing, the build is excluded (fail closed).
    """
    if not prs or not components:
        return []
    
    if not teamcity_rest_base or not teamcity_token:
        return []  # TeamCity not available - cannot correlate builds
    
    correlated: list[dict] = []
    
    for pr in prs:
        repo = (pr.get("repo") or "").strip()
        merged_at = (pr.get("mergedAt") or "").strip()
        merged_dt = _parse_iso_safe(merged_at)
        
        if not repo or not merged_dt:
            continue
        
        # Find components matching this PR's repository
        for comp in components:
            comp_repo = (comp.get("repo") or comp.get("repository") or "").strip()
            if comp_repo != repo:
                continue
            
            build_no = (comp.get("build") or comp.get("buildNumber") or "").strip()
            build_type_id = (comp.get("teamcityBuildTypeId") or "").strip()
            
            if not build_no or not build_type_id:
                continue
            
            # Fetch build details from TeamCity
            try:
                tc_build = teamcity_get_build(teamcity_rest_base, teamcity_token, build_type_id, build_no)
                if not tc_build:
                    continue
                
                # Extract timestamps
                start_raw = tc_build.get("startDate") or ""
                finish_raw = tc_build.get("finishDate") or tc_build.get("finishOnAgentDate") or ""
                
                # Parse TeamCity timestamps using same logic as parse_iso_teamcity
                # Format: 20251204T141343+0000 -> ISO format
                def _parse_tc_ts(s: str) -> str:
                    if not s:
                        return ""
                    try:
                        # TeamCity format: 20251204T141343+0000
                        # Use same parsing logic as parse_iso_teamcity (defined at line ~2317)
                        dt = datetime.strptime(s, "%Y%m%dT%H%M%S%z")
                        return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
                    except Exception:
                        return s
                
                started_at = _parse_tc_ts(start_raw) if start_raw else ""
                finished_at = _parse_tc_ts(finish_raw) if finish_raw else ""
                
                started_dt = _parse_iso_safe(started_at)
                
                # TIME CONSTRAINT: Build must start at or after PR merge
                if not started_dt:
                    continue  # Missing start date - fail closed
                
                if started_dt < merged_dt:
                    continue  # Build started before PR merge - impossible to contain it
                
                # Build passed time constraint - include it
                correlated.append({
                    "build": build_no,
                    "buildNumber": build_no,
                    "buildTypeId": build_type_id,
                    "startedAt": started_at,
                    "finishedAt": finished_at,
                    "branch": (comp.get("branch") or "").strip(),
                    "tag": (comp.get("tag") or "").strip(),
                    "repo": repo,
                    "component": (comp.get("name") or "").strip(),
                    "prMergedAt": merged_at,
                })
            except Exception:
                continue  # Skip on error - fail closed
    
    return correlated


def correlate_builds_with_deployments_time_aware(
    builds: list[dict],
    components: list[dict],
) -> list[dict]:
    """
    Time-aware Build → Deployment correlation.
    
    Rule: deployment.at >= build.finishedAt
    
    Returns list of {deployment, environment, deployedAt, build, tag} for deployments that:
    - Occurred at or after the build finished
    - Are associated with the same component/tag as the build
    
    If deployment timestamp is missing, the deployment is excluded (fail closed).
    """
    if not builds or not components:
        return []
    
    correlated: list[dict] = []
    
    for build_info in builds:
        build_no = build_info.get("buildNumber") or ""
        build_tag = (build_info.get("tag") or "").strip()
        build_finished_at = (build_info.get("finishedAt") or "").strip()
        build_finished_dt = _parse_iso_safe(build_finished_at)
        build_component = (build_info.get("component") or "").strip()
        
        if not build_finished_dt:
            continue  # Missing build finish date - cannot validate deployment time
        
        # Find components matching this build
        for comp in components:
            comp_tag = (comp.get("tag") or "").strip()
            comp_name = (comp.get("name") or "").strip()
            comp_build = (comp.get("build") or comp.get("buildNumber") or "").strip()
            
            # Match by tag (preferred) or build number
            tag_match = comp_tag and build_tag and comp_tag == build_tag
            build_match = comp_build and build_no and comp_build == build_no
            component_match = comp_name and build_component and comp_name == build_component
            
            if not (tag_match or build_match or component_match):
                continue
            
            # Get deployment timestamp
            deployed_at = (comp.get("deployedAt") or "").strip()
            deployed_dt = _parse_iso_safe(deployed_at)
            
            # TIME CONSTRAINT: Deployment must occur at or after build finished
            if not deployed_dt:
                continue  # Missing deployment timestamp - fail closed
            
            if deployed_dt < build_finished_dt:
                continue  # Deployment before build finished - impossible
            
            # Get environment info
            # Note: We need to get this from the parent context (env)
            # For now, we'll include what we can from component
            correlated.append({
                "deployment": deployed_at,
                "deployedAt": deployed_at,
                "build": build_no,
                "tag": comp_tag or build_tag,
                "component": comp_name,
                "buildFinishedAt": build_finished_at,
            })
    
    return correlated


def enrich_ticket_index_time_aware(
    ticket_index: dict,
    projects_out: list[dict],
    github_org: str,
    github_token: str,
    teamcity_rest_base: str | None,
    teamcity_token: str | None,
    *,
    max_repos: int = 20,
    max_branches_per_repo: int = 50,
) -> None:
    """
    Time-aware, deterministic ticket → release → deployment correlation.
    
    This function enriches ticket_index with time-validated correlations:
    - PRs → Branches (only if branch.createdAt >= pr.mergedAt)
    - PRs → Builds (only if build.startedAt >= pr.mergedAt)
    - Builds → Deployments (only if deployment.at >= build.finishedAt)
    - Environment presence (build-driven, not branch-driven)
    
    All correlations require BOTH reachability AND time constraints.
    Missing timestamps result in exclusion (fail closed).
    
    This is an ADDITIVE enrichment - it adds new fields but does not remove existing ones.
    """
    if not ticket_index or not github_token:
        return
    
    # Collect unique repos from ticket PRs
    repos: set[str] = set()
    for ticket in ticket_index.values():
        for pr in ticket.get("prs") or []:
            repo = (pr.get("repo") or "").strip()
            if repo:
                repos.add(repo)
    
    if not repos:
        return
    
    owner = github_org or GITHUB_ORG_DEFAULT
    repos_list = sorted(list(repos))[:max_repos]
    
    # Cache branches per repo with creation dates
    repo_branches_cache: dict[str, list[dict]] = {}
    for repo in repos_list:
        try:
            branches = github_list_branches(owner, repo, github_token, limit=max_branches_per_repo)
            repo_branches_cache[repo] = branches
        except Exception:
            pass
    
    # Build component map for build/deployment correlation
    component_map: dict[str, list[dict]] = {}  # repo -> [components]
    for proj in projects_out:
        for env in (proj.get("environments") or []):
            for comp in (env.get("components") or []):
                repo = (comp.get("repo") or comp.get("repository") or "").strip()
                if repo:
                    if repo not in component_map:
                        component_map[repo] = []
                    component_map[repo].append(comp)
    
    # Enrich each ticket with time-aware correlations
    for ticket_key, ticket in ticket_index.items():
        prs = ticket.get("prs") or []
        
        # Initialize time-aware fields if not present
        if "timeAwareBranches" not in ticket:
            ticket["timeAwareBranches"] = []
        if "timeAwareBuilds" not in ticket:
            ticket["timeAwareBuilds"] = []
        if "timeAwareDeployments" not in ticket:
            ticket["timeAwareDeployments"] = []
        
        for pr in prs:
            repo = (pr.get("repo") or "").strip()
            if not repo:
                continue
            
            # 1. Time-aware branch correlation
            branches = repo_branches_cache.get(repo, [])
            if branches:
                correlated_branches = correlate_prs_with_branches_time_aware(
                    [pr],
                    branches,
                    owner,
                    repo,
                    github_token,
                )
                for branch_info in correlated_branches:
                    # Add to ticket's time-aware branches (deduplicate)
                    branch_name = branch_info.get("branch")
                    if branch_name and not any(
                        b.get("branch") == branch_name for b in ticket["timeAwareBranches"]
                    ):
                        ticket["timeAwareBranches"].append(branch_info)
            
            # 2. Time-aware build correlation
            components = component_map.get(repo, [])
            if components and teamcity_rest_base and teamcity_token:
                correlated_builds = correlate_prs_with_builds_time_aware(
                    [pr],
                    components,
                    teamcity_rest_base,
                    teamcity_token,
                )
                for build_info in correlated_builds:
                    # Add to ticket's time-aware builds (deduplicate by build number)
                    build_no = build_info.get("buildNumber")
                    if build_no and not any(
                        b.get("buildNumber") == build_no for b in ticket["timeAwareBuilds"]
                    ):
                        ticket["timeAwareBuilds"].append(build_info)
            
            # 3. Time-aware deployment correlation (build-driven)
            builds = ticket.get("timeAwareBuilds", [])
            if builds and components:
                correlated_deployments = correlate_builds_with_deployments_time_aware(
                    builds,
                    components,
                )
                for deploy_info in correlated_deployments:
                    # Add to ticket's time-aware deployments (deduplicate)
                    deploy_key = f"{deploy_info.get('component')}:{deploy_info.get('deployedAt')}"
                    if not any(
                        f"{d.get('component')}:{d.get('deployedAt')}" == deploy_key
                        for d in ticket["timeAwareDeployments"]
                    ):
                        ticket["timeAwareDeployments"].append(deploy_info)


def jira_headers() -> dict:
    return {"Accept": "application/json"}


def enrich_ticket_index_with_jira(ticket_index: dict, *, jira_base: str, jira_email: str, jira_token: str, max_tickets: int = 250) -> None:
    """In-place enrichment of ticketIndex with Jira issue fields."""
    jira_base = jira_base.rstrip("/")
    if not jira_base.startswith("http"):
        jira_base = f"https://{jira_base}"

    email = _strip(jira_email)
    token = _strip(jira_token)
    if not email or not token:
        return

    keys = list(ticket_index.keys())[:max_tickets]
    for k in keys:
        url = f"{jira_base}/rest/api/3/issue/{urllib.parse.quote(k)}"
        params = {"fields": "summary,status,assignee,fixVersions,project"}
        try:
            # Use retry helper for rate limiting and transient errors
            r = _api_request_with_retry("GET", url, headers=jira_headers(), params=params, auth=(email, token), timeout=30)
        except requests.exceptions.HTTPError as e:
            if e.response:
                status = e.response.status_code
                if status in (401, 403, 404):
                    # 404 is normal if key doesn't exist in this Jira
                    continue
                if status == 429:
                    # Rate limit - stop rather than hammer (retry helper already handled retries)
                    break
            continue
        except Exception:
            continue

        j = r.json() or {}
        fields = j.get("fields") or {}
        status = (fields.get("status") or {}).get("name") or ""
        assignee = (fields.get("assignee") or {}).get("displayName") or ""
        summary = fields.get("summary") or ""
        fix_versions = [fv.get("name") for fv in (fields.get("fixVersions") or []) if fv.get("name")]
        project_key = (fields.get("project") or {}).get("key") or ""

        ticket_index[k]["jira"] = {
            "key": k,
            "project": project_key,
            "summary": summary,
            "status": status,
            "assignee": assignee,
            "fixVersions": fix_versions,
            "url": f"{jira_base}/browse/{k}",
        }



# ============================================================
# Config loading (MVP1)
# ============================================================

def load_project_configs() -> list[dict]:
    """Load all YAML configs from MVP1/snapshot/configs/*.yaml.

    Each file describes ONE logical project (e.g., TCBP_MFES, TAP2).
    This keeps MVP1 scope stable: no auto-discovery, only explicit mapping.
    """
    cfg_dir = Path(__file__).resolve().parent / "configs"
    if not cfg_dir.exists():
        raise SystemExit(f"Brak katalogu configów: {cfg_dir}")

    configs: list[dict] = []
    for p in sorted(cfg_dir.glob("*.yaml")):
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        if not isinstance(data, dict) or not data.get("project"):
            raise SystemExit(f"Niepoprawny config: {p}")
        configs.append(data)

    if not configs:
        raise SystemExit(f"Brak plików YAML w: {cfg_dir}")
    return configs

GITHUB_API = "https://api.github.com"
GITHUB_ORG_DEFAULT = ""  # Set GITHUB_ORG env or in config per project

# ============================================================
# Helpers
# ============================================================

def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()

def encode_branch_for_github_url(branch: str) -> str:
    """Encode branch for GitHub URL while preserving slashes."""
    parts = [urllib.parse.quote(p, safe="") for p in (branch or "").split("/")]
    return "/".join(parts)

def parse_github_blob_url(url: str):
    """
    Example:
      https://github.com/your-org/repo-infra/blob/main/envs/dev/kustomization.yaml
    """
    parts = url.strip().split("/")
    if len(parts) < 8 or parts[2] != "github.com":
        raise ValueError(f"Not a GitHub blob URL: {url}")
    owner = parts[3]
    repo = parts[4]
    ref = parts[6]
    path = "/".join(parts[7:])
    return owner, repo, ref, path

def github_blob_url(owner: str, repo: str, ref: str, path: str) -> str:
    return f"https://github.com/{owner}/{repo}/blob/{ref}/{path}"

def github_api_headers(token: str) -> dict:
    return {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "release-ops-snapshot",
    }

def fetch_github_file(owner: str, repo: str, path: str, ref: str, token: str) -> str:
    api_url = f"{GITHUB_API}/repos/{owner}/{repo}/contents/{path}?ref={ref}"
    r = requests.get(api_url, headers=github_api_headers(token), timeout=30)
    r.raise_for_status()
    data = r.json()

    if data.get("encoding") != "base64" or "content" not in data:
        raise RuntimeError(f"Unexpected GitHub response for {owner}/{repo}/{path}")

    return base64.b64decode(data["content"]).decode("utf-8", errors="replace")

def github_get_last_commit_for_file(owner: str, repo: str, path: str, ref: str, token: str) -> dict | None:
    """
    GET /repos/{owner}/{repo}/commits?path=...&sha=...&per_page=1
    Returns: {authorLogin, authorName, date, url} or None.
    """
    url = f"{GITHUB_API}/repos/{owner}/{repo}/commits"
    params = {"path": path, "sha": ref, "per_page": 1}
    r = requests.get(url, headers=github_api_headers(token), params=params, timeout=30)
    if r.status_code in (401, 403):
        return None
    r.raise_for_status()
    arr = r.json() or []
    if not arr:
        return None

    c = arr[0]
    author_login = (c.get("author") or {}).get("login") or ""
    author_name = (((c.get("commit") or {}).get("author") or {}).get("name")) or ""
    date = (((c.get("commit") or {}).get("author") or {}).get("date")) or ""
    html_url = c.get("html_url") or ""
    return {"authorLogin": author_login, "authorName": author_name, "date": date, "url": html_url}


def github_list_commits_for_file(owner: str, repo: str, path: str, ref: str, token: str, *, per_page: int = 20, page: int = 1) -> list[dict]:
    """Return recent commits for a file (newest first). Supports pagination via page."""
    url = f"{GITHUB_API}/repos/{owner}/{repo}/commits"
    params = {"path": path, "sha": ref, "per_page": int(per_page), "page": int(page)}
    r = requests.get(url, headers=github_api_headers(token), params=params, timeout=30)
    if r.status_code in (401, 403):
        return []
    r.raise_for_status()
    return r.json() or []


def _commits_spanning_days(owner: str, repo: str, path: str, ref: str, token: str, days: int, max_pages: int) -> tuple[list[dict], list[str]]:
    """Fetch commits for a file until we span `days` or hit max_pages. Returns (commits, warnings)."""
    per_page = min(100, BOOTSTRAP_COMMITS_TO_SCAN_PER_FILE)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat().replace("+00:00", "Z")
    all_commits: list[dict] = []
    warnings: list[str] = []

    for page in range(1, max_pages + 1):
        batch = github_list_commits_for_file(owner, repo, path, ref, token, per_page=per_page, page=page)
        if not batch:
            break
        all_commits.extend(batch)
        oldest = batch[-1]
        date_str = (((oldest.get("commit") or {}).get("author") or {}).get("date")) or ""
        if date_str and date_str < cutoff:
            break
        if len(batch) < per_page:
            break

    if all_commits:
        oldest_date = (((all_commits[-1].get("commit") or {}).get("author") or {}).get("date")) or ""
        if oldest_date and oldest_date >= cutoff and len(all_commits) >= per_page * max_pages:
            warnings.append(
                f"GitHub commits for {owner}/{repo}/{path}: hit max pages ({max_pages}); "
                f"events may not span full {days} days. Consider increasing RELEASE_HISTORY_BOOTSTRAP_MAX_PAGES."
            )
    return all_commits, warnings


# ============================================================
# Argo CD (MVP1) - Read-only health/sync signal
#
# Assumptions:
# - Argo hosts and app naming rules are defined in project YAML under `argocd:`
# - Tokens are provided via env vars:
#     ARGOCD_TOKEN           (fallback for all)
#     ARGOCD_TOKEN_<ENVKEY>  (e.g. ARGOCD_TOKEN_DEV / ARGOCD_TOKEN_QA / ...)
# - We never trigger syncs. This is observation-only.
# ============================================================

def _argocd_pick_token(env_key: str) -> str:
    tok = _strip(os.getenv("ARGOCD_TOKEN"))
    if tok:
        return tok
    tok = _strip(os.getenv(f"ARGOCD_TOKEN_{(env_key or '').upper()}"))
    return tok


def _argocd_host_for_env(argocd_cfg: dict, env_key: str) -> str:
    env_hosts = (argocd_cfg or {}).get("env_hosts") or {}
    # config may use uppercase keys (DEV/QA/...) while our env keys are usually lowercase.
    env_up = (env_key or "").upper()
    dev_host_envs = {str(x).upper() for x in ((argocd_cfg or {}).get("dev_host_envs") or [])}
    env_hosts_up = {str(k).upper(): v for k, v in env_hosts.items()}
    if env_up in dev_host_envs and "DEV" in env_hosts_up:
        return _strip(env_hosts_up.get("DEV"))
    return _strip(env_hosts_up.get(env_up))


def _argocd_app_name_for_env(argocd_cfg: dict, env_key: str, base_app: str) -> str:
    rules = (argocd_cfg or {}).get("app_name_rules") or {}
    env_up = (env_key or "").upper()
    rules_up = {str(k).upper(): v for k, v in rules.items()}
    tmpl = rules_up.get(env_up) or "{app}"
    try:
        return tmpl.format(app=base_app)
    except Exception:
        return base_app


def _argocd_api_headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "User-Agent": "release-ops-snapshot",
    }


def argocd_fetch_app_status(host: str, token: str, app_name: str) -> dict:
    """Fetch ArgoCD application status.

    Returns dict with: {health, sync, appUrl}
    """
    host = host.rstrip("/")
    api = f"{host}/api/v1/applications/{urllib.parse.quote(app_name, safe='') }"
    r = requests.get(api, headers=_argocd_api_headers(token), timeout=25)
    if r.status_code in (401, 403):
        raise PermissionError("ArgoCD: unauthorized")
    if r.status_code == 404:
        return {"health": "Missing", "sync": "Unknown", "appUrl": f"{host}/applications/{app_name}"}
    r.raise_for_status()
    data = r.json() or {}
    st = (data.get("status") or {})
    health = ((st.get("health") or {}).get("status") or "Unknown").strip()
    sync = ((st.get("sync") or {}).get("status") or "Unknown").strip()
    return {"health": health or "Unknown", "sync": sync or "Unknown", "appUrl": f"{host}/applications/{app_name}"}


def _argocd_rank_health(h: str) -> int:
    key = normalize_tag(h)
    if key == "healthy":
        return 0
    if key == "progressing":
        return 1
    if key == "suspended":
        return 2
    if key == "degraded":
        return 3
    if key == "missing":
        return 4
    if key == "unknown":
        return 5
    return 6


def _argocd_rank_sync(s: str) -> int:
    key = normalize_tag(s)
    if key == "synced":
        return 0
    if key == "outofsync":
        return 2
    if key == "unknown":
        return 3
    return 4


def _kustom_tag_signature_from_text(yaml_text: str) -> str:
    """Return a stable signature of tags/newTags found in kustomization.

    We only care about tag values (because we want "last deployer" to change
    only when the tag changes). Other infra changes (nodeSelector, resources, etc.)
    must NOT affect deployer.
    """
    try:
        comps = extract_components_from_kustomization(yaml_text)
    except Exception:
        comps = []
    tags = []
    for c in comps:
        t = (c.get("tag") or c.get("newTag") or "").strip()
        if t:
            tags.append(normalize_tag(t))
    tags = sorted(set(tags))
    return "|".join(tags)


def github_find_last_tag_change_commit(owner: str, repo: str, path: str, ref: str, token: str, *, current_signature: str, commits_to_scan: int = 12) -> dict | None:
    """Find the most recent commit that changed the tag(s) to the current value.

    We fetch a short commit list for the kustomization.yaml and compare the tag
    signature between adjacent commits:
      - if commit[i] signature == current_signature
      - and commit[i+1] signature != current_signature
    then commit[i] is the tag-changing commit.

    If we can't prove it, we fall back to the latest commit (old behaviour).
    """
    commits = github_list_commits_for_file(owner, repo, path, ref, token, per_page=commits_to_scan)
    if not commits:
        return None

    # Cache file contents per SHA to avoid repeat calls if same SHA appears.
    sig_cache: dict[str, str] = {}

    def sig_for_sha(sha: str) -> str:
        if sha in sig_cache:
            return sig_cache[sha]
        try:
            txt = fetch_github_file(owner, repo, path, sha, token)
            sig = _kustom_tag_signature_from_text(txt)
        except Exception:
            sig = ""
        sig_cache[sha] = sig
        return sig

    # Iterate adjacent commits
    for i in range(0, len(commits) - 1):
        sha_new = commits[i].get("sha") or ""
        sha_old = commits[i + 1].get("sha") or ""
        if not sha_new or not sha_old:
            continue
        sig_new = sig_for_sha(sha_new)
        sig_old = sig_for_sha(sha_old)
        if sig_new == current_signature and sig_old != current_signature:
            c = commits[i]
            author_login = (c.get("author") or {}).get("login") or ""
            author_name = (((c.get("commit") or {}).get("author") or {}).get("name")) or ""
            date = (((c.get("commit") or {}).get("author") or {}).get("date")) or ""
            html_url = c.get("html_url") or ""
            return {"authorLogin": author_login, "authorName": author_name, "date": date, "url": html_url}

    # If current signature isn't even present in the newest commit snapshot, don't guess.
    # Otherwise: fallback to latest commit metadata.
    sha0 = commits[0].get("sha") or ""
    if sha0 and sig_for_sha(sha0) == current_signature:
        c = commits[0]
        author_login = (c.get("author") or {}).get("login") or ""
        author_name = (((c.get("commit") or {}).get("author") or {}).get("name")) or ""
        date = (((c.get("commit") or {}).get("author") or {}).get("date")) or ""
        html_url = c.get("html_url") or ""
        return {"authorLogin": author_login, "authorName": author_name, "date": date, "url": html_url}
    return None

# ============================================================
# Kustomization parsing
# ============================================================

TAG_RE = re.compile(r"^(?P<service>.+)-v\.?((?P<ver>\d+\.\d+\.\d+))$")

def normalize_tag(tag: str) -> str:
    """Normalize common tag variants so parsing is resilient.

    We have seen tags like `v.0.0.588` (dot after `v`) in some infra repos.
    TeamCity/image tags typically use `v0.0.588`.
    """
    if not tag:
        return ""
    t = str(tag).strip()
    # v.0.0.588 -> v0.0.588
    t = re.sub(r"^v\.(?=\d)", "v", t)
    # service-v.0.0.588 -> service-v0.0.588
    t = re.sub(r"-v\.(?=\d)", "-v", t)
    return t

def extract_build_number(tag: str) -> str:
    """
    Extracts build number from:
      - my-service-air-v0.0.112 -> 112
      - v0.0.112                -> 112
    """
    tag = normalize_tag(tag)
    if not tag:
        return ""
    m = re.search(r"v\d+\.\d+\.(\d+)$", tag)
    return m.group(1) if m else ""

def derive_service_key(image_name: str, tag: str) -> str:
    """
    Prefer service key from tag prefix (my-service-air from my-service-air-v0.0.112),
    fallback to last segment of image name.
    """
    tag = normalize_tag(tag)
    if tag:
        m = TAG_RE.match(tag)
        if m:
            return m.group("service")
    if image_name:
        return image_name.split("/")[-1]
    return "unknown"

def extract_components_from_kustomization(yaml_text: str) -> list[dict]:
    """
    Supports:
    images:
      - name: 123456789.dkr.ecr.region.amazonaws.com/my-project
        newTag: my-service-v0.0.112
    """
    doc = yaml.safe_load(yaml_text) or {}
    images = doc.get("images", []) or []
    components: list[dict] = []

    for img in images:
        if not isinstance(img, dict):
            continue
        image_name = (img.get("name") or img.get("newName") or "").strip()
        tag = (img.get("newTag") or "").strip()

        service_key = derive_service_key(image_name, tag)
        build_no = extract_build_number(tag)

        components.append({
            "name": service_key,   # displayed as repository/service name
            "image": image_name,
            "tag": tag,
            "build": build_no,
        })

    return components

# ============================================================
# TeamCity helpers
# ============================================================

def teamcity_rest_base(base: str) -> str:
    """
    Accepts:
      - https://teamcity.example.com
      - https://teamcity.example.com/app/rest
      - https://teamcity.example.com/httpAuth/app/rest (legacy)
    Returns: https://teamcity.example.com/app/rest
    """
    b = (base or "").strip().rstrip("/")
    if not b:
        return ""
    b = b.replace("/httpAuth", "")
    if "/app/rest" in b:
        i = b.find("/app/rest")
        return b[: i + len("/app/rest")]
    return b + "/app/rest"

def teamcity_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Accept": "application/json"}

def parse_iso_teamcity(s: str) -> str:
    """
    TeamCity typical: 20251204T141343+0000
    Convert to ISO8601 Z.
    """
    if not s:
        return ""
    try:
        dt = datetime.strptime(s, "%Y%m%dT%H%M%S%z")
        return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    except Exception:
        return ""

def teamcity_get_build_id_by_number(rest_base: str, token: str, build_type_id: str, build_number: str) -> int | None:
    """
    Find a build by buildType id + build number, across all branches.
    """
    if not (rest_base and token and build_type_id and build_number):
        return None

    locator = f"buildType:(id:{build_type_id}),number:{build_number},branch:(default:any),state:finished"
    url = f"{rest_base}/builds"
    params = {"locator": locator, "fields": "build(id,href)"}

    r = requests.get(url, headers=teamcity_headers(token), params=params, timeout=30)
    if r.status_code in (401, 403):
        return None
    r.raise_for_status()

    data = r.json() or {}
    builds = data.get("build") or []
    if not builds:
        return None
    return builds[0].get("id")

def teamcity_get_build_details(rest_base: str, token: str, build_id: int) -> dict | None:
    """
    GET /builds/id:{id}?fields=...
    
    Returns build details including startDate and finishDate for time-aware correlation.
    """
    if not (rest_base and token and build_id):
        return None
    fields = ",".join([
        "id",
        "number",
        "status",
        "state",
        "branchName",
        "defaultBranch",
        "webUrl",
        "startDate",  # Added for time-aware correlation: build.startedAt >= pr.mergedAt
        "finishDate",
        "finishOnAgentDate",
        "triggered(user(username,name))",
        "buildTypeId",
    ])
    url = f"{rest_base}/builds/id:{build_id}"
    params = {"fields": fields}
    r = requests.get(url, headers=teamcity_headers(token), params=params, timeout=30)
    if r.status_code in (401, 403):
        return None
    r.raise_for_status()
    return r.json() or {}

def teamcity_get_build(rest_base: str, token: str, build_type_id: str, build_number: str) -> dict | None:
    build_id = teamcity_get_build_id_by_number(rest_base, token, build_type_id, build_number)
    if not build_id:
        return None
    return teamcity_get_build_details(rest_base, token, build_id)

def build_kustomization_candidates(env_key: str) -> list[str]:
    """Return candidate paths for a given env.

    In practice we see both kustomization.yaml and kustomization.yml, and
    both envs/{env} and overlays/{env} layouts. We try envs/ first, then overlays/.
    """
    env_key = (env_key or "").strip().lower()
    return [
        f"envs/{env_key}/kustomization.yaml",
        f"envs/{env_key}/kustomization.yml",
        f"overlays/{env_key}/kustomization.yaml",
        f"overlays/{env_key}/kustomization.yml",
    ]


def fetch_kustomization_text(owner: str, infra_repo: str, ref: str, env_key: str, token: str) -> tuple[str, str]:
    """Fetch kustomization file content.

    Returns: (yaml_text, path_used)
    """
    last_err: Exception | None = None
    for path in build_kustomization_candidates(env_key):
        try:
            return fetch_github_file(owner, infra_repo, path, ref, token), path
        except Exception as e:
            last_err = e
            continue
    raise RuntimeError(f"Nie znaleziono kustomization.(yaml|yml) dla {infra_repo} env={env_key}: {last_err}")

# ============================================================
# Output
# ============================================================

def write_latest_json(payload: dict):
    """Write latest.json atomically with file locking."""
    repo_root = Path(__file__).resolve().parents[2]  # .../release-ops-control-room-main
    out_path = repo_root / "data" / "latest.json"
    atomic_write_json(out_path, payload)
    logger.info("snapshot_generated", output_path=str(out_path))



# ------------------------------------------------------------
# RELEASE HISTORY STAGE 1 (MVP1): snapshot archiving + diff events
# Source of truth: snapshots (latest.json archived under data/history).
# No extra API calls. UI remains read-only.
# ------------------------------------------------------------
HISTORY_SNAPSHOTS_KEEP = 100
RELEASE_HISTORY_MAX_EVENTS_PER_PROJECT = 2000
BOOTSTRAP_EVENTS_PER_ENV = 10
BOOTSTRAP_COMMITS_TO_SCAN_PER_FILE = 60

# Bootstrap: collect events spanning ~60 days (GitHub kustomization history)
RELEASE_HISTORY_BOOTSTRAP_DAYS = int(os.getenv("RELEASE_HISTORY_BOOTSTRAP_DAYS", "60"))
BOOTSTRAP_MAX_PAGES = int(os.getenv("RELEASE_HISTORY_BOOTSTRAP_MAX_PAGES", "20"))
RELEASE_HISTORY_BACKFILL_60_DAYS = os.getenv("RELEASE_HISTORY_BACKFILL_60_DAYS", "1").strip().lower() in ("1", "true", "yes", "on")

# Enterprise Release History: Append-only storage configuration
RELEASE_HISTORY_RETENTION_DAYS = int(os.getenv("RELEASE_HISTORY_RETENTION_DAYS", "90"))
RELEASE_HISTORY_DEFAULT_LIMIT = int(os.getenv("RELEASE_HISTORY_DEFAULT_LIMIT", "20"))
RELEASE_HISTORY_APPEND_ONLY = os.getenv("RELEASE_HISTORY_APPEND_ONLY", "1").strip().lower() in ("1", "true", "yes", "on")


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _history_dir() -> Path:
    d = _repo_root() / 'data' / 'history'
    d.mkdir(parents=True, exist_ok=True)
    return d


def _release_history_path() -> Path:
    """Legacy release history path (for backward compatibility)."""
    p = _repo_root() / 'data' / 'release_history.json'
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _release_history_dir() -> Path:
    """Enterprise release history directory (append-only storage)."""
    d = _repo_root() / 'data' / 'release_history'
    d.mkdir(parents=True, exist_ok=True)
    return d


def _release_history_events_path() -> Path:
    """Path to append-only events.jsonl file."""
    return _release_history_dir() / 'events.jsonl'


def _release_history_index_path() -> Path:
    """Path to metadata index.json file."""
    return _release_history_dir() / 'index.json'


def _release_history_archive_dir() -> Path:
    """Path to archived events directory."""
    d = _release_history_dir() / 'archive'
    d.mkdir(parents=True, exist_ok=True)
    return d


def _safe_ts_for_filename(ts_iso: str) -> str:
    # Example: 2026-01-19T12:34:56Z -> 2026-01-19T12-34-56Z
    return (ts_iso or '').replace(':', '-').replace('.', '-')


def _list_history_snapshots() -> list[Path]:
    d = _history_dir()
    files = sorted(d.glob('*.json'))
    return files


def load_previous_snapshot_from_history() -> dict | None:
    files = _list_history_snapshots()
    if not files:
        return None
    # newest file is last (lexicographic works with ISO-like names)
    try:
        return json.loads(files[-1].read_text(encoding='utf-8'))
    except Exception:
        return None


def archive_latest_snapshot(current_payload: dict):
    repo_root = _repo_root()
    latest_path = repo_root / 'data' / 'latest.json'
    if not latest_path.exists():
        return

    ts = _safe_ts_for_filename(current_payload.get('generatedAt') or iso_now())
    out = _history_dir() / f'{ts}.json'

    # Avoid duplicate writes for the same timestamp
    if not out.exists():
        out.write_text(latest_path.read_text(encoding='utf-8'), encoding='utf-8')

    # Retention
    files = _list_history_snapshots()
    if len(files) > HISTORY_SNAPSHOTS_KEEP:
        to_delete = files[: max(0, len(files) - HISTORY_SNAPSHOTS_KEEP)]
        for f in to_delete:
            try:
                f.unlink()
            except Exception:
                pass


def _extract_commit_sha(commit_url: str) -> str:
    # GitHub commit URL often ends with /commit/<sha>
    if not commit_url:
        return ''
    parts = commit_url.rstrip('/').split('/')
    if 'commit' in parts:
        i = parts.index('commit')
        if i + 1 < len(parts):
            return parts[i + 1]
    # fallback: last segment
    return parts[-1] if parts else ''


def _component_map(snapshot: dict) -> dict[tuple[str, str, str], dict]:
    out: dict[tuple[str, str, str], dict] = {}
    for proj in (snapshot or {}).get('projects') or []:
        pkey = (proj.get('key') or '').strip()
        for env in proj.get('environments') or []:
            ekey = (env.get('key') or '').strip().lower()
            for comp in env.get('components') or []:
                cname = (comp.get('name') or '').strip()
                if pkey and ekey and cname:
                    out[(pkey, ekey, cname)] = {
                        'project': proj,
                        'env': env,
                        'comp': comp,
                    }
    return out


def _mk_history_event(project_key: str, env: dict, comp: dict, from_tag: str, to_tag: str) -> dict:
    env_key = _normalize_env_key(env.get('key')) or ""
    env_name = (env.get('name') or env_key.upper()).strip()
    comp_name = (comp.get('name') or '').strip()

    commit_url = (comp.get('deployerCommitUrl') or '').strip()
    kustom_url = (comp.get('kustomizationUrl') or '').strip()
    build_url = (comp.get('buildUrl') or '').strip()

    at = (comp.get('deployedAt') or '').strip() or iso_now()
    by = (comp.get('deployer') or '').strip() or (env.get('deployer') or '').strip()

    sha = _extract_commit_sha(commit_url)
    ev_id = f"{sha}:{project_key}:{env_key}:{comp_name}:{to_tag}" if sha else f"{project_key}:{env_key}:{comp_name}:{to_tag}:{at}"

    links = []
    if commit_url:
        links.append({'type': 'commit', 'label': 'Commit', 'url': commit_url})
    if kustom_url:
        links.append({'type': 'kustomization', 'label': 'Kustomization', 'url': kustom_url})
    if build_url:
        links.append({'type': 'teamcity', 'label': 'TeamCity', 'url': build_url})

    warnings = []
    comp_w = comp.get('warnings')
    if isinstance(comp_w, list) and comp_w:
        warnings.append({'code': 'HISTORY_PARTIAL', 'message': 'Historical entry may be partial due to missing metadata.'})

    return {
        'id': ev_id,
        'kind': 'TAG_CHANGE',
        'bootstrap': False,
        'projectKey': project_key,
        'envKey': env_key,
        'envName': env_name,
        'component': comp_name,
        'fromTag': (from_tag or '').strip(),
        'toTag': (to_tag or '').strip(),
        'fromBuild': extract_build_number(from_tag or '') or '',
        'toBuild': extract_build_number(to_tag or '') or '',
        'at': at,
        'by': by,
        'commitUrl': commit_url,
        'kustomizationUrl': kustom_url,
        'links': links,
        **({'warnings': warnings} if warnings else {}),
    }


def compute_tag_change_events(prev_snapshot: dict | None, current_snapshot: dict) -> dict[str, list[dict]]:
    prev_map = _component_map(prev_snapshot or {})
    cur_map = _component_map(current_snapshot or {})
    out: dict[str, list[dict]] = {}

    for key, cur in cur_map.items():
        pkey, ekey, cname = key
        cur_comp = cur['comp']
        cur_env = cur['env']
        cur_tag = (cur_comp.get('tag') or '').strip()

        prev_tag = ''
        if key in prev_map:
            prev_tag = (prev_map[key]['comp'].get('tag') or '').strip()

        if prev_tag and cur_tag and prev_tag != cur_tag:
            ev = _mk_history_event(pkey, cur_env, cur_comp, prev_tag, cur_tag)
            out.setdefault(pkey, []).append(ev)

    # Sort per project by event time desc (ISO)
    for pk in out:
        out[pk].sort(key=lambda e: e.get('at') or '', reverse=True)
    return out


def _bootstrap_events_for_component(project_key: str, env_key: str, env_name: str, comp_name: str, kustomization_url: str, token: str) -> tuple[list[dict], list[str]]:
    """Reconstruct recent tag changes for a single component from GitHub commit history.

    Source of truth: commits of the kustomization.yaml file in the infra repo.
    We detect tag changes by comparing tag signatures between adjacent commits.
    Fetches commits spanning RELEASE_HISTORY_BOOTSTRAP_DAYS (~60 days).
    Returns (events, warnings).
    """
    if not kustomization_url:
        return [], []

    try:
        owner, repo, ref, path = parse_github_blob_url(kustomization_url)
    except Exception:
        return [], []

    commits, span_warnings = _commits_spanning_days(
        owner, repo, path, ref, token,
        days=RELEASE_HISTORY_BOOTSTRAP_DAYS,
        max_pages=BOOTSTRAP_MAX_PAGES,
    )
    if not commits or len(commits) < 2:
        return [], list(span_warnings)

    sig_cache: dict[str, str] = {}

    def sig_for_sha(sha: str) -> str:
        if sha in sig_cache:
            return sig_cache[sha]
        try:
            txt = fetch_github_file(owner, repo, path, sha, token)
            sig = _kustom_tag_signature_from_text(txt)
        except Exception:
            sig = ""
        sig_cache[sha] = sig
        return sig

    out: list[dict] = []

    for i in range(0, len(commits) - 1):
        c_new = commits[i]
        c_old = commits[i + 1]
        sha_new = c_new.get("sha") or ""
        sha_old = c_old.get("sha") or ""
        if not sha_new or not sha_old:
            continue

        sig_new = sig_for_sha(sha_new)
        sig_old = sig_for_sha(sha_old)
        if not sig_new or not sig_old:
            continue

        if sig_new == sig_old:
            continue

        # Best-effort from/to tags
        new_tags = [t for t in (sig_new.split("|") if sig_new else []) if t]
        old_tags = [t for t in (sig_old.split("|") if sig_old else []) if t]
        from_tag = old_tags[0] if len(old_tags) == 1 else sig_old
        to_tag = new_tags[0] if len(new_tags) == 1 else sig_new

        author_login = (c_new.get("author") or {}).get("login") or ""
        author_name = (((c_new.get("commit") or {}).get("author") or {}).get("name")) or ""
        date = (((c_new.get("commit") or {}).get("author") or {}).get("date")) or ""
        commit_url = c_new.get("html_url") or ""

        ev_id = f"bootstrap:{sha_new}:{project_key}:{env_key}:{comp_name}:{to_tag}"

        warnings = []
        if len(old_tags) != 1 or len(new_tags) != 1:
            warnings.append({
                "code": "BOOTSTRAP_MULTI_TAG_CHANGE",
                "message": "Bootstrap entry reconstructed from multiple tag changes in one commit (partial).",
            })

        out.append({
            "id": ev_id,
            "kind": "TAG_CHANGE",
            "bootstrap": True,
            "projectKey": project_key,
            "envKey": env_key,
            "envName": env_name,
            "component": comp_name,
            "fromTag": from_tag,
            "toTag": to_tag,
            "fromBuild": extract_build_number(from_tag or "") or "",
            "toBuild": extract_build_number(to_tag or "") or "",
            "at": date or iso_now(),
            "by": author_login or author_name,
            "commitUrl": commit_url,
            "kustomizationUrl": github_blob_url(owner, repo, sha_new, path),
            "links": [
                *([{ "type": "commit", "label": "Commit", "url": commit_url }] if commit_url else []),
                {"type": "kustomization", "label": "Kustomization", "url": github_blob_url(owner, repo, sha_new, path)},
            ],
            **({"warnings": warnings} if warnings else {}),
        })

    # newest first
    out.sort(key=lambda e: e.get("at") or "", reverse=True)
    return out, list(span_warnings)


def compute_bootstrap_events(current_payload: dict, token: str) -> tuple[dict[str, list[dict]], list[str]]:
    """Compute bootstrap events: tag changes per project+env spanning ~60 days.

    We reconstruct events from GitHub commit history of the infra kustomization files.
    Fetches commits spanning RELEASE_HISTORY_BOOTSTRAP_DAYS. Keeps all deduped events
    (no per-env cap) so we retain ~60 days of data. Returns (events_by_project, warnings).
    """
    projects = (current_payload or {}).get("projects") or []
    out: dict[str, list[dict]] = {}
    all_warnings: list[str] = []

    # Collect candidate events per project/env by scanning each component's kustomization history.
    for proj in projects:
        pkey = (proj.get("key") or "").strip()
        if not pkey:
            continue
        for env in proj.get("environments") or []:
            env_key = _normalize_env_key(env.get("key")) or ""
            env_name = (env.get("name") or env_key.upper()).strip() or env_key
            if not env_key:
                continue

            candidates: list[dict] = []
            for comp in env.get("components") or []:
                comp_name = (comp.get("name") or "").strip()
                kustom_url = (comp.get("kustomizationUrl") or "").strip()
                if not comp_name or not kustom_url:
                    continue
                evs, w = _bootstrap_events_for_component(pkey, env_key, env_name, comp_name, kustom_url, token)
                candidates.extend(evs)
                all_warnings.extend(w)

            # Dedupe by signature (project, env, component, fromTag, toTag, at) to catch duplicates
            # even if IDs differ. Also dedupe by id for backward compatibility.
            def _event_signature(event: dict) -> tuple:
                """Create unique signature for event deduplication."""
                return (
                    event.get("projectKey", ""),
                    event.get("envKey", ""),
                    event.get("component", ""),
                    event.get("fromTag", ""),
                    event.get("toTag", ""),
                    (event.get("at") or "")[:19],  # Truncate to second precision (YYYY-MM-DDTHH:MM:SS)
                )
            
            candidates.sort(key=lambda e: e.get("at") or "", reverse=True)
            seen_ids: set[str] = set()
            seen_signatures: set[tuple] = set()
            trimmed: list[dict] = []
            for ev in candidates:
                eid = ev.get("id")
                sig = _event_signature(ev)
                
                # Skip if we've seen this ID or signature before
                if (eid and eid in seen_ids) or sig in seen_signatures:
                    continue
                
                if eid:
                    seen_ids.add(eid)
                seen_signatures.add(sig)
                trimmed.append(ev)

            if trimmed:
                out.setdefault(pkey, []).extend(trimmed)

    # Final sort per project; cap per project to avoid explosion
    for pk in out:
        out[pk].sort(key=lambda e: e.get("at") or "", reverse=True)
        out[pk] = out[pk][:RELEASE_HISTORY_MAX_EVENTS_PER_PROJECT]

    return out, all_warnings


# ============================================================================
# ENTERPRISE RELEASE HISTORY: Append-Only Storage
# ============================================================================
# This module implements append-only storage for release history events,
# enabling scalable, performant history tracking without loading entire
# datasets into memory.
# ============================================================================

def _load_release_history_index() -> dict:
    """Load metadata index.json (lightweight, fast) with proper error handling."""
    index_path = _release_history_index_path()
    
    # Default index structure
    default_index = {
        "version": "2.0",
        "generatedAt": iso_now(),
        "retention": {
            "days": RELEASE_HISTORY_RETENTION_DAYS,
            "lastCleanup": None,
        },
        "stats": {
            "totalEvents": 0,
            "oldestEvent": None,
            "newestEvent": None,
        },
        "projects": {},
    }
    
    if not index_path.exists():
        return default_index
    
    # Use safe_read_json to handle errors gracefully (no recursion)
    loaded = safe_read_json(index_path, default=default_index)
    return loaded if loaded is not None else default_index


def _save_release_history_index(index: dict) -> None:
    """Save metadata index.json atomically with file locking."""
    index_path = _release_history_index_path()
    index["generatedAt"] = iso_now()
    atomic_write_json(index_path, index)


def _update_release_history_index(new_events_by_project: dict[str, list[dict]]) -> None:
    """Update metadata index with new events (thread-safe with retry logic).
    
    This function uses a retry loop to handle concurrent updates safely.
    If multiple snapshots run simultaneously, they will retry until successful.
    """
    index_path = _release_history_index_path()
    max_retries = 5
    retry_delay = 0.1
    
    for attempt in range(max_retries):
        try:
            # Read current index (with shared lock if available)
            index = _load_release_history_index()
            
            # Store original modification time if file exists (for conflict detection)
            original_mtime = None
            if index_path.exists():
                try:
                    original_mtime = index_path.stat().st_mtime
                except OSError:
                    pass
            
            # Modify in memory
            for project_key, events in new_events_by_project.items():
                if not events:
                    continue
                
                project_meta = index.setdefault("projects", {}).setdefault(project_key, {
                    "eventCount": 0,
                    "firstEventAt": None,
                    "lastEventAt": None,
                    "environments": [],
                })
                
                # Update event count
                project_meta["eventCount"] += len(events)
                
                # Update timestamps
                for event in events:
                    event_at = event.get("at") or ""
                    if not event_at:
                        continue
                    
                    # Update first/last event timestamps
                    if not project_meta["firstEventAt"] or event_at < project_meta["firstEventAt"]:
                        project_meta["firstEventAt"] = event_at
                    if not project_meta["lastEventAt"] or event_at > project_meta["lastEventAt"]:
                        project_meta["lastEventAt"] = event_at
                    
                    # Track environments
                    env_key = _normalize_env_key(event.get("envKey"))
                    if env_key:
                        if env_key not in project_meta["environments"]:
                            project_meta["environments"].append(env_key)
                
                # Ensure environments is a sorted list
                project_meta["environments"] = sorted(list(set(project_meta["environments"])))
            
            # Update global stats (approximate, doesn't require full scan)
            total_events = sum(p.get("eventCount", 0) for p in index.get("projects", {}).values())
            index["stats"]["totalEvents"] = total_events
            
            # Update oldest/newest (from project metadata)
            all_first = [p.get("firstEventAt") for p in index.get("projects", {}).values() if p.get("firstEventAt")]
            all_last = [p.get("lastEventAt") for p in index.get("projects", {}).values() if p.get("lastEventAt")]
            
            if all_first:
                index["stats"]["oldestEvent"] = min(all_first)
            if all_last:
                index["stats"]["newestEvent"] = max(all_last)
            
            # Write with exclusive lock (atomic_write_json handles this)
            _save_release_history_index(index)
            
            # Check if file was modified during our operation (conflict detection)
            if original_mtime is not None and index_path.exists():
                try:
                    current_mtime = index_path.stat().st_mtime
                    # If file was modified by another process, retry
                    if current_mtime > original_mtime + 0.1:  # Small tolerance for filesystem precision
                        if attempt < max_retries - 1:
                            time.sleep(retry_delay * (attempt + 1))
                            continue
                except OSError:
                    pass  # Ignore stat errors, assume success
            
            return  # Success
            
        except (IOError, OSError) as e:
            if attempt < max_retries - 1:
                wait_time = retry_delay * (attempt + 1)
                logger.warn("index_update_retry", attempt=attempt+1, error=str(e), wait_seconds=wait_time)
                time.sleep(wait_time)
                continue
            else:
                logger.error("index_update_failed", error=str(e), attempts=max_retries)
                raise


def _release_history_existing_ids() -> set[str]:
    """Stream events.jsonl and return set of all event ids. Used for backfill dedup."""
    events_path = _release_history_events_path()
    ids: set[str] = set()
    if not events_path.exists():
        return ids
    try:
        with open(events_path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    eid = obj.get("id")
                    if eid:
                        ids.add(eid)
                except Exception:
                    pass
    except Exception:
        pass
    return ids


def _append_events_to_jsonl(events_by_project: dict[str, list[dict]]) -> None:
    """Append new events to events.jsonl atomically with file locking."""
    events_path = _release_history_events_path()
    
    # Flatten events from all projects into single list
    all_events: list[dict] = []
    for project_key, events in events_by_project.items():
        for event in events:
            # Ensure event has required fields
            if not event.get("id"):
                continue
            all_events.append(event)
    
    # Atomic append with locking
    if all_events:
        atomic_append_jsonl(events_path, all_events)


def _apply_retention_policy() -> None:
    """Apply retention policy: archive events older than retention period.
    
    This runs periodically (not every snapshot) to avoid performance impact.
    """
    index = _load_release_history_index()
    retention_days = index.get("retention", {}).get("days", RELEASE_HISTORY_RETENTION_DAYS)
    last_cleanup = index.get("retention", {}).get("lastCleanup")
    
    # Run cleanup at most once per day
    if last_cleanup:
        try:
            last_cleanup_dt = datetime.fromisoformat(last_cleanup.replace("Z", "+00:00"))
            if (datetime.now(timezone.utc) - last_cleanup_dt).days < 1:
                return  # Skip if cleaned up in last 24 hours
        except Exception:
            pass  # Continue if date parsing fails
    
    cutoff_date = datetime.now(timezone.utc) - timedelta(days=retention_days)
    cutoff_iso = cutoff_date.isoformat().replace("+00:00", "Z")
    
    events_path = _release_history_events_path()
    if not events_path.exists():
        return  # No events to archive
    
    # Read events, separate by date
    events_to_keep: list[dict] = []
    events_to_archive: list[dict] = []
    archived_count = 0
    
    try:
        with open(events_path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                    event_at = event.get("at") or ""
                    if event_at and event_at < cutoff_iso:
                        events_to_archive.append(event)
                        archived_count += 1
                    else:
                        events_to_keep.append(event)
                except Exception:
                    continue  # Skip malformed lines
        
        # Archive old events (if any)
        if events_to_archive:
            archive_dir = _release_history_archive_dir()
            archive_file = archive_dir / f"events-{cutoff_date.strftime('%Y-%m')}.jsonl"
            
            # Append to monthly archive file
            with open(archive_file, 'a', encoding='utf-8') as f:
                for event in events_to_archive:
                    f.write(json.dumps(event, ensure_ascii=False) + '\n')
            
            # Rewrite events.jsonl with only recent events
            with open(events_path, 'w', encoding='utf-8') as f:
                for event in events_to_keep:
                    f.write(json.dumps(event, ensure_ascii=False) + '\n')
            
            # Update index
            index["retention"]["lastCleanup"] = iso_now()
            index["stats"]["totalEvents"] = len(events_to_keep)
            _save_release_history_index(index)
            
            logger.info("release_history_archived", archived_count=archived_count, retention_days=retention_days)
    except Exception as e:
        logger.warn("release_history_retention_cleanup_failed", error=str(e))


def _migrate_legacy_release_history() -> bool:
    """Migrate legacy release_history.json to append-only format.
    
    Returns True if migration was performed, False otherwise.
    """
    old_path = _release_history_path()
    if not old_path.exists():
        return False  # No legacy data to migrate
    
    events_path = _release_history_events_path()
    if events_path.exists():
        return False  # Already migrated
    
    try:
        print("[INFO] Release History: Migrating legacy format to append-only storage...")
        
        # Read legacy format
        old_data = json.loads(old_path.read_text(encoding='utf-8'))
        projects_obj = old_data.get('projects', {})
        
        # Convert to JSONL
        event_count = 0
        with open(events_path, 'w', encoding='utf-8') as f:
            for project_key, project_data in projects_obj.items():
                events = project_data.get('events', [])
                for event in events:
                    f.write(json.dumps(event, ensure_ascii=False) + '\n')
                    event_count += 1
        
        # Generate index from migrated events
        index = {
            "version": "2.0",
            "generatedAt": iso_now(),
            "retention": {
                "days": RELEASE_HISTORY_RETENTION_DAYS,
                "lastCleanup": None,
            },
            "stats": {
                "totalEvents": event_count,
                "oldestEvent": None,
                "newestEvent": None,
            },
            "projects": {},
        }
        
        # Build project metadata
        for project_key, project_data in projects_obj.items():
            events = project_data.get('events', [])
            if not events:
                continue
            
            meta = project_data.get('meta', {})
            first_event_at = meta.get('firstEventAt')
            last_event_at = meta.get('lastEventAt')
            
            # Extract environments from events
            envs = set()
            for event in events:
                env_key = _normalize_env_key(event.get("envKey")) or ""
                if env_key:
                    envs.add(env_key)
            
            index["projects"][project_key] = {
                "eventCount": len(events),
                "firstEventAt": first_event_at,
                "lastEventAt": last_event_at,
                "environments": sorted(list(envs)),
            }
        
        # Update global stats
        all_first = [p.get("firstEventAt") for p in index["projects"].values() if p.get("firstEventAt")]
        all_last = [p.get("lastEventAt") for p in index["projects"].values() if p.get("lastEventAt")]
        if all_first:
            index["stats"]["oldestEvent"] = min(all_first)
        if all_last:
            index["stats"]["newestEvent"] = max(all_last)
        
        _save_release_history_index(index)
        
        # Backup old file
        backup_path = old_path.with_suffix('.json.backup')
        old_path.rename(backup_path)
        
        logger.info("release_history_migrated", event_count=event_count, backup_file=backup_path.name)
        return True
    except Exception as e:
        logger.warn("release_history_migration_failed", error=str(e))
        return False


def update_release_history_append_only(current_payload: dict, prev_snapshot: dict | None, github_token: str) -> None:
    """Update release history using append-only storage (enterprise-grade).
    
    This function:
    - Only appends new events (no reprocessing)
    - Updates lightweight index.json
    - Applies retention policy (periodically)
    - Never loads entire history into memory
    - Snapshot runtime: O(new_events), not O(total_events)
    """
    # Migrate legacy format if needed (one-time)
    if not _release_history_events_path().exists():
        _migrate_legacy_release_history()
    
    # Bootstrap if index is empty
    index = _load_release_history_index()
    if index.get("stats", {}).get("totalEvents", 0) == 0:
        # Bootstrap: compute initial events spanning ~60 days
        boot_events, bootstrap_warnings = compute_bootstrap_events(current_payload, github_token)
        if boot_events:
            _append_events_to_jsonl(boot_events)
            _update_release_history_index(boot_events)
        if bootstrap_warnings:
            index = _load_release_history_index()
            index["bootstrapWarnings"] = list(dict.fromkeys(bootstrap_warnings))  # dedupe, preserve order
            _save_release_history_index(index)
            for w in bootstrap_warnings:
                logger.warn("release_history_bootstrap_warning", warning=w)
        # Skip backfill when we just bootstrapped
    elif RELEASE_HISTORY_BACKFILL_60_DAYS and not index.get("backfill60DaysRun"):
        # One-time backfill: we have data but < 60 days – fetch older events and append
        oldest = (index.get("stats") or {}).get("oldestEvent") or ""
        run_backfill = False
        if not oldest:
            run_backfill = True
        else:
            try:
                oldest_dt = datetime.fromisoformat(oldest.replace("Z", "+00:00"))
                days_span = (datetime.now(timezone.utc) - oldest_dt).days
                run_backfill = days_span < RELEASE_HISTORY_BOOTSTRAP_DAYS
            except Exception:
                run_backfill = True
        if run_backfill:
            existing_ids = _release_history_existing_ids()
            boot_events, bootstrap_warnings = compute_bootstrap_events(current_payload, github_token)
            to_append: dict[str, list[dict]] = {}
            for pkey, evs in (boot_events or {}).items():
                new_evs = [
                    e for e in evs
                    if e.get("id") and e["id"] not in existing_ids
                    and (not oldest or (e.get("at") or "") < oldest)
                ]
                for e in new_evs:
                    existing_ids.add(e["id"])
                if new_evs:
                    to_append[pkey] = new_evs
            if to_append:
                _append_events_to_jsonl(to_append)
                _update_release_history_index(to_append)
                total_new = sum(len(v) for v in to_append.values())
                logger.info("release_history_backfilled", event_count=total_new, window_days=60)
            index = _load_release_history_index()
            index["backfill60DaysRun"] = True
            if bootstrap_warnings:
                prev = index.get("bootstrapWarnings") or []
                index["bootstrapWarnings"] = list(dict.fromkeys([*prev, *bootstrap_warnings]))
                for w in bootstrap_warnings:
                    logger.warn("release_history_backfill_warning", warning=w)
            _save_release_history_index(index)

    # Compute new events (unchanged logic)
    new_events_by_project = compute_tag_change_events(prev_snapshot, current_payload)
    
    if not new_events_by_project:
        return  # No new events
    
    # Append new events to JSONL (append-only)
    _append_events_to_jsonl(new_events_by_project)
    
    # Update index (lightweight, only metadata)
    _update_release_history_index(new_events_by_project)
    
    # Apply retention policy (runs periodically, not every snapshot)
    _apply_retention_policy()


def update_release_history_file(current_payload: dict, prev_snapshot: dict | None, github_token: str):
    path = _release_history_path()
    try:
        existing = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
    except Exception:
        existing = {}

    if not existing:
        existing = {
            'generatedAt': current_payload.get('generatedAt') or iso_now(),
            'retention': {
                'snapshotsKept': HISTORY_SNAPSHOTS_KEEP,
                'bootstrapEventsPerEnv': BOOTSTRAP_EVENTS_PER_ENV,
            },
            'projects': {},
        }

    # Ensure retention meta is up to date
    existing['generatedAt'] = current_payload.get('generatedAt') or iso_now()
    existing['retention'] = {
        'snapshotsKept': HISTORY_SNAPSHOTS_KEEP,
        'bootstrapEventsPerEnv': BOOTSTRAP_EVENTS_PER_ENV,
    }

    projects_obj = existing.get('projects') or {}

    # Bootstrap policy: always start with events spanning ~60 days when history is empty.
    if not projects_obj:
        boot, _ = compute_bootstrap_events(current_payload, github_token)
        for pkey, boot_events in (boot or {}).items():
            proj_entry = projects_obj.get(pkey) or {'events': [], 'meta': {'bootstrapDone': False}}
            events = proj_entry.get('events') or []
            events.extend(boot_events)
            events.sort(key=lambda e: e.get('at') or '', reverse=True)
            proj_entry['events'] = events[:RELEASE_HISTORY_MAX_EVENTS_PER_PROJECT]
            meta = proj_entry.get('meta') or {}
            meta['bootstrapDone'] = True
            if proj_entry['events']:
                meta['lastEventAt'] = proj_entry['events'][0].get('at')
                meta['firstEventAt'] = proj_entry['events'][-1].get('at')
            proj_entry['meta'] = meta
            projects_obj[pkey] = proj_entry

    new_events_by_project = compute_tag_change_events(prev_snapshot, current_payload)
    for pkey, new_events in new_events_by_project.items():
        proj_entry = projects_obj.get(pkey) or {'events': [], 'meta': {'bootstrapDone': False}}
        events = proj_entry.get('events') or []
        seen_ids = set()
        for e in events:
            eid = e.get('id')
            if eid:
                seen_ids.add(eid)

        # append new ones (avoid dup)
        for ev in new_events:
            if ev.get('id') and ev['id'] in seen_ids:
                continue
            events.append(ev)

        # sort by at desc and trim
        events.sort(key=lambda e: e.get('at') or '', reverse=True)
        if len(events) > RELEASE_HISTORY_MAX_EVENTS_PER_PROJECT:
            events = events[:RELEASE_HISTORY_MAX_EVENTS_PER_PROJECT]

        proj_entry['events'] = events
        meta = proj_entry.get('meta') or {}
        if events:
            meta['lastEventAt'] = events[0].get('at')
            meta['firstEventAt'] = events[-1].get('at')
        meta.setdefault('bootstrapDone', False)
        proj_entry['meta'] = meta
        projects_obj[pkey] = proj_entry

    existing['projects'] = projects_obj
    path.write_text(json.dumps(existing, indent=2), encoding='utf-8')


# ============================================================================
# DETERMINISTIC TICKET DEPLOYMENT HISTORY: Persistent Deployment Detection
# ============================================================================
# This module implements deterministic, persistent deployment detection for
# Ticket Tracker that ensures deployment visibility remains stable across
# snapshots. Once a ticket is deployed to an environment, it remains visible
# unless a rollback is detected.
#
# Architecture:
# 1. Deployment Event Log: Append-only storage of tag changes (deployments)
# 2. Ticket → Deployment Correlation: Deterministic correlation using GitHub
# 3. Environment Presence: Computed from deployment history, not just current snapshot
# 4. Persistent State: Once deployed, stay deployed (unless rollback)
# ============================================================================

def _deployment_history_dir() -> Path:
    """Deployment history directory (append-only storage)."""
    d = _repo_root() / 'data' / 'deployment_history'
    d.mkdir(parents=True, exist_ok=True)
    return d


def _deployment_history_events_path() -> Path:
    """Path to append-only deployment events.jsonl file."""
    return _deployment_history_dir() / 'events.jsonl'


def _store_deployment_events(deployment_events: list[dict]) -> None:
    """Store deployment events to append-only JSONL file.
    
    Args:
        deployment_events: List of deployment event dicts (from compute_tag_change_events)
    """
    if not deployment_events:
        return
    
    events_path = _deployment_history_events_path()
    
    # Append mode - never overwrites
    with open(events_path, 'a', encoding='utf-8') as f:
        for event in deployment_events:
            # Ensure event has required fields
            if not event.get("id"):
                continue
            # Add deployment-specific metadata
            deployment_event = {
                **event,
                "kind": "DEPLOYMENT",  # Override kind to be explicit
            }
            # Write as single-line JSON
            f.write(json.dumps(deployment_event, ensure_ascii=False) + '\n')


def _load_deployment_history(*, max_events: int = 10000) -> list[dict]:
    """Load all deployment events from history.
    
    Args:
        max_events: Maximum number of events to load (for performance)
    
    Returns:
        List of deployment events, sorted by time (newest first)
    """
    events_path = _deployment_history_events_path()
    if not events_path.exists():
        return []
    
    events: list[dict] = []
    try:
        with open(events_path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                    if event.get("kind") == "DEPLOYMENT":
                        events.append(event)
                except Exception:
                    continue  # Skip malformed lines
        
        # Sort by time (newest first)
        events.sort(key=lambda e: e.get("at") or "", reverse=True)
        
        # Limit to most recent events
        if len(events) > max_events:
            events = events[:max_events]
        
        return events
    except Exception:
        return []


def _extract_tag_sha_from_event(event: dict, github_token: str, github_org: str) -> str:
    """Extract the SHA of the tag/commit that was deployed.
    
    This is used for deterministic correlation: we need to know which commit
    was actually deployed to check if PR merge commits are included.
    
    Args:
        event: Deployment event dict
        github_token: GitHub API token
        github_org: GitHub organization name
    
    Returns:
        SHA of the deployed tag/commit, or empty string if unavailable
    """
    to_tag = (event.get("toTag") or "").strip()
    repo = (event.get("repo") or "").strip()
    
    if not to_tag or not repo:
        return ""
    
    # Try to get tag SHA from GitHub
    try:
        owner = github_org or GITHUB_ORG_DEFAULT
        url = f"{GITHUB_API}/repos/{owner}/{repo}/git/refs/tags/{urllib.parse.quote(to_tag, safe='')}"
        headers = github_api_headers(github_token)
        r = requests.get(url, headers=headers, timeout=10)
        if r.status_code == 200:
            data = r.json() or {}
            obj = data.get("object") or {}
            sha = (obj.get("sha") or "").strip()
            if sha:
                return sha
    except Exception:
        pass
    
    # Fallback: try to extract from commitUrl
    commit_url = (event.get("commitUrl") or "").strip()
    if commit_url:
        # Extract SHA from GitHub commit URL
        parts = commit_url.rstrip('/').split('/')
        if 'commit' in parts:
            i = parts.index('commit')
            if i + 1 < len(parts):
                return parts[i + 1]
    
    return ""


def correlate_tickets_to_deployments(
    ticket_index: dict,
    deployment_events: list[dict],
    github_org: str,
    github_token: str,
    *,
    max_repos: int = 50,
) -> dict[str, list[str]]:
    """Correlate tickets to deployments deterministically.
    
    For each deployment event, determine which tickets are included by checking
    if PR merge commits are reachable from the deployed tag.
    
    Args:
        ticket_index: Ticket index dict (ticket_key -> ticket_data)
        deployment_events: List of deployment events
        github_org: GitHub organization name
        github_token: GitHub API token
        max_repos: Maximum number of repos to process (for performance)
    
    Returns:
        Dict mapping deployment_id -> list of ticket_keys that are included
    """
    correlation: dict[str, list[str]] = {}
    
    if not ticket_index or not deployment_events:
        return correlation
    
    # Group deployments by repo for efficient processing
    deployments_by_repo: dict[str, list[dict]] = {}
    for event in deployment_events:
        repo = (event.get("repo") or "").strip()
        if repo:
            deployments_by_repo.setdefault(repo, []).append(event)
    
    # Process each repo
    repos_processed = 0
    for repo, repo_deployments in deployments_by_repo.items():
        if repos_processed >= max_repos:
            break
        
        # Find all tickets with PRs in this repo
        tickets_in_repo: list[tuple[str, dict]] = []  # (ticket_key, ticket_data)
        for ticket_key, ticket in ticket_index.items():
            prs = ticket.get("prs") or ticket.get("pullRequests") or []
            for pr in prs:
                pr_repo = (pr.get("repo") or "").strip()
                if pr_repo == repo:
                    tickets_in_repo.append((ticket_key, ticket))
                    break
        
        if not tickets_in_repo:
            continue
        
        # For each deployment, check which tickets are included
        owner = github_org or GITHUB_ORG_DEFAULT
        for deployment in repo_deployments:
            deployment_id = deployment.get("id") or ""
            if not deployment_id:
                continue
            
            # Get deployed tag SHA
            tag_sha = _extract_tag_sha_from_event(deployment, github_token, github_org)
            if not tag_sha:
                # Fallback: use toTag as branch/tag name
                tag_sha = (deployment.get("toTag") or "").strip()
            
            if not tag_sha:
                continue
            
            # Check each ticket's PR merge commits
            for ticket_key, ticket in tickets_in_repo:
                prs = ticket.get("prs") or ticket.get("pullRequests") or []
                for pr in prs:
                    pr_repo = (pr.get("repo") or "").strip()
                    if pr_repo != repo:
                        continue
                    
                    merge_sha = (pr.get("mergeSha") or "").strip()
                    merged_at = (pr.get("mergedAt") or "").strip()
                    deployed_at = (deployment.get("at") or deployment.get("deployedAt") or "").strip()
                    
                    if not merge_sha:
                        continue
                    
                    # Time validation: deployment must be after merge
                    if merged_at and deployed_at:
                        try:
                            merged_dt = datetime.fromisoformat(merged_at.replace("Z", "+00:00"))
                            deployed_dt = datetime.fromisoformat(deployed_at.replace("Z", "+00:00"))
                            if deployed_dt < merged_dt:
                                continue  # Deployment before merge - impossible
                        except Exception:
                            pass  # Continue if date parsing fails
                    
                    # Deterministic correlation: check if merge commit is in deployed tag
                    if github_check_commit_in_branch(owner, repo, merge_sha, tag_sha, github_token):
                        # Ticket is included in this deployment
                        correlation.setdefault(deployment_id, []).append(ticket_key)
                        break  # One PR match is enough for this ticket
        
        repos_processed += 1
    
    return correlation


def compute_ticket_environment_presence_from_history(
    ticket_index: dict,
    deployment_events: list[dict],
    ticket_deployment_correlation: dict[str, list[str]],
    projects_out: list[dict],
) -> dict[str, dict]:
    """Compute persistent environment presence from deployment history.
    
    For each ticket, find all deployments that include it, then determine
    environment presence based on the latest deployment per environment.
    
    Args:
        ticket_index: Ticket index dict
        deployment_events: List of deployment events (from history)
        ticket_deployment_correlation: Dict mapping deployment_id -> [ticket_keys]
        projects_out: Current projects data (for environment mapping)
    
    Returns:
        Dict mapping ticket_key -> {envPresence: {...}, envPresenceMeta: {...}}
    """
    result: dict[str, dict] = {}
    
    if not ticket_index or not deployment_events:
        return result
    
    # Build environment mapping: (project_key, env_key) -> stage
    env_mapping: dict[tuple[str, str], str] = {}
    for proj in projects_out:
        pkey = (proj.get("key") or "").strip()
        for env in (proj.get("environments") or []):
            ekey = _normalize_env_key(env.get("key")) or ""
            stage = _env_to_stage(env.get("name"))
            if pkey and ekey:
                env_mapping[(pkey, ekey)] = stage
    
    # Build reverse correlation: ticket_key -> [deployment_events]
    ticket_deployments: dict[str, list[dict]] = {}
    for deployment_id, ticket_keys in ticket_deployment_correlation.items():
        # Find deployment event
        deployment_event = None
        for event in deployment_events:
            if event.get("id") == deployment_id:
                deployment_event = event
                break
        
        if not deployment_event:
            continue
        
        # Add to each ticket's deployment list
        for ticket_key in ticket_keys:
            ticket_deployments.setdefault(ticket_key, []).append(deployment_event)
    
    # For each ticket, compute environment presence
    for ticket_key, ticket in ticket_index.items():
        deployments = ticket_deployments.get(ticket_key, [])
        if not deployments:
            continue
        
        presence = {"DEV": False, "QA": False, "UAT": False, "PROD": False}
        presence_meta = {"DEV": None, "QA": None, "UAT": None, "PROD": None}
        
        # Group deployments by environment
        deployments_by_stage: dict[str, list[dict]] = {}
        for deployment in deployments:
            pkey = (deployment.get("projectKey") or "").strip()
            ekey = (deployment.get("envKey") or "").strip().lower()
            stage = env_mapping.get((pkey, ekey), "DEV")  # Default to DEV if not found
            
            deployments_by_stage.setdefault(stage, []).append(deployment)
        
        # For each environment, find the latest deployment
        for stage, stage_deployments in deployments_by_stage.items():
            if not stage_deployments:
                continue
            
            # Sort by deployment time (newest first)
            stage_deployments.sort(key=lambda d: d.get("at") or d.get("deployedAt") or "", reverse=True)
            latest_deployment = stage_deployments[0]
            
            # Check for rollback: if current tag < previous tag (semantic versioning)
            # For now, we assume no rollback if deployment exists (can be enhanced)
            # TODO: Implement rollback detection (compare tag versions)
            
            # Mark as present
            presence[stage] = True
            
            # Store metadata
            deployed_at = (latest_deployment.get("at") or latest_deployment.get("deployedAt") or "").strip()
            repo = (latest_deployment.get("repo") or "").strip()
            to_tag = (latest_deployment.get("toTag") or "").strip()
            
            presence_meta[stage] = {
                "when": deployed_at,
                "repo": repo,
                "tag": to_tag,
                "branch": "",  # Not available from deployment event
                "source": "deployment_history",
                "confidence": "high",
            }
        
        result[ticket_key] = {
            "envPresence": presence,
            "envPresenceMeta": presence_meta,
        }
    
    return result


def merge_deployment_presence(
    current_presence: dict[str, dict],
    historical_presence: dict[str, dict],
) -> dict[str, dict]:
    """Merge current snapshot and historical deployment presence.
    
    Strategy:
    - If current snapshot shows deployment → use it (latest)
    - If current snapshot doesn't show deployment but history does → keep historical
    - Only remove if rollback detected (not implemented yet)
    
    Args:
        current_presence: Current snapshot presence (from add_env_presence_to_ticket_index)
        historical_presence: Historical presence (from compute_ticket_environment_presence_from_history)
    
    Returns:
        Merged presence dict
    """
    merged: dict[str, dict] = {}
    
    # Get all ticket keys
    all_ticket_keys = set(current_presence.keys()) | set(historical_presence.keys())
    
    for ticket_key in all_ticket_keys:
        current = current_presence.get(ticket_key, {})
        historical = historical_presence.get(ticket_key, {})
        
        current_env = current.get("envPresence", {})
        current_meta = current.get("envPresenceMeta", {})
        historical_env = historical.get("envPresence", {})
        historical_meta = historical.get("envPresenceMeta", {})
        
        # Merge: current takes precedence, but historical fills gaps
        merged_env = {"DEV": False, "QA": False, "UAT": False, "PROD": False}
        merged_meta = {"DEV": None, "QA": None, "UAT": None, "PROD": None}
        
        for stage in ["DEV", "QA", "UAT", "PROD"]:
            current_present = current_env.get(stage, False)
            historical_present = historical_env.get(stage, False)
            
            # Once deployed, stay deployed (unless rollback)
            if current_present:
                merged_env[stage] = True
                merged_meta[stage] = current_meta.get(stage)
            elif historical_present:
                merged_env[stage] = True
                merged_meta[stage] = historical_meta.get(stage)
        
        merged[ticket_key] = {
            "envPresence": merged_env,
            "envPresenceMeta": merged_meta,
        }
    
    return merged


def add_persistent_deployment_presence_to_tickets(
    ticket_index: dict,
    projects_out: list[dict],
    prev_snapshot: dict | None,
    github_org: str,
    github_token: str,
    *,
    warnings: list = None,
) -> None:
    """Add persistent deployment presence to tickets using deployment history.
    
    This function:
    1. Stores current deployment events to history
    2. Loads deployment history
    3. Computes environment presence from history (human-like, time-based; no provenance blocking)
    5. Merges with current snapshot presence
    
    Args:
        ticket_index: Ticket index dict (modified in-place)
        projects_out: Current projects data
        prev_snapshot: Previous snapshot (for computing new deployments)
        github_org: GitHub organization name (unused in heuristic mode)
        github_token: GitHub API token (unused in heuristic mode)
        warnings: Optional list to append warnings to
    """
    if not ticket_index or not projects_out:
        return
    
    if warnings is None:
        warnings = []
    
    # 1. Compute current deployment events (tag changes)
    current_snapshot = {"projects": projects_out}
    new_deployment_events_by_project = compute_tag_change_events(prev_snapshot, current_snapshot)
    
    # Flatten to list of events and enrich with repo information
    new_deployment_events: list[dict] = []
    
    # Build component map for efficient lookup
    component_repo_map: dict[tuple[str, str, str], str] = {}  # (project_key, env_key, component_name) -> repo
    for proj in projects_out:
        pkey = (proj.get("key") or "").strip()
        for env in (proj.get("environments") or []):
            ekey = _normalize_env_key(env.get("key")) or ""
            for comp in (env.get("components") or []):
                cname = (comp.get("name") or "").strip()
                repo = (comp.get("repo") or comp.get("repository") or "").strip()
                if pkey and ekey and cname and repo:
                    component_repo_map[(pkey, ekey, cname)] = repo
    
    for project_key, events in new_deployment_events_by_project.items():
        for event in events:
            # Add repo information if available
            component_name = (event.get("component") or "").strip()
            event_project_key = (event.get("projectKey") or "").strip()
            env_key = _normalize_env_key(event.get("envKey")) or ""
            
            # Look up repo from component map
            repo = component_repo_map.get((event_project_key, env_key, component_name), "")
            
            if repo:
                event["repo"] = repo
            
            new_deployment_events.append(event)
    
    # 2. Store new deployment events to history
    if new_deployment_events:
        _store_deployment_events(new_deployment_events)
    
    # 3. Load deployment history
    deployment_history = _load_deployment_history(max_events=5000)  # Reasonable limit
    
    if not deployment_history:
        # No history yet - use current snapshot only (first run)
        return
    
    # 4. Compute environment presence from history (heuristic, time-based; deterministic; no GitHub calls)
    # Build env mapping (project/env -> stage)
    env_mapping: dict[tuple[str, str], str] = {}
    for proj in projects_out:
        pkey = (proj.get("key") or "").strip()
        for env in (proj.get("environments") or []):
            ekey = _normalize_env_key(env.get("key")) or ""
            if pkey and ekey:
                env_mapping[(pkey, ekey)] = _env_to_stage(env.get("name"))

    # Index deployments by repo for efficiency
    dep_by_repo: dict[str, list[dict]] = {}
    for ev in deployment_history:
        repo = (ev.get("repo") or "").strip()
        if repo:
            dep_by_repo.setdefault(repo, []).append(ev)
    for repo in dep_by_repo:
        dep_by_repo[repo].sort(key=lambda e: (e.get("at") or ""), reverse=True)

    def _parse_iso_safe_local(s: str) -> datetime | None:
        if not s:
            return None
        try:
            return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        except Exception:
            return None

    historical_presence: dict[str, dict] = {}
    for ticket_key, ticket in ticket_index.items():
        prs = ticket.get("prs") or ticket.get("pullRequests") or []
        if not prs:
            continue

        presence = {"DEV": False, "QA": False, "UAT": False, "PROD": False}
        presence_meta = {"DEV": None, "QA": None, "UAT": None, "PROD": None}

        for pr in prs:
            repo = (pr.get("repo") or pr.get("repository") or "").strip()
            merged_at = (pr.get("mergedAt") or pr.get("merged_at") or "").strip()
            merged_dt = _parse_iso_safe_local(merged_at)
            if not repo or not merged_dt:
                continue

            for dep in dep_by_repo.get(repo, []):
                dep_at = (dep.get("at") or "").strip()
                dep_dt = _parse_iso_safe_local(dep_at)
                if not dep_dt:
                    continue
                if dep_dt < merged_dt:
                    # deployments are sorted newest-first; once we hit older than merge, stop
                    break

                pkey = (dep.get("projectKey") or "").strip()
                ekey = (dep.get("envKey") or "").strip().lower()
                stage = env_mapping.get((pkey, ekey), _env_to_stage(dep.get("envName") or ""))

                if stage not in presence:
                    continue

                presence[stage] = True
                existing = presence_meta.get(stage)
                # Keep earliest deploy time per stage for the ticket (so timeline shows entry point)
                if not existing or (dep_at and dep_at < (existing.get("when") or "")):
                    presence_meta[stage] = {
                        "when": dep_at,
                        "repo": repo,
                        "tag": (dep.get("toTag") or "").strip(),
                        "branch": "",  # not required for heuristic mode
                        "confidence": "heuristic",
                        "inferred": True,
                        "source": "deployment_history_time",
                        "warning": "Deployment inferred from timing (provenance not verified).",
                    }

        if any(presence.values()):
            historical_presence[ticket_key] = {"envPresence": presence, "envPresenceMeta": presence_meta}
    
    # 6. Get current snapshot presence (from existing logic)
    # We'll call the existing function first, then merge
    current_presence: dict[str, dict] = {}
    for ticket_key, ticket in ticket_index.items():
        env_presence = ticket.get("envPresence", {})
        env_meta = ticket.get("envPresenceMeta", {})
        if env_presence or env_meta:
            current_presence[ticket_key] = {
                "envPresence": env_presence,
                "envPresenceMeta": env_meta,
            }
    
    # 7. Merge historical and current presence
    merged_presence = merge_deployment_presence(current_presence, historical_presence)
    
    # 8. Update ticket_index with merged presence
    for ticket_key, merged_data in merged_presence.items():
        if ticket_key in ticket_index:
            ticket_index[ticket_key]["envPresence"] = merged_data["envPresence"]
            ticket_index[ticket_key]["envPresenceMeta"] = merged_data["envPresenceMeta"]
            
            # Also update timeline with historical deployments
            # Add deployment events from history to timeline
            timeline = ticket_index[ticket_key].get("timeline", [])
            
            # Add deployment timeline entries for stages we marked from history.
            deployment_events_for_ticket: list[dict] = []
            hm = merged_data.get("envPresenceMeta") or {}
            if isinstance(hm, dict):
                for stage, meta in hm.items():
                    if isinstance(meta, dict) and meta.get("source") == "deployment_history_time" and meta.get("when"):
                        deployment_events_for_ticket.append({
                            "envName": stage,
                            "at": meta.get("when") or "",
                            "toTag": meta.get("tag") or "",
                            "repo": meta.get("repo") or "",
                        })
            
            # Add to timeline (avoid duplicates)
            existing_timeline_stages = {ev.get("stage", "") for ev in timeline if ev.get("type") == "deployment"}
            
            for deployment_event in deployment_events_for_ticket:
                stage_label = str(deployment_event.get("envName") or "").strip().upper() or "DEV"
                stage_str = f"Deployed to {stage_label}"
                
                if stage_str not in existing_timeline_stages:
                    deployed_at = (deployment_event.get("at") or "").strip()
                    to_tag = (deployment_event.get("toTag") or "").strip()
                    
                    timeline.append({
                        "stage": stage_str,
                        "at": deployed_at,
                        "ref": to_tag or "",
                        "source": deployment_event.get("repo") or "",
                        "type": "deployment",
                        "timeAware": False,
                        "fromHistory": True,  # Mark as from history
                    })
            
            # Sort timeline by time
            timeline.sort(key=lambda ev: ev.get("at") or "", reverse=True)
            ticket_index[ticket_key]["timeline"] = timeline


def main():
    # Load env from MVP1/.env (this file sits in MVP1/snapshot/)
    env_path = Path(__file__).resolve().parents[1] / ".env"
    load_dotenv(env_path)

    github_token = os.getenv("GITHUB_TOKEN", "").strip()
    if not github_token:
        raise SystemExit("Brak GITHUB_TOKEN w zmiennych środowiskowych (.env).")

    github_org = (os.getenv("GITHUB_ORG") or GITHUB_ORG_DEFAULT).strip() or GITHUB_ORG_DEFAULT

    teamcity_base = (os.getenv("TEAMCITY_URL") or os.getenv("TEAMCITY_API") or "").strip()
    teamcity_token = (os.getenv("TEAMCITY_TOKEN") or "").strip()
    rest_base = teamcity_rest_base(teamcity_base)

    # Jira integration (initialize early for integrations payload)
    jira_base = (os.getenv("JIRA_BASE") or os.getenv("JIRA_URL") or "").strip() or ""
    jira_email = (os.getenv("JIRA_EMAIL") or "").strip()
    jira_token = (os.getenv("JIRA_API_TOKEN") or os.getenv("JIRA_TOKEN") or "").strip()

    # ------------------------------------------------------------
    # Snapshot validation (MVP1):
    # - globalAlerts: existing UI banner format
    # - warnings[]: Stage 8.1 normalized warnings for data quality + integrations
    # ------------------------------------------------------------
    global_alerts: list[dict] = []
    warnings_root: list[dict] = []

    teamcity_enabled = bool(rest_base and teamcity_token)
    teamcity_down = False
    teamcity_down_reason = ""

    if not teamcity_enabled:
        # Common case: token removed from repo before sharing.
        global_alerts.append({
            "id": "teamcity-disabled",
            "level": "warn",
            "title": "TeamCity enrichment disabled",
            "message": "TeamCity URL/token missing - build/branch metadata will be partial.",
            "ts": iso_now(),
        })
        warnings_root.append(_warning(
            level="warning",
            scope="global",
            reason="teamcity_enrichment_disabled",
            source="teamcity",
            message="TeamCity URL/token missing - build/branch metadata will be partial.",
        ))

    # ------------------------------------------------------------
    # Datadog connectivity check (Stage 8.1 handshake only)
    # - site is required (you confirmed datadoghq.com)
    # - store only connected flag + reason, NEVER store keys
    # ------------------------------------------------------------
    dd_site = _env_any("DATADOG_SITE", "DD_SITE") or "datadoghq.com"
    dd_api_key = _env_any("DATADOG_API_KEY", "DD_API_KEY")
    dd_app_key = _env_any(
        "DATADOG_APP_KEY",
        "DATADOG_APPLICATION_KEY",
        "DD_APP_KEY",
        "DD_APPLICATION_KEY",
    )

    dd_enabled = bool(dd_api_key and dd_app_key)
    dd_connected = False
    dd_reason = "disabled"
    if dd_enabled:
        dd_connected, dd_reason = datadog_validate(dd_api_key, dd_app_key, site=dd_site)
        if not dd_connected:
            warnings_root.append(_warning(
                level="warning",
                scope="global",
                reason="datadog_connection_failed",
                source="datadog",
                message=f"Datadog validate failed ({dd_reason}). Check keys/permissions or site.",
            ))
    else:
        # If user intends Datadog but hasn't provided keys, we stay silent.
        # (No warning; many users won't use Datadog in MVP1.)
        dd_reason = "missing_keys"

    # ArgoCD integration is optional per project (config YAML may include `argocd:`).
    # We keep a small cache to avoid repeated calls across envs.
    argocd_cache: dict[str, dict] = {}
    argocd_disabled_reported: set[str] = set()
    argocd_error_reported: set[str] = set()

    # ------------------------------------------------------------
    # Observability data structures (initialize early to avoid UnboundLocalError)
    # ------------------------------------------------------------
    observability_summary: list[dict] = []
    observability_news: list[dict] = []
    observability_warnings: list[dict] = []

    # ------------------------------------------------------------
    # Datadog alert feed (monitors -> globalAlerts)
    # This is enrichment: read-only, best effort.
    # ------------------------------------------------------------
    # Collect env_selectors from all configs for deterministic monitor filtering
    dd_env_selectors_global: dict[str, dict[str, str]] = {}
    if dd_connected:
        configs_for_selectors = load_project_configs()
        for cfg in configs_for_selectors:
            datadog_cfg = cfg.get("datadog") or {}
            env_selectors = datadog_cfg.get("envSelectors") or {}
            for env_key, selector in env_selectors.items():
                if isinstance(selector, dict):
                    dd_env_selectors_global[env_key.lower()] = selector
    
    if dd_connected:
        try:
            dd_env_keys = ["dev", "qa", "uat", "prod"]
            dd_alerts, dd_alert_warns = datadog_collect_alert_feed(
                dd_api_key,
                dd_app_key,
                site=dd_site,
                env_keys=dd_env_keys,
                limit=10,
                env_selectors=dd_env_selectors_global if dd_env_selectors_global else None,
            )
            if dd_alerts:
                global_alerts.extend(dd_alerts)
            if dd_alert_warns:
                warnings_root.extend(dd_alert_warns)
        except Exception as e:
            warnings_root.append(_warning(
                level="warning",
                scope="global",
                reason="datadog_alert_feed_failed",
                source="datadog",
                message=str(e),
            ))

    # ------------------------------------------------------------
    # Datadog news feed (monitors -> observability.news)
    # Collects latest monitor alerts/warnings for News feed.
    # ------------------------------------------------------------
    if dd_connected:
        try:
            dd_news, dd_news_warns = datadog_collect_news_feed(
                dd_api_key,
                dd_app_key,
                site=dd_site,
                limit=10,
            )
            if dd_news:
                observability_news.extend(dd_news)
            if dd_news_warns:
                observability_warnings.extend(dd_news_warns)
        except Exception as e:
            observability_warnings.append({
                "code": "DATADOG_NEWS_FEED_FAILED",
                "severity": "warn",
                "message": f"Datadog news feed failed: {e}",
                "source": {"integration": "datadog", "site": dd_site},
            })

    configs = load_project_configs()



    # ------------------------------------------------------------
    # Observability (Datadog) – optional enrichment used by UI tiles
    # Note: observability_news and observability_warnings initialized earlier (line ~1988)
    # ------------------------------------------------------------

    projects_out: list[dict] = []

    for cfg in configs:
        p = cfg.get("project") or {}
        p_key = (p.get("key") or "").strip()
        p_name = (p.get("name") or p_key).strip()
        if not p_key:
            raise SystemExit("Config bez project.key")

        owner = (p.get("githubOwner") or github_org).strip() or github_org
        infra_ref = (p.get("infraRef") or "main").strip() or "main"

        envs_cfg = cfg.get("environments") or []
        services_cfg = cfg.get("services") or []
        argocd_cfg = cfg.get("argocd") or {}
        datadog_cfg = cfg.get("datadog") or {}
        dd_use_for_project = bool(dd_enabled and dd_connected and datadog_cfg.get("enabled"))
        dd_env_map = datadog_cfg.get("envMap") or {}
        dd_queries = datadog_cfg.get("queries") or {}
        dd_thresholds = datadog_cfg.get("thresholds") or {}
        # NEW: MVP1-safe deterministic selectors
        dd_env_selectors = datadog_cfg.get("envSelectors") or {}
        dd_component_selectors = datadog_cfg.get("componentSelectors") or {}
        if not envs_cfg or not services_cfg:
            logger.warn("config_skipped", project=p_key, reason="missing environments or services")
            global_alerts.append({
                "id": f"config-skipped-{p_key}",
                "level": "warn",
                "title": "Project skipped",
                "message": f"Config {p_key} has no environments or services - skipped.",
                "ts": iso_now(),
            })
            continue

        # Datadog defaults (MVP)
        dd_env_map: dict[str, str] = datadog_cfg.get("envMap") or {}
        dd_tags: dict[str, str] = datadog_cfg.get("tags") or {}
        dd_queries: dict[str, str] = datadog_cfg.get("queries") or {}
        dd_thresholds: dict[str, dict] = datadog_cfg.get("thresholds") or {}

        # Map service key -> base argo app name (defaults to service key)
        svc_to_argo_app: dict[str, str] = {}
        for s in services_cfg:
            sk = _strip(s.get("key") or "")
            if not sk:
                continue
            base_app = _strip(s.get("argoApp") or s.get("argocdApp") or sk)
            svc_to_argo_app[sk] = base_app

        envs_out: list[dict] = []

        for env in envs_cfg:
            env_key = _normalize_env_key(env.get("key")) or ""
            env_name = (env.get("name") or env_key.upper()).strip() or env_key
            if not env_key:
                continue

            # Pre-calc Argo host/token for this env (best-effort)
            argo_host = ""
            argo_token = ""
            if isinstance(argocd_cfg, dict) and argocd_cfg:
                argo_host = _argocd_host_for_env(argocd_cfg, env_key)
                if argo_host:
                    argo_token = _argocd_pick_token(env_key)
                    if not argo_token and p_key not in argocd_disabled_reported:
                        argocd_disabled_reported.add(p_key)
                        global_alerts.append({
                            "id": f"argocd-disabled-{p_key}",
                            "level": "warn",
                            "title": "ArgoCD health disabled",
                            "message": "ArgoCD host is configured, but ARGOCD_TOKEN is missing - runtime health will be Unknown.",
                            "ts": iso_now(),
                        })

            env_components: list[dict] = []
            env_last_deploy_candidates: list[str] = []

            for svc in services_cfg:
                svc_key = (svc.get("key") or "").strip()
                code_repo = (svc.get("codeRepo") or svc_key).strip()
                infra_repo = (svc.get("infraRepo") or "").strip()
                build_type_id = (svc.get("teamcityBuildTypeId") or "").strip()
                if not infra_repo:
                    raise SystemExit(f"Service bez infraRepo w {p_key}: {svc_key}")

                # Optional per-service env filter (some infra repos don't have all env folders)
                svc_envs = svc.get("envs")
                if svc_envs:
                    svc_envs_norm = [str(e).strip().lower() for e in svc_envs if str(e).strip()]
                    if env_key not in svc_envs_norm:
                        continue

                # per-service infra ref override (some repos use master)
                svc_infra_ref = (svc.get("infraRef") or infra_ref)

                # kustomization (source of truth)
                # MVP1 principle: snapshot generation must be resilient.
                # If a single repo/env has a wrong path / missing file / wrong repo name,
                # we skip that component for this env and continue (instead of killing the run).
                try:
                    yaml_text, kustom_path = fetch_kustomization_text(
                        owner, infra_repo, svc_infra_ref, env_key, github_token
                    )
                except Exception as e:
                    # Keep a placeholder component so UI can show partial-data warnings.
                    print(
                        f"[WARN] {svc_key} ({infra_repo}) env={env_key} – nie mogę pobrać kustomization: {e}"
                    )
                    logger.warn(
                        "kustomization_fetch_failed",
                        project=p_key,
                        env=env_key,
                        service=svc_key,
                        infra_repo=infra_repo,
                        error=str(e),
                    )
                    env_components.append({
                        "name": svc_key,
                        "repo": code_repo,
                        "repoUrl": f"https://github.com/{github_org}/{code_repo}",
                        "branch": "",
                        "branchUrl": "",
                        "image": "",
                        "tag": "",
                        "build": "",
                        "buildUrl": "",
                        "buildFinishedAt": "",
                        "triggeredBy": "",
                        "deployer": "",
                        "deployerCommitUrl": "",
                        "deployedAt": "",
                        "infraRepo": infra_repo,
                        "infraRepoUrl": f"https://github.com/{owner}/{infra_repo}",
                        "kustomizationUrl": "",
                        "warnings": [
                            {
                                "code": "NO_KUSTOMIZATION",
                                "message": "kustomization.yaml not found or cannot be fetched for this environment",
                            }
                        ],
                    })
                    continue

                components = extract_components_from_kustomization(yaml_text)

                if not components:
                    # Keep placeholder component (kustomization exists, but we couldn't extract images/tags)
                    logger.warn(
                        "kustomization_no_tags",
                        project=p_key,
                        env=env_key,
                        service=svc_key,
                        infra_repo=infra_repo,
                    )
                    env_components.append({
                        "name": svc_key,
                        "repo": code_repo,
                        "repoUrl": f"https://github.com/{github_org}/{code_repo}",
                        "branch": "",
                        "branchUrl": "",
                        "image": "",
                        "tag": "",
                        "build": "",
                        "buildUrl": "",
                        "buildFinishedAt": "",
                        "triggeredBy": "",
                        "deployer": "",
                        "deployerCommitUrl": "",
                        "deployedAt": "",
                        "infraRepo": infra_repo,
                        "infraRepoUrl": f"https://github.com/{owner}/{infra_repo}",
                        "kustomizationUrl": github_blob_url(owner, infra_repo, infra_ref, kustom_path),
                        "warnings": [
                            {
                                "code": "NO_TAG_FOUND",
                                "message": "kustomization.yaml fetched, but no image tags/newTags were found",
                            }
                        ],
                    })
                    continue

                # Most infra repos describe exactly one image; if there are many, we keep them.
                # But we set service name to svc_key if we can.
                if len(components) == 1:
                    components[0]["name"] = svc_key

                # Deployer + deployedAt
                # IMPORTANT: DevOps may commit infra-only changes without changing tags.
                # We want "last deployer" to change ONLY when tag(s) changed.
                current_sig = _kustom_tag_signature_from_text(yaml_text)
                last_commit = None
                try:
                    last_commit = github_find_last_tag_change_commit(
                        owner,
                        infra_repo,
                        kustom_path,
                        svc_infra_ref,
                        github_token,
                        current_signature=current_sig,
                        commits_to_scan=12,
                    )
                except Exception as e:
                    print(
                        f"[WARN] Nie mogę pobrać tag-change commit dla {infra_repo}:{kustom_path} (env={env_key}): {e}"
                    )
                    last_commit = None
                # Fallback to old behaviour if we couldn't find tag-change commit
                if not last_commit:
                    try:
                        last_commit = github_get_last_commit_for_file(
                            owner, infra_repo, kustom_path, svc_infra_ref, github_token
                        )
                    except Exception:
                        last_commit = None

                deployer = ""
                deployed_at = ""
                kustom_commit_url = ""
                if last_commit:
                    deployer = last_commit.get("authorLogin") or last_commit.get("authorName") or ""
                    deployed_at = last_commit.get("date") or ""
                    kustom_commit_url = last_commit.get("url") or ""

                latest_finish_iso = ""
                for c in components:
                    # -----------------------------
                    # Warnings (data quality) - component level
                    # -----------------------------
                    c_warnings: list[dict] = []

                    build_no = (c.get("build") or "").strip()

                    # GitHub links
                    c["repo"] = code_repo
                    c["repoUrl"] = f"https://github.com/{github_org}/{code_repo}"

                    # Deployment meta (from infra commit)
                    c["deployer"] = deployer
                    c["deployerCommitUrl"] = kustom_commit_url
                    c["deployedAt"] = deployed_at

                    # TeamCity meta
                    c["branch"] = ""
                    c["buildUrl"] = ""
                    c["buildFinishedAt"] = ""
                    c["triggeredBy"] = ""

                    # basic tag/build warnings
                    if not (c.get("tag") or "").strip():
                        c_warnings.append({
                            "code": "NO_TAG_FOUND",
                            "message": "No deployed tag found in kustomization",
                        })
                    elif not build_no:
                        c_warnings.append({
                            "code": "NO_BUILD_NUMBER",
                            "message": "Cannot extract build number from tag",
                        })

                    # TeamCity warnings are aggregated at GLOBAL level when TeamCity is disabled/down.
                    # Here we only add per-component warnings for misconfiguration (missing buildTypeId)
                    # or for a specific build not found.
                    if build_no and teamcity_enabled and (not build_type_id):
                        c_warnings.append({
                            "code": "NO_TEAMCITY_BUILDTYPE",
                            "message": "TeamCity buildTypeId missing in config for this service",
                        })

                    tc_build = None
                    if teamcity_enabled and (not teamcity_down) and build_type_id and build_no:
                        try:
                            tc_build = teamcity_get_build(rest_base, teamcity_token, build_type_id, build_no)
                        except Exception as e:
                            # Mark TeamCity as down once, stop further calls, and emit a single global alert.
                            teamcity_down = True
                            teamcity_down_reason = str(e)
                            global_alerts.append({
                                "id": "teamcity-down",
                                "level": "warn",
                                "title": "TeamCity API unreachable",
                                "message": f"TeamCity enrichment failed - build/branch metadata will be partial. ({teamcity_down_reason})",
                                "ts": iso_now(),
                            })
                            tc_build = None

                    if tc_build:
                        c["branch"] = tc_build.get("branchName") or ""
                        c["buildUrl"] = tc_build.get("webUrl") or ""
                        fin_raw = tc_build.get("finishDate") or tc_build.get("finishOnAgentDate") or ""
                        c["buildFinishedAt"] = parse_iso_teamcity(fin_raw)
                        trig = (tc_build.get("triggered") or {}).get("user") or {}
                        c["triggeredBy"] = trig.get("username") or trig.get("name") or ""

                        fin = c["buildFinishedAt"]
                        if fin and (not latest_finish_iso or fin > latest_finish_iso):
                            latest_finish_iso = fin
                    else:
                        # TeamCity is enabled and up, but we couldn't find this specific build
                        if teamcity_enabled and (not teamcity_down) and build_type_id and build_no:
                            c_warnings.append({
                                "code": "NO_TEAMCITY",
                                "message": "TeamCity build metadata unavailable (build not found)",
                            })

                    if teamcity_enabled and (not teamcity_down) and tc_build and build_no and not (c.get("branch") or "").strip():
                        c_warnings.append({
                            "code": "NO_BRANCH_INFO",
                            "message": "Branch information not available from CI",
                        })

                    c["branchUrl"] = (
                        f"https://github.com/{github_org}/{code_repo}/tree/{encode_branch_for_github_url(c['branch'])}"
                        if c.get("branch")
                        else ""
                    )

                    # Infra links
                    c["infraRepo"] = infra_repo
                    c["infraRepoUrl"] = f"https://github.com/{owner}/{infra_repo}"
                    c["kustomizationUrl"] = github_blob_url(owner, infra_repo, infra_ref, kustom_path)

                    # attach warnings only if present
                    if c_warnings:
                        c["warnings"] = c_warnings

                env_components.extend(components)

                # Last deploy heuristic per-service: prefer kustom commit date, else TeamCity finish
                if deployed_at:
                    env_last_deploy_candidates.append(deployed_at)
                elif latest_finish_iso:
                    env_last_deploy_candidates.append(latest_finish_iso)

            # Environment-level summary
            env_last_deploy = max(env_last_deploy_candidates) if env_last_deploy_candidates else ""

            # Build / Deployer summary (best-effort): pick the component with the latest
            # deployedAt (kustom commit) or buildFinishedAt (TeamCity) timestamp.
            def comp_ts(c: dict) -> str:
                return (
                    (c.get("deployedAt") or "").strip()
                    or (c.get("buildFinishedAt") or "").strip()
                )

            best = None
            best_ts = ""
            for c in env_components:
                ts = comp_ts(c)
                if ts and (not best_ts or ts > best_ts):
                    best_ts = ts
                    best = c

            env_build = ""
            env_deployer = ""
            if best:
                env_build = (best.get("build") or "").strip() or extract_build_number(best.get("tag") or "")
                env_deployer = (
                    (best.get("deployer") or "").strip()
                    or (best.get("deployedBy") or "").strip()
                    or (best.get("triggeredBy") or "").strip()
                )

            # -----------------------------
            # Warnings (data quality) - environment level
            # -----------------------------
            env_warnings: list[dict] = []
            has_comp_warnings = any(
                isinstance(c.get("warnings"), list) and len(c.get("warnings") or []) > 0
                for c in env_components
            )
            if has_comp_warnings:
                env_warnings.append({
                    "code": "PARTIAL_COMPONENT_DATA",
                    "message": "Some components have incomplete deployment metadata",
                })

            # -----------------------------
            # ArgoCD health / sync (runtime signal) - environment level
            # -----------------------------
            argo_health = "Unknown"
            argo_sync = "Unknown"

            if argo_host and argo_token:
                # Resolve unique apps used in this environment (best-effort)
                apps_base: list[str] = []
                for c in env_components:
                    base = svc_to_argo_app.get(_strip(c.get("name") or "")) or svc_to_argo_app.get(_strip(c.get("repo") or ""))
                    if base:
                        apps_base.append(base)
                apps_base = sorted(set([a for a in apps_base if a]))

                worst_h = 0
                worst_s = 0
                # attach per-component argo fields as well
                for base_app in apps_base:
                    app_name = _argocd_app_name_for_env(argocd_cfg, env_key, base_app)
                    cache_key = f"{argo_host}::{app_name}"
                    try:
                        if cache_key not in argocd_cache:
                            argocd_cache[cache_key] = argocd_fetch_app_status(argo_host, argo_token, app_name)
                        st = argocd_cache[cache_key]
                    except Exception as e:
                        # Report each host/app error only once to avoid noise
                        err_id = f"{p_key}:{env_key}:{app_name}"
                        if err_id not in argocd_error_reported:
                            argocd_error_reported.add(err_id)
                            global_alerts.append({
                                "id": f"argocd-error-{p_key}-{env_key}",
                                "level": "warn",
                                "title": "ArgoCD API issue",
                                "message": f"Cannot fetch ArgoCD app status for {p_key}/{env_key}: {app_name}",
                                "ts": iso_now(),
                            })
                        st = {"health": "Error", "sync": "Unknown", "appUrl": ""}

                    h = _strip(st.get("health") or "Unknown")
                    s = _strip(st.get("sync") or "Unknown")
                    worst_h = max(worst_h, _argocd_rank_health(h))
                    worst_s = max(worst_s, _argocd_rank_sync(s))

                    # attach to matching components
                    for c in env_components:
                        base = svc_to_argo_app.get(_strip(c.get("name") or "")) or svc_to_argo_app.get(_strip(c.get("repo") or ""))
                        if base == base_app:
                            c["argoApp"] = app_name
                            c["argoAppUrl"] = st.get("appUrl") or ""
                            c["argoHealth"] = h
                            c["argoSync"] = s

                # Convert worst ranks back into a readable summary
                # We keep a single string for UI: "Health / Sync"
                # (UI uses keywords like 'degraded' / 'outofsync' for alerting)
                # Determine representative labels from worst ranks
                # Note: ranks are ordered from best to worst.
                health_by_rank = {
                    0: "Healthy",
                    1: "Progressing",
                    2: "Suspended",
                    3: "Degraded",
                    4: "Missing",
                    5: "Unknown",
                    6: "Error",
                }
                sync_by_rank = {
                    0: "Synced",
                    2: "OutOfSync",
                    3: "Unknown",
                    4: "Error",
                }
                argo_health = health_by_rank.get(worst_h, "Unknown")
                argo_sync = sync_by_rank.get(worst_s, "Unknown")

            argo_status_str = f"{argo_health} / {argo_sync}" if (argo_health or argo_sync) else "Unknown"

            # -----------------------------
            # Datadog health signals (enrichment)
            # -----------------------------
            dd_out = None
            if dd_use_for_project:
                # MVP1-safe: Use deterministic envSelector if available
                env_selector = dd_env_selectors.get(env_key) if isinstance(dd_env_selectors.get(env_key), dict) else None
                
                # NOTE: Datadog env tags are typically lowercase (env:dev/env:qa/...)
                # Our env labels in configs/UI can be uppercase (DEV/QA/UAT/PROD) – normalize here.
                dd_env_raw = _strip(dd_env_map.get(env_key, env_key)) or env_key
                dd_env = dd_env_raw.strip().lower()
                dd_signals: dict[str, float | None] = {}
                dd_meta: dict[str, dict] = {}

                # If using deterministic selector, build tags from selector
                if env_selector:
                    selector_tags = _dd_build_selector_tags(env_selector)
                    selector_tags_str = _dd_join_tags(selector_tags)
                for sig_key, q_tmpl in (dd_queries or {}).items():
                    # Replace $env and $project, then append selector tags
                    q = _dd_subst(q_tmpl, env=dd_env, project=p_key)
                    # Append selector tags to query (if query has {tags} placeholder, replace it; otherwise append)
                    if env_selector and selector_tags_str:
                        if "{tags}" in q:
                            q = q.replace("{tags}", selector_tags_str)
                        elif "{" in q and q.rstrip().endswith("}"):
                            # Query ends with tag filter, append to it
                            q = q.rstrip()[:-1] + "," + selector_tags_str + "}"
                        else:
                            # No tag filter, add one
                            q = q.rstrip() + "{" + selector_tags_str + "}"
                    val, st = datadog_query_timeseries(dd_api_key, dd_app_key, site=dd_site, query=q, minutes=int(datadog_cfg.get("windowMinutes") or 5))
                    dd_signals[sig_key] = val
                    dd_meta[sig_key] = {"status": st, "query": q, "deterministic": True}
                else:
                    # Legacy: use envMap substitution
                    for sig_key, q_tmpl in (dd_queries or {}).items():
                        q = _dd_subst(q_tmpl, env=dd_env, project=p_key)
                        val, st = datadog_query_timeseries(dd_api_key, dd_app_key, site=dd_site, query=q, minutes=int(datadog_cfg.get("windowMinutes") or 5))
                        dd_signals[sig_key] = val
                        dd_meta[sig_key] = {"status": st, "query": q, "deterministic": False}

                dd_status = _dd_pick_status(dd_signals, dd_thresholds)
                dd_out = {
                    "source": "datadog",
                    "status": dd_status,
                    "windowMinutes": int(datadog_cfg.get("windowMinutes") or 5),
                    "updatedAt": iso_now(),
                    "signals": dd_signals,
                    "meta": dd_meta,
                }

                # Warnings for visibility
                any_ok = any((m.get("status") == "ok") for m in dd_meta.values())
                if not any_ok:
                    env_warnings.append({
                        "key": "datadog_no_data",
                        "severity": "info",
                        "message": "Datadog enabled but no data returned for configured queries.",
                        "source": {"integration": "datadog", "site": dd_site},
                    })

                if dd_status in ("degraded", "unhealthy"):
                    env_warnings.append({
                        "key": f"datadog_{dd_status}",
                        "severity": "warn" if dd_status == "degraded" else "error",
                        "message": f"Datadog signals indicate: {dd_status}.",
                        "source": {"integration": "datadog", "site": dd_site},
                    })

                # -----------------------------
                # Datadog observability summary (CPU/Mem/Pods/Error/P95)
                # Used in Overview "Parameters & Logs" tiles.
                # -----------------------------
                # MVP1-safe: Use deterministic selector if available
                env_value = str((dd_env_map or {}).get(env_key, env_key))
                env_value_norm = env_value.strip().lower()
                base_tags = datadog_cfg.get("baseTags") or datadog_cfg.get("tags") or []
                if isinstance(base_tags, str):
                    base_tags = [t.strip() for t in base_tags.split(",") if t.strip()]
                elif not isinstance(base_tags, list):
                    base_tags = []
                
                # Get component selector for this service+env (if available)
                component_selector = None
                if svc_key and dd_component_selectors:
                    svc_selectors = dd_component_selectors.get(svc_key, {})
                    if isinstance(svc_selectors, dict):
                        component_selector = svc_selectors.get(env_key)
                
                # Use deterministic selector if available, otherwise fall back to tag candidates
                raw_tag_candidates = datadog_cfg.get("tagCandidates") or ["env", "environment", "kube_namespace", "kubernetes_namespace"]
                tag_candidates = []
                for tc in (raw_tag_candidates or []):
                    tc_s = str(tc).strip()
                    if not tc_s:
                        continue
                    if ":" in tc_s:
                        tag_candidates.append(tc_s)
                    else:
                        tag_candidates.append(f"{tc_s}:{env_value_norm}")
                
                obs, obs_warns = datadog_collect_observability(
                    dd_api_key,
                    dd_app_key,
                    site=dd_site,
                    project_key=p_key,
                    env_key=env_key,
                    env_value=env_value_norm,
                    base_tags=base_tags,
                    tag_candidates=tag_candidates,
                    minutes=int(datadog_cfg.get("windowMinutes") or 10),
                    datadog_cfg=datadog_cfg,
                    project_name=p_name,
                    dd_env_map=dd_env_map,
                    env_selector=env_selector,
                    component_selector=component_selector,
                )
                if obs:
                    env_warnings.extend(obs.get("envWarnings") or [])
                    env_out_obs = dict(obs)
                    # Keep env object lean; store full summary at top-level.
                    env_out_obs.pop("envWarnings", None)
                    observability_summary.append(env_out_obs)
                if obs_warns:
                    observability_warnings.extend(obs_warns)

            env_status = "warn" if env_warnings else "healthy"

            env_out = {
                "key": env_key,
                "name": env_name,
                "status": env_status,
                "argoStatus": argo_status_str,
                "health": {"datadog": dd_out} if dd_out else {},
                "healthSignals": [dd_out] if dd_out else [],
                "lastDeploy": env_last_deploy,
                "deployer": env_deployer,
                "build": env_build,
                "notes": [],
                "components": env_components,
            }
            if env_warnings:
                env_out["warnings"] = env_warnings

            envs_out.append(env_out)

        projects_out.append({
            "key": p_key,
            "name": p_name,
            "generatedAt": iso_now(),
            "environments": envs_out,
        })

    # Calculate data coverage for integrations
    generated_at = iso_now()
    
    # Datadog coverage: count projects/envs with observability data
    dd_coverage = {
        "projects": len(set(s.get("projectKey") for s in observability_summary if s.get("projectKey"))),
        "envs": len(observability_summary),
    }
    
    # TeamCity coverage: count components with build data
    tc_coverage = {"components": 0}
    for proj in projects_out:
        for env in (proj.get("environments") or []):
            for comp in (env.get("components") or []):
                if comp.get("buildUrl") or comp.get("buildFinishedAt"):
                    tc_coverage["components"] += 1

    payload = {
        "generatedAt": generated_at,
        "source": "snapshot",
        "projects": projects_out,
        "ticketIndex": {},
        "warnings": warnings_root,
        "observability": {
            "summary": observability_summary,
            "warnings": observability_warnings,
            "news": observability_news,  # Datadog Monitors as news items
        },
        "integrations": {
            "datadog": {
                "enabled": bool(dd_enabled),
                "connected": bool(dd_connected),
                "site": dd_site,
                "reason": dd_reason,
                "lastFetch": generated_at if dd_connected else None,
                "coverage": dd_coverage,
            },
            "teamcity": {
                "enabled": bool(teamcity_enabled),
                "connected": not teamcity_down if teamcity_enabled else False,
                "reason": teamcity_down_reason if (teamcity_enabled and teamcity_down) else ("disabled" if not teamcity_enabled else "ok"),
                "lastFetch": generated_at if (teamcity_enabled and not teamcity_down) else None,
                "coverage": tc_coverage,
            },
            "github": {
                "enabled": bool(github_token),
                "connected": bool(github_token),  # GitHub is always "connected" if token exists
                "reason": "ok" if github_token else "missing_token",
                "lastFetch": generated_at if github_token else None,
                "coverage": {"tickets": 0},  # Will be updated after ticket index is built
            },
            "jira": {
                "enabled": bool(jira_token),
                "connected": bool(jira_token),  # Jira is always "connected" if token exists
                "reason": "ok" if jira_token else "missing_token",
                "lastFetch": generated_at if jira_token else None,
                "coverage": {"tickets": 0},  # Will be updated after ticket index is built
            },
        },
    }

    # Surface aggregated validation/integration issues in UI.
    if global_alerts:
        payload["globalAlerts"] = global_alerts

    # ------------------------------------------------------------
    # Ticket Tracker (GitHub -> ticketIndex) + Jira enrichment
    # ------------------------------------------------------------
    # IMPORTANT: load previous snapshot BEFORE building envPresence so that
    # both Ticket Tracker and Release History share the same baseline.
    prev_snapshot = load_previous_snapshot_from_history()
    try:
        # How many days of GitHub history to scan for tickets/PRs.
        # Default widened from 30 -> 120 so Ticket Tracker has richer data out of the box.
        days = int((os.getenv("TICKET_TRACKER_DAYS") or "120").strip() or "120")
    except Exception:
        days = 120

    try:
        ticket_index = build_ticket_index_from_github(projects_out, github_org, github_token, days=days)
        
        # Best-effort fallback: if GitHub returned no tickets, extract from component metadata
        if not ticket_index:
            ticket_index = build_ticket_index_from_components(projects_out)
        
        # Feature flag: TICKET_HISTORY_ADVANCED (default: enabled)
        # Controls branch/tag correlation enrichment
        ticket_history_advanced = os.getenv("TICKET_HISTORY_ADVANCED", "1").strip().lower() in ("1", "true", "yes", "on")
        
        # Feature flag: TICKET_HISTORY_TIME_AWARE (default: enabled)
        # Controls time-aware, deterministic correlation (requires both reachability AND time)
        ticket_history_time_aware = os.getenv("TICKET_HISTORY_TIME_AWARE", "1").strip().lower() in ("1", "true", "yes", "on")
        
        # Validate feature flags and warn if disabled
        if not ticket_history_advanced:
            warnings_root.append(_warning(
                level="warning",
                scope="global",
                reason="feature_flag_disabled",
                source="ticket_tracker",
                message="TICKET_HISTORY_ADVANCED is disabled - branch/tag correlation enrichment will not run. Ticket history will be incomplete.",
            ))
        
        if not ticket_history_time_aware:
            warnings_root.append(_warning(
                level="warning",
                scope="global",
                reason="feature_flag_disabled",
                source="ticket_tracker",
                message="TICKET_HISTORY_TIME_AWARE is disabled - time-aware correlation will not run. Deployment detection may include false positives.",
            ))
        
        if ticket_history_advanced:
            # Enrich with branch and tag information (tracks PRs through releases)
            try:
                enrich_ticket_index_with_branches_and_tags(
                    ticket_index,
                    projects_out,
                    github_org,
                    github_token,
                    max_repos=20,
                    max_branches_per_repo=50,
                    max_tags_per_repo=100,
                )
            except Exception as e:
                logger.warn("ticket_tracker_enrichment_failed", error=str(e), exc_info=True)
        
        # Time-aware correlation (deterministic, time-validated)
        if ticket_history_time_aware:
            try:
                enrich_ticket_index_time_aware(
                    ticket_index,
                    projects_out,
                    github_org,
                    github_token,
                    rest_base if teamcity_enabled and not teamcity_down else None,
                    teamcity_token if teamcity_enabled and not teamcity_down else None,
                    max_repos=20,
                    max_branches_per_repo=50,
                )
            except Exception as e:
                logger.warn("ticket_tracker_time_aware_correlation_failed", error=str(e), exc_info=True)
        
        # Note: jira_base, jira_email, jira_token initialized earlier (line ~2693)
        # Enrichment order matters: Jira first (adds summary/status), then env presence (needs PR data)
        enrich_ticket_index_with_jira(ticket_index, jira_base=jira_base, jira_email=jira_email, jira_token=jira_token)
        
        # Add environment presence and build timeline (uses tag changes from prev_snapshot)
        # NOTE: This function will be enhanced to use time-aware build-driven logic
        
        # NOTE (product decision): deployment detection no longer requires a previous snapshot.
        # We keep snapshots archived for persistence, but we do not warn on first run.
        
        # Add environment presence using current snapshot (existing logic)
        add_env_presence_to_ticket_index(ticket_index, projects_out, prev_snapshot=prev_snapshot, warnings=warnings_root)
        
        # Add persistent deployment presence from history (deterministic, persistent)
        # This ensures deployments remain visible across snapshots
        try:
            add_persistent_deployment_presence_to_tickets(
                ticket_index,
                projects_out,
                prev_snapshot=prev_snapshot,
                github_org=github_org,
                github_token=github_token,
                warnings=warnings_root,
            )
        except Exception as e:
            # Don't fail snapshot if deployment history processing fails
            warnings_root.append(_warning(
                level="warning",
                scope="global",
                reason="deployment_history_processing_failed",
                source="ticket_tracker",
                message=f"Failed to process deployment history: {e}",
            ))
            logger.warn("ticket_tracker_deployment_history_failed", error=str(e), exc_info=True)
        
        # Ensure all tickets have required structure even if enrichment failed
        # Also validate data completeness and add warnings
        tickets_with_missing_data = 0
        tickets_with_missing_prs = 0
        
        for ticket_key, ticket in ticket_index.items():
            ticket.setdefault("envPresence", {})
            ticket.setdefault("envPresenceMeta", {})
            ticket.setdefault("timeline", [])
            ticket.setdefault("repos", [])
            ticket.setdefault("prs", [])
            
            # Validate ticket data completeness
            prs = ticket.get("prs") or ticket.get("pullRequests") or []
            if not prs:
                tickets_with_missing_prs += 1
            
            # Check for missing critical fields
            has_missing_data = False
            if not ticket.get("envPresence"):
                has_missing_data = True
            if not ticket.get("timeline"):
                has_missing_data = True
            
            if has_missing_data:
                tickets_with_missing_data += 1
            
            # Ensure Jira summary is accessible for AI narrative (flatten jira.summary -> summary)
            if ticket.get("jira") and ticket.get("jira").get("summary"):
                ticket["summary"] = ticket["jira"]["summary"]
            if ticket.get("jira") and ticket.get("jira").get("status"):
                ticket["status"] = ticket["jira"]["status"]
            if ticket.get("jira") and ticket.get("jira").get("url"):
                ticket["jiraUrl"] = ticket["jira"]["url"]
        
        # Add validation warnings if data completeness issues detected
        if tickets_with_missing_prs > 0:
            warnings_root.append(_warning(
                level="warning",
                scope="global",
                reason="tickets_missing_prs",
                source="ticket_tracker",
                message=f"{tickets_with_missing_prs} ticket(s) have no associated PRs - deployment correlation may be incomplete",
            ))
        
        if tickets_with_missing_data > 0:
            warnings_root.append(_warning(
                level="warning",
                scope="global",
                reason="tickets_missing_deployment_data",
                source="ticket_tracker",
                message=f"{tickets_with_missing_data} ticket(s) have incomplete deployment data - may indicate enrichment failures",
            ))
        
        payload["ticketIndex"] = ticket_index
        
        # Calculate GitHub and Jira coverage after ticket index is built
        github_coverage = {"tickets": 0}
        jira_coverage = {"tickets": 0}
        for ticket in ticket_index.values():
            if ticket.get("pullRequests") or ticket.get("prs"):
                github_coverage["tickets"] += 1
            if ticket.get("jira"):
                jira_coverage["tickets"] += 1
        
        # Update coverage in payload
        if payload.get("integrations", {}).get("github"):
            # Include the time window so UI can explain \"recent\" coverage.
            github_coverage_with_window = dict(github_coverage)
            github_coverage_with_window["windowDays"] = days
            payload["integrations"]["github"]["coverage"] = github_coverage_with_window
        if payload.get("integrations", {}).get("jira"):
            jira_coverage_with_window = dict(jira_coverage)
            jira_coverage_with_window["windowDays"] = days
            payload["integrations"]["jira"]["coverage"] = jira_coverage_with_window
    except Exception as e:
        logger.error("ticket_tracker_build_failed", error=str(e), exc_info=True)

    # ------------------------------------------------------------
    # Release History (Stage 1):
    # - write latest.json
    # - archive latest.json under data/history (retention: 100)
    # - update data/release_history.json with TAG_CHANGE events
    # ------------------------------------------------------------

    write_latest_json(payload)

    try:
        archive_latest_snapshot(payload)
        # Use append-only storage if enabled (enterprise-grade)
        if RELEASE_HISTORY_APPEND_ONLY:
            update_release_history_append_only(payload, prev_snapshot, github_token)
        else:
            # Legacy format (backward compatibility)
            update_release_history_file(payload, prev_snapshot, github_token)
    except Exception as e:
        logger.warn("release_history_update_failed", error=str(e), exc_info=True)


if __name__ == "__main__":
    # Check for CLI flags
    if len(sys.argv) > 1:
        if sys.argv[1] == "--onboard-select":
            # Run selection-based onboarding wizard (NEW - recommended)
            try:
                from selection_onboarding_wizard import run_selection_onboarding_wizard
                run_selection_onboarding_wizard()
            except ImportError as e:
                print(f"❌ Error: Could not import selection onboarding wizard: {e}")
                print("   Make sure selection_onboarding_wizard.py is in the same directory as snapshot.py")
                sys.exit(1)
            except Exception as e:
                print(f"❌ Error running selection onboarding wizard: {e}")
                import traceback
                traceback.print_exc()
                sys.exit(1)
            sys.exit(0)
        elif sys.argv[1] == "--onboard":
            # Run unified onboarding wizard (mapping-based, legacy)
            try:
                from unified_onboarding_wizard import run_unified_onboarding_wizard
                run_unified_onboarding_wizard()
            except ImportError as e:
                print(f"❌ Error: Could not import onboarding wizard: {e}")
                print("   Make sure unified_onboarding_wizard.py is in the same directory as snapshot.py")
                sys.exit(1)
            except Exception as e:
                print(f"❌ Error running onboarding wizard: {e}")
                sys.exit(1)
            sys.exit(0)
        elif sys.argv[1] == "--dd-map":
            # Run Datadog mapping wizard (legacy, for backward compatibility)
            try:
                from datadog_mapping_wizard import run_mapping_wizard
                run_mapping_wizard()
            except ImportError as e:
                print(f"❌ Error: Could not import Datadog mapping wizard: {e}")
                sys.exit(1)
            except Exception as e:
                print(f"❌ Error running Datadog mapping wizard: {e}")
                sys.exit(1)
            sys.exit(0)
        elif sys.argv[1] in ("--help", "-h"):
            print("Usage: python snapshot.py [OPTIONS]")
            print("\nOptions:")
            print("  --onboard-select  Run selection-based onboarding wizard (RECOMMENDED)")
            print("                    Discovers resources, lets you select what to track")
            print("  --onboard         Run mapping-based onboarding wizard (legacy)")
            print("  --dd-map          Run Datadog mapping wizard (legacy)")
            print("  --help, -h        Show this help message")
            print("\nIf no options are provided, runs the snapshot generation.")
            sys.exit(0)
    
    # Default: run snapshot generation
    main()
