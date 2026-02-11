"""
Unified Integration Discovery Module.

Discovers resources from all integrations (Datadog, TeamCity, GitHub, Jira)
to enable automated onboarding without manual configuration.

This module provides discovery functions that scan each integration's API
and return structured data that can be used by the onboarding wizard.
"""

import json
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests
import yaml

# Import from snapshot.py
sys.path.insert(0, str(Path(__file__).parent))
try:
    from snapshot import (
        _api_request_with_retry,
        datadog_api_base,
        datadog_list_monitors,
        logger,
    )
except ImportError:
    # Fallback for direct execution
    from snapshot import (
        _api_request_with_retry,
        datadog_api_base,
        datadog_list_monitors,
    )
    # Simple logger fallback
    class SimpleLogger:
        def info(self, *args, **kwargs): print(f"[INFO] {args}")
        def warn(self, *args, **kwargs): print(f"[WARN] {args}")
        def error(self, *args, **kwargs): print(f"[ERROR] {args}")
    logger = SimpleLogger()


# ============================================================================
# Datadog Discovery (Enhanced)
# ============================================================================

def discover_datadog_resources(
    api_key: str,
    app_key: str,
    site: str = "datadoghq.com",
    *,
    lookback_hours: int = 24,
) -> Dict[str, Any]:
    """
    Discover all Datadog resources with enhanced coverage.
    
    Returns comprehensive inventory including:
    - namespaces, clusters, services, deployments
    - monitors with tags
    - namespace statistics (services/deployments per namespace)
    - Pod counts per namespace (to verify queries work)
    """
    base = datadog_api_base(site)
    headers = {
        "DD-API-KEY": api_key,
        "DD-APPLICATION-KEY": app_key,
    }
    
    inventory: Dict[str, Any] = {
        "discoveredAt": datetime.now(tz=timezone.utc).isoformat(),
        "site": site,
        "namespaces": set(),
        "clusters": set(),
        "services": set(),
        "deployments": set(),
        "monitors": [],
        "namespaceStats": defaultdict(lambda: {
            "services": Counter(),
            "deployments": Counter(),
            "serviceCount": 0,
            "deploymentCount": 0,
            "podCount": 0,  # NEW: Track pod counts
        }),
    }
    
    logger.info("datadog_discovery_started", site=site)
    
    # 1. Query timeseries to discover tags from real data
    logger.info("datadog_discovery_querying_timeseries")
    try:
        now = int(datetime.now(tz=timezone.utc).timestamp())
        from_ts = now - (lookback_hours * 3600)
        
        discovery_queries = [
            "kubernetes.pods.running",
            "kubernetes.containers.running",
            "trace.http.request.hits",
            "trace.http.request.errors",
        ]
        
        for query_template in discovery_queries:
            try:
                query = f"avg:{query_template}{{*}}"
                url = f"{base}/api/v1/query"
                params = {
                    "from": from_ts,
                    "to": now,
                    "query": query,
                }
                r = _api_request_with_retry("GET", url, headers=headers, params=params, timeout=30.0)
                if r.status_code == 200:
                    data = r.json()
                    series = data.get("series", [])
                    for s in series:
                        tags = s.get("tag_set", []) or []
                        namespace = None
                        
                        for tag in tags:
                            if tag.startswith("kube_namespace:"):
                                ns = tag.split(":", 1)[1].strip()
                                if ns:
                                    inventory["namespaces"].add(ns)
                                    namespace = ns
                                    # Track pod counts per namespace
                                    if query_template == "kubernetes.pods.running":
                                        # Get latest value from series
                                        pointlist = s.get("pointlist", [])
                                        if pointlist:
                                            # Sum all non-null values (approximate pod count)
                                            pod_values = [p[1] for p in pointlist if len(p) > 1 and p[1] is not None]
                                            if pod_values:
                                                max_pods = max(pod_values)
                                                inventory["namespaceStats"][ns]["podCount"] = max(int(max_pods), inventory["namespaceStats"][ns]["podCount"])
                            
                            elif tag.startswith("kube_cluster_name:"):
                                cluster = tag.split(":", 1)[1].strip()
                                if cluster:
                                    inventory["clusters"].add(cluster)
                            
                            elif tag.startswith("service:") and ":" in tag:
                                svc = tag.split(":", 1)[1].strip()
                                if svc:
                                    inventory["services"].add(svc)
                                    if namespace:
                                        inventory["namespaceStats"][namespace]["services"][svc] += 1
                            
                            elif tag.startswith("kube_deployment:") and ":" in tag:
                                dep = tag.split(":", 1)[1].strip()
                                if dep:
                                    inventory["deployments"].add(dep)
                                    if namespace:
                                        inventory["namespaceStats"][namespace]["deployments"][dep] += 1
            except Exception as e:
                logger.warn("datadog_query_failed", query=query_template, error=str(e))
                continue
    except Exception as e:
        logger.warn("datadog_timeseries_discovery_failed", error=str(e))
    
    # 2. Discover monitors
    logger.info("datadog_discovery_fetching_monitors")
    try:
        monitors = datadog_list_monitors(api_key, app_key, site=site)
        for m in monitors:
            monitor_info = {
                "id": m.get("id"),
                "name": m.get("name", ""),
                "status": m.get("overall_state", "").upper(),
                "type": m.get("type", ""),
                "tags": m.get("tags", []),
                "query": m.get("query", ""),
            }
            inventory["monitors"].append(monitor_info)
    except Exception as e:
        logger.warn("datadog_monitor_discovery_failed", error=str(e))
    
    # Convert sets to sorted lists
    inventory["namespaces"] = sorted(list(inventory["namespaces"]))
    inventory["clusters"] = sorted(list(inventory["clusters"]))
    inventory["services"] = sorted(list(inventory["services"]))
    inventory["deployments"] = sorted(list(inventory["deployments"]))
    
    # Convert namespaceStats to regular dict
    ns_stats = {}
    for ns, stats in inventory["namespaceStats"].items():
        ns_stats[ns] = {
            "services": [{"name": k, "count": v} for k, v in stats["services"].most_common(20)],
            "deployments": [{"name": k, "count": v} for k, v in stats["deployments"].most_common(20)],
            "serviceCount": len(stats["services"]),
            "deploymentCount": len(stats["deployments"]),
            "podCount": stats["podCount"],
        }
    inventory["namespaceStats"] = ns_stats
    
    logger.info("datadog_discovery_complete",
                namespaces=len(inventory["namespaces"]),
                clusters=len(inventory["clusters"]),
                services=len(inventory["services"]),
                deployments=len(inventory["deployments"]),
                monitors=len(inventory["monitors"]))
    
    return inventory


# ============================================================================
# TeamCity Discovery
# ============================================================================

def discover_teamcity_build_types(
    base_url: str,
    token: str,
) -> Dict[str, Any]:
    """
    Discover all TeamCity build types.
    
    Returns:
    - buildTypes: list of build type objects with id, name, projectId, etc.
    - projects: list of project objects
    - buildTypeStats: frequency stats for build type naming patterns
    """
    inventory: Dict[str, Any] = {
        "discoveredAt": datetime.now(tz=timezone.utc).isoformat(),
        "baseUrl": base_url,
        "buildTypes": [],
        "projects": [],
        "buildTypeStats": {
            "byProject": defaultdict(list),
            "namingPatterns": Counter(),
        },
    }
    
    logger.info("teamcity_discovery_started", base_url=base_url)
    
    try:
        # 1. List all projects
        projects_url = f"{base_url.rstrip('/')}/app/rest/projects"
        headers = {"Authorization": f"Bearer {token}"}
        
        r = _api_request_with_retry("GET", projects_url, headers=headers, timeout=30.0)
        if r.status_code == 200:
            projects_data = r.json()
            projects = projects_data.get("project", []) if isinstance(projects_data, dict) else []
            
            for proj in projects:
                project_info = {
                    "id": proj.get("id", ""),
                    "name": proj.get("name", ""),
                    "parentProjectId": proj.get("parentProjectId"),
                }
                inventory["projects"].append(project_info)
        
        # 2. List all build types
        build_types_url = f"{base_url.rstrip('/')}/app/rest/buildTypes"
        r = _api_request_with_retry("GET", build_types_url, headers=headers, timeout=30.0)
        if r.status_code == 200:
            build_types_data = r.json()
            build_types = build_types_data.get("buildType", []) if isinstance(build_types_data, dict) else []
            
            for bt in build_types:
                build_type_info = {
                    "id": bt.get("id", ""),
                    "name": bt.get("name", ""),
                    "projectId": bt.get("projectId", ""),
                    "projectName": bt.get("projectName", ""),
                    "href": bt.get("href", ""),
                }
                inventory["buildTypes"].append(build_type_info)
                
                # Track by project
                project_id = build_type_info["projectId"]
                if project_id:
                    inventory["buildTypeStats"]["byProject"][project_id].append(build_type_info["id"])
                
                # Analyze naming patterns
                name = build_type_info["name"].lower()
                # Common patterns: "DockerBuildAndPush", "Build", "Deploy", etc.
                if "docker" in name and "build" in name:
                    inventory["buildTypeStats"]["namingPatterns"]["docker_build"] += 1
                elif "build" in name:
                    inventory["buildTypeStats"]["namingPatterns"]["build"] += 1
                elif "deploy" in name:
                    inventory["buildTypeStats"]["namingPatterns"]["deploy"] += 1
        
        logger.info("teamcity_discovery_complete",
                    projects=len(inventory["projects"]),
                    buildTypes=len(inventory["buildTypes"]))
        
    except Exception as e:
        logger.error("teamcity_discovery_failed", error=str(e))
        inventory["error"] = str(e)
    
    return inventory


# ============================================================================
# GitHub Discovery
# ============================================================================

def discover_github_repositories(
    org: str,
    token: str,
    *,
    max_repos: int = 200,
) -> Dict[str, Any]:
    """
    Discover all repositories in a GitHub organization.
    
    Returns:
    - repositories: list of repo objects with name, full_name, default_branch, etc.
    - repoStats: statistics about repo naming patterns
    """
    inventory: Dict[str, Any] = {
        "discoveredAt": datetime.now(tz=timezone.utc).isoformat(),
        "org": org,
        "repositories": [],
        "repoStats": {
            "total": 0,
            "byType": Counter(),  # frontend, backend, infra, etc.
            "namingPatterns": Counter(),
        },
    }
    
    logger.info("github_discovery_started", org=org)
    
    try:
        headers = {
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github.v3+json",
        }
        
        url = f"https://api.github.com/orgs/{org}/repos"
        page = 1
        per_page = 100
        
        while len(inventory["repositories"]) < max_repos:
            params = {
                "page": page,
                "per_page": per_page,
                "sort": "updated",
                "direction": "desc",
            }
            
            r = _api_request_with_retry("GET", url, headers=headers, params=params, timeout=30.0)
            if r.status_code != 200:
                logger.warn("github_api_error", status=r.status_code)
                break
            
            repos = r.json()
            if not repos or len(repos) == 0:
                break
            
            for repo in repos:
                repo_info = {
                    "name": repo.get("name", ""),
                    "full_name": repo.get("full_name", ""),
                    "default_branch": repo.get("default_branch", "main"),
                    "private": repo.get("private", False),
                    "archived": repo.get("archived", False),
                    "language": repo.get("language", ""),
                    "topics": repo.get("topics", []),
                    "description": repo.get("description", ""),
                }
                inventory["repositories"].append(repo_info)
                
                # Analyze naming patterns
                name_lower = repo_info["name"].lower()
                if "-infra" in name_lower or "infra" in name_lower:
                    inventory["repoStats"]["byType"]["infra"] += 1
                elif "-frontend" in name_lower or "-web" in name_lower or "-ui" in name_lower:
                    inventory["repoStats"]["byType"]["frontend"] += 1
                elif "-api" in name_lower or "-service" in name_lower or "-backend" in name_lower:
                    inventory["repoStats"]["byType"]["backend"] += 1
                else:
                    inventory["repoStats"]["byType"]["other"] += 1
            
            if len(repos) < per_page:
                break
            page += 1
        
        inventory["repoStats"]["total"] = len(inventory["repositories"])
        
        logger.info("github_discovery_complete", repos=len(inventory["repositories"]))
        
    except Exception as e:
        logger.error("github_discovery_failed", error=str(e))
        inventory["error"] = str(e)
    
    return inventory


# ============================================================================
# Jira Discovery
# ============================================================================

def discover_jira_projects(
    base_url: str,
    email: str,
    token: str,
) -> Dict[str, Any]:
    """
    Discover all Jira projects.
    
    Returns:
    - projects: list of project objects with key, name, projectType, etc.
    - projectStats: statistics about project keys and naming
    """
    inventory: Dict[str, Any] = {
        "discoveredAt": datetime.now(tz=timezone.utc).isoformat(),
        "baseUrl": base_url,
        "projects": [],
        "projectStats": {
            "total": 0,
            "keys": [],
        },
    }
    
    logger.info("jira_discovery_started", base_url=base_url)
    
    try:
        url = f"{base_url.rstrip('/')}/rest/api/3/project"
        auth = (email, token)
        headers = {"Accept": "application/json"}
        
        r = _api_request_with_retry("GET", url, headers=headers, auth=auth, timeout=30.0)
        if r.status_code == 200:
            projects = r.json()
            
            for proj in projects:
                project_info = {
                    "key": proj.get("key", ""),
                    "name": proj.get("name", ""),
                    "projectTypeKey": proj.get("projectTypeKey", ""),
                    "simplified": proj.get("simplified", False),
                }
                inventory["projects"].append(project_info)
                inventory["projectStats"]["keys"].append(project_info["key"])
            
            inventory["projectStats"]["total"] = len(inventory["projects"])
            
            logger.info("jira_discovery_complete", projects=len(inventory["projects"]))
        else:
            logger.warn("jira_api_error", status=r.status_code)
            inventory["error"] = f"HTTP {r.status_code}"
        
    except Exception as e:
        logger.error("jira_discovery_failed", error=str(e))
        inventory["error"] = str(e)
    
    return inventory


# ============================================================================
# Unified Discovery Runner
# ============================================================================

def discover_all_integrations(
    datadog_api_key: Optional[str] = None,
    datadog_app_key: Optional[str] = None,
    datadog_site: str = "datadoghq.com",
    teamcity_base_url: Optional[str] = None,
    teamcity_token: Optional[str] = None,
    github_org: Optional[str] = None,
    github_token: Optional[str] = None,
    jira_base_url: Optional[str] = None,
    jira_email: Optional[str] = None,
    jira_token: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Run discovery for all configured integrations.
    
    Returns unified inventory with all discovered resources.
    """
    inventory: Dict[str, Any] = {
        "discoveredAt": datetime.now(tz=timezone.utc).isoformat(),
        "datadog": None,
        "teamcity": None,
        "github": None,
        "jira": None,
    }
    
    # Discover Datadog
    if datadog_api_key and datadog_app_key:
        try:
            inventory["datadog"] = discover_datadog_resources(
                datadog_api_key, datadog_app_key, site=datadog_site
            )
        except Exception as e:
            logger.error("datadog_discovery_exception", error=str(e))
            inventory["datadog"] = {"error": str(e)}
    else:
        logger.info("datadog_discovery_skipped", reason="missing_credentials")
    
    # Discover TeamCity
    if teamcity_base_url and teamcity_token:
        try:
            inventory["teamcity"] = discover_teamcity_build_types(
                teamcity_base_url, teamcity_token
            )
        except Exception as e:
            logger.error("teamcity_discovery_exception", error=str(e))
            inventory["teamcity"] = {"error": str(e)}
    else:
        logger.info("teamcity_discovery_skipped", reason="missing_credentials")
    
    # Discover GitHub
    if github_org and github_token:
        try:
            inventory["github"] = discover_github_repositories(
                github_org, github_token
            )
        except Exception as e:
            logger.error("github_discovery_exception", error=str(e))
            inventory["github"] = {"error": str(e)}
    else:
        logger.info("github_discovery_skipped", reason="missing_credentials")
    
    # Discover Jira
    if jira_base_url and jira_email and jira_token:
        try:
            inventory["jira"] = discover_jira_projects(
                jira_base_url, jira_email, jira_token
            )
        except Exception as e:
            logger.error("jira_discovery_exception", error=str(e))
            inventory["jira"] = {"error": str(e)}
    else:
        logger.info("jira_discovery_skipped", reason="missing_credentials")
    
    return inventory


def save_discovery_inventory(inventory: Dict[str, Any], output_path: Path | str) -> None:
    """Save discovery inventory to JSON file."""
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(inventory, f, indent=2, ensure_ascii=False)
    logger.info("discovery_inventory_saved", path=str(path))


def load_discovery_inventory(inventory_path: Path | str) -> Dict[str, Any] | None:
    """Load previously discovered inventory."""
    path = Path(inventory_path)
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.warn("discovery_inventory_load_failed", path=str(path), error=str(e))
        return None


if __name__ == "__main__":
    # CLI entry point for discovery
    import os
    
    # Load from environment
    dd_api_key = os.getenv("DATADOG_API_KEY") or os.getenv("DD_API_KEY", "")
    dd_app_key = os.getenv("DATADOG_APP_KEY") or os.getenv("DD_APP_KEY") or os.getenv("DATADOG_APPLICATION_KEY") or os.getenv("DD_APPLICATION_KEY", "")
    dd_site = os.getenv("DATADOG_SITE") or os.getenv("DD_SITE", "datadoghq.com")
    
    tc_url = os.getenv("TEAMCITY_BASE_URL", "")
    tc_token = os.getenv("TEAMCITY_TOKEN", "")
    
    gh_org = os.getenv("GITHUB_ORG", "")
    gh_token = os.getenv("GITHUB_TOKEN", "")
    
    jira_url = os.getenv("JIRA_BASE_URL", "")
    jira_email = os.getenv("JIRA_EMAIL", "")
    jira_token = os.getenv("JIRA_TOKEN", "")
    
    # Run discovery
    inventory = discover_all_integrations(
        datadog_api_key=dd_api_key if dd_api_key else None,
        datadog_app_key=dd_app_key if dd_app_key else None,
        datadog_site=dd_site,
        teamcity_base_url=tc_url if tc_url else None,
        teamcity_token=tc_token if tc_token else None,
        github_org=gh_org if gh_org else None,
        github_token=gh_token if gh_token else None,
        jira_base_url=jira_url if jira_url else None,
        jira_email=jira_email if jira_email else None,
        jira_token=jira_token if jira_token else None,
    )
    
    # Save to data/integration_inventory.json
    data_dir = Path(__file__).parent.parent.parent / "data"
    output_path = data_dir / "integration_inventory.json"
    save_discovery_inventory(inventory, output_path)
    
    print(f"\n✅ Discovery complete! Inventory saved to {output_path}")
