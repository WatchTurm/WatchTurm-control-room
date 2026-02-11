"""
Datadog Mapping Wizard for MVP1.

Interactive CLI wizard that:
1. Loads project configs (YAML)
2. Loads Datadog inventory (from datadog_discovery.py)
3. Proposes candidate selectors for each project+environment with confidence scores
4. Proposes component/service mappings
5. Allows user to confirm/choose interactively
6. Writes results back to YAML configs

Usage:
    python snapshot.py --dd-map
    or
    python datadog_mapping_wizard.py
"""

import json
import os
import sys
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

import yaml

# Import from snapshot.py
sys.path.insert(0, str(Path(__file__).parent))
from snapshot import load_project_configs


def similarity_score(a: str, b: str) -> float:
    """Calculate string similarity score (0.0 to 1.0)."""
    if not a or not b:
        return 0.0
    a_lower = a.lower().strip()
    b_lower = b.lower().strip()
    if a_lower == b_lower:
        return 1.0
    return SequenceMatcher(None, a_lower, b_lower).ratio()


def normalize_env_name(env_key: str, env_name: str) -> list[str]:
    """Generate normalized candidate names for environment matching."""
    candidates = []
    # Use both key and name
    for source in [env_key, env_name]:
        if not source:
            continue
        source = source.lower().strip()
        candidates.append(source)
        # Common variations
        candidates.append(source.replace("-", "_"))
        candidates.append(source.replace("_", "-"))
        # Remove common prefixes/suffixes
        for prefix in ["env-", "env_", "kube-", "kube_"]:
            if source.startswith(prefix):
                candidates.append(source[len(prefix):])
    return list(set(candidates))


def propose_env_selector(
    project_key: str,
    env_key: str,
    env_name: str,
    inventory: dict[str, Any],
) -> list[dict[str, Any]]:
    """
    Propose Datadog environment selectors with confidence scores.
    
    Returns list of candidates sorted by confidence (highest first).
    Each candidate has:
    - selector: dict with namespace/cluster/service tags
    - confidence: float 0.0-1.0
    - reason: str explaining the match
    """
    candidates = []
    
    # Normalize environment names for matching
    env_candidates = normalize_env_name(env_key, env_name)
    
    namespaces = inventory.get("namespaces", [])
    clusters = inventory.get("clusters", [])
    
    # Strategy 1: Direct namespace match
    for ns in namespaces:
        ns_lower = ns.lower()
        best_match_score = 0.0
        best_env_candidate = ""
        
        for env_candidate in env_candidates:
            score = similarity_score(ns_lower, env_candidate)
            if score > best_match_score:
                best_match_score = score
                best_env_candidate = env_candidate
        
        if best_match_score > 0.3:  # Threshold for consideration
            # Check if namespace contains project hint
            project_hint_score = 0.0
            project_lower = project_key.lower()
            if project_lower in ns_lower:
                project_hint_score = 0.2
            
            confidence = min(0.95, best_match_score + project_hint_score)
            
            selector = {
                "namespace": ns,
            }
            
            # Try to find a matching cluster (optional)
            for cluster in clusters:
                cluster_lower = cluster.lower()
                if project_lower in cluster_lower or env_candidate in cluster_lower:
                    selector["cluster"] = cluster
                    confidence += 0.05
                    break
            
            candidates.append({
                "selector": selector,
                "confidence": confidence,
                "reason": f"Namespace '{ns}' matches environment '{env_key}' (similarity: {best_match_score:.2f})",
            })
    
    # Strategy 2: Project + environment pattern (e.g., "po1-qa", "tap2-dev")
    project_lower = project_key.lower().replace("_", "-").replace(".", "-")
    for env_candidate in env_candidates:
        # Try patterns like "po1-qa", "tap2-dev"
        pattern1 = f"{project_lower}-{env_candidate}"
        pattern2 = f"{project_lower}_{env_candidate}"
        
        for ns in namespaces:
            ns_lower = ns.lower()
            if pattern1 in ns_lower or pattern2 in ns_lower:
                score = similarity_score(ns_lower, pattern1)
                if score > 0.5:
                    selector = {"namespace": ns}
                    # Try cluster match
                    for cluster in clusters:
                        cluster_lower = cluster.lower()
                        if project_lower in cluster_lower:
                            selector["cluster"] = cluster
                            break
                    
                    candidates.append({
                        "selector": selector,
                        "confidence": min(0.9, score + 0.1),
                        "reason": f"Namespace '{ns}' matches pattern '{pattern1}'",
                    })
    
    # Strategy 3: Check namespace stats for project-related services
    namespace_stats = inventory.get("namespaceStats", {})
    for ns, stats in namespace_stats.items():
        ns_lower = ns.lower()
        # If namespace has many services, it might be a good candidate
        service_count = stats.get("serviceCount", 0)
        if service_count > 0:
            # Check if any env candidate matches
            for env_candidate in env_candidates:
                score = similarity_score(ns_lower, env_candidate)
                if score > 0.4:
                    selector = {"namespace": ns}
                    candidates.append({
                        "selector": selector,
                        "confidence": min(0.7, score + (0.1 if service_count > 5 else 0)),
                        "reason": f"Namespace '{ns}' matches '{env_candidate}' and has {service_count} services",
                    })
    
    # Strategy 4: Use monitor tags to boost confidence
    # Monitors tagged with namespace/env can provide additional signal
    monitors = inventory.get("monitors", [])
    project_lower = project_key.lower()
    for monitor in monitors:
        monitor_tags = monitor.get("tags", [])
        monitor_name = (monitor.get("name") or "").lower()
        
        # Check if monitor has namespace tag matching our candidates
        for tag in monitor_tags:
            if isinstance(tag, str) and tag.startswith("kube_namespace:"):
                ns = tag.split(":", 1)[1].strip().lower()
                for env_candidate in env_candidates:
                    if ns == env_candidate or env_candidate in ns:
                        # Check if monitor name/query suggests this project
                        project_match = project_lower in monitor_name or project_lower in (monitor.get("query") or "").lower()
                        if project_match:
                            # Boost confidence for existing candidates with this namespace
                            for c in candidates:
                                if c["selector"].get("namespace", "").lower() == ns:
                                    c["confidence"] = min(0.95, c["confidence"] + 0.1)
                                    c["reason"] += f" (monitor '{monitor.get('name', '')}' confirms)"
                                    break
    
    # Sort by confidence (highest first)
    candidates.sort(key=lambda x: x["confidence"], reverse=True)
    
    # Remove duplicates (same selector)
    seen_selectors = set()
    unique_candidates = []
    for c in candidates:
        selector_key = json.dumps(c["selector"], sort_keys=True)
        if selector_key not in seen_selectors:
            seen_selectors.add(selector_key)
            unique_candidates.append(c)
    
    return unique_candidates[:5]  # Top 5 candidates


def propose_component_selector(
    component_key: str,
    code_repo: str,
    namespace: str,
    inventory: dict[str, Any],
) -> list[dict[str, Any]]:
    """
    Propose Datadog component/service selectors for a given component.
    
    Returns list of candidates sorted by confidence.
    """
    candidates = []
    
    # Normalize component names
    component_lower = component_key.lower().replace("_", "-")
    repo_lower = code_repo.lower().replace("_", "-")
    
    # Get services and deployments from namespace stats
    namespace_stats = inventory.get("namespaceStats", {})
    ns_stats = namespace_stats.get(namespace, {})
    
    services = ns_stats.get("services", [])
    deployments = ns_stats.get("deployments", [])
    
    # Collect monitor signals for this namespace/component
    monitors = inventory.get("monitors", [])
    monitor_service_signals = {}  # service_name -> count of matching monitors
    monitor_deployment_signals = {}  # deployment_name -> count of matching monitors
    
    for monitor in monitors:
        monitor_tags = monitor.get("tags", [])
        monitor_name = (monitor.get("name") or "").lower()
        monitor_query = (monitor.get("query") or "").lower()
        
        # Check if monitor is scoped to this namespace
        has_namespace = any(
            isinstance(tag, str) and tag.startswith(f"kube_namespace:{namespace}")
            for tag in monitor_tags
        )
        
        if has_namespace:
            # Check if monitor mentions component/repo in name or query
            component_mentioned = (
                component_lower in monitor_name or
                component_lower in monitor_query or
                repo_lower in monitor_name or
                repo_lower in monitor_query
            )
            
            if component_mentioned:
                # Extract service/deployment from monitor tags
                for tag in monitor_tags:
                    if isinstance(tag, str):
                        if tag.startswith("service:"):
                            svc = tag.split(":", 1)[1].strip()
                            monitor_service_signals[svc] = monitor_service_signals.get(svc, 0) + 1
                        elif tag.startswith("kube_deployment:"):
                            dep = tag.split(":", 1)[1].strip()
                            monitor_deployment_signals[dep] = monitor_deployment_signals.get(dep, 0) + 1
    
    # Match against services
    for svc_info in services:
        svc_name = svc_info.get("name", "")
        svc_lower = svc_name.lower()
        
        # Check similarity with component key and repo
        score1 = similarity_score(svc_lower, component_lower)
        score2 = similarity_score(svc_lower, repo_lower)
        best_score = max(score1, score2)
        
        if best_score > 0.4:
            # Boost confidence if monitors reference this service
            monitor_boost = 0.1 * min(3, monitor_service_signals.get(svc_name, 0))
            confidence = min(0.95, best_score + monitor_boost)
            
            reason = f"Service '{svc_name}' matches component '{component_key}' (similarity: {best_score:.2f})"
            if monitor_service_signals.get(svc_name, 0) > 0:
                reason += f" (confirmed by {monitor_service_signals[svc_name]} monitor(s))"
            
            candidates.append({
                "selector": {"service": svc_name},
                "confidence": confidence,
                "reason": reason,
            })
    
    # Match against deployments
    for dep_info in deployments:
        dep_name = dep_info.get("name", "")
        dep_lower = dep_name.lower()
        
        score1 = similarity_score(dep_lower, component_lower)
        score2 = similarity_score(dep_lower, repo_lower)
        best_score = max(score1, score2)
        
        if best_score > 0.4:
            # Boost confidence if monitors reference this deployment
            monitor_boost = 0.1 * min(3, monitor_deployment_signals.get(dep_name, 0))
            confidence = min(0.95, best_score + monitor_boost)
            
            reason = f"Deployment '{dep_name}' matches component '{component_key}' (similarity: {best_score:.2f})"
            if monitor_deployment_signals.get(dep_name, 0) > 0:
                reason += f" (confirmed by {monitor_deployment_signals[dep_name]} monitor(s))"
            
            candidates.append({
                "selector": {"kube_deployment": dep_name},
                "confidence": confidence,
                "reason": reason,
            })
    
    # Sort by confidence
    candidates.sort(key=lambda x: x["confidence"], reverse=True)
    
    # Remove duplicates
    seen = set()
    unique = []
    for c in candidates:
        key = json.dumps(c["selector"], sort_keys=True)
        if key not in seen:
            seen.add(key)
            unique.append(c)
    
    return unique[:5]


def format_selector(selector: dict[str, Any]) -> str:
    """Format selector dict as Datadog tag string."""
    parts = []
    if "namespace" in selector:
        parts.append(f"kube_namespace:{selector['namespace']}")
    if "cluster" in selector:
        parts.append(f"kube_cluster_name:{selector['cluster']}")
    if "service" in selector:
        parts.append(f"service:{selector['service']}")
    if "kube_deployment" in selector:
        parts.append(f"kube_deployment:{selector['kube_deployment']}")
    return ", ".join(parts) if parts else "(empty)"


def interactive_choose(
    prompt: str,
    options: list[dict[str, Any]],
    default_index: int = 0,
) -> dict[str, Any] | None:
    """Interactive selection from options."""
    if not options:
        return None
    
    print(f"\n{prompt}")
    for i, opt in enumerate(options):
        marker = "→" if i == default_index else " "
        confidence_bar = "█" * int(opt["confidence"] * 10)
        print(f"  {marker} [{i+1}] {confidence_bar} ({opt['confidence']:.0%}) - {opt['reason']}")
        if "selector" in opt:
            print(f"      Selector: {format_selector(opt['selector'])}")
    
    print(f"  [s] Skip this mapping")
    print(f"  [q] Quit wizard")
    
    while True:
        try:
            choice = input("\nYour choice: ").strip().lower()
            if choice == "q":
                return None  # Signal to quit
            if choice == "s":
                return {"skip": True}
            idx = int(choice) - 1
            if 0 <= idx < len(options):
                return options[idx]
            print(f"Invalid choice. Enter 1-{len(options)}, 's', or 'q'.")
        except (ValueError, KeyboardInterrupt):
            print("\nCancelled.")
            return None


def run_mapping_wizard() -> None:
    """Main wizard entry point."""
    print("=" * 70)
    print("Datadog Mapping Wizard for MVP1")
    print("=" * 70)
    print("\nThis wizard will help you map your project environments and components")
    print("to Datadog selectors (namespaces, clusters, services, deployments).")
    print("\nRequirements:")
    print("  - DATADOG_API_KEY and DATADOG_APP_KEY (or DD_* variants) must be set")
    print("  - Datadog inventory must be available (run discovery first if needed)")
    print()
    
    # Load inventory
    data_dir = Path(__file__).parent.parent.parent / "data"
    inventory_path = data_dir / "datadog_inventory.json"
    
    if not inventory_path.exists():
        print(f"❌ Inventory not found at {inventory_path}")
        print("   Run discovery first: python datadog_discovery.py")
        sys.exit(1)
    
    inventory = json.load(open(inventory_path, "r", encoding="utf-8"))
    print(f"✅ Loaded inventory: {len(inventory.get('namespaces', []))} namespaces, {len(inventory.get('services', []))} services")
    
    # Load project configs
    configs = load_project_configs()
    print(f"✅ Loaded {len(configs)} project config(s)")
    
    # Process each project
    mappings: dict[str, dict[str, Any]] = {}  # project_key -> {envs: {}, components: {}}
    
    for cfg in configs:
        project = cfg.get("project", {})
        project_key = project.get("key", "").strip()
        if not project_key:
            continue
        
        print(f"\n{'='*70}")
        print(f"Project: {project_key}")
        print(f"{'='*70}")
        
        envs_cfg = cfg.get("environments", [])
        services_cfg = cfg.get("services", [])
        
        project_mappings = {
            "envs": {},
            "components": {},
        }
        
        # Map environments
        print(f"\n📋 Mapping {len(envs_cfg)} environment(s)...")
        for env_cfg in envs_cfg:
            env_key = env_cfg.get("key", "").strip()
            env_name = env_cfg.get("name", "").strip()
            
            if not env_key:
                continue
            
            candidates = propose_env_selector(project_key, env_key, env_name, inventory)
            
            if not candidates:
                print(f"\n⚠️  No candidates found for environment '{env_key}'")
                choice = input("  Skip this environment? [y/n]: ").strip().lower()
                if choice == "y":
                    continue
                # Allow manual entry
                print("  Enter selector manually (e.g., 'kube_namespace:qa'):")
                manual = input("  > ").strip()
                if manual:
                    # Parse manual entry (simple)
                    selector = {}
                    for part in manual.split(","):
                        part = part.strip()
                        if ":" in part:
                            k, v = part.split(":", 1)
                            k = k.strip()
                            v = v.strip()
                            if k == "kube_namespace":
                                selector["namespace"] = v
                            elif k == "kube_cluster_name":
                                selector["cluster"] = v
                    if selector:
                        project_mappings["envs"][env_key] = {
                            "selector": selector,
                            "confidence": 0.5,
                            "reason": "Manual entry",
                        }
                continue
            
            result = interactive_choose(
                f"Environment: {env_key} ({env_name})",
                candidates,
                default_index=0,
            )
            
            if result is None:
                print("Wizard cancelled.")
                return
            if result.get("skip"):
                continue
            
            project_mappings["envs"][env_key] = result
        
        # Map components (optional, per environment)
        print(f"\n📦 Mapping {len(services_cfg)} component(s) (optional)...")
        for svc_cfg in services_cfg:
            svc_key = svc_cfg.get("key", "").strip()
            code_repo = svc_cfg.get("codeRepo", "").strip()
            
            if not svc_key:
                continue
            
            # For each environment that was mapped, propose component selectors
            component_mappings = {}
            for env_key, env_mapping in project_mappings["envs"].items():
                namespace = env_mapping.get("selector", {}).get("namespace")
                if not namespace:
                    continue
                
                candidates = propose_component_selector(svc_key, code_repo, namespace, inventory)
                if candidates:
                    result = interactive_choose(
                        f"Component: {svc_key} in environment: {env_key}",
                        candidates,
                        default_index=0,
                    )
                    
                    if result is None:
                        print("Wizard cancelled.")
                        return
                    if not result.get("skip"):
                        component_mappings[env_key] = result
            
            if component_mappings:
                project_mappings["components"][svc_key] = component_mappings
        
        mappings[project_key] = project_mappings
    
    # Write mappings back to YAML configs
    print(f"\n{'='*70}")
    print("Writing mappings to YAML configs...")
    print(f"{'='*70}")
    
    cfg_dir = Path(__file__).parent / "configs"
    for cfg_file in cfg_dir.glob("*.yaml"):
        cfg_data = yaml.safe_load(cfg_file.read_text(encoding="utf-8")) or {}
        project = cfg_data.get("project", {})
        project_key = project.get("key", "").strip()
        
        if project_key not in mappings:
            continue
        
        project_mappings = mappings[project_key]
        
        # Ensure datadog section exists
        if "datadog" not in cfg_data:
            cfg_data["datadog"] = {}
        
        datadog_cfg = cfg_data["datadog"]
        
        # Write environment selectors
        if "envSelectors" not in datadog_cfg:
            datadog_cfg["envSelectors"] = {}
        
        for env_key, env_mapping in project_mappings["envs"].items():
            selector = env_mapping.get("selector", {})
            datadog_cfg["envSelectors"][env_key] = selector
        
        # Write component selectors (optional)
        if project_mappings["components"]:
            if "componentSelectors" not in datadog_cfg:
                datadog_cfg["componentSelectors"] = {}
            
            for svc_key, env_mappings in project_mappings["components"].items():
                if svc_key not in datadog_cfg["componentSelectors"]:
                    datadog_cfg["componentSelectors"][svc_key] = {}
                
                for env_key, comp_mapping in env_mappings.items():
                    selector = comp_mapping.get("selector", {})
                    datadog_cfg["componentSelectors"][svc_key][env_key] = selector
        
        # Write back to file
        with open(cfg_file, "w", encoding="utf-8") as f:
            yaml.dump(cfg_data, f, default_flow_style=False, sort_keys=False, allow_unicode=True)
        
        print(f"✅ Updated {cfg_file.name}")
    
    print("\n✅ Mapping wizard complete!")
    print("\nNext steps:")
    print("  1. Review the updated YAML configs")
    print("  2. Run snapshot to test the mappings")
    print("  3. Adjust selectors in YAML if needed")


if __name__ == "__main__":
    run_mapping_wizard()
