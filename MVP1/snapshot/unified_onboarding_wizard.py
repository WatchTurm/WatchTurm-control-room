"""
Unified Onboarding Wizard for MVP1.

This wizard automates the entire onboarding process:
1. Discovers resources from all integrations (Datadog, TeamCity, GitHub, Jira)
2. Proposes mappings with confidence scores
3. Allows interactive confirmation/selection
4. Writes mappings back to YAML configs

This solves the critical onboarding problem where customers would otherwise
need to manually configure hundreds of mappings.

Usage:
    python unified_onboarding_wizard.py
    or
    python snapshot.py --onboard
"""

import json
import os
import sys
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

# Import from snapshot.py
sys.path.insert(0, str(Path(__file__).parent))
from snapshot import load_project_configs
from integration_discovery import (
    discover_all_integrations,
    save_discovery_inventory,
    load_discovery_inventory,
)


def similarity_score(a: str, b: str) -> float:
    """Calculate string similarity score (0.0 to 1.0)."""
    if not a or not b:
        return 0.0
    a_lower = a.lower().strip().replace("_", "-").replace(".", "-")
    b_lower = b.lower().strip().replace("_", "-").replace(".", "-")
    if a_lower == b_lower:
        return 1.0
    return SequenceMatcher(None, a_lower, b_lower).ratio()


def normalize_name(name: str) -> List[str]:
    """Generate normalized candidate names for matching."""
    if not name:
        return []
    candidates = []
    name_lower = name.lower().strip()
    candidates.append(name_lower)
    candidates.append(name_lower.replace("-", "_"))
    candidates.append(name_lower.replace("_", "-"))
    # Remove common prefixes
    for prefix in ["kube-", "kube_", "kubernetes-", "kubernetes_", "env-", "env_"]:
        if name_lower.startswith(prefix):
            candidates.append(name_lower[len(prefix):])
    return list(set(candidates))


# ============================================================================
# Datadog Mapping Proposals
# ============================================================================

def propose_datadog_env_selector(
    project_key: str,
    env_key: str,
    env_name: str,
    inventory: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """Propose Datadog environment selectors with confidence scores."""
    candidates = []
    
    if not inventory or "namespaces" not in inventory:
        return candidates
    
    namespaces = inventory.get("namespaces", [])
    clusters = inventory.get("clusters", [])
    namespace_stats = inventory.get("namespaceStats", {})
    
    env_candidates = normalize_name(env_key) + normalize_name(env_name)
    project_lower = project_key.lower().replace("_", "-")
    
    # Strategy 1: Direct namespace match
    for ns in namespaces:
        ns_lower = ns.lower()
        best_score = 0.0
        
        for env_candidate in env_candidates:
            score = similarity_score(ns_lower, env_candidate)
            if score > best_score:
                best_score = score
        
        if best_score > 0.3:
            confidence = best_score
            # Boost if project name in namespace
            if project_lower in ns_lower:
                confidence = min(0.95, confidence + 0.2)
            
            selector = {"namespace": ns}
            
            # Try to find matching cluster
            for cluster in clusters:
                cluster_lower = cluster.lower()
                if project_lower in cluster_lower or any(ec in cluster_lower for ec in env_candidates):
                    selector["cluster"] = cluster
                    confidence += 0.05
                    break
            
            # Boost confidence if namespace has many services/pods (active namespace)
            ns_stats = namespace_stats.get(ns, {})
            if ns_stats.get("serviceCount", 0) > 5:
                confidence = min(0.95, confidence + 0.1)
            if ns_stats.get("podCount", 0) > 10:
                confidence = min(0.95, confidence + 0.1)
            
            candidates.append({
                "selector": selector,
                "confidence": confidence,
                "reason": f"Namespace '{ns}' matches '{env_key}' (similarity: {best_score:.2f}, {ns_stats.get('podCount', 0)} pods, {ns_stats.get('serviceCount', 0)} services)",
            })
    
    # Strategy 2: Project + environment pattern
    for env_candidate in env_candidates:
        pattern1 = f"{project_lower}-{env_candidate}"
        pattern2 = f"{project_lower}_{env_candidate}"
        
        for ns in namespaces:
            ns_lower = ns.lower()
            if pattern1 in ns_lower or pattern2 in ns_lower:
                score = similarity_score(ns_lower, pattern1)
                if score > 0.5:
                    selector = {"namespace": ns}
                    for cluster in clusters:
                        if project_lower in cluster.lower():
                            selector["cluster"] = cluster
                            break
                    
                    candidates.append({
                        "selector": selector,
                        "confidence": min(0.9, score + 0.1),
                        "reason": f"Namespace '{ns}' matches pattern '{pattern1}'",
                    })
    
    # Remove duplicates and sort
    seen = set()
    unique = []
    for c in candidates:
        key = json.dumps(c["selector"], sort_keys=True)
        if key not in seen:
            seen.add(key)
            unique.append(c)
    
    unique.sort(key=lambda x: x["confidence"], reverse=True)
    return unique[:5]


def propose_datadog_component_selector(
    component_key: str,
    code_repo: str,
    namespace: str,
    inventory: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """Propose Datadog component/service selectors."""
    candidates = []
    
    if not inventory:
        return candidates
    
    namespace_stats = inventory.get("namespaceStats", {})
    ns_stats = namespace_stats.get(namespace, {})
    
    component_candidates = normalize_name(component_key) + normalize_name(code_repo)
    
    # Match services
    for svc_info in ns_stats.get("services", [])[:20]:  # Top 20
        svc_name = svc_info.get("name", "")
        svc_lower = svc_name.lower()
        
        best_score = 0.0
        for comp_candidate in component_candidates:
            score = similarity_score(svc_lower, comp_candidate)
            if score > best_score:
                best_score = score
        
        if best_score > 0.4:
            candidates.append({
                "selector": {"service": svc_name},
                "confidence": best_score,
                "reason": f"Service '{svc_name}' matches component '{component_key}'",
            })
    
    # Match deployments
    for dep_info in ns_stats.get("deployments", [])[:20]:
        dep_name = dep_info.get("name", "")
        dep_lower = dep_name.lower()
        
        best_score = 0.0
        for comp_candidate in component_candidates:
            score = similarity_score(dep_lower, comp_candidate)
            if score > best_score:
                best_score = score
        
        if best_score > 0.4:
            candidates.append({
                "selector": {"kube_deployment": dep_name},
                "confidence": best_score,
                "reason": f"Deployment '{dep_name}' matches component '{component_key}'",
            })
    
    candidates.sort(key=lambda x: x["confidence"], reverse=True)
    return candidates[:5]


# ============================================================================
# TeamCity Mapping Proposals
# ============================================================================

def propose_teamcity_build_type(
    component_key: str,
    code_repo: str,
    inventory: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """Propose TeamCity build type IDs for a component."""
    candidates = []
    
    if not inventory or "buildTypes" not in inventory:
        return candidates
    
    build_types = inventory.get("buildTypes", [])
    component_candidates = normalize_name(component_key) + normalize_name(code_repo)
    
    for bt in build_types:
        bt_id = bt.get("id", "")
        bt_name = bt.get("name", "").lower()
        bt_id_lower = bt_id.lower()
        
        # Check if component/repo name appears in build type ID or name
        best_score = 0.0
        for comp_candidate in component_candidates:
            if comp_candidate in bt_id_lower or comp_candidate in bt_name:
                score = similarity_score(bt_id_lower, comp_candidate)
                if score > best_score:
                    best_score = score
        
        if best_score > 0.4:
            candidates.append({
                "buildTypeId": bt_id,
                "name": bt.get("name", ""),
                "confidence": best_score,
                "reason": f"Build type '{bt_id}' matches component '{component_key}'",
            })
    
    candidates.sort(key=lambda x: x["confidence"], reverse=True)
    return candidates[:5]


# ============================================================================
# GitHub Mapping Proposals
# ============================================================================

def propose_github_repositories(
    component_key: str,
    inventory: Dict[str, Any],
    *,
    prefer_infra: bool = False,
) -> List[Dict[str, Any]]:
    """Propose GitHub repositories for a component."""
    candidates = []
    
    if not inventory or "repositories" not in inventory:
        return candidates
    
    repos = inventory.get("repositories", [])
    component_candidates = normalize_name(component_key)
    
    for repo in repos:
        repo_name = repo.get("name", "")
        repo_lower = repo_name.lower()
        
        # Skip archived repos
        if repo.get("archived", False):
            continue
        
        # Check if component name matches repo name
        best_score = 0.0
        for comp_candidate in component_candidates:
            score = similarity_score(repo_lower, comp_candidate)
            if score > best_score:
                best_score = score
        
        # Boost infra repos if requested
        is_infra = "-infra" in repo_lower or "infra" in repo_lower
        if prefer_infra and is_infra:
            best_score = min(0.95, best_score + 0.2)
        elif prefer_infra and not is_infra:
            best_score = max(0.0, best_score - 0.1)
        
        if best_score > 0.5:
            candidates.append({
                "repo": repo_name,
                "full_name": repo.get("full_name", ""),
                "default_branch": repo.get("default_branch", "main"),
                "confidence": best_score,
                "reason": f"Repository '{repo_name}' matches component '{component_key}'",
            })
    
    candidates.sort(key=lambda x: x["confidence"], reverse=True)
    return candidates[:5]


# ============================================================================
# Jira Mapping Proposals
# ============================================================================

def propose_jira_project_key(
    project_key: str,
    inventory: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """Propose Jira project keys for a project."""
    candidates = []
    
    if not inventory or "projects" not in inventory:
        return candidates
    
    projects = inventory.get("projects", [])
    project_candidates = normalize_name(project_key)
    
    for proj in projects:
        jira_key = proj.get("key", "")
        jira_name = proj.get("name", "").lower()
        jira_key_lower = jira_key.lower()
        
        # Check if project key/name matches
        best_score = 0.0
        for proj_candidate in project_candidates:
            score1 = similarity_score(jira_key_lower, proj_candidate)
            score2 = similarity_score(jira_name, proj_candidate)
            score = max(score1, score2)
            if score > best_score:
                best_score = score
        
        if best_score > 0.3:
            candidates.append({
                "key": jira_key,
                "name": proj.get("name", ""),
                "confidence": best_score,
                "reason": f"Jira project '{jira_key}' matches project '{project_key}'",
            })
    
    candidates.sort(key=lambda x: x["confidence"], reverse=True)
    return candidates[:5]


# ============================================================================
# Interactive Selection
# ============================================================================

def interactive_choose(
    prompt: str,
    options: List[Dict[str, Any]],
    default_index: int = 0,
    allow_manual: bool = False,
) -> Optional[Dict[str, Any]]:
    """Interactive selection from options."""
    if not options:
        if allow_manual:
            print(f"\n{prompt}")
            manual = input("  No candidates found. Enter manually (or press Enter to skip): ").strip()
            if manual:
                return {"manual": manual, "confidence": 0.5, "reason": "Manual entry"}
        return None
    
    print(f"\n{prompt}")
    for i, opt in enumerate(options):
        marker = "→" if i == default_index else " "
        confidence_bar = "█" * int(opt.get("confidence", 0) * 10)
        reason = opt.get("reason", "")
        print(f"  {marker} [{i+1}] {confidence_bar} ({opt.get('confidence', 0):.0%}) - {reason}")
        # Show selector details if present
        if "selector" in opt:
            selector = opt["selector"]
            parts = []
            if "namespace" in selector:
                parts.append(f"namespace:{selector['namespace']}")
            if "cluster" in selector:
                parts.append(f"cluster:{selector['cluster']}")
            if "service" in selector:
                parts.append(f"service:{selector['service']}")
            if "kube_deployment" in selector:
                parts.append(f"deployment:{selector['kube_deployment']}")
            if parts:
                print(f"      Tags: {', '.join(parts)}")
        elif "buildTypeId" in opt:
            print(f"      Build Type: {opt['buildTypeId']}")
        elif "repo" in opt:
            print(f"      Repo: {opt['repo']} (branch: {opt.get('default_branch', 'main')})")
        elif "key" in opt:
            print(f"      Jira Key: {opt['key']}")
    
    if allow_manual:
        print(f"  [m] Enter manually")
    print(f"  [s] Skip this mapping")
    print(f"  [q] Quit wizard")
    
    while True:
        try:
            choice = input("\nYour choice: ").strip().lower()
            if choice == "q":
                return None
            if choice == "s":
                return {"skip": True}
            if choice == "m" and allow_manual:
                manual = input("  Enter value: ").strip()
                if manual:
                    return {"manual": manual, "confidence": 0.5, "reason": "Manual entry"}
                continue
            idx = int(choice) - 1
            if 0 <= idx < len(options):
                return options[idx]
            print(f"Invalid choice. Enter 1-{len(options)}, 's', 'q'" + (", 'm'" if allow_manual else ""))
        except (ValueError, KeyboardInterrupt):
            print("\nCancelled.")
            return None


# ============================================================================
# Main Wizard
# ============================================================================

def run_unified_onboarding_wizard() -> None:
    """Main unified onboarding wizard entry point."""
    print("=" * 70)
    print("Unified Onboarding Wizard for WatchTurm Control Room")
    print("=" * 70)
    print("\nThis wizard will:")
    print("  1. Discover resources from all integrations (Datadog, TeamCity, GitHub, Jira)")
    print("  2. Propose mappings with confidence scores")
    print("  3. Allow you to confirm/choose interactively")
    print("  4. Write mappings back to YAML configs")
    print("\nRequirements:")
    print("  - Integration credentials must be set in environment variables")
    print("  - Project configs must exist in MVP1/snapshot/configs/")
    print()
    
    # Load credentials from environment
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
    
    # Check what's available
    integrations_available = []
    if dd_api_key and dd_app_key:
        integrations_available.append("Datadog")
    if tc_url and tc_token:
        integrations_available.append("TeamCity")
    if gh_org and gh_token:
        integrations_available.append("GitHub")
    if jira_url and jira_email and jira_token:
        integrations_available.append("Jira")
    
    if not integrations_available:
        print("❌ Error: No integration credentials found in environment variables.")
        print("\nRequired environment variables:")
        print("  - Datadog: DATADOG_API_KEY, DATADOG_APP_KEY")
        print("  - TeamCity: TEAMCITY_BASE_URL, TEAMCITY_TOKEN")
        print("  - GitHub: GITHUB_ORG, GITHUB_TOKEN")
        print("  - Jira: JIRA_BASE_URL, JIRA_EMAIL, JIRA_TOKEN")
        sys.exit(1)
    
    print(f"✅ Found credentials for: {', '.join(integrations_available)}")
    
    # Step 1: Run discovery
    print(f"\n{'='*70}")
    print("Step 1: Discovering Resources")
    print(f"{'='*70}")
    
    inventory_path = Path(__file__).parent.parent.parent / "data" / "integration_inventory.json"
    
    use_cached = False
    if inventory_path.exists():
        choice = input(f"\nFound existing inventory at {inventory_path.name}. Use cached? [y/n]: ").strip().lower()
        use_cached = choice == "y"
    
    if use_cached:
        print("📂 Loading cached inventory...")
        inventory = load_discovery_inventory(inventory_path)
    else:
        print("🔍 Running discovery (this may take a few minutes)...")
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
        save_discovery_inventory(inventory, inventory_path)
        print(f"✅ Discovery complete! Inventory saved to {inventory_path.name}")
    
    # Print discovery summary
    print("\n📊 Discovery Summary:")
    if inventory.get("datadog"):
        dd = inventory["datadog"]
        if "error" not in dd:
            print(f"  Datadog: {len(dd.get('namespaces', []))} namespaces, {len(dd.get('services', []))} services, {len(dd.get('monitors', []))} monitors")
        else:
            print(f"  Datadog: Error - {dd.get('error')}")
    if inventory.get("teamcity"):
        tc = inventory["teamcity"]
        if "error" not in tc:
            print(f"  TeamCity: {len(tc.get('buildTypes', []))} build types, {len(tc.get('projects', []))} projects")
        else:
            print(f"  TeamCity: Error - {tc.get('error')}")
    if inventory.get("github"):
        gh = inventory["github"]
        if "error" not in gh:
            print(f"  GitHub: {len(gh.get('repositories', []))} repositories")
        else:
            print(f"  GitHub: Error - {gh.get('error')}")
    if inventory.get("jira"):
        jira = inventory["jira"]
        if "error" not in jira:
            print(f"  Jira: {len(jira.get('projects', []))} projects")
        else:
            print(f"  Jira: Error - {jira.get('error')}")
    
    # Step 2: Load project configs
    print(f"\n{'='*70}")
    print("Step 2: Mapping Resources to Projects")
    print(f"{'='*70}")
    
    configs = load_project_configs()
    print(f"✅ Loaded {len(configs)} project config(s)")
    
    if not configs:
        print("❌ Error: No project configs found in MVP1/snapshot/configs/")
        sys.exit(1)
    
    # Step 3: Process each project
    mappings: Dict[str, Dict[str, Any]] = {}
    
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
            "datadog": {"envs": {}, "components": {}},
            "teamcity": {},
            "github": {},
            "jira": {},
        }
        
        # Map Datadog environments
        if inventory.get("datadog") and "error" not in inventory["datadog"]:
            print(f"\n📋 Mapping {len(envs_cfg)} Datadog environment(s)...")
            for env_cfg in envs_cfg:
                env_key = env_cfg.get("key", "").strip()
                env_name = env_cfg.get("name", "").strip()
                
                if not env_key:
                    continue
                
                candidates = propose_datadog_env_selector(
                    project_key, env_key, env_name, inventory["datadog"]
                )
                
                result = interactive_choose(
                    f"Datadog Environment: {env_key} ({env_name})",
                    candidates,
                    default_index=0,
                    allow_manual=True,
                )
                
                if result is None:
                    print("Wizard cancelled.")
                    return
                if result.get("skip"):
                    continue
                
                if "manual" in result:
                    # Parse manual entry
                    manual = result["manual"]
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
                    project_mappings["datadog"]["envs"][env_key] = {
                        "selector": selector,
                        "confidence": 0.5,
                        "reason": "Manual entry",
                    }
                else:
                    project_mappings["datadog"]["envs"][env_key] = result
        
        # Map Datadog components (optional, per environment)
        if inventory.get("datadog") and "error" not in inventory["datadog"]:
            print(f"\n📦 Mapping {len(services_cfg)} Datadog component(s) (optional)...")
            for svc_cfg in services_cfg:
                svc_key = svc_cfg.get("key", "").strip()
                code_repo = svc_cfg.get("codeRepo", "").strip()
                
                if not svc_key:
                    continue
                
                component_mappings = {}
                for env_key, env_mapping in project_mappings["datadog"]["envs"].items():
                    namespace = env_mapping.get("selector", {}).get("namespace")
                    if not namespace:
                        continue
                    
                    candidates = propose_datadog_component_selector(
                        svc_key, code_repo, namespace, inventory["datadog"]
                    )
                    if candidates:
                        result = interactive_choose(
                            f"Component: {svc_key} in environment: {env_key}",
                            candidates,
                            default_index=0,
                            allow_manual=True,
                        )
                        
                        if result is None:
                            print("Wizard cancelled.")
                            return
                        if not result.get("skip"):
                            component_mappings[env_key] = result
                
                if component_mappings:
                    project_mappings["datadog"]["components"][svc_key] = component_mappings
        
        # Map TeamCity build types
        if inventory.get("teamcity") and "error" not in inventory["teamcity"]:
            print(f"\n🔨 Mapping {len(services_cfg)} TeamCity build type(s)...")
            for svc_cfg in services_cfg:
                svc_key = svc_cfg.get("key", "").strip()
                code_repo = svc_cfg.get("codeRepo", "").strip()
                
                if not svc_key:
                    continue
                
                # Check if already configured
                existing = svc_cfg.get("teamcityBuildTypeId", "").strip()
                if existing:
                    print(f"  ⏭️  {svc_key}: Already configured as '{existing}' (skipping)")
                    continue
                
                candidates = propose_teamcity_build_type(svc_key, code_repo, inventory["teamcity"])
                
                result = interactive_choose(
                    f"TeamCity Build Type: {svc_key}",
                    candidates,
                    default_index=0,
                    allow_manual=True,
                )
                
                if result is None:
                    print("Wizard cancelled.")
                    return
                if not result.get("skip"):
                    project_mappings["teamcity"][svc_key] = result
        
        # Map GitHub repositories
        if inventory.get("github") and "error" not in inventory["github"]:
            print(f"\n🐙 Mapping {len(services_cfg)} GitHub repository/repositories...")
            for svc_cfg in services_cfg:
                svc_key = svc_cfg.get("key", "").strip()
                
                if not svc_key:
                    continue
                
                # Check if already configured
                existing_code = svc_cfg.get("codeRepo", "").strip()
                existing_infra = svc_cfg.get("infraRepo", "").strip()
                
                # Propose code repo
                if not existing_code:
                    candidates = propose_github_repositories(svc_key, inventory["github"], prefer_infra=False)
                    result = interactive_choose(
                        f"GitHub Code Repository: {svc_key}",
                        candidates,
                        default_index=0,
                        allow_manual=True,
                    )
                    
                    if result is None:
                        print("Wizard cancelled.")
                        return
                    if not result.get("skip"):
                        project_mappings["github"][f"{svc_key}_code"] = result
                
                # Propose infra repo
                if not existing_infra:
                    candidates = propose_github_repositories(svc_key, inventory["github"], prefer_infra=True)
                    result = interactive_choose(
                        f"GitHub Infrastructure Repository: {svc_key}",
                        candidates,
                        default_index=0,
                        allow_manual=True,
                    )
                    
                    if result is None:
                        print("Wizard cancelled.")
                        return
                    if not result.get("skip"):
                        project_mappings["github"][f"{svc_key}_infra"] = result
        
        # Map Jira project
        if inventory.get("jira") and "error" not in inventory["jira"]:
            print(f"\n🎫 Mapping Jira project...")
            
            # Check if already configured
            github_cfg = cfg.get("github", {})
            existing_regex = github_cfg.get("ticket_regex", "").strip()
            
            if not existing_regex:
                candidates = propose_jira_project_key(project_key, inventory["jira"])
                result = interactive_choose(
                    f"Jira Project Key: {project_key}",
                    candidates,
                    default_index=0,
                    allow_manual=True,
                )
                
                if result is None:
                    print("Wizard cancelled.")
                    return
                if not result.get("skip"):
                    project_mappings["jira"] = result
        
        mappings[project_key] = project_mappings
    
    # Step 4: Write mappings back to YAML configs
    print(f"\n{'='*70}")
    print("Step 4: Writing Mappings to YAML Configs")
    print(f"{'='*70}")
    
    cfg_dir = Path(__file__).parent / "configs"
    for cfg_file in cfg_dir.glob("*.yaml"):
        cfg_data = yaml.safe_load(cfg_file.read_text(encoding="utf-8")) or {}
        project = cfg_data.get("project", {})
        project_key = project.get("key", "").strip()
        
        if project_key not in mappings:
            continue
        
        project_mappings = mappings[project_key]
        updated = False
        
        # Write Datadog mappings
        if project_mappings.get("datadog"):
            dd_mappings = project_mappings["datadog"]
            if "datadog" not in cfg_data:
                cfg_data["datadog"] = {}
            
            datadog_cfg = cfg_data["datadog"]
            
            # Write environment selectors
            if dd_mappings.get("envs"):
                if "envSelectors" not in datadog_cfg:
                    datadog_cfg["envSelectors"] = {}
                for env_key, env_mapping in dd_mappings["envs"].items():
                    selector = env_mapping.get("selector", {})
                    datadog_cfg["envSelectors"][env_key] = selector
                    updated = True
            
            # Write component selectors
            if dd_mappings.get("components"):
                if "componentSelectors" not in datadog_cfg:
                    datadog_cfg["componentSelectors"] = {}
                for svc_key, env_mappings in dd_mappings["components"].items():
                    if svc_key not in datadog_cfg["componentSelectors"]:
                        datadog_cfg["componentSelectors"][svc_key] = {}
                    for env_key, comp_mapping in env_mappings.items():
                        selector = comp_mapping.get("selector", {})
                        datadog_cfg["componentSelectors"][svc_key][env_key] = selector
                        updated = True
        
        # Write TeamCity mappings
        if project_mappings.get("teamcity"):
            services = cfg_data.get("services", [])
            for svc in services:
                svc_key = svc.get("key", "").strip()
                if svc_key in project_mappings["teamcity"]:
                    mapping = project_mappings["teamcity"][svc_key]
                    if "manual" in mapping:
                        svc["teamcityBuildTypeId"] = mapping["manual"]
                    elif "buildTypeId" in mapping:
                        svc["teamcityBuildTypeId"] = mapping["buildTypeId"]
                    updated = True
        
        # Write GitHub mappings
        if project_mappings.get("github"):
            services = cfg_data.get("services", [])
            for svc in services:
                svc_key = svc.get("key", "").strip()
                code_key = f"{svc_key}_code"
                infra_key = f"{svc_key}_infra"
                
                if code_key in project_mappings["github"]:
                    mapping = project_mappings["github"][code_key]
                    if "manual" in mapping:
                        svc["codeRepo"] = mapping["manual"]
                    elif "repo" in mapping:
                        svc["codeRepo"] = mapping["repo"]
                    updated = True
                
                if infra_key in project_mappings["github"]:
                    mapping = project_mappings["github"][infra_key]
                    if "manual" in mapping:
                        svc["infraRepo"] = mapping["manual"]
                    elif "repo" in mapping:
                        svc["infraRepo"] = mapping["repo"]
                    updated = True
        
        # Write Jira mapping (ticket regex)
        if project_mappings.get("jira"):
            github_cfg = cfg_data.get("github", {})
            mapping = project_mappings["jira"]
            if "manual" in mapping:
                # Extract project key from manual entry
                jira_key = mapping["manual"].upper()
                github_cfg["ticket_regex"] = f"{jira_key}-\\d+"
            elif "key" in mapping:
                jira_key = mapping["key"]
                github_cfg["ticket_regex"] = f"{jira_key}-\\d+"
            cfg_data["github"] = github_cfg
            updated = True
        
        # Write back to file
        if updated:
            with open(cfg_file, "w", encoding="utf-8") as f:
                yaml.dump(cfg_data, f, default_flow_style=False, sort_keys=False, allow_unicode=True)
            print(f"✅ Updated {cfg_file.name}")
        else:
            print(f"⏭️  {cfg_file.name} (no changes)")
    
    print("\n✅ Onboarding wizard complete!")
    print("\nNext steps:")
    print("  1. Review the updated YAML configs")
    print("  2. Run snapshot to test the mappings")
    print("  3. Adjust mappings in YAML if needed")
    print("  4. Re-run wizard if you need to add more mappings")


if __name__ == "__main__":
    run_unified_onboarding_wizard()
