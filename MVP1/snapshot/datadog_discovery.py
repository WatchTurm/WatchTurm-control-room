"""
Datadog Discovery Module for MVP1.

Scans Datadog API to discover:
- kube_namespace values
- kube_cluster_name values
- service values (if present)
- kube_deployment values (if present)
- monitors list (name, id, status, tags/scope)
- Top services/deployments per namespace (frequency)

Outputs to data/datadog_inventory.json for use by mapping wizard.
"""

import json
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

# Import from snapshot.py
sys.path.insert(0, str(Path(__file__).parent))
from snapshot import (
    _api_request_with_retry,
    datadog_api_base,
    datadog_list_monitors,
    datadog_query_timeseries,
)


def discover_datadog_resources(
    api_key: str,
    app_key: str,
    site: str = "datadoghq.com",
    *,
    lookback_hours: int = 24,
) -> dict[str, Any]:
    """
    Discover all Datadog resources available for mapping.
    
    Returns a dictionary with:
    - namespaces: list of unique kube_namespace values
    - clusters: list of unique kube_cluster_name values
    - services: list of unique service values
    - deployments: list of unique kube_deployment values
    - monitors: list of monitor objects
    - namespaceStats: dict mapping namespace -> {services: [], deployments: [], counts: {}}
    """
    base = datadog_api_base(site)
    headers = {
        "DD-API-KEY": api_key,
        "DD-APPLICATION-KEY": app_key,
    }
    
    inventory: dict[str, Any] = {
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
        }),
    }
    
    print("🔍 Discovering Datadog resources...")
    
    # 1. Discover namespaces and clusters via metrics metadata
    print("  → Querying metrics metadata for Kubernetes tags...")
    try:
        # Query a common Kubernetes metric to get tag metadata
        # We'll query multiple metrics to get comprehensive tag coverage
        metric_queries = [
            "kubernetes.pods.running",
            "kubernetes.containers.running",
            "kubernetes.cpu.usage.total",
            "trace.http.request.hits",
        ]
        
        for metric_name in metric_queries:
            try:
                # Use metrics metadata API to get available tags
                url = f"{base}/api/v1/metrics/{metric_name}"
                r = _api_request_with_retry("GET", url, headers=headers, timeout=30.0)
                if r.status_code == 200:
                    data = r.json()
                    if isinstance(data, dict):
                        # Extract tag values from metadata
                        tags = data.get("tags", [])
                        for tag in tags:
                            if tag.startswith("kube_namespace:"):
                                ns = tag.split(":", 1)[1].strip()
                                if ns:
                                    inventory["namespaces"].add(ns)
                            elif tag.startswith("kube_cluster_name:"):
                                cluster = tag.split(":", 1)[1].strip()
                                if cluster:
                                    inventory["clusters"].add(cluster)
            except Exception as e:
                # Continue if one metric fails
                print(f"    ⚠️  Could not query {metric_name}: {e}")
                continue
    except Exception as e:
        print(f"  ⚠️  Error discovering namespaces/clusters: {e}")
    
    # 2. Query actual timeseries to discover tags from real data
    print("  → Querying timeseries data for tag values...")
    try:
        # Query recent data to get actual tag values
        now = int(datetime.now(tz=timezone.utc).timestamp())
        from_ts = now - (lookback_hours * 3600)
        
        # Query common metrics that have rich tagging
        discovery_queries = [
            "kubernetes.pods.running",
            "kubernetes.containers.running",
            "trace.http.request.hits",
            "trace.http.request.errors",
        ]
        
        for query_template in discovery_queries:
            try:
                # Query without filters to get all available series
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
                        for tag in tags:
                            if tag.startswith("kube_namespace:"):
                                ns = tag.split(":", 1)[1].strip()
                                if ns:
                                    inventory["namespaces"].add(ns)
                                    # Track services/deployments per namespace
                                    for t in tags:
                                        if t.startswith("service:"):
                                            svc = t.split(":", 1)[1].strip()
                                            if svc:
                                                inventory["services"].add(svc)
                                                inventory["namespaceStats"][ns]["services"][svc] += 1
                                        elif t.startswith("kube_deployment:"):
                                            dep = t.split(":", 1)[1].strip()
                                            if dep:
                                                inventory["deployments"].add(dep)
                                                inventory["namespaceStats"][ns]["deployments"][dep] += 1
                            elif tag.startswith("kube_cluster_name:"):
                                cluster = tag.split(":", 1)[1].strip()
                                if cluster:
                                    inventory["clusters"].add(cluster)
                            elif tag.startswith("service:") and ":" in tag:
                                svc = tag.split(":", 1)[1].strip()
                                if svc:
                                    inventory["services"].add(svc)
                            elif tag.startswith("kube_deployment:") and ":" in tag:
                                dep = tag.split(":", 1)[1].strip()
                                if dep:
                                    inventory["deployments"].add(dep)
            except Exception as e:
                print(f"    ⚠️  Could not query {query_template}: {e}")
                continue
    except Exception as e:
        print(f"  ⚠️  Error querying timeseries: {e}")
    
    # 3. Discover monitors
    print("  → Fetching monitors...")
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
        print(f"  ⚠️  Error fetching monitors: {e}")
    
    # Convert sets to sorted lists for JSON serialization
    inventory["namespaces"] = sorted(list(inventory["namespaces"]))
    inventory["clusters"] = sorted(list(inventory["clusters"]))
    inventory["services"] = sorted(list(inventory["services"]))
    inventory["deployments"] = sorted(list(inventory["deployments"]))
    
    # Convert namespaceStats to regular dict with sorted lists
    ns_stats = {}
    for ns, stats in inventory["namespaceStats"].items():
        ns_stats[ns] = {
            "services": [{"name": k, "count": v} for k, v in stats["services"].most_common(20)],
            "deployments": [{"name": k, "count": v} for k, v in stats["deployments"].most_common(20)],
            "serviceCount": len(stats["services"]),
            "deploymentCount": len(stats["deployments"]),
        }
    inventory["namespaceStats"] = ns_stats
    
    print(f"  ✅ Discovered: {len(inventory['namespaces'])} namespaces, {len(inventory['clusters'])} clusters, {len(inventory['services'])} services, {len(inventory['deployments'])} deployments, {len(inventory['monitors'])} monitors")
    
    return inventory


def save_inventory(inventory: dict[str, Any], output_path: Path | str) -> None:
    """Save discovered inventory to JSON file."""
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(inventory, f, indent=2, ensure_ascii=False)
    print(f"💾 Saved inventory to {path}")


def load_inventory(inventory_path: Path | str) -> dict[str, Any] | None:
    """Load previously discovered inventory."""
    path = Path(inventory_path)
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"⚠️  Could not load inventory: {e}")
        return None


if __name__ == "__main__":
    # CLI entry point for discovery only
    import os
    
    api_key = os.getenv("DATADOG_API_KEY") or os.getenv("DD_API_KEY", "")
    app_key = os.getenv("DATADOG_APP_KEY") or os.getenv("DD_APP_KEY") or os.getenv("DATADOG_APPLICATION_KEY") or os.getenv("DD_APPLICATION_KEY", "")
    site = os.getenv("DATADOG_SITE") or os.getenv("DD_SITE", "datadoghq.com")
    
    if not api_key or not app_key:
        print("❌ Error: DATADOG_API_KEY and DATADOG_APP_KEY (or DD_API_KEY/DD_APP_KEY) must be set")
        sys.exit(1)
    
    # Discover resources
    inventory = discover_datadog_resources(api_key, app_key, site)
    
    # Save to data/datadog_inventory.json
    data_dir = Path(__file__).parent.parent.parent / "data"
    output_path = data_dir / "datadog_inventory.json"
    save_inventory(inventory, output_path)
