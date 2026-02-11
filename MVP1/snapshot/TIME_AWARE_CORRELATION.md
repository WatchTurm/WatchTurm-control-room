# Time-Aware, Deterministic Ticket → Release → Deployment Correlation

## Overview

This document describes the **time-aware, deterministic correlation engine** that produces real, trustworthy ticket histories suitable for enterprise SaaS usage. The system is fact-based, explainable, and auditable.

## Problem Statement

### ❌ Previous Issues

The original system correlated:
- Jira tickets → GitHub PRs
- PRs → branches/tags
- Tags → deployments

**BUT** it only checked commit reachability and did NOT properly account for **time**.

**Result:**
- Tickets appeared as "included in" many branches/releases even if those branches were created BEFORE the PR was merged
- Some tickets showed dozens of `included in release/*` entries, which is logically impossible
- Environment presence (DEV/QA/UAT/PROD) was often missing or unreliable

This made the data misleading and not commercially viable.

## Solution: Time-Aware Correlation

### 🧠 Mental Model

**Reachability alone is NOT enough.  
Time without reachability is NOT enough.  
Only reachability + time together define truth.**

All correlations MUST respect real-world delivery timelines.

## Logical Rules

### 1️⃣ PR → Branch Inclusion (GitHub)

A branch MAY contain a PR ONLY IF:

```
branch.createdAt >= pr.mergedAt
AND
mergeSha ∈ branch
```

**Formal rule:**
- The PR merge commit is reachable from the branch
- **AND** the branch was created at or after the PR was merged

**If a branch was created before the PR merge:**
- ❌ it MUST be ignored, even if Git allows the commit to appear reachable
- This prevents false "included in release" noise

### 2️⃣ PR → Build Inclusion (TeamCity)

A build MAY contain a PR ONLY IF:

```
build.startedAt >= pr.mergedAt
```

**Example:**
- If a PR was merged at 13:00:
  - Build at 12:55 ❌ cannot contain it
  - Build at 13:01 ✅ can contain it

**Applies to:**
- `main`
- `release/*`
- `hotfix/*`
- Any branch

### 3️⃣ Build → Deployment (TeamCity)

A deployment is valid ONLY IF:

```
deployment.at >= build.finishedAt
```

**Requirements:**
- Deployments MUST be tied to:
  - A concrete build
  - A concrete tag or artifact
  - A real timestamp

**No timestamp = no deployment fact.**

### 4️⃣ Environment Presence is Build-Driven, NOT Branch-Driven

**This is CRITICAL.**

The same PR can reach environments via DIFFERENT branches at DIFFERENT times:

**Example:**
- PR merged to `main` at 13:00
- Build from `main` deployed to DEV at 13:10
- Later, `release/0.1.0` branched from `main` at 13:15
- Build from `release/0.1.0` deployed to QA at 13:25

**Correct outcome:**
- DEV: present at 13:10 (from `main` build)
- QA: present at 13:25 (from `release/0.1.0` build)

Even though branches differ.

**Therefore:**
- ❌ Do NOT infer env presence from branches
- ✅ Infer env presence ONLY from builds + deployments

## Implementation

### Architecture

The time-aware correlation is implemented as an **additive, isolated module** that does NOT rewrite existing logic.

**Location:** `MVP1/snapshot/snapshot.py`

**Functions:**
- `correlate_prs_with_branches_time_aware()` - PR → Branch correlation with time validation
- `correlate_prs_with_builds_time_aware()` - PR → Build correlation with time validation
- `correlate_builds_with_deployments_time_aware()` - Build → Deployment correlation with time validation
- `enrich_ticket_index_time_aware()` - Main enrichment function

### Data Sources

**GitHub:**
- PR `mergedAt` timestamp
- PR `mergeSha` (merge commit SHA)
- Branch `createdAt` (approximated from commit date of branch tip)
- Tag `commitDate` (from tag commit)

**TeamCity:**
- Build `startDate` / `startedAt`
- Build `finishDate` / `finishedAt`
- Build `branchName`
- Build `buildTypeId` and `number`

**Components:**
- Component `deployedAt` (deployment timestamp)
- Component `tag` (deployed tag)
- Component `build` / `buildNumber`
- Component `repo` (repository)

### New Ticket Fields

**Additive fields (do not remove existing):**
- `ticket.timeAwareBranches[]` - Time-validated branch inclusions
- `ticket.timeAwareBuilds[]` - Time-validated build correlations
- `ticket.timeAwareDeployments[]` - Time-validated deployment correlations

**Enhanced fields:**
- `ticket.envPresenceMeta[stage].confidence` - "high" | "medium" | "low"
- `ticket.envPresenceMeta[stage].source` - "time_aware_build" | legacy
- `ticket.timeline[].timeAware` - `true` | `false` flag

### Feature Flag

**Environment Variable:** `TICKET_HISTORY_TIME_AWARE`

**Default:** `1` (enabled)

**Usage:**
```bash
# Enable (default)
export TICKET_HISTORY_TIME_AWARE=1

# Disable
export TICKET_HISTORY_TIME_AWARE=0
```

**Location:** `MVP1/snapshot/snapshot.py` line ~3463

## Safety & Fail Closed

### Missing Data Handling

**If any timestamp is missing:**
- Mark correlation as unknown
- Do NOT invent or assume
- Exclude from results (fail closed)

**Examples:**
- Branch creation date missing → branch excluded
- Build start date missing → build excluded
- Deployment timestamp missing → deployment excluded

### Error Handling

- If time-aware correlation fails:
  - Log warning: `[WARN] Ticket tracker: failed time-aware correlation: {error}`
  - Do NOT block snapshot
  - Do NOT remove existing ticket data
  - Fall back to legacy correlation (if available)

### Backward Compatibility

- Existing fields are preserved
- Legacy correlation still runs (if `TICKET_HISTORY_ADVANCED=1`)
- UI continues to render even if new fields are missing
- Timeline events marked with `timeAware: true/false` flag

## Timeline Structure

Each ticket ends up with a **ticket-centric event stream**:

```json
{
  "timeline": [
    {
      "stage": "PR merged",
      "at": "2026-01-20T13:00:00Z",
      "ref": "main",
      "source": "repo#123",
      "type": "pr_merge"
    },
    {
      "stage": "Included in release/0.1.0",
      "at": "2026-01-20T13:15:00Z",
      "ref": "release/0.1.0",
      "source": "repo",
      "type": "branch",
      "timeAware": true
    },
    {
      "stage": "Build 456",
      "at": "2026-01-20T13:05:00Z",
      "ref": "v0.0.121",
      "source": "repo",
      "type": "build",
      "timeAware": true,
      "finishedAt": "2026-01-20T13:08:00Z"
    },
    {
      "stage": "Deployed to DEV",
      "at": "2026-01-20T13:10:00Z",
      "ref": "v0.0.121",
      "source": "component-name",
      "type": "deployment",
      "timeAware": true,
      "build": "456"
    }
  ]
}
```

**Timeline MUST be:**
- Chronologically ordered
- Reproducible
- Explainable

## Environment Presence Logic

### Build-Driven Detection

For each environment:
- First valid deployment = ticket entered environment
- Subsequent deployments = promotions / redeploys

**Store:**
```json
{
  "envPresence": {
    "DEV": true,
    "QA": true,
    "UAT": false,
    "PROD": false
  },
  "envPresenceMeta": {
    "DEV": {
      "when": "2026-01-20T13:10:00Z",
      "build": "456",
      "tag": "v0.0.121",
      "branch": "",
      "component": "component-name",
      "confidence": "high",
      "source": "time_aware_build"
    }
  }
}
```

### Confidence Rules

- **HIGH**: merge → build → deploy all time-consistent
  - All timestamps present and in correct order
  - Build-driven (not branch-driven)
  
- **MEDIUM**: missing some metadata but order preserved
  - Some timestamps missing but remaining ones are consistent
  
- **LOW**: partial evidence, explicitly marked as such
  - Missing critical timestamps
  - Time order uncertain

## Integration with Existing System

### Execution Order

1. **Build ticket index** from GitHub PRs (`build_ticket_index_from_github`)
2. **Legacy branch/tag enrichment** (if `TICKET_HISTORY_ADVANCED=1`)
3. **Time-aware correlation** (if `TICKET_HISTORY_TIME_AWARE=1`) ← NEW
4. **Jira enrichment** (`enrich_ticket_index_with_jira`)
5. **Environment presence** (`add_env_presence_to_ticket_index`)
   - Now uses time-aware deployments if available
   - Falls back to legacy logic if time-aware unavailable

### Data Flow

```
GitHub PRs → Ticket Index
    ↓
Time-Aware Branch Correlation (branch.createdAt >= pr.mergedAt)
    ↓
Time-Aware Build Correlation (build.startedAt >= pr.mergedAt)
    ↓
Time-Aware Deployment Correlation (deployment.at >= build.finishedAt)
    ↓
Build-Driven Environment Presence
    ↓
Timeline + envPresence + envPresenceMeta
```

## Testing & Validation

### Verify Time-Aware Correlation

**Check `latest.json`:**
```bash
cat latest.json | jq '.ticketIndex["TCBP-4034"] | {
  timeAwareBranches,
  timeAwareBuilds,
  timeAwareDeployments,
  envPresence,
  envPresenceMeta
}'
```

**Expected:**
- `timeAwareBranches[]` - Only branches created at/after PR merge
- `timeAwareBuilds[]` - Only builds started at/after PR merge
- `timeAwareDeployments[]` - Only deployments at/after build finished
- `envPresence` - Accurate based on build-driven logic
- `envPresenceMeta[].confidence` - "high" | "medium" | "low"
- `envPresenceMeta[].source` - "time_aware_build" for time-validated

### Verify Timeline

**Check timeline events:**
```bash
cat latest.json | jq '.ticketIndex["TCBP-4034"].timeline[] | {
  stage,
  at,
  type,
  timeAware
}'
```

**Expected:**
- Events in chronological order
- `timeAware: true` for time-validated events
- No impossible branch inclusions (branches created before PR merge)
- Build events before deployment events

## Troubleshooting

### No Time-Aware Data

**Possible causes:**
1. Feature flag disabled: `TICKET_HISTORY_TIME_AWARE=0`
2. Missing timestamps in GitHub/TeamCity data
3. GitHub API rate limiting
4. TeamCity API unavailable

**Check:**
- Snapshot logs for `[WARN] Ticket tracker: failed time-aware correlation`
- Verify GitHub token has `repo` scope
- Verify TeamCity API is accessible

### Missing Branch Creation Dates

**Issue:** GitHub API doesn't directly provide branch creation dates.

**Solution:** We approximate using commit date of branch tip (best available proxy).

**Limitation:** If branch tip commit is older than actual branch creation, some valid branches may be excluded (fail closed - safe).

### Missing Build Start Dates

**Issue:** TeamCity build `startDate` field not populated.

**Solution:** Build is excluded from time-aware correlation (fail closed).

**Fallback:** Legacy correlation may still work if branch info is available.

## Summary

The time-aware correlation engine:

✅ **Respects time constraints** - Only includes branches/builds/deployments that are chronologically possible

✅ **Build-driven environment presence** - Not branch-driven, reflects actual deployment reality

✅ **Fail closed** - Missing data results in exclusion, not guessing

✅ **Additive and isolated** - Does not break existing functionality

✅ **Feature-flagged** - Can be disabled if needed

✅ **Explainable** - All correlations are deterministic and auditable

✅ **Enterprise-ready** - Suitable for SaaS onboarding and audits

This transforms the ticket tracking system from a heuristic system into a **deterministic, time-aware delivery truth engine**.
