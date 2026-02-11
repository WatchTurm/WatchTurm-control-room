"""
Simple API Server for Snapshot Management.

Provides REST endpoints for:
- GET /api/snapshot/status - Get snapshot status
- POST /api/snapshot/trigger - Trigger manual snapshot
- GET /api/snapshot/progress - Get progress (same as status.progress)

Usage:
    python snapshot_api_server.py [--port 8001] [--interval 30]
"""

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    from flask import Flask, jsonify, make_response, request
except ImportError:
    print("Error: Flask is required. Install with: pip install flask")
    sys.exit(1)

from snapshot_scheduler import get_scheduler, DEFAULT_INTERVAL_MINUTES
from snapshot import (
    build_ticket_index_from_github,
    load_project_configs,
    GITHUB_ORG_DEFAULT,
)

import requests

app = Flask(__name__)

# Enable CORS for all routes (OPTIONS handled in before_request with its own CORS)
@app.after_request
def after_request(response):
    if request.method == "OPTIONS":
        return response
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    response.headers["Access-Control-Allow-Methods"] = "GET, PUT, POST, DELETE, OPTIONS"
    return response


@app.before_request
def handle_cors_preflight():
    """Respond to OPTIONS preflight with 200 + CORS so browser allows actual request (e.g. POST :8080 → :8001)."""
    if request.method != "OPTIONS":
        return None
    r = make_response("", 200)
    r.headers["Access-Control-Allow-Origin"] = "*"
    r.headers["Access-Control-Allow-Methods"] = "GET, PUT, POST, DELETE, OPTIONS"
    r.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    r.headers["Access-Control-Max-Age"] = "86400"
    return r


@app.route("/api/runbooks/<path:sub>", methods=["OPTIONS"])
def options_runbooks(sub):
    """Explicit OPTIONS route for runbooks so preflight never 404s (before_request can miss in some setups)."""
    r = make_response("", 200)
    r.headers["Access-Control-Allow-Origin"] = "*"
    r.headers["Access-Control-Allow-Methods"] = "GET, PUT, POST, DELETE, OPTIONS"
    r.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    r.headers["Access-Control-Max-Age"] = "86400"
    return r


# Global scheduler instance
scheduler = None


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _load_dotenv_and_github() -> Tuple[str, str]:
    """Load .env and return (github_org, github_token).

    This is shared between ticket endpoint and runbooks. All runbooks are
    strictly read-only and only use GitHub compare / branch APIs.
    """
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parents[1] / ".env")

    github_token = (os.getenv("GITHUB_TOKEN") or "").strip()
    github_org = (os.getenv("GITHUB_ORG") or GITHUB_ORG_DEFAULT).strip() or GITHUB_ORG_DEFAULT

    if not github_token:
        raise RuntimeError("GITHUB_TOKEN is missing. Runbooks require GitHub access.")

    return github_org, github_token


def _github_request(
    method: str,
    org: str,
    repo: str,
    path: str,
    token: str,
    params: Optional[Dict[str, Any]] = None,
) -> requests.Response:
    """Small GitHub REST v3 wrapper used by runbooks.

    - Adds auth header
    - Raises for non-2xx responses
    """
    url = f"https://api.github.com/repos/{org}/{repo}{path}"
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "release-ops-control-room-mvp1",
    }
    resp = requests.request(method.upper(), url, headers=headers, params=params, timeout=30)
    if resp.status_code >= 400:
        # Do not raise for 404 in some helper paths – callers can handle.
        if resp.status_code == 404:
            return resp
        raise RuntimeError(f"GitHub API {method} {url} failed with {resp.status_code}: {resp.text[:200]}")
    return resp


def _github_ref_exists(org: str, repo: str, ref: str, token: str) -> bool:
    """Best-effort: check if a branch or tag exists."""
    if not ref:
        return False
    # Branch
    r = _github_request("GET", org, repo, f"/branches/{ref}", token)
    if r.status_code == 200:
        return True
    # Tag
    r = _github_request("GET", org, repo, f"/git/ref/tags/{ref}", token)
    return r.status_code == 200


def _pick_release_branch(
    org: str,
    repo: str,
    token: str,
    patterns: List[str],
    strategy: str,
    version_regex: Optional[str] = None,
) -> Optional[str]:
    """Pick a baseline release branch using flexible pattern matching and strategy rules.

    - patterns: list of regex or glob-like patterns, e.g. ["release/.*", "release/BE\\.\\d+\\.\\d+"]
    - strategy: "semver" (recommended) or "recent"
    - version_regex: optional regex to extract version (auto-detected if not provided)

    Supports:
    - Regex patterns: "release/\\d+\\.\\d+\\.\\d+"
    - Glob-like patterns: "release/*"
    - Auto-detection of common formats (TAP2.0, TCBP, semantic versioning)
    - Frontend/backend filtering when patterns include BE/FE
    """
    if not patterns:
        return None

    # Detect if repo is frontend (heuristic: check repo name)
    is_frontend = "frontend" in repo.lower() or "-fe" in repo.lower() or repo.endswith("-fe")

    # Fetch ALL branches with pagination
    all_names = []
    page = 1
    while True:
        resp = _github_request("GET", org, repo, "/branches", token, params={"per_page": 100, "page": page})
        if resp.status_code != 200:
            break
        data = resp.json() or []
        if not data:
            break
        names = [b.get("name") for b in data if isinstance(b, dict) and b.get("name")]
        all_names.extend(names)
        if len(names) < 100:  # Last page
            break
        page += 1

    if not all_names:
        return None

    def matches(name: str) -> bool:
        """Check if branch name matches any pattern using regex or glob matching."""
        for pat in patterns:
            pat = pat.strip()
            if not pat:
                continue
            
            # Check if pattern is regex (contains regex special chars) or glob-like
            is_regex = any(c in pat for c in ["\\", "^", "$", "[", "(", "?", "+", "{", "|"])
            
            if is_regex:
                # Try regex matching
                try:
                    if re.match(pat, name):
                        # For FE/BE filtering: if pattern matches both, filter by repo type
                        if "BE" in pat or "FE" in pat:
                            if is_frontend and "FE" in pat:
                                return True
                            elif not is_frontend and "BE" in pat:
                                return True
                        else:
                            return True
                except re.error:
                    # Invalid regex, fall through to glob matching
                    pass
            
            # Glob-like matching (simple prefix/suffix)
            if pat.endswith("*"):
                prefix = pat[:-1]
                if name.startswith(prefix):
                    # Additional FE/BE filtering for generic patterns
                    if prefix == "release/" and ("BE" in name or "FE" in name):
                        if is_frontend and name.startswith("release/FE."):
                            return True
                        elif not is_frontend and name.startswith("release/BE."):
                            return True
                    else:
                        return True
            elif name == pat:
                return True
        
        return False

    candidates = [n for n in all_names if matches(n)]
    if not candidates:
        return None

    if strategy == "semver":
        def extract_semver(name: str) -> Tuple[int, int, int]:
            """Extract semantic version from branch name using provided regex or auto-detection."""
            # Use provided regex if available
            if version_regex:
                try:
                    m = re.search(version_regex, name)
                    if m:
                        groups = m.groups()
                        major = int(groups[0]) if len(groups) > 0 else 0
                        minor = int(groups[1]) if len(groups) > 1 else 0
                        patch = int(groups[2]) if len(groups) > 2 else 0
                        return (major, minor, patch)
                except (re.error, ValueError, IndexError):
                    pass  # Fall through to auto-detection
            
            # Auto-detection: try common formats
            # TAP2.0 format: release/BE.1.31 or release/FE.1.31
            m = re.search(r"(?:BE|FE)\.(\d+)\.(\d+)", name)
            if m:
                return (int(m.group(1)), int(m.group(2)), 0)
            
            # Semantic version: x.y.z
            m = re.search(r"(\d+)\.(\d+)\.(\d+)", name)
            if m:
                return (int(m.group(1)), int(m.group(2)), int(m.group(3)))
            
            # Major.minor: x.y
            m = re.search(r"(\d+)\.(\d+)", name)
            if m:
                return (int(m.group(1)), int(m.group(2)), 0)
            
            return (0, 0, 0)

        candidates.sort(key=lambda n: extract_semver(n), reverse=True)
        return candidates[0]

    # Default: "recent" – lexicographic ordering
    candidates.sort()
    return candidates[-1]


def _iter_project_services(project_cfg: dict) -> List[Dict[str, Any]]:
    """Return list of service descriptors with at least a codeRepo."""
    services = project_cfg.get("services") or []
    out = []
    for svc in services:
        if not isinstance(svc, dict):
            continue
        code_repo = (svc.get("codeRepo") or "").strip()
        if not code_repo:
            continue
        out.append(
            {
                "key": svc.get("key") or code_repo,
                "codeRepo": code_repo,
            }
        )
    return out


def _get_project_cfg(project_key: str) -> Optional[dict]:
    """Find a project config by key (case-insensitive)."""
    for cfg in load_project_configs():
        proj = cfg.get("project") or {}
        if not proj:
            continue
        key = str(proj.get("key") or "").strip()
        if key and key.upper() == project_key.upper():
            return cfg
    return None


def _load_branching_strategy(project_cfg: dict, repo: Optional[str] = None) -> Dict[str, Any]:
    """Load optional runbook branching strategy from project config.

    Supports per-repo overrides and auto-detection for common patterns.
    
    Example YAML snippet:

        runbooks:
          branching:
            defaultBranch: main
            releaseBranchPatterns:
              - "release/.*"              # Regex pattern
              - "release/BE\\.\\d+\\.\\d+" # TAP2.0 backend
            releaseBranchPickStrategy: semver
            versionExtractionRegex: "(\\d+)\\.(\\d+)(?:\\.(\\d+))?"
            repoOverrides:
              "frontend-repo":
                releaseBranchPatterns:
                  - "release/FE\\.\\d+\\.\\d+"
    """
    run_cfg = (project_cfg.get("runbooks") or {}).get("branching") or {}
    
    # Check for per-repo override
    repo_overrides = {}
    if repo:
        repo_overrides = (run_cfg.get("repoOverrides") or {}).get(repo) or {}
    
    # Merge base config with repo override (repo override takes precedence)
    merged_cfg = {**run_cfg, **repo_overrides}
    
    default_branch = (merged_cfg.get("defaultBranch") or "").strip() or "main"
    patterns = [str(p).strip() for p in merged_cfg.get("releaseBranchPatterns") or [] if str(p).strip()]
    strategy = (merged_cfg.get("releaseBranchPickStrategy") or "semver").strip() or "semver"
    version_regex = (merged_cfg.get("versionExtractionRegex") or "").strip()
    
    # Auto-detect patterns if none provided
    if not patterns:
        patterns = ["release/.*", "release/\\d+\\.\\d+(\\.\\d+)?", "release/v?\\d+\\.\\d+"]
    
    return {
        "defaultBranch": default_branch,
        "releaseBranchPatterns": patterns,
        "releaseBranchPickStrategy": strategy,
        "versionExtractionRegex": version_regex,
    }


@app.route("/api/snapshot/status", methods=["GET"])
def get_status():
    """Get snapshot status."""
    status = scheduler.get_status()
    return jsonify(status)


@app.route("/api/snapshot/trigger", methods=["POST"])
def trigger_snapshot():
    """Trigger manual snapshot."""
    if scheduler.trigger_manual():
        return jsonify({"success": True, "message": "Snapshot triggered"})
    else:
        return jsonify({"success": False, "message": "Snapshot already running"}), 409


@app.route("/api/snapshot/progress", methods=["GET"])
def get_progress():
    """Get snapshot progress (alias for status.progress)."""
    status = scheduler.get_status()
    progress = status.get("progress", {})
    return jsonify(progress)


@app.route("/api/datadog/health", methods=["GET"])
def datadog_health():
    """Lightweight Datadog runtime health check used by the UI.

    For now this is a simple stub that only verifies that the snapshot API
    server is reachable. If needed in the future we can extend this to
    actually call Datadog APIs and return a richer payload.
    """
    return jsonify({"status": "ok"})


@app.route("/health", methods=["GET"])
def health():
    """Health check endpoint."""
    return jsonify({"status": "ok"})


@app.route("/api/ticket/<ticket_key>", methods=["GET"])
def get_ticket(ticket_key: str):
    """Return on-demand ticket details for a given key.

    This is a best-effort live view built from the latest snapshot logic.
    It scans GitHub PRs (using widened window) and returns a single ticket
    entry in the same normalized shape that Ticket Tracker UI expects.
    """
    key = (ticket_key or "").strip().upper()
    if not key:
        return jsonify({"status": "error", "message": "Empty ticket key"}), 400

    # Load env and configs in-process.
    from dotenv import load_dotenv
    import os

    load_dotenv(Path(__file__).resolve().parents[1] / ".env")

    github_token = os.getenv("GITHUB_TOKEN", "").strip()
    github_org = (os.getenv("GITHUB_ORG") or GITHUB_ORG_DEFAULT).strip() or GITHUB_ORG_DEFAULT

    if not github_token:
        return jsonify({
            "key": key,
            "status": "error",
            "message": "GITHUB_TOKEN is missing. Live ticket fetch requires GitHub access."
        }), 503

    # Load project configs (same as snapshot.py)
    configs = load_project_configs()
    projects_out = []
    for cfg in configs:
        proj = cfg.get("project", {})
        if not proj.get("key"):
            continue
        # We only need project key + environments + services with repos
        envs = cfg.get("environments", [])
        services = cfg.get("services", [])
        projects_out.append({
            "key": proj.get("key"),
            "name": proj.get("name") or proj.get("key"),
            "environments": [
                {"key": e.get("key"), "name": e.get("name") or e.get("key")}
                for e in envs
            ],
            "services": services,
        })

    # Reuse GitHub ticket index builder with wide window, then extract this ticket.
    try:
        days_env = (os.getenv("TICKET_TRACKER_DAYS") or "").strip()
        days = int(days_env or "120")
    except Exception:
        days = 120

    try:
        ticket_index = build_ticket_index_from_github(projects_out, github_org, github_token, days=days)
    except Exception as e:
        return jsonify({
            "key": key,
            "status": "error",
            "message": f"Failed to build ticket index from GitHub: {type(e).__name__}",
        }), 500

    t = ticket_index.get(key)
    if not t:
        return jsonify({
            "key": key,
            "status": "not_found",
            "message": f"Ticket '{key}' not found in GitHub PR history (last {days} days) using current config."
        }), 404

    # Normalize to a frontend-friendly shape.
    jira = t.get("jira") or {}
    prs = t.get("pullRequests") or t.get("prs") or []
    evidence = t.get("evidence") or []
    timeline = t.get("timeline") or []

    norm_prs = []
    for p in prs:
        norm_prs.append({
            "repo": p.get("repo") or p.get("repository") or "",
            "number": p.get("number") or p.get("pr") or "",
            "title": p.get("title") or "",
            "url": p.get("url") or p.get("html_url") or "",
            "mergedAt": p.get("mergedAt") or p.get("merged_at") or "",
            "baseRef": p.get("baseRef") or p.get("base") or "",
            "headRef": p.get("headRef") or p.get("head") or "",
        })

    norm_evidence = []
    for e in evidence:
        norm_evidence.append({
            "repo": e.get("repo") or e.get("repository") or "",
            "component": e.get("component") or "",
            "tag": e.get("tag") or "",
            "branch": e.get("branch") or "",
            "build": e.get("build") or "",
            "deployedAt": e.get("deployedAt") or "",
            "buildUrl": e.get("buildUrl") or "",
            "source": e.get("source") or "component_metadata",
        })

    return jsonify({
        "key": key,
        "status": "ok",
        "sources": {
            "jira": bool(jira),
            "github": bool(norm_prs),
            "teamcity": bool(norm_evidence),
        },
        "jira": jira,
        "prs": norm_prs,
        "evidence": norm_evidence,
        "timeline": timeline,
    })


# ---------------------------------------------------------------------------
# Runbooks (read-only) – Scope / Drift / Readiness
# ---------------------------------------------------------------------------


def _fetch_branch_names(org: str, repo: str, token: str) -> List[str]:
    """Fetch all branch names for a repo (with pagination)."""
    names: List[str] = []
    page = 1
    while True:
        resp = _github_request("GET", org, repo, "/branches", token, params={"per_page": 100, "page": page})
        if resp.status_code != 200:
            break
        data = resp.json() or []
        if not data:
            break
        for b in data:
            if isinstance(b, dict) and b.get("name"):
                names.append(str(b["name"]))
        if len(data) < 100:
            break
        page += 1
    return names


def _version_sort_key(suffix: str) -> Tuple[int, ...]:
    """Turn branch suffix (e.g. 0.21.0 or 0.23.0) into a tuple for sorting. Latest = max."""
    parts = re.findall(r"\d+", suffix)
    return tuple(int(p) for p in parts) if parts else (0,)


def _latest_branch_with_prefix(org: str, repo: str, token: str, prefix: str) -> Optional[str]:
    """Return the 'latest' branch name that starts with prefix (e.g. release/0.23.0). Sorts by version."""
    if not prefix:
        return None
    names = _fetch_branch_names(org, repo, token)
    matching = [n for n in names if n.startswith(prefix)]
    if not matching:
        return None
    # Sort by version: suffix after prefix (e.g. 0.21.0, 0.23.0) - take latest
    def key(name: str) -> Tuple[int, ...]:
        suffix = name[len(prefix):].lstrip("/") if len(name) > len(prefix) else name
        return _version_sort_key(suffix)
    matching.sort(key=key)
    return matching[-1]


def _extract_tickets(text: str, ticket_regex: Optional[str]) -> List[str]:
    """Extract ticket IDs from text using the provided regex or a default.

    Default pattern allows hyphen or space between prefix and number, case-insensitive,
    e.g. TCBP-2881, TCBP 2881, Tcbp 2881.
    """
    if not text:
        return []
    pattern = ticket_regex or r"(?i)[A-Z][A-Z0-9]+[-\s]\d+"
    try:
        rx = re.compile(pattern)
    except re.error:
        rx = re.compile(r"(?i)[A-Z][A-Z0-9]+[-\s]\d+")
    return rx.findall(text)


def _extract_prs(text: str) -> List[str]:
    """Extract PR numbers from commit message (e.g. Merge pull request #123, (#456))."""
    if not text:
        return []
    # Match #N as PR refs; dedupe and return as "#123", "#456"
    matches = re.findall(r"#(\d+)", text)
    return [f"#{n}" for n in sorted(set(int(m) for m in matches), key=int)]


@app.route("/api/runbooks/scope", methods=["POST"])
def runbook_scope():
    """Runbook #1: GitHub scope checker between versions (read-only).

    Request JSON:
      {
        "projectKey": "TCBP_MFES",
        "baselineRef": "release/2026.01.01",   # optional - specific branch for all repos
        "baselinePrefix": "release",          # optional - use latest branch per repo matching prefix (either baselineRef or baselinePrefix)
        "headRef": "main",                    # optional
        "ticketRegex": "[A-Z]+-\\d+"          # optional
      }

    Response JSON:
      {
        "status": "ok",
        "projectKey": "...",
        "baselineRef": "...",
        "headRef": "...",
        "repos": [
          {
            "repo": "tcbp-mfe-air",
            "baselineRef": "...",
            "headRef": "...",
            "baselineExists": true,
            "headExists": true,
            "compareUrl": "https://github.com/org/repo/compare/baseline...head",
            "commitCount": 12,
            "tickets": ["TAP2-123", "TAP2-456"]
          },
          ...
        ],
        "summary": {
          "uniqueTickets": ["TAP2-123", "TAP2-456"],
          "totalCommits": 42
        },
        "warnings": [...]
      }
    """
    body = request.get_json(silent=True) or {}
    project_key = str(body.get("projectKey") or "").strip()
    if not project_key:
        return jsonify({"status": "error", "message": "projectKey is required"}), 400

    ticket_regex = body.get("ticketRegex") or None

    try:
        github_org, github_token = _load_dotenv_and_github()
    except RuntimeError as e:
        return jsonify({"status": "error", "message": str(e)}), 503

    project_cfg = _get_project_cfg(project_key)
    if not project_cfg:
        return jsonify({"status": "error", "message": f"Project config not found for key '{project_key}'"}), 404

    default_branch = body.get("headRef") or "main"
    baseline_override = body.get("baselineRef") or None
    baseline_prefix = (body.get("baselinePrefix") or "").strip() or None  # e.g. "release" -> latest per repo

    services = _iter_project_services(project_cfg)
    if not services:
        return jsonify({"status": "error", "message": "No services with codeRepo defined for this project"}), 400

    repos_out = []
    all_tickets: set[str] = set()
    total_commits = 0
    warnings: List[str] = []

    for svc in services:
        repo = svc["codeRepo"]
        # Load branching strategy (with per-repo override support)
        branching = _load_branching_strategy(project_cfg, repo)
        
        # Decide baseline/head per repo
        head_ref = body.get("headRef") or branching["defaultBranch"]
        if baseline_prefix:
            # Use latest branch per repo matching prefix (e.g. release/0.21.0, release/0.23.0)
            try:
                baseline_ref = _latest_branch_with_prefix(
                    github_org,
                    repo,
                    github_token,
                    baseline_prefix,
                )
            except Exception as e:
                baseline_ref = None
                warnings.append(f"{repo}: latest branch for prefix '{baseline_prefix}' failed: {e}")
        else:
            baseline_ref = baseline_override

        if not baseline_ref:
            baseline_ref = _pick_release_branch(
                github_org,
                repo,
                github_token,
                branching["releaseBranchPatterns"],
                branching["releaseBranchPickStrategy"],
                branching.get("versionExtractionRegex"),
            )

        repo_entry: Dict[str, Any] = {
            "repo": repo,
            "baselineRef": baseline_ref or "",
            "headRef": head_ref,
            "baselineExists": False,
            "headExists": False,
            "compareUrl": "",
            "commitCount": 0,
            "tickets": [],
        }

        # Validate refs
        if baseline_ref:
            repo_entry["baselineExists"] = _github_ref_exists(github_org, repo, baseline_ref, github_token)
        if head_ref:
            repo_entry["headExists"] = _github_ref_exists(github_org, repo, head_ref, github_token)

        if not (repo_entry["baselineExists"] and repo_entry["headExists"]):
            warnings.append(
                f"{repo}: baseline/head ref not fully available "
                f"(baseline={baseline_ref or '-'}, head={head_ref or '-'})"
            )
            repos_out.append(repo_entry)
            continue

        compare_path = f"/compare/{baseline_ref}...{head_ref}"
        try:
            resp = _github_request("GET", github_org, repo, compare_path, github_token)
        except Exception as e:
            warnings.append(f"{repo}: compare API failed: {e}")
            repos_out.append(repo_entry)
            continue

        data = resp.json() or {}
        commits = data.get("commits") or []
        repo_entry["commitCount"] = len(commits)
        repo_entry["compareUrl"] = data.get("html_url") or f"https://github.com/{github_org}/{repo}/compare/{baseline_ref}...{head_ref}"

        tickets_for_repo: set[str] = set()
        for c in commits:
            msg = str((c.get("commit") or {}).get("message") or "")
            for t in _extract_tickets(msg, ticket_regex):
                tickets_for_repo.add(t)
                all_tickets.add(t)

        repo_entry["tickets"] = sorted(tickets_for_repo)
        total_commits += len(commits)
        repos_out.append(repo_entry)

    return jsonify(
        {
            "status": "ok",
            "projectKey": project_key,
            "baselineRef": "" if baseline_prefix else (baseline_override or ""),  # per-repo baseline in repos[]
            "baselinePrefix": baseline_prefix or "",
            "headRef": default_branch,
            "repos": repos_out,
            "summary": {
                "uniqueTickets": sorted(all_tickets),
                "totalCommits": total_commits,
            },
            "warnings": warnings,
        }
    )


@app.route("/api/runbooks/latest-branches", methods=["POST"])
def runbook_latest_branches():
    """Return the latest branch (by version) matching a prefix for each repo.

    Request JSON:
      { "projectKey": "TCBP_MFES", "prefix": "release" }

    Response JSON:
      { "status": "ok", "repos": [{ "repo": "...", "branch": "release/0.23.0" }, ...], "warnings": [] }
    """
    body = request.get_json(silent=True) or {}
    project_key = str(body.get("projectKey") or "").strip()
    if not project_key:
        return jsonify({"status": "error", "message": "projectKey is required"}), 400
    prefix = (body.get("prefix") or "release").strip() or "release"

    try:
        github_org, github_token = _load_dotenv_and_github()
    except RuntimeError as e:
        return jsonify({"status": "error", "message": str(e)}), 503

    project_cfg = _get_project_cfg(project_key)
    if not project_cfg:
        return jsonify({"status": "error", "message": f"Project config not found for key '{project_key}'"}), 404

    services = _iter_project_services(project_cfg)
    if not services:
        return jsonify({"status": "error", "message": "No services with codeRepo defined for this project"}), 400

    repos_out: List[Dict[str, Any]] = []
    warnings: List[str] = []

    for svc in services:
        repo = svc["codeRepo"]
        try:
            latest = _latest_branch_with_prefix(github_org, repo, github_token, prefix)
            repos_out.append({"repo": repo, "branch": latest or ""})
        except Exception as e:
            warnings.append(f"{repo}: {e}")
            repos_out.append({"repo": repo, "branch": ""})

    return jsonify({
        "status": "ok",
        "projectKey": project_key,
        "prefix": prefix,
        "repos": repos_out,
        "warnings": warnings,
    })


@app.route("/api/runbooks/drift", methods=["POST"])
def runbook_drift():
    """Runbook #2: Drift / back-merge checker (read-only).

    Detects commits present on the selected release branch but missing from
    the default branch (hotfixes not back-merged).

    Request JSON:
      {
        "projectKey": "TCBP_MFES",
        "baselineRef": "release/2026.01.01",   # optional (release branch)
        "headRef": "main",                    # optional (default branch)
        "ticketRegex": "[A-Z]+-\\d+"          # optional
      }
    """
    body = request.get_json(silent=True) or {}
    project_key = str(body.get("projectKey") or "").strip()
    if not project_key:
        return jsonify({"status": "error", "message": "projectKey is required"}), 400

    ticket_regex = body.get("ticketRegex") or None

    try:
        github_org, github_token = _load_dotenv_and_github()
    except RuntimeError as e:
        return jsonify({"status": "error", "message": str(e)}), 503

    project_cfg = _get_project_cfg(project_key)
    if not project_cfg:
        return jsonify({"status": "error", "message": f"Project config not found for key '{project_key}'"}), 404

    default_branch = body.get("headRef") or "main"
    baseline_override = body.get("baselineRef") or None

    services = _iter_project_services(project_cfg)
    if not services:
        return jsonify({"status": "error", "message": "No services with codeRepo defined for this project"}), 400

    repos_out = []
    all_tickets: set[str] = set()
    total_commits = 0
    warnings: List[str] = []

    for svc in services:
        repo = svc["codeRepo"]
        # Load branching strategy (with per-repo override support)
        branching = _load_branching_strategy(project_cfg, repo)
        
        # For drift: baseline is release branch, head is default branch
        main_ref = body.get("headRef") or branching["defaultBranch"]
        release_ref = baseline_override

        if not release_ref:
            release_ref = _pick_release_branch(
                github_org,
                repo,
                github_token,
                branching["releaseBranchPatterns"],
                branching["releaseBranchPickStrategy"],
                branching.get("versionExtractionRegex"),
            )

        repo_entry: Dict[str, Any] = {
            "repo": repo,
            "releaseRef": release_ref or "",
            "mainRef": main_ref,
            "releaseExists": False,
            "mainExists": False,
            "compareUrl": "",
            "commitCount": 0,
            "tickets": [],
            "hasDrift": False,
        }

        if release_ref:
            repo_entry["releaseExists"] = _github_ref_exists(github_org, repo, release_ref, github_token)
        if main_ref:
            repo_entry["mainExists"] = _github_ref_exists(github_org, repo, main_ref, github_token)

        if not (repo_entry["releaseExists"] and repo_entry["mainExists"]):
            warnings.append(
                f"{repo}: release/main ref not fully available "
                f"(release={release_ref or '-'}, main={main_ref or '-'})"
            )
            repos_out.append(repo_entry)
            continue

        # We want commits that are in release but not in main:
        # compare main...release
        compare_path = f"/compare/{main_ref}...{release_ref}"
        try:
            resp = _github_request("GET", github_org, repo, compare_path, github_token)
        except Exception as e:
            warnings.append(f"{repo}: compare API failed: {e}")
            repos_out.append(repo_entry)
            continue

        data = resp.json() or {}
        commits = data.get("commits") or []
        ahead_by = int(data.get("ahead_by") or 0)
        repo_entry["hasDrift"] = ahead_by > 0
        repo_entry["commitCount"] = ahead_by
        repo_entry["compareUrl"] = data.get("html_url") or f"https://github.com/{github_org}/{repo}/compare/{main_ref}...{release_ref}"

        tickets_for_repo: set[str] = set()
        for c in commits:
            msg = str((c.get("commit") or {}).get("message") or "")
            for t in _extract_tickets(msg, ticket_regex):
                tickets_for_repo.add(t)
                all_tickets.add(t)

        repo_entry["tickets"] = sorted(tickets_for_repo)
        total_commits += ahead_by
        repos_out.append(repo_entry)

    return jsonify(
        {
            "status": "ok",
            "projectKey": project_key,
            "baselineRef": baseline_override or "",
            "headRef": default_branch,
            "repos": repos_out,
            "summary": {
                "uniqueTickets": sorted(all_tickets),
                "totalDriftCommits": total_commits,
            },
            "warnings": warnings,
        }
    )


@app.route("/api/runbooks/release-diff", methods=["POST"])
def runbook_release_diff():
    """Runbook: Release diff (release vs release). Older → newer: +commits, +tickets.

    Request JSON:
      {
        "projectKey": "TCBP_MFES",
        "releaseRefA": "release/0.21.0",   # older (old release)
        "releaseRefB": "release/0.23.0",   # newer (Release X / newest)
        "ticketRegex": "[A-Z]+-\\d+"       # optional
      }

    Response JSON:
      {
        "status": "ok",
        "projectKey": "...",
        "releaseRefA": "...",   # older
        "releaseRefB": "...",   # newer
        "repos": [
          {
            "repo": "...",
            "releaseA": "older", "releaseB": "newer",
            "added": { "commitCount": N, "tickets": [...], "prs": [...], "compareUrl": "..." }
          },
          ...
        ],
        "summary": { "addedTickets": [...], "totalAdded": N },
        "warnings": [...]
      }
    """
    body = request.get_json(silent=True) or {}
    project_key = str(body.get("projectKey") or "").strip()
    if not project_key:
        return jsonify({"status": "error", "message": "projectKey is required"}), 400

    ref_a = (body.get("releaseRefA") or "").strip() or None  # older
    ref_b = (body.get("releaseRefB") or "").strip() or None  # newer
    if not ref_a or not ref_b:
        return jsonify({"status": "error", "message": "releaseRefA (older) and releaseRefB (newer) are required"}), 400

    ticket_regex = body.get("ticketRegex") or None

    try:
        try:
            github_org, github_token = _load_dotenv_and_github()
        except RuntimeError as e:
            return jsonify({"status": "error", "message": str(e)}), 503

        try:
            project_cfg = _get_project_cfg(project_key)
        except Exception as e:
            return jsonify({"status": "error", "message": f"Project config error: {e}"}), 500

        if not project_cfg:
            return jsonify({"status": "error", "message": f"Project config not found for key '{project_key}'"}), 404

        services = _iter_project_services(project_cfg)
        if not services:
            return jsonify({"status": "error", "message": "No services with codeRepo defined for this project"}), 400

        repos_out: List[Dict[str, Any]] = []
        all_added_tickets: set[str] = set()
        total_added = 0
        warnings: List[str] = []

        for svc in services:
            repo = svc["codeRepo"]
            try:
                a_exists = _github_ref_exists(github_org, repo, ref_a, github_token)
                b_exists = _github_ref_exists(github_org, repo, ref_b, github_token)
            except Exception as e:
                warnings.append(f"{repo}: could not check refs ({e})")
                repos_out.append({
                    "repo": repo,
                    "releaseA": ref_a,
                    "releaseB": ref_b,
                    "refsAvailable": False,
                    "added": {"commitCount": 0, "tickets": [], "prs": [], "compareUrl": ""},
                })
                continue

            if not a_exists or not b_exists:
                warnings.append(
                    f"{repo}: refs not fully available (A={ref_a} exists={a_exists}, B={ref_b} exists={b_exists}). "
                    "Ensure both branches exist in this repo."
                )
                repos_out.append({
                    "repo": repo,
                    "releaseA": ref_a,
                    "releaseB": ref_b,
                    "refsAvailable": False,
                    "added": {"commitCount": 0, "tickets": [], "prs": [], "compareUrl": ""},
                })
                continue

            added_count = 0
            added_tickets: set[str] = set()
            added_prs: set[str] = set()
            compare_url_added = ""

            # Added: older → newer - compare older...newer (ref_a...ref_b)
            try:
                resp = _github_request("GET", github_org, repo, f"/compare/{ref_a}...{ref_b}", github_token)
                data = resp.json() or {}
                commits = data.get("commits") or []
                added_count = int(data.get("ahead_by") or 0)
                compare_url_added = data.get("html_url") or f"https://github.com/{github_org}/{repo}/compare/{ref_a}...{ref_b}"
                for c in commits:
                    commit = c.get("commit") or {}
                    msg = str(commit.get("message") or "")
                    for t in _extract_tickets(msg, ticket_regex):
                        added_tickets.add(t)
                        all_added_tickets.add(t)
                    for p in _extract_prs(msg):
                        added_prs.add(p)
            except Exception as e:
                warnings.append(f"{repo}: compare older...newer failed: {e}")

            total_added += added_count
            repos_out.append({
                "repo": repo,
                "releaseA": ref_a,
                "releaseB": ref_b,
                "refsAvailable": True,
                "added": {
                    "commitCount": added_count,
                    "tickets": sorted(added_tickets),
                    "prs": sorted(added_prs, key=lambda x: int(x.lstrip("#"))),
                    "compareUrl": compare_url_added,
                },
            })

        return jsonify({
            "status": "ok",
            "projectKey": project_key,
            "releaseRefA": ref_a,
            "releaseRefB": ref_b,
            "repos": repos_out,
            "summary": {
                "addedTickets": sorted(all_added_tickets),
                "totalAdded": total_added,
            },
            "warnings": warnings,
        })
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/runbooks/readiness", methods=["POST"])
def runbook_readiness():
    """Runbook #3: Release readiness / missing references validator (read-only).

    Validates that baseline/head refs exist for each repo and surfaces
    mismatches or missing refs.

    Request JSON:
      {
        "projectKey": "TCBP_MFES",
        "baselineRef": "release/2026.01.01",   # optional
        "headRef": "main"                     # optional
      }
    """
    body = request.get_json(silent=True) or {}
    project_key = str(body.get("projectKey") or "").strip()
    if not project_key:
        return jsonify({"status": "error", "message": "projectKey is required"}), 400

    try:
        github_org, github_token = _load_dotenv_and_github()
    except RuntimeError as e:
        return jsonify({"status": "error", "message": str(e)}), 503

    project_cfg = _get_project_cfg(project_key)
    if not project_cfg:
        return jsonify({"status": "error", "message": f"Project config not found for key '{project_key}'"}), 404

    default_branch = body.get("headRef") or "main"
    baseline_override = body.get("baselineRef") or None

    services = _iter_project_services(project_cfg)
    if not services:
        return jsonify({"status": "error", "message": "No services with codeRepo defined for this project"}), 400

    repos_out = []
    warnings: List[str] = []

    for svc in services:
        repo = svc["codeRepo"]
        # Load branching strategy (with per-repo override support)
        branching = _load_branching_strategy(project_cfg, repo)

        head_ref = body.get("headRef") or branching["defaultBranch"]
        baseline_ref = baseline_override

        if not baseline_ref:
            baseline_ref = _pick_release_branch(
                github_org,
                repo,
                github_token,
                branching["releaseBranchPatterns"],
                branching["releaseBranchPickStrategy"],
                branching.get("versionExtractionRegex"),
            )

        baseline_exists = bool(baseline_ref and _github_ref_exists(github_org, repo, baseline_ref, github_token))
        head_exists = bool(head_ref and _github_ref_exists(github_org, repo, head_ref, github_token))

        status = "ok"
        messages: List[str] = []

        if not baseline_ref:
            status = "warn"
            messages.append("No baseline release branch could be determined for this repo.")
        elif not baseline_exists:
            status = "warn"
            messages.append(f"Baseline ref '{baseline_ref}' does not exist in this repo.")

        if not head_exists:
            status = "warn"
            messages.append(f"Head ref '{head_ref}' does not exist in this repo.")

        if baseline_ref and head_exists and baseline_exists:
            # Optional: detect obvious pattern mismatches (e.g. FE/BE split)
            pat_list = branching["releaseBranchPatterns"]
            if pat_list:
                # Very simple heuristic: if repo name suggests FE/BE but pattern is generic, warn.
                if ("-fe" in repo.lower() or "frontend" in repo.lower()) and not any("FE" in p or "fe" in p for p in pat_list):
                    status = "warn"
                    messages.append("Repo looks like FE but releaseBranchPatterns do not contain FE-specific pattern.")

        repos_out.append(
            {
                "repo": repo,
                "baselineRef": baseline_ref or "",
                "headRef": head_ref,
                "baselineExists": baseline_exists,
                "headExists": head_exists,
                "status": status,
                "messages": messages,
            }
        )

    overall_status = "ok"
    if any(r["status"] != "ok" for r in repos_out):
        overall_status = "warn"

    return jsonify(
        {
            "status": overall_status,
            "projectKey": project_key,
            "baselineRef": baseline_override or "",
            "headRef": default_branch,
            "repos": repos_out,
            "warnings": warnings,
        }
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Snapshot API Server")
    parser.add_argument("--port", type=int, default=8001,
                        help="Port to listen on (default: 8001)")
    parser.add_argument("--interval", type=int, default=DEFAULT_INTERVAL_MINUTES,
                        help="Snapshot interval in minutes (default: 30)")
    parser.add_argument("--host", type=str, default="127.0.0.1",
                        help="Host to bind to (default: 127.0.0.1)")
    
    args = parser.parse_args()
    
    # Initialize scheduler
    scheduler = get_scheduler(interval_minutes=args.interval)
    scheduler.start()
    
    print(f"Starting snapshot API server on http://{args.host}:{args.port}")
    print(f"Snapshot interval: {args.interval} minutes")
    print("\nEndpoints:")
    print(f"  GET  http://{args.host}:{args.port}/api/snapshot/status")
    print(f"  POST http://{args.host}:{args.port}/api/snapshot/trigger")
    print(f"  GET  http://{args.host}:{args.port}/api/snapshot/progress")
    print(f"  GET  http://{args.host}:{args.port}/api/datadog/health")
    print(f"  GET  http://{args.host}:{args.port}/api/ticket/<key>")
    print(f"  POST http://{args.host}:{args.port}/api/runbooks/scope")
    print(f"  POST http://{args.host}:{args.port}/api/runbooks/drift")
    print(f"  POST http://{args.host}:{args.port}/api/runbooks/release-diff")
    print(f"  POST http://{args.host}:{args.port}/api/runbooks/readiness")
    print(f"  GET  http://{args.host}:{args.port}/health")
    print("\nPress Ctrl+C to stop")
    
    try:
        app.run(host=args.host, port=args.port, debug=False)
    except KeyboardInterrupt:
        print("\nStopping scheduler...")
        scheduler.stop()
