"""
Selection-Based Onboarding Wizard for MVP1.

This wizard provides a much better UX:
1. Discovers ALL resources from integrations
2. Shows organized lists with suggestions
3. User selects what to track (checkboxes)
4. System auto-generates project configs

Flow:
- "Here's what we found - please confirm which ones you want to track"
- User checks: "this repo - yes, this one - no, these three - yes"
- System creates configs automatically

Usage:
    python snapshot.py --onboard-select
"""

import json
import os
import sys
from collections import defaultdict
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

import yaml

# Import from snapshot.py
sys.path.insert(0, str(Path(__file__).parent))
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
    for prefix in ["kube-", "kube_", "kubernetes-", "kubernetes_", "env-", "env_"]:
        if name_lower.startswith(prefix):
            candidates.append(name_lower[len(prefix):])
    return list(set(candidates))


def group_resources_by_pattern(resources: List[Dict[str, Any]], key_field: str) -> Dict[str, List[Dict[str, Any]]]:
    """Group resources by common naming patterns (e.g., project prefixes)."""
    groups: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    
    for resource in resources:
        name = resource.get(key_field, "").lower()
        # Try to extract project prefix (e.g., "po1-tap-", "tcbp-mfe-")
        parts = name.split("-")
        if len(parts) >= 2:
            prefix = f"{parts[0]}-{parts[1]}"
            groups[prefix].append(resource)
        else:
            groups["_other"].append(resource)
    
    return dict(groups)


def interactive_multi_select(
    title: str,
    items: List[Dict[str, Any]],
    *,
    display_func: callable = None,
    default_selected: bool = False,
    group_by: Optional[str] = None,
) -> Set[int]:
    """
    Interactive multi-select with checkboxes.
    
    Returns set of selected indices.
    """
    if not items:
        return set()
    
    print(f"\n{'='*70}")
    print(title)
    print(f"{'='*70}")
    
    # Group items if requested
    if group_by:
        groups = group_resources_by_pattern(items, group_by)
        print(f"\nFound {len(items)} items in {len(groups)} groups:")
        for group_name, group_items in sorted(groups.items()):
            print(f"  {group_name}: {len(group_items)} items")
        print("\nShowing all items together (grouping is for reference):")
    else:
        print(f"\nFound {len(items)} items:")
    
    # Display items with checkboxes
    selected: Set[int] = set()
    
    # Show items in pages if too many
    page_size = 50
    if len(items) > page_size:
        print(f"\n⚠️  Too many items ({len(items)}). Showing first {page_size}.")
        print("   You can select all/none, then refine later.")
        items_to_show = items[:page_size]
    else:
        items_to_show = items
    
    for i, item in enumerate(items_to_show):
        checkbox = "[✓]" if (default_selected or i in selected) else "[ ]"
        if display_func:
            display = display_func(item)
        else:
            display = str(item.get("name", item.get("id", item.get("key", str(item)))))
        print(f"  {checkbox} [{i+1:3d}] {display}")
    
    print(f"\nCommands:")
    print(f"  Enter numbers to toggle (e.g., '1 3 5' or '1-10' or 'all' or 'none')")
    print(f"  's' = select all shown, 'u' = unselect all shown")
    print(f"  'd' = done (continue), 'q' = quit")
    
    while True:
        try:
            choice = input("\nYour selection: ").strip().lower()
            
            if choice == "q":
                return None  # Signal quit
            if choice == "d":
                break
            if choice == "s" or choice == "all":
                selected = set(range(len(items_to_show)))
                print(f"✓ Selected all {len(items_to_show)} items")
                continue
            if choice == "u" or choice == "none":
                selected = set()
                print("✓ Unselected all")
                continue
            
            # Parse number ranges
            indices = set()
            for part in choice.split():
                part = part.strip()
                if "-" in part:
                    # Range like "1-10"
                    try:
                        start, end = part.split("-", 1)
                        start_idx = int(start) - 1
                        end_idx = int(end) - 1
                        indices.update(range(start_idx, end_idx + 1))
                    except ValueError:
                        continue
                else:
                    try:
                        idx = int(part) - 1
                        if 0 <= idx < len(items_to_show):
                            indices.add(idx)
                    except ValueError:
                        continue
            
            # Toggle selected items
            for idx in indices:
                if idx in selected:
                    selected.remove(idx)
                else:
                    selected.add(idx)
            
            # Redisplay with updated checkboxes
            print(f"\n{'='*70}")
            print(title)
            print(f"{'='*70}")
            for i, item in enumerate(items_to_show):
                checkbox = "[✓]" if i in selected else "[ ]"
                if display_func:
                    display = display_func(item)
                else:
                    display = str(item.get("name", item.get("id", item.get("key", str(item)))))
                print(f"  {checkbox} [{i+1:3d}] {display}")
            print(f"\nSelected: {len(selected)}/{len(items_to_show)} items")
            
        except (ValueError, KeyboardInterrupt):
            print("\nCancelled.")
            return None
    
    # Convert to full indices if we showed a subset
    if len(items) > page_size:
        # User selected from first page_size items
        return selected
    else:
        return selected


def auto_suggest_groupings(
    repos: List[Dict[str, Any]],
    build_types: List[Dict[str, Any]],
    namespaces: List[str],
) -> Dict[str, Dict[str, Any]]:
    """
    Auto-suggest project groupings based on naming patterns.
    
    Returns dict mapping project_key -> {
        "repos": [...],
        "buildTypes": [...],
        "namespaces": [...],
        "confidence": float
    }
    """
    suggestions: Dict[str, Dict[str, Any]] = defaultdict(lambda: {
        "repos": [],
        "buildTypes": [],
        "namespaces": [],
        "confidence": 0.0,
    })
    
    # Group repos by common prefixes
    repo_groups = group_resources_by_pattern(repos, "name")
    
    for prefix, group_repos in repo_groups.items():
        if prefix == "_other" or len(group_repos) < 2:
            continue
        
        project_key = prefix.upper().replace("-", "_")
        suggestions[project_key]["repos"] = group_repos
        suggestions[project_key]["confidence"] = 0.7
        
        # Find matching build types
        for bt in build_types:
            bt_id_lower = bt.get("id", "").lower()
            bt_name_lower = bt.get("name", "").lower()
            if prefix in bt_id_lower or prefix in bt_name_lower:
                suggestions[project_key]["buildTypes"].append(bt)
                suggestions[project_key]["confidence"] = min(0.95, suggestions[project_key]["confidence"] + 0.1)
        
        # Find matching namespaces
        for ns in namespaces:
            ns_lower = ns.lower()
            if prefix in ns_lower:
                suggestions[project_key]["namespaces"].append(ns)
                suggestions[project_key]["confidence"] = min(0.95, suggestions[project_key]["confidence"] + 0.05)
    
    return dict(suggestions)


def generate_project_config(
    project_key: str,
    project_name: str,
    selected_repos: List[Dict[str, Any]],
    selected_build_types: List[Dict[str, Any]],
    selected_namespaces: List[str],
    selected_jira_projects: List[Dict[str, Any]],
    github_org: str,
    inventory: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Auto-generate a project config YAML from selected resources.
    """
    config: Dict[str, Any] = {
        "project": {
            "key": project_key,
            "name": project_name,
            "githubOwner": github_org,
            "infraRef": "main",
        },
        "environments": [],
        "services": [],
    }
    
    # Extract environments from namespaces (common patterns: dev, qa, uat, prod)
    env_patterns = {
        "dev": ["dev", "development"],
        "qa": ["qa", "test"],
        "uat": ["uat", "staging"],
        "prod": ["prod", "production"],
    }
    
    env_keys_seen = set()
    for ns in selected_namespaces:
        ns_lower = ns.lower()
        for env_key, patterns in env_patterns.items():
            if any(p in ns_lower for p in patterns):
                if env_key not in env_keys_seen:
                    config["environments"].append({
                        "key": env_key,
                        "name": env_key.upper(),
                    })
                    env_keys_seen.add(env_key)
                break
    
    # If no environments found, create default set
    if not config["environments"]:
        config["environments"] = [
            {"key": "dev", "name": "DEV"},
            {"key": "qa", "name": "QA"},
            {"key": "prod", "name": "PROD"},
        ]
    
    # Map repos to services
    # Try to match repos with build types
    repo_to_build_type: Dict[str, Dict[str, Any]] = {}
    for repo in selected_repos:
        repo_name = repo.get("name", "").lower()
        # Find matching build type
        best_match = None
        best_score = 0.0
        for bt in selected_build_types:
            bt_id_lower = bt.get("id", "").lower()
            bt_name_lower = bt.get("name", "").lower()
            score1 = similarity_score(repo_name, bt_id_lower)
            score2 = similarity_score(repo_name, bt_name_lower)
            score = max(score1, score2)
            if score > best_score and score > 0.5:
                best_score = score
                best_match = bt
        if best_match:
            repo_to_build_type[repo.get("name", "")] = best_match
    
    # Create services from repos
    for repo in selected_repos:
        repo_name = repo.get("name", "")
        repo_lower = repo_name.lower()
        
        # Skip infra repos (they're handled separately)
        if "-infra" in repo_lower or repo_name.endswith("-infra"):
            continue
        
        service_key = repo_name.replace("_", "-").lower()
        
        # Find infra repo
        infra_repo_name = f"{repo_name}-infra"
        infra_repo = None
        for r in selected_repos:
            if r.get("name", "").lower() == infra_repo_name.lower():
                infra_repo = r
                break
        
        service: Dict[str, Any] = {
            "key": service_key,
            "codeRepo": repo_name,
        }
        
        if infra_repo:
            service["infraRepo"] = infra_repo.get("name", "")
        
        # Add build type if matched
        if repo_name in repo_to_build_type:
            service["teamcityBuildTypeId"] = repo_to_build_type[repo_name].get("id", "")
        
        config["services"].append(service)
    
    # Add Datadog config
    if selected_namespaces:
        config["datadog"] = {
            "enabled": True,
            "windowMinutes": 10,
            "envSelectors": {},
        }
        
        # Map environments to namespaces
        for env in config["environments"]:
            env_key = env["key"]
            # Find matching namespace
            matching_ns = None
            for ns in selected_namespaces:
                ns_lower = ns.lower()
                if env_key in ns_lower:
                    matching_ns = ns
                    break
            
            if matching_ns:
                config["datadog"]["envSelectors"][env_key] = {
                    "namespace": matching_ns,
                }
    
    # Add GitHub ticket regex from Jira
    if selected_jira_projects:
        jira_project = selected_jira_projects[0]  # Use first selected
        jira_key = jira_project.get("key", "")
        if jira_key:
            if "github" not in config:
                config["github"] = {}
            config["github"]["ticket_regex"] = f"{jira_key}-\\d+"
    
    return config


def run_selection_onboarding_wizard() -> None:
    """Main selection-based onboarding wizard."""
    print("=" * 70)
    print("Selection-Based Onboarding Wizard")
    print("=" * 70)
    print("\nThis wizard will:")
    print("  1. Discover ALL resources from your integrations")
    print("  2. Show you organized lists")
    print("  3. Let you select what to track (checkboxes)")
    print("  4. Auto-generate project configs")
    print("\nFlow: 'Here's what we found - please confirm which ones you want to track'")
    print()
    
    # Load credentials
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
        print("❌ Error: No integration credentials found.")
        sys.exit(1)
    
    print(f"✅ Found credentials for: {', '.join(integrations_available)}")
    
    # Step 1: Discovery
    print(f"\n{'='*70}")
    print("Step 1: Discovering Resources")
    print(f"{'='*70}")
    
    inventory_path = Path(__file__).parent.parent.parent / "data" / "integration_inventory.json"
    
    use_cached = False
    if inventory_path.exists():
        choice = input(f"\nFound existing inventory. Use cached? [y/n]: ").strip().lower()
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
        print(f"✅ Discovery complete!")
    
    # Step 2: Selection
    print(f"\n{'='*70}")
    print("Step 2: Select Resources to Track")
    print(f"{'='*70}")
    
    selected_repos: List[Dict[str, Any]] = []
    selected_build_types: List[Dict[str, Any]] = []
    selected_namespaces: List[str] = []
    selected_jira_projects: List[Dict[str, Any]] = []
    
    # Select GitHub repositories
    if inventory.get("github") and "error" not in inventory["github"]:
        repos = inventory["github"].get("repositories", [])
        # Filter out archived
        active_repos = [r for r in repos if not r.get("archived", False)]
        
        if active_repos:
            def repo_display(repo: Dict[str, Any]) -> str:
                name = repo.get("name", "")
                lang = repo.get("language", "")
                desc = repo.get("description", "")[:50]
                return f"{name:40s} {lang:10s} {desc}"
            
            selected_indices = interactive_multi_select(
                "GitHub Repositories - Select which ones to track",
                active_repos,
                display_func=repo_display,
                default_selected=False,
                group_by="name",
            )
            
            if selected_indices is None:
                print("Wizard cancelled.")
                return
            
            selected_repos = [active_repos[i] for i in selected_indices]
            print(f"✓ Selected {len(selected_repos)} repositories")
    
    # Select TeamCity build types
    if inventory.get("teamcity") and "error" not in inventory["teamcity"]:
        build_types = inventory["teamcity"].get("buildTypes", [])
        
        if build_types:
            def bt_display(bt: Dict[str, Any]) -> str:
                bt_id = bt.get("id", "")
                bt_name = bt.get("name", "")
                project = bt.get("projectName", "")
                return f"{bt_id:40s} {bt_name:30s} ({project})"
            
            selected_indices = interactive_multi_select(
                "TeamCity Build Types - Select which ones to track",
                build_types,
                display_func=bt_display,
                default_selected=False,
            )
            
            if selected_indices is None:
                print("Wizard cancelled.")
                return
            
            selected_build_types = [build_types[i] for i in selected_indices]
            print(f"✓ Selected {len(selected_build_types)} build types")
    
    # Select Datadog namespaces
    if inventory.get("datadog") and "error" not in inventory["datadog"]:
        namespaces = inventory["datadog"].get("namespaces", [])
        
        if namespaces:
            def ns_display(ns: str) -> str:
                stats = inventory["datadog"].get("namespaceStats", {}).get(ns, {})
                pod_count = stats.get("podCount", 0)
                svc_count = stats.get("serviceCount", 0)
                return f"{ns:40s} {pod_count:3d} pods, {svc_count:2d} services"
            
            selected_indices = interactive_multi_select(
                "Datadog Namespaces - Select which ones to track",
                [{"name": ns} for ns in namespaces],
                display_func=lambda x: ns_display(x["name"]),
                default_selected=False,
            )
            
            if selected_indices is None:
                print("Wizard cancelled.")
                return
            
            selected_namespaces = [namespaces[i] for i in selected_indices]
            print(f"✓ Selected {len(selected_namespaces)} namespaces")
    
    # Select Jira projects
    if inventory.get("jira") and "error" not in inventory["jira"]:
        jira_projects = inventory["jira"].get("projects", [])
        
        if jira_projects:
            def jira_display(proj: Dict[str, Any]) -> str:
                key = proj.get("key", "")
                name = proj.get("name", "")
                return f"{key:10s} {name}"
            
            selected_indices = interactive_multi_select(
                "Jira Projects - Select which ones to track",
                jira_projects,
                display_func=jira_display,
                default_selected=False,
            )
            
            if selected_indices is None:
                print("Wizard cancelled.")
                return
            
            selected_jira_projects = [jira_projects[i] for i in selected_indices]
            print(f"✓ Selected {len(selected_jira_projects)} Jira projects")
    
    # Step 3: Auto-suggest project groupings
    print(f"\n{'='*70}")
    print("Step 3: Auto-Generate Project Configs")
    print(f"{'='*70}")
    
    suggestions = auto_suggest_groupings(
        selected_repos,
        selected_build_types,
        selected_namespaces,
    )
    
    if suggestions:
        print(f"\n📋 Auto-detected {len(suggestions)} project(s) from naming patterns:")
        for proj_key, proj_data in suggestions.items():
            print(f"  {proj_key}: {len(proj_data['repos'])} repos, {len(proj_data['buildTypes'])} build types, {len(proj_data['namespaces'])} namespaces")
        
        choice = input("\nUse auto-detected projects? [y/n]: ").strip().lower()
        if choice == "y":
            # Generate configs for each suggested project
            cfg_dir = Path(__file__).parent / "configs"
            cfg_dir.mkdir(exist_ok=True)
            
            for proj_key, proj_data in suggestions.items():
                proj_name = proj_key.replace("_", " ").title()
                
                config = generate_project_config(
                    project_key=proj_key,
                    project_name=proj_name,
                    selected_repos=proj_data["repos"],
                    selected_build_types=proj_data["buildTypes"],
                    selected_namespaces=proj_data["namespaces"],
                    selected_jira_projects=selected_jira_projects,
                    github_org=gh_org,
                    inventory=inventory,
                )
                
                cfg_file = cfg_dir / f"{proj_key.lower()}.yaml"
                with open(cfg_file, "w", encoding="utf-8") as f:
                    yaml.dump(config, f, default_flow_style=False, sort_keys=False, allow_unicode=True)
                
                print(f"✅ Generated {cfg_file.name}")
        else:
            # Manual project creation
            print("\nEnter project details manually:")
            proj_key = input("Project key (e.g., TCBP_MFES): ").strip()
            proj_name = input("Project name: ").strip() or proj_key.replace("_", " ").title()
            
            config = generate_project_config(
                project_key=proj_key,
                project_name=proj_name,
                selected_repos=selected_repos,
                selected_build_types=selected_build_types,
                selected_namespaces=selected_namespaces,
                selected_jira_projects=selected_jira_projects,
                github_org=gh_org,
                inventory=inventory,
            )
            
            cfg_dir = Path(__file__).parent / "configs"
            cfg_dir.mkdir(exist_ok=True)
            cfg_file = cfg_dir / f"{proj_key.lower()}.yaml"
            with open(cfg_file, "w", encoding="utf-8") as f:
                yaml.dump(config, f, default_flow_style=False, sort_keys=False, allow_unicode=True)
            
            print(f"✅ Generated {cfg_file.name}")
    else:
        # No auto-detection, manual creation
        print("\n⚠️  Could not auto-detect projects from naming patterns.")
        print("Creating a single project with all selected resources...")
        
        proj_key = input("Project key (e.g., MY_PROJECT): ").strip() or "MY_PROJECT"
        proj_name = input("Project name: ").strip() or proj_key.replace("_", " ").title()
        
        config = generate_project_config(
            project_key=proj_key,
            project_name=proj_name,
            selected_repos=selected_repos,
            selected_build_types=selected_build_types,
            selected_namespaces=selected_namespaces,
            selected_jira_projects=selected_jira_projects,
            github_org=gh_org,
            inventory=inventory,
        )
        
        cfg_dir = Path(__file__).parent / "configs"
        cfg_dir.mkdir(exist_ok=True)
        cfg_file = cfg_dir / f"{proj_key.lower()}.yaml"
        with open(cfg_file, "w", encoding="utf-8") as f:
            yaml.dump(config, f, default_flow_style=False, sort_keys=False, allow_unicode=True)
        
        print(f"✅ Generated {cfg_file.name}")
    
    print("\n✅ Onboarding complete!")
    print("\nNext steps:")
    print("  1. Review the generated YAML configs in MVP1/snapshot/configs/")
    print("  2. Adjust mappings if needed")
    print("  3. Run snapshot to test: python snapshot.py")


if __name__ == "__main__":
    run_selection_onboarding_wizard()
