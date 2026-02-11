# Ticket Deployment Diagnostic Tool

## Overview

The `diagnose_ticket_deployments.py` script is an enterprise-level diagnostic tool that analyzes snapshot data to determine why tickets do or don't have deployment presence on DEV, QA, UAT, PROD environments.

## Usage

### Basic Usage

```bash
# Generate JSON diagnostic report
python diagnose_ticket_deployments.py

# Generate table-formatted report
python diagnose_ticket_deployments.py --output table

# Filter by project
python diagnose_ticket_deployments.py --project TAP2

# Filter by environment
python diagnose_ticket_deployments.py --env qa

# Filter by date range
python diagnose_ticket_deployments.py --date-from 2026-01-01 --date-to 2026-01-31
```

### Command-Line Options

- `--project PROJECT`: Filter tickets by project key (e.g., TAP2, PO1V8, B2C)
- `--env ENV`: Filter by environment key (e.g., qa, uat, prod)
- `--date-from DATE`: Filter by date from (ISO format: YYYY-MM-DD)
- `--date-to DATE`: Filter by date to (ISO format: YYYY-MM-DD)
- `--output FORMAT`: Output format - `json` (default) or `table`

## How It Works

### 1. Data Loading

The tool loads:
- **Current snapshot**: `data/latest.json`
- **Previous snapshot**: Most recent file from `data/history/latest-*.json`

If no previous snapshot exists, all tickets are marked with reason: "First snapshot – no previous snapshot to compare tags"

### 2. Tag Change Detection

The tool detects tag changes using the same logic as Release History:
- Compares `prev_tag` vs `cur_tag` for each component in each environment
- Only tracks actual tag changes (`prev_tag != cur_tag`)
- Builds a map: `(project_key, env_key, component_name) -> {repo, fromTag, toTag, deployedAt, branch, ...}`

### 3. Stage Mapping

Environments are mapped to stages using `_env_to_stage()`:
- **DEV**: dev, alpha, beta, and other non-prod environments
- **QA**: qa, green
- **UAT**: uat
- **PROD**: prod

### 4. Ticket Analysis

For each ticket:
1. **Extract PRs**: repo, branch, mergedAt
2. **Match PRs to deployments**: 
   - PR repo → component repo
   - PR branch → component branch (with time-based relaxation)
   - Check `deployedAt >= PR mergedAt`
3. **Generate diagnostic**: Present/Not present with reason

### 5. Diagnostic Reasons

The tool provides specific reasons for why a ticket is **not present** in an environment:

1. **First snapshot** – no previous snapshot to compare tags
2. **No tag change** – no tag changes detected between snapshots
3. **No tag change for stage** – no tag changes for any component in this stage
4. **Missing deployedAt** – component missing `deployedAt` timestamp
5. **Time constraint violation** – deployment timestamp < PR mergedAt
6. **Branch mismatch** – PR branch != deployed branch, and time gap < 24h
7. **No PR match** – no PR matched deployment criteria

## Output Format

### JSON Output

```json
{
  "generatedAt": "2026-01-20T12:00:00Z",
  "snapshotInfo": {
    "currentSnapshot": "2026-01-20T10:00:00Z",
    "previousSnapshot": "2026-01-19T10:00:00Z",
    "tagChangesCount": 15,
    "ticketsAnalyzed": 100
  },
  "topTagChanges": [
    {
      "project": "TAP2",
      "environment": "QA",
      "component": "frontend",
      "repo": "tcbp-mfe-tour",
      "fromTag": "v0.18.0",
      "toTag": "v0.19.0",
      "deployedAt": "2026-01-20T08:00:00Z"
    }
  ],
  "tickets": [
    {
      "ticketKey": "TAP2-1256",
      "prs": [
        {
          "repo": "tcbp-mfe-tour",
          "branch": "feature/xyz",
          "mergedAt": "2026-01-14T13:00:00Z",
          "number": "123"
        }
      ],
      "envPresence": {
        "DEV": {
          "present": false,
          "reason": "No tag change detected for any component in DEV",
          "details": {}
        },
        "QA": {
          "present": true,
          "reason": "Deployment detected: exact branch match (feature/xyz)",
          "details": {
            "repo": "tcbp-mfe-tour",
            "branch": "feature/xyz",
            "tag": "v0.19.0",
            "deployedAt": "2026-01-20T08:00:00Z",
            "component": "frontend"
          }
        },
        "UAT": {
          "present": false,
          "reason": "First snapshot – no previous snapshot to compare tags",
          "details": {}
        },
        "PROD": {
          "present": false,
          "reason": "No tag changes detected between snapshots",
          "details": {}
        }
      },
      "summary": "Deployed to: QA"
    }
  ]
}
```

### Table Output

The table format provides a human-readable summary:

```
====================================================================================================
TICKET DEPLOYMENT DIAGNOSTIC REPORT
====================================================================================================

Generated: 2026-01-20T12:00:00Z
Current Snapshot: 2026-01-20T10:00:00Z
Previous Snapshot: 2026-01-19T10:00:00Z
Tag Changes Detected: 15
Tickets Analyzed: 100

----------------------------------------------------------------------------------------------------
TOP 5 TAG CHANGES
----------------------------------------------------------------------------------------------------

1. TAP2 / QA / frontend
   Repo: tcbp-mfe-tour
   Tag: v0.18.0 → v0.19.0
   Deployed: 2026-01-20T08:00:00Z

====================================================================================================
TICKET DIAGNOSTICS
====================================================================================================

====================================================================================================
TICKET: TAP2-1256
Summary: Deployed to: QA

PRs:
  - Repo: tcbp-mfe-tour, Branch: feature/xyz, Merged: 2026-01-14T13:00:00Z

Environment Presence:
  DEV: ❌ Not present
    Reason: No tag change detected for any component in DEV
  QA: ✅ Present
    Reason: Deployment detected: exact branch match (feature/xyz)
    repo: tcbp-mfe-tour
    branch: feature/xyz
    tag: v0.19.0
    deployedAt: 2026-01-20T08:00:00Z
    component: frontend
  UAT: ❌ Not present
    Reason: First snapshot – no previous snapshot to compare tags
  PROD: ❌ Not present
    Reason: No tag changes detected between snapshots
```

## Key Features

### 1. Tag Change Detection

- Uses the same logic as Release History (`prev_tag != cur_tag`)
- Only tracks actual tag changes (not just deployments)
- Builds comprehensive component map

### 2. Branch Matching

- **Exact match**: High confidence if PR branch == deployed branch
- **Time-based relaxation**: If time gap >= 24 hours, allows branch mismatch (promotion scenario)
- **Missing branch info**: Conservative approach (within 3 days)

### 3. Time Constraints

- Enforces `deployedAt >= PR mergedAt`
- Handles timezone-aware ISO timestamps
- Provides time difference in diagnostic details

### 4. Stage Mapping

- Uses the same `_env_to_stage()` function as snapshot.py
- Maps environments to DEV/QA/UAT/PROD consistently
- Handles edge cases (missing env names, color-based envs)

## Common Scenarios

### Scenario 1: First Snapshot

**Symptom**: All tickets show "First snapshot – no previous snapshot to compare tags"

**Cause**: No previous snapshot exists in `data/history/`

**Solution**: Run snapshot again after a deployment to create a baseline

### Scenario 2: No Tag Changes

**Symptom**: All tickets show "No tag changes detected between snapshots"

**Cause**: No components had tag changes between snapshots

**Solution**: This is correct behavior - no deployments occurred

### Scenario 3: Branch Mismatch

**Symptom**: Ticket shows "Branch mismatch: PR branch != deployed branch"

**Cause**: PR was merged to `main`, but deployment used `release/0.19.0`

**Solution**: If time gap >= 24 hours, this is likely a promotion scenario (handled automatically)

### Scenario 4: Missing deployedAt

**Symptom**: Ticket shows "Component missing deployedAt timestamp"

**Cause**: TeamCity integration didn't provide `deployedAt` for the component

**Solution**: Check TeamCity integration configuration

## Integration with Existing System

The diagnostic tool:
- ✅ Uses the same tag change detection logic as `snapshot.py`
- ✅ Uses the same `_env_to_stage()` mapping
- ✅ Reads the same snapshot format (`latest.json`, `history/*.json`)
- ✅ Provides detailed explanations for each diagnostic result
- ✅ Does NOT modify snapshot data (read-only)

## Troubleshooting

### No Previous Snapshot

If you see "First snapshot" for all tickets:
1. Check if `data/history/` directory exists
2. Check if any `latest-*.json` files exist
3. Run snapshot again to create a baseline

### No Tag Changes

If you see "No tag changes detected":
1. Verify that components actually changed tags between snapshots
2. Check `latest.json` for component tags
3. Compare with previous snapshot manually

### Missing deployedAt

If you see "Component missing deployedAt timestamp":
1. Check TeamCity integration in `snapshot.py`
2. Verify `teamcity_get_build_details()` returns `deployedAt`
3. Check component metadata in `latest.json`

## Example Workflow

1. **Run snapshot** to collect current state
2. **Deploy to QA** (triggers tag change)
3. **Run snapshot again** to capture tag change
4. **Run diagnostic tool**:
   ```bash
   python diagnose_ticket_deployments.py --output table
   ```
5. **Review diagnostics** to understand why tickets are/aren't deployed

## Summary

This diagnostic tool provides:
- ✅ Detailed analysis of ticket deployment presence
- ✅ Specific reasons for missing deployments
- ✅ Top tag changes summary
- ✅ JSON and table output formats
- ✅ Filtering by project, environment, date range
- ✅ Read-only (doesn't modify snapshot data)

Use this tool to understand why tickets do or don't show deployment presence in the Ticket Tracker UI.
