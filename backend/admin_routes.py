"""
Admin API: test connections (read-only), validate config, dry-run.
Credentials are accepted only in request body for the test call; never stored or logged.
"""

import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import requests
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from .datadog_client import DatadogConfig, validate as dd_validate

router = APIRouter(prefix="/api/admin", tags=["admin"])

GITHUB_API = "https://api.github.com"


# ---------------------------------------------------------------------------
# Request/response models
# ---------------------------------------------------------------------------


class TestGitHubRequest(BaseModel):
    org: str = ""
    token: str = ""


class TestTeamCityRequest(BaseModel):
    baseUrl: str = ""
    token: str = ""


class TestJiraRequest(BaseModel):
    baseUrl: str = ""
    email: str = ""
    token: str = ""


class TestDatadogRequest(BaseModel):
    site: str = "datadoghq.com"
    apiKey: str = ""
    appKey: str = ""


class TestArgoCDRequest(BaseModel):
    baseUrl: str = ""
    token: str = ""


class ValidateRequest(BaseModel):
    config: Dict[str, Any] = Field(default_factory=dict)


class DryRunRequest(BaseModel):
    config: Dict[str, Any] = Field(default_factory=dict)


def _ok(checked_at: str, message: str = "ok", meta: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return {"ok": True, "checkedAt": checked_at, "message": message, "meta": meta or {}}


def _fail(checked_at: str, message: str, meta: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return {"ok": False, "checkedAt": checked_at, "message": message, "meta": meta or {}}


def _mask_token(t: str) -> str:
    if not t or len(t) < 8:
        return "***"
    return t[:4] + "…" + t[-4:]


# ---------------------------------------------------------------------------
# Test connections (read-only)
# ---------------------------------------------------------------------------


@router.post("/test/github")
def test_github(req: TestGitHubRequest) -> Dict[str, Any]:
    """Validate GitHub token and org access. Read-only."""
    ts = datetime.now(tz=timezone.utc).isoformat()
    org = (req.org or "").strip()
    token = (req.token or "").strip()
    if not token:
        return _fail(ts, "GitHub token is required.")
    if not org:
        return _fail(ts, "GitHub org/owner is required.")

    url = f"{GITHUB_API}/orgs/{org}"
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "release-ops-admin",
    }
    try:
        r = requests.get(url, headers=headers, timeout=15)
        if r.status_code == 401:
            return _fail(ts, "Invalid token or token expired.", {"masked": _mask_token(token)})
        if r.status_code == 404:
            return _fail(ts, "Org not found or no access.", {"org": org, "masked": _mask_token(token)})
        r.raise_for_status()
        return _ok(ts, "Token valid, org accessible.", {"org": org, "masked": _mask_token(token)})
    except requests.RequestException as e:
        return _fail(ts, f"Request failed: {getattr(e, 'message', str(e))}", {"error": str(e)})


def _teamcity_rest_base(base: str) -> str:
    b = (base or "").strip().rstrip("/").replace("/httpAuth", "")
    if not b:
        return ""
    if "/app/rest" in b:
        i = b.find("/app/rest")
        return b[: i + len("/app/rest")]
    return b + "/app/rest"


@router.post("/test/teamcity")
def test_teamcity(req: TestTeamCityRequest) -> Dict[str, Any]:
    """Check TeamCity URL and token. Read-only."""
    ts = datetime.now(tz=timezone.utc).isoformat()
    base = (req.baseUrl or "").strip()
    token = (req.token or "").strip()
    if not base:
        return _fail(ts, "TeamCity base URL is required.")
    if not token:
        return _fail(ts, "TeamCity token is required.")

    rest = _teamcity_rest_base(base)
    url = f"{rest}/server"
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    try:
        r = requests.get(url, headers=headers, timeout=15)
        if r.status_code in (401, 403):
            return _fail(ts, "Invalid token or insufficient permissions.", {"masked": _mask_token(token)})
        r.raise_for_status()
        return _ok(ts, "TeamCity reachable, token valid.", {"baseUrl": base, "masked": _mask_token(token)})
    except requests.RequestException as e:
        return _fail(ts, f"Request failed: {str(e)}", {"error": str(e)})


@router.post("/test/jira")
def test_jira(req: TestJiraRequest) -> Dict[str, Any]:
    """Check Jira base URL and API token. Read-only."""
    ts = datetime.now(tz=timezone.utc).isoformat()
    base = (req.baseUrl or "").strip().rstrip("/")
    email = (req.email or "").strip()
    token = (req.token or "").strip()
    if not base:
        return _fail(ts, "Jira base URL is required.")
    if not email or not token:
        return _fail(ts, "Jira email and API token are required.")

    url = f"{base}/rest/api/3/myself"
    auth = (email, token)
    headers = {"Accept": "application/json"}
    try:
        r = requests.get(url, auth=auth, headers=headers, timeout=15)
        if r.status_code in (401, 403):
            return _fail(ts, "Invalid email/token or insufficient permissions.", {"masked": _mask_token(token)})
        r.raise_for_status()
        return _ok(ts, "Jira reachable, credentials valid.", {"baseUrl": base, "masked": _mask_token(token)})
    except requests.RequestException as e:
        return _fail(ts, f"Request failed: {str(e)}", {"error": str(e)})


def _normalize_dd_site(s: str) -> str:
    s = (s or "").strip().lower().replace("https://", "").replace("http://", "").strip("/")
    return s or "datadoghq.com"


@router.post("/test/datadog")
def test_datadog(req: TestDatadogRequest) -> Dict[str, Any]:
    """Validate Datadog API and Application keys. Read-only."""
    ts = datetime.now(tz=timezone.utc).isoformat()
    site = _normalize_dd_site(req.site or "datadoghq.com")
    api_key = (req.apiKey or "").strip()
    app_key = (req.appKey or "").strip()
    if not api_key:
        return _fail(ts, "Datadog API key is required.")
    if not app_key:
        return _fail(ts, "Datadog Application key is required.")

    cfg = DatadogConfig(site=site, api_key=api_key, app_key=app_key)
    ok, reason, meta = dd_validate(cfg)
    if ok:
        return _ok(ts, "Datadog keys valid.", {**meta, "masked": _mask_token(api_key)})
    return _fail(ts, reason or "Validation failed.", {**(meta or {}), "masked": _mask_token(api_key)})


@router.post("/test/argocd")
def test_argocd(req: TestArgoCDRequest) -> Dict[str, Any]:
    """Check ArgoCD URL and token. Read-only."""
    ts = datetime.now(tz=timezone.utc).isoformat()
    base = (req.baseUrl or "").strip().rstrip("/")
    token = (req.token or "").strip()
    if not base:
        return _fail(ts, "ArgoCD base URL is required.")
    if not token:
        return _fail(ts, "ArgoCD token is required.")

    url = f"{base}/api/v1/session"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    try:
        r = requests.get(url, headers=headers, timeout=15)
        if r.status_code in (401, 403):
            return _fail(ts, "Invalid token or insufficient permissions.", {"masked": _mask_token(token)})
        r.raise_for_status()
        return _ok(ts, "ArgoCD reachable, token valid.", {"baseUrl": base, "masked": _mask_token(token)})
    except requests.RequestException as e:
        return _fail(ts, f"Request failed: {str(e)}", {"error": str(e)})


# ---------------------------------------------------------------------------
# Validate config (server-side)
# ---------------------------------------------------------------------------


def _validate_config(c: Dict[str, Any]) -> Dict[str, Any]:
    errors: List[Dict[str, str]] = []
    warnings: List[Dict[str, str]] = []

    t = c.get("tenant") or {}
    tk = c.get("ticketing") or {}
    projs = c.get("projects") or []
    intg = c.get("integrations") or {}
    gh = intg.get("github") or {}

    if not str(t.get("name") or "").strip():
        errors.append({"field": "tenant.name", "message": "Tenant name is required."})
    slug = str(t.get("slug") or "").strip()
    if slug and not re.match(r"^[a-z0-9][a-z0-9-]*[a-z0-9]$|^[a-z0-9]$", slug):
        errors.append({"field": "tenant.slug", "message": "Slug must be lowercase letters, numbers, hyphens (e.g. my-org)."})

    regex = str(tk.get("regex") or "").strip()
    if regex:
        try:
            re.compile(regex)
        except re.error as e:
            errors.append({"field": "ticketing.regex", "message": f"Invalid ticket regex: {e}"})

    if not str(gh.get("org") or "").strip():
        errors.append({"field": "integrations.github.org", "message": "GitHub org/owner is required."})
    if not str(gh.get("token") or "").strip():
        errors.append({"field": "integrations.github.token", "message": "GitHub token is required."})

    if not projs:
        errors.append({"field": "projects", "message": "At least one project is required."})
    for i, p in enumerate(projs):
        pk = (p or {}).get("key") or ""
        pn = (p or {}).get("name") or ""
        envs = (p or {}).get("environments") or []
        svcs = (p or {}).get("services") or []
        if not str(pk).strip():
            errors.append({"field": f"projects[{i}].key", "message": "Project key is required."})
        if not str(pn).strip():
            warnings.append({"field": f"projects[{i}].name", "message": "Project display name is recommended."})
        if not envs:
            errors.append({"field": f"projects[{i}].environments", "message": "Each project must have at least one environment."})
        if not svcs:
            warnings.append({"field": f"projects[{i}].services", "message": "No services: deployment data will be missing."})

    return {
        "valid": len(errors) == 0,
        "errors": errors,
        "warnings": warnings,
    }


@router.post("/validate")
def validate_config(req: ValidateRequest) -> Dict[str, Any]:
    """Validate tenant config. No credentials stored."""
    result = _validate_config(req.config or {})
    result["checkedAt"] = datetime.now(tz=timezone.utc).isoformat()
    return result


# ---------------------------------------------------------------------------
# Dry-run (simulate snapshot)
# ---------------------------------------------------------------------------


@router.post("/snapshot/dry-run")
def snapshot_dry_run(req: DryRunRequest) -> Dict[str, Any]:
    """
    Validate config and return a simulated snapshot summary.
    Does not run the real snapshot generator; no writes.
    """
    ts = datetime.now(tz=timezone.utc).isoformat()
    c = req.config or {}
    val = _validate_config(c)
    if not val["valid"]:
        return {
            "ok": False,
            "checkedAt": ts,
            "message": "Config invalid; fix errors before dry-run.",
            "validation": val,
            "summary": None,
        }

    projs = c.get("projects") or []
    projects_summary = []
    for p in projs:
        envs = (p or {}).get("environments") or []
        svcs = (p or {}).get("services") or []
        projects_summary.append({
            "key": (p or {}).get("key") or "",
            "name": (p or {}).get("name") or (p or {}).get("key") or "",
            "environments": len(envs),
            "services": len(svcs),
        })

    intg = c.get("integrations") or {}
    gh = intg.get("github") or {}
    tc = intg.get("teamcity") or {}
    jira = intg.get("jira") or {}
    dd = intg.get("datadog") or {}
    argo = intg.get("argocd") or {}

    integration_states = {
        "github": "configured" if (str(gh.get("org") or "").strip() and str(gh.get("token") or "").strip()) else "missing",
        "teamcity": "configured" if (str(tc.get("baseUrl") or "").strip() and str(tc.get("token") or "").strip()) else "disabled",
        "jira": "configured" if (str(jira.get("baseUrl") or "").strip() and str(jira.get("email") or "").strip() and str(jira.get("token") or "").strip()) else "disabled",
        "datadog": "configured" if (str(dd.get("apiKey") or "").strip() and str(dd.get("appKey") or "").strip()) else "disabled",
        "argocd": "configured" if (argo.get("envHosts") and isinstance(argo.get("envHosts"), dict) and len(argo.get("envHosts", {})) > 0) else "disabled",
    }

    warnings_count = len(val.get("warnings") or [])

    return {
        "ok": True,
        "checkedAt": ts,
        "message": "Dry-run complete.",
        "validation": val,
        "summary": {
            "projectsCount": len(projects_summary),
            "projects": projects_summary,
            "integrationStates": integration_states,
            "warningsCount": warnings_count,
        },
    }
