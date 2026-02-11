#!/usr/bin/env python3
"""
Enterprise Ticket Deployment Diagnostic Tool

Analyzes snapshot data to determine why tickets do or don't have deployment presence
on DEV, QA, UAT, PROD environments.

Usage:
    python diagnose_ticket_deployments.py [--project PROJECT] [--env ENV] [--date-from DATE] [--date-to DATE]
"""

import json
import os
import sys
from pathlib import Path
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple, Set
from collections import defaultdict
import argparse


def _repo_root() -> Path:
    """Get repository root directory."""
    return Path(__file__).resolve().parents[2]


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


def _normalize_branch_name(branch: str) -> str:
    """Normalize branch name for comparison."""
    if not branch:
        return ""
    return branch.strip().lower().replace("refs/heads/", "").replace("refs/remotes/origin/", "")


def _parse_iso(s: str) -> Optional[datetime]:
    """Parse ISO timestamp string to datetime, return None if invalid."""
    if not s or not isinstance(s, str):
        return None
    try:
        s_clean = s.replace("Z", "+00:00")
        return datetime.fromisoformat(s_clean)
    except Exception:
        return None


def load_latest_snapshot() -> Optional[dict]:
    """Load latest.json snapshot."""
    path = _repo_root() / "data" / "latest.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return None


def load_previous_snapshot() -> Optional[dict]:
    """Load most recent historical snapshot."""
    history_dir = _repo_root() / "data" / "history"
    if not history_dir.exists():
        return None
    
    # Find all snapshot files
    snapshots = sorted(history_dir.glob("latest-*.json"), reverse=True)
    if not snapshots:
        return None
    
    try:
        return json.loads(snapshots[0].read_text(encoding='utf-8'))
    except Exception:
        return None


def build_component_map(snapshot: dict) -> Dict[Tuple[str, str, str], dict]:
    """Build component map: (project_key, env_key, component_name) -> component dict."""
    out = {}
    for proj in snapshot.get("projects", []):
        pkey = (proj.get("key") or "").strip()
        for env in proj.get("environments", []):
            ekey = (env.get("key") or "").strip().lower()
            for comp in env.get("components", []):
                cname = (comp.get("name") or "").strip()
                if pkey and ekey and cname:
                    key = (pkey, ekey, cname)
                    out[key] = {
                        "comp": comp,
                        "project": proj,
                        "env": env,
                    }
    return out


def detect_tag_changes(prev_snapshot: Optional[dict], current_snapshot: dict) -> Dict[Tuple[str, str, str], dict]:
    """Detect tag changes between snapshots.
    
    Returns: {(project_key, env_key, component_name): {repo, fromTag, toTag, deployedAt, branch, ...}}
    """
    tag_changes = {}
    
    if not prev_snapshot:
        return tag_changes  # No previous snapshot - cannot detect changes
    
    prev_map = build_component_map(prev_snapshot)
    cur_map = build_component_map(current_snapshot)
    
    for key, cur in cur_map.items():
        pkey, ekey, cname = key
        cur_comp = cur['comp']
        cur_tag = (cur_comp.get('tag') or '').strip()
        prev_tag = ''
        
        if key in prev_map:
            prev_tag = (prev_map[key]['comp'].get('tag') or '').strip()
        
        # Only track actual tag changes (same criteria as Release History: prev_tag != cur_tag)
        if prev_tag and cur_tag and prev_tag != cur_tag:
            repo = (cur_comp.get('repo') or cur_comp.get('repository') or '').strip()
            if repo:
                tag_changes[key] = {
                    'repo': repo,
                    'fromTag': prev_tag,
                    'toTag': cur_tag,
                    'deployedAt': (cur_comp.get('deployedAt') or '').strip(),
                    'branch': _normalize_branch_name(cur_comp.get('branch') or cur_comp.get('ref') or ''),
                    'tag': cur_tag,
                    'component': cname,
                    'projectKey': pkey,
                    'envKey': ekey,
                    'envName': (cur['env'].get('name') or ekey).strip(),
                }
    
    return tag_changes


def build_stage_repo_info(tag_changes: Dict[Tuple[str, str, str], dict], projects: List[dict]) -> Dict[str, Dict[str, dict]]:
    """Build stage -> repo -> {branch, deployedAt, tag} mapping (only for actual tag changes)."""
    stage_repo_info: Dict[str, Dict[str, dict]] = {"DEV": {}, "QA": {}, "UAT": {}, "PROD": {}}
    
    def _pick_better(existing: Optional[dict], candidate: dict) -> dict:
        """Pick the entry with the newest deployedAt; fallback to existing."""
        if not existing:
            return candidate
        e_ts = (existing.get("deployedAt") or "").strip()
        c_ts = (candidate.get("deployedAt") or "").strip()
        if c_ts and (not e_ts or c_ts > e_ts):
            return candidate
        return existing
    
    # Build stage -> repo mapping from tag changes
    for key, change_info in tag_changes.items():
        pkey, ekey, cname = key
        # Find environment to get stage
        for proj in projects:
            if (proj.get("key") or "").strip() != pkey:
                continue
            for env in proj.get("environments", []):
                if (env.get("key") or "").strip().lower() != ekey:
                    continue
                stage = _env_to_stage(env.get("name"))
                repo = change_info.get("repo")
                if repo:
                    entry = {
                        "branch": change_info.get("branch", ""),
                        "deployedAt": change_info.get("deployedAt", ""),
                        "tag": change_info.get("tag", ""),
                        "component": cname,
                        "projectKey": pkey,
                        "envKey": ekey,
                    }
                    stage_repo_info[stage][repo] = _pick_better(stage_repo_info[stage].get(repo), entry)
                break
            break
    
    return stage_repo_info


def diagnose_ticket_deployment(
    ticket_key: str,
    ticket: dict,
    stage_repo_info: Dict[str, Dict[str, dict]],
    tag_changes: Dict[Tuple[str, str, str], dict],
    prev_snapshot: Optional[dict],
) -> dict:
    """Diagnose deployment presence for a single ticket.
    
    Returns diagnostic report for the ticket.
    """
    prs = ticket.get("pullRequests") or ticket.get("prs") or []
    
    # Extract PR information
    pr_info = []
    for pr in prs:
        pr_info.append({
            "repo": (pr.get("repo") or pr.get("repository") or "").strip(),
            "branch": _normalize_branch_name(pr.get("baseRef") or pr.get("base") or pr.get("baseBranch") or ""),
            "mergedAt": (pr.get("mergedAt") or pr.get("merged_at") or "").strip(),
            "number": pr.get("number") or pr.get("pr") or "",
        })
    
    # Diagnose each stage
    env_presence = {}
    stage_order = ["DEV", "QA", "UAT", "PROD"]
    
    for stage in stage_order:
        presence_info = {"present": False, "reason": "", "details": {}}
        
        # Check if we have tag changes at all
        if not prev_snapshot:
            presence_info["present"] = False
            presence_info["reason"] = "First snapshot – no previous snapshot to compare tags"
            env_presence[stage] = presence_info
            continue
        
        if not tag_changes:
            presence_info["present"] = False
            presence_info["reason"] = "No tag changes detected between snapshots"
            env_presence[stage] = presence_info
            continue
        
        # Check if we have deployment info for this stage
        repo_map = stage_repo_info.get(stage, {})
        if not repo_map:
            presence_info["present"] = False
            presence_info["reason"] = f"No tag changes detected for any component in {stage}"
            env_presence[stage] = presence_info
            continue
        
        # Check each PR against stage deployments
        pr_matched = False
        for pr in pr_info:
            repo = pr.get("repo")
            if not repo:
                continue
            
            # Check if this repo has deployments in this stage
            stage_info = repo_map.get(repo)
            if not stage_info:
                continue  # No deployment for this repo in this stage
            
            deployed_at = stage_info.get("deployedAt", "").strip()
            deployed_branch = stage_info.get("branch", "").strip()
            pr_branch = pr.get("branch", "").strip()
            pr_merged_at = pr.get("mergedAt", "").strip()
            
            # Check deployedAt timestamp
            if not deployed_at:
                presence_info["present"] = False
                presence_info["reason"] = f"Component missing deployedAt timestamp for {repo} in {stage}"
                presence_info["details"] = {
                    "repo": repo,
                    "component": stage_info.get("component", ""),
                    "tag": stage_info.get("tag", ""),
                }
                env_presence[stage] = presence_info
                continue
            
            # Check PR merge timestamp
            if not pr_merged_at:
                continue  # Skip PRs without merge timestamp
            
            pr_merged_dt = _parse_iso(pr_merged_at)
            deployed_dt = _parse_iso(deployed_at)
            
            if not deployed_dt:
                presence_info["present"] = False
                presence_info["reason"] = f"Invalid deployedAt timestamp format for {repo} in {stage}"
                env_presence[stage] = presence_info
                continue
            
            if not pr_merged_dt:
                continue  # Skip PRs with invalid merge timestamp
            
            # Time constraint: deployment must be after PR merge
            if deployed_dt < pr_merged_dt:
                presence_info["present"] = False
                presence_info["reason"] = f"Deployment timestamp ({deployed_at}) < PR mergedAt ({pr_merged_at})"
                presence_info["details"] = {
                    "repo": repo,
                    "deployedAt": deployed_at,
                    "prMergedAt": pr_merged_at,
                    "timeDiff": str(deployed_dt - pr_merged_dt),
                }
                env_presence[stage] = presence_info
                continue
            
            # Branch matching
            if pr_branch and deployed_branch:
                if pr_branch == deployed_branch:
                    # Exact branch match - high confidence
                    presence_info["present"] = True
                    presence_info["reason"] = f"Deployment detected: exact branch match ({pr_branch})"
                    presence_info["details"] = {
                        "repo": repo,
                        "branch": pr_branch,
                        "tag": stage_info.get("tag", ""),
                        "deployedAt": deployed_at,
                        "component": stage_info.get("component", ""),
                    }
                    pr_matched = True
                    break
                else:
                    # Branch mismatch - check time gap (promotion scenario)
                    time_diff = (deployed_dt - pr_merged_dt).total_seconds()
                    if time_diff >= 86400:  # At least 24 hours
                        presence_info["present"] = True
                        presence_info["reason"] = f"Deployment detected: branch mismatch but significant time gap (promotion scenario)"
                        presence_info["details"] = {
                            "repo": repo,
                            "prBranch": pr_branch,
                            "deployedBranch": deployed_branch,
                            "timeDiffHours": time_diff / 3600,
                            "tag": stage_info.get("tag", ""),
                            "deployedAt": deployed_at,
                        }
                        pr_matched = True
                        break
                    else:
                        # Branch mismatch + recent deployment = likely false positive
                        presence_info["present"] = False
                        presence_info["reason"] = f"Branch mismatch: PR branch ({pr_branch}) != deployed branch ({deployed_branch}), and time gap < 24h"
                        presence_info["details"] = {
                            "repo": repo,
                            "prBranch": pr_branch,
                            "deployedBranch": deployed_branch,
                            "timeDiffHours": time_diff / 3600,
                        }
                        env_presence[stage] = presence_info
                        continue
            else:
                # Missing branch info - be conservative (within 3 days)
                time_diff = (deployed_dt - pr_merged_dt).total_seconds()
                if time_diff <= 259200:  # Within 3 days
                    presence_info["present"] = False
                    presence_info["reason"] = f"Deployment detected but branch info missing (conservative: within 3 days)"
                    presence_info["details"] = {
                        "repo": repo,
                        "tag": stage_info.get("tag", ""),
                        "deployedAt": deployed_at,
                        "timeDiffHours": time_diff / 3600,
                    }
                    pr_matched = True
                    break
                else:
                    presence_info["present"] = False
                    presence_info["reason"] = f"Deployment too old (>3 days) and branch info missing"
                    presence_info["details"] = {
                        "repo": repo,
                        "deployedAt": deployed_at,
                        "prMergedAt": pr_merged_at,
                        "timeDiffDays": time_diff / 86400,
                    }
                    env_presence[stage] = presence_info
                    continue
        
        # If no PR matched, mark as not present
        if not pr_matched and stage not in env_presence:
            presence_info["present"] = False
            presence_info["reason"] = f"No PR matched deployment criteria for {stage}"
            presence_info["details"] = {
                "availableRepos": list(repo_map.keys()),
                "prRepos": [pr.get("repo") for pr in pr_info if pr.get("repo")],
            }
            env_presence[stage] = presence_info
    
    # Generate summary
    present_stages = [s for s, info in env_presence.items() if info.get("present")]
    if present_stages:
        summary = f"Deployed to: {', '.join(present_stages)}"
    else:
        summary = "No environments detected"
    
    return {
        "ticketKey": ticket_key,
        "prs": pr_info,
        "envPresence": env_presence,
        "summary": summary,
    }


def get_top_tag_changes(tag_changes: Dict[Tuple[str, str, str], dict], limit: int = 5) -> List[dict]:
    """Get top N tag changes sorted by deployedAt (newest first)."""
    changes_list = []
    for key, info in tag_changes.items():
        changes_list.append({
            "project": info.get("projectKey", ""),
            "environment": info.get("envName", ""),
            "component": info.get("component", ""),
            "repo": info.get("repo", ""),
            "fromTag": info.get("fromTag", ""),
            "toTag": info.get("toTag", ""),
            "deployedAt": info.get("deployedAt", ""),
        })
    
    # Sort by deployedAt (newest first)
    changes_list.sort(key=lambda x: x.get("deployedAt", ""), reverse=True)
    return changes_list[:limit]


def main():
    parser = argparse.ArgumentParser(description="Diagnose ticket deployment presence")
    parser.add_argument("--project", help="Filter by project key")
    parser.add_argument("--env", help="Filter by environment key")
    parser.add_argument("--date-from", help="Filter by date from (ISO format)")
    parser.add_argument("--date-to", help="Filter by date to (ISO format)")
    parser.add_argument("--output", choices=["json", "table"], default="json", help="Output format")
    args = parser.parse_args()
    
    # Load snapshots
    print("[INFO] Loading snapshots...", file=sys.stderr)
    current_snapshot = load_latest_snapshot()
    if not current_snapshot:
        print("[ERROR] latest.json not found", file=sys.stderr)
        sys.exit(1)
    
    prev_snapshot = load_previous_snapshot()
    if not prev_snapshot:
        print("[WARN] No previous snapshot found - will mark all as 'First snapshot'", file=sys.stderr)
    
    # Detect tag changes
    print("[INFO] Detecting tag changes...", file=sys.stderr)
    tag_changes = detect_tag_changes(prev_snapshot, current_snapshot)
    print(f"[INFO] Found {len(tag_changes)} tag changes", file=sys.stderr)
    
    # Build stage -> repo mapping
    projects = current_snapshot.get("projects", [])
    stage_repo_info = build_stage_repo_info(tag_changes, projects)
    
    # Get tickets
    ticket_index = current_snapshot.get("ticketIndex", {})
    if not ticket_index:
        print("[WARN] No tickets found in ticketIndex", file=sys.stderr)
        sys.exit(0)
    
    print(f"[INFO] Analyzing {len(ticket_index)} tickets...", file=sys.stderr)
    
    # Diagnose each ticket
    diagnostics = []
    for ticket_key, ticket in ticket_index.items():
        # Apply filters
        if args.project:
            # Check if ticket has PRs in the specified project
            prs = ticket.get("prs") or ticket.get("pullRequests") or []
            ticket_projects = set()
            for pr in prs:
                # Try to match project from component data
                repo = (pr.get("repo") or "").strip()
                for proj in projects:
                    for env in proj.get("environments", []):
                        for comp in env.get("components", []):
                            comp_repo = (comp.get("repo") or comp.get("repository") or "").strip()
                            if comp_repo == repo:
                                ticket_projects.add(proj.get("key", ""))
            if args.project not in ticket_projects:
                continue
        
        diagnostic = diagnose_ticket_deployment(
            ticket_key,
            ticket,
            stage_repo_info,
            tag_changes,
            prev_snapshot,
        )
        diagnostics.append(diagnostic)
    
    # Generate report
    report = {
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "snapshotInfo": {
            "currentSnapshot": current_snapshot.get("generatedAt", ""),
            "previousSnapshot": prev_snapshot.get("generatedAt", "") if prev_snapshot else None,
            "tagChangesCount": len(tag_changes),
            "ticketsAnalyzed": len(diagnostics),
        },
        "topTagChanges": get_top_tag_changes(tag_changes, 5),
        "tickets": diagnostics,
    }
    
    # Output
    if args.output == "json":
        print(json.dumps(report, indent=2))
    else:
        # Table format
        print("\n" + "=" * 100)
        print("TICKET DEPLOYMENT DIAGNOSTIC REPORT")
        print("=" * 100)
        print(f"\nGenerated: {report['generatedAt']}")
        print(f"Current Snapshot: {report['snapshotInfo']['currentSnapshot']}")
        print(f"Previous Snapshot: {report['snapshotInfo']['previousSnapshot'] or 'None (first snapshot)'}")
        print(f"Tag Changes Detected: {report['snapshotInfo']['tagChangesCount']}")
        print(f"Tickets Analyzed: {report['snapshotInfo']['ticketsAnalyzed']}")
        
        print("\n" + "-" * 100)
        print("TOP 5 TAG CHANGES")
        print("-" * 100)
        for i, change in enumerate(report['topTagChanges'], 1):
            print(f"\n{i}. {change['project']} / {change['environment']} / {change['component']}")
            print(f"   Repo: {change['repo']}")
            print(f"   Tag: {change['fromTag']} → {change['toTag']}")
            print(f"   Deployed: {change['deployedAt']}")
        
        print("\n" + "=" * 100)
        print("TICKET DIAGNOSTICS")
        print("=" * 100)
        
        for diag in diagnostics:
            print(f"\n{'=' * 100}")
            print(f"TICKET: {diag['ticketKey']}")
            print(f"Summary: {diag['summary']}")
            print(f"\nPRs:")
            for pr in diag['prs']:
                print(f"  - Repo: {pr['repo']}, Branch: {pr['branch']}, Merged: {pr['mergedAt']}")
            
            print(f"\nEnvironment Presence:")
            for stage in ["DEV", "QA", "UAT", "PROD"]:
                info = diag['envPresence'].get(stage, {})
                status = "✅ Present" if info.get("present") else "❌ Not present"
                reason = info.get("reason", "Unknown")
                print(f"  {stage}: {status}")
                print(f"    Reason: {reason}")
                if info.get("details"):
                    details = info.get("details", {})
                    for k, v in details.items():
                        print(f"    {k}: {v}")


if __name__ == "__main__":
    main()
