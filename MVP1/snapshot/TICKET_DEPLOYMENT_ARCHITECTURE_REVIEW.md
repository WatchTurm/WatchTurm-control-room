# Ticket Tracker Deployment Detection: Architecture Review & Diagnostic Report

## Executive Summary

**Observed Behavior**: Deployment information in Ticket Tracker appears and disappears non-deterministically between snapshots.

**Root Cause**: The system has **two independent deployment detection paths** that both have strict requirements. Neither path persists deployment correlation between snapshots-everything is recomputed from scratch each run. This creates a "snapshot-only" architecture where deployment visibility depends entirely on what changed between the current and previous snapshot.

---

## How It Works Today

### Architecture Overview

The deployment detection system operates in **two parallel paths**:

1. **Legacy Tag-Change Path** (primary, snapshot-dependent)
2. **Time-Aware Build-Driven Path** (secondary, requires TeamCity)

Both paths are **stateless**-they compute deployment correlation fresh on each snapshot run. No deployment history is persisted in ticket data between snapshots.

### Path 1: Legacy Tag-Change Detection

**Location**: `add_env_presence_to_ticket_index()` (lines 1282-1695)

**How It Works**:

1. **Requires Previous Snapshot**: 
   - Loads `prev_snapshot` via `load_previous_snapshot_from_history()` (line 4162)
   - If `prev_snapshot` is `None`, this path produces **zero deployments** (line 1307)

2. **Detects Tag Changes**:
   - Builds component maps from `prev_snapshot` and current `projects_out` (lines 1308-1309)
   - Compares tags: `prev_tag != cur_tag` (line 1318)
   - **Only tracks components where tags actually changed**

3. **Correlates to Tickets**:
   - For each ticket PR and each tag change:
     - Checks if `deployedAt >= mergedAt` (time validation, line 1443)
     - Checks if `deployed_branch == pr.baseRef` (branch matching, line 1452)
     - Marks environment as present if conditions met (line 1454)

4. **Output**:
   - Sets `ticket["envPresence"][stage] = True`
   - Sets `ticket["envPresenceMeta"][stage] = {when, repo, tag, branch}`
   - Adds timeline events: `{stage: "Deployed to DEV", type: "deployment", ...}` (lines 1676-1684)

**Critical Dependency**: 
- **Requires**: `prev_snapshot` exists AND tag changes detected
- **If no tag changes**: `tag_changes_by_key` is empty → no deployments detected
- **If first snapshot**: `prev_snapshot` is `None` → no deployments detected

### Path 2: Time-Aware Build-Driven Detection

**Location**: `enrich_ticket_index_time_aware()` (lines 1962-2090) + `add_env_presence_to_ticket_index()` (lines 1484-1653)

**How It Works**:

1. **Correlates PRs → Builds → Deployments**:
   - PR → Build: `build.startedAt >= pr.mergedAt` (line 1870)
   - Build → Deployment: `deployment.at >= build.finishedAt` (line 1944)
   - Stores in `ticket["timeAwareBuilds"]` and `ticket["timeAwareDeployments"]` (lines 2033-2090)

2. **Environment Presence**:
   - Uses `timeAwareDeployments` to mark environments (lines 1509-1547)
   - Maps deployments to environments via component location (lines 1522-1526)
   - Sets `envPresence[stage] = True` with build-driven metadata (line 1533)

3. **Timeline Events**:
   - Adds deployment events from `timeAwareDeployments` (lines 1628-1653)
   - Marks as `timeAware: true` for UI distinction

**Critical Dependencies**:
- **Requires**: TeamCity enabled (`teamcity_rest_base` and `teamcity_token`, line 2061)
- **Requires**: Components have `build`, `tag`, `deployedAt` fields populated
- **Requires**: TeamCity API returns build timestamps (`startedAt`, `finishedAt`)
- **If TeamCity unavailable**: This path produces **zero deployments**

### Data Flow Summary

```
Snapshot Run:
├─ Load prev_snapshot (or None)
├─ Build ticket_index from GitHub PRs
├─ [Optional] Enrich with branches/tags (TICKET_HISTORY_ADVANCED)
├─ [Optional] Enrich with time-aware correlation (TICKET_HISTORY_TIME_AWARE)
│  └─ Stores in: ticket["timeAwareBranches"], ["timeAwareBuilds"], ["timeAwareDeployments"]
├─ Add environment presence
│  ├─ Path 1 (Legacy): Compare prev_snapshot tags → current tags
│  │  └─ Only if prev_snapshot exists AND tag changes detected
│  └─ Path 2 (Time-aware): Use timeAwareDeployments from above
│     └─ Only if TeamCity enabled and build data available
└─ Write to latest.json (ticket data is snapshot-only, not persisted)
```

**Key Point**: Both paths are **stateless**. No deployment correlation is stored between snapshots. Each snapshot recomputes everything from scratch.

---

## How It Was Intended to Work

### Original Vision (From Documentation)

Based on `TICKET_HISTORY_ENHANCEMENTS.md` and `TIME_AWARE_CORRELATION.md`:

1. **Infer deployments from snapshot history**:
   - The system was intended to correlate PRs → deployments over time
   - Historical inference was mentioned as a goal

2. **AI/Heuristic validation layer**:
   - Documentation mentions an "AI reasoning layer" for validation
   - This appears to be in `web/app.js` (UI-side narrative generation), not in snapshot logic

3. **Time-aware correlation**:
   - Intended to be deterministic and time-validated
   - Build-driven (not branch-driven) environment presence
   - Should work independently of snapshot history

### Gap Between Intention and Reality

**What's Missing**:

1. **No Historical Persistence**:
   - Deployment correlation is **not stored** in ticket data between snapshots
   - Each snapshot starts fresh-no memory of previous deployments
   - The system cannot "remember" that a ticket was deployed to QA last week if tags haven't changed since then

2. **No Historical Inference**:
   - The system does **not** scan historical snapshots to infer deployments
   - It only compares `prev_snapshot` (the most recent one) to current
   - No logic exists to walk through `data/history/*.json` files to build deployment history

3. **Snapshot-Only Architecture**:
   - Everything is computed from current snapshot + previous snapshot
   - If a deployment happened 3 snapshots ago but tags haven't changed since, it disappears
   - The system treats each snapshot as an independent point-in-time view

---

## Why Deployments Appear and Disappear

### Scenario Analysis

**Scenario 1: Deployment Appears Then Disappears**

```
Snapshot 1 (baseline):
  - Component tag: v1.0.0
  - No prev_snapshot → no deployments detected

Snapshot 2:
  - Component tag: v1.0.1 (changed!)
  - prev_snapshot exists → tag change detected
  - Deployment to DEV detected → appears in UI

Snapshot 3:
  - Component tag: v1.0.1 (unchanged)
  - prev_snapshot exists BUT no tag changes
  - tag_changes_by_key is empty → no deployments detected
  - Deployment disappears from UI
```

**Why**: Legacy path requires tag changes. If tags don't change, deployments disappear.

**Scenario 2: Time-Aware Path Intermittent**

```
Snapshot 1:
  - TeamCity available → time-aware deployments detected
  - Deployments appear

Snapshot 2:
  - TeamCity API timeout/failure → time-aware correlation fails
  - No timeAwareDeployments → deployments disappear
  - Legacy path also fails (no tag changes)
  - Result: zero deployments
```

**Why**: Time-aware path depends on TeamCity availability and data quality.

**Scenario 3: First Snapshot After Deployment**

```
Snapshot 1:
  - Deployment happened yesterday
  - First snapshot run → prev_snapshot is None
  - Legacy path: no deployments (expected)
  - Time-aware path: might work if TeamCity data available

Snapshot 2:
  - prev_snapshot exists but tags haven't changed since Snapshot 1
  - Legacy path: no tag changes → no deployments
  - Time-aware path: might work if TeamCity data available
```

**Why**: System cannot detect deployments that happened before the first snapshot baseline.

### Root Cause Summary

**The non-deterministic behavior is caused by**:

1. **Legacy Path Dependency on Tag Changes**:
   - Only detects deployments when `prev_tag != cur_tag`
   - If tags are stable, deployments disappear
   - This is **by design** (same logic as Release History)

2. **No Persistence Between Snapshots**:
   - Deployment correlation is recomputed each run
   - Previous deployment state is not stored
   - System has no "memory" of past deployments

3. **Dual-Path Architecture**:
   - Two independent paths with different dependencies
   - If one path fails, the other might not compensate
   - No fallback or merging strategy

4. **Time-Aware Path Requires TeamCity**:
   - If TeamCity is unavailable or returns incomplete data, path fails silently
   - No graceful degradation

---

## Architectural Gap Analysis

### Current State: Snapshot-Only Architecture

```
Each Snapshot:
  ┌─────────────────────────────────┐
  │  Load prev_snapshot (or None)   │
  │  Compute tag changes             │
  │  Correlate to tickets            │
  │  Write to latest.json            │
  └─────────────────────────────────┘
         ↓
  No persistence of deployment state
         ↓
  Next Snapshot:
  ┌─────────────────────────────────┐
  │  Load prev_snapshot (or None)   │
  │  Compute tag changes             │  ← Starts fresh
  │  Correlate to tickets            │
  │  Write to latest.json            │
  └─────────────────────────────────┘
```

**Problem**: Each snapshot is independent. Deployment visibility depends entirely on what changed between snapshots.

### Intended State: History-Aware Architecture

```
Each Snapshot:
  ┌─────────────────────────────────┐
  │  Load prev_snapshot             │
  │  Load deployment history        │  ← Should exist
  │  Infer from history             │  ← Should exist
  │  Merge with current state        │  ← Should exist
  │  Persist deployment state        │  ← Should exist
  └─────────────────────────────────┘
```

**Gap**: The "infer from history" and "persist deployment state" layers do not exist.

---

## Would In-Memory/Derived State Make Sense?

### Question: Should deployment correlation be kept in memory or derived state?

**Answer: YES, but with important caveats.**

### Why It Makes Sense (MVP-Safe)

1. **Preserves Deployment Visibility**:
   - Once a deployment is detected, it should remain visible
   - Even if tags don't change in subsequent snapshots
   - Users need to see "this ticket was deployed to QA last week" even if nothing changed today

2. **Matches User Expectations**:
   - Users expect deployment information to be persistent
   - Non-deterministic appearance/disappearance is confusing
   - Historical context is valuable

3. **MVP-Safe Implementation**:
   - Can be implemented as **additive** enrichment
   - Does not require refactoring existing logic
   - Can merge with existing `envPresence` data

### Implementation Approach (MVP-Safe)

**Option 1: Persist in Ticket Data (Recommended)**

Store deployment state in ticket data structure:

```python
# In add_env_presence_to_ticket_index():
# After computing current deployments, merge with persisted state

# Load persisted deployment history (if exists)
persisted_deployments = ticket.get("deploymentHistory", {})

# Merge: current deployments override persisted, but don't remove old ones
for stage in ["DEV", "QA", "UAT", "PROD"]:
    current_presence = presence.get(stage, False)
    persisted_presence = persisted_deployments.get(stage, {}).get("present", False)
    
    # Once deployed, stay deployed (unless explicitly removed)
    if current_presence:
        presence[stage] = True
        # Update metadata with latest deployment
    elif persisted_presence:
        # Keep persisted state if current snapshot doesn't show deployment
        presence[stage] = True
        # Use persisted metadata
```

**Option 2: Derive from Snapshot History**

Scan historical snapshots to build deployment timeline:

```python
# Load all historical snapshots
history_files = _list_history_snapshots()

# For each ticket, walk through history
for snapshot_file in history_files:
    snapshot = json.loads(snapshot_file.read_text())
    # Extract deployment events from each snapshot
    # Build cumulative deployment state
```

**Option 3: Hybrid Approach (Best for MVP)**

1. **Current snapshot**: Compute deployments (existing logic)
2. **Previous ticket data**: Load from `prev_snapshot.ticketIndex` (if exists)
3. **Merge strategy**: 
   - If current snapshot shows deployment → use it
   - If current snapshot doesn't show deployment but previous did → keep previous
   - Only remove deployment state if explicitly invalidated (e.g., rollback detected)

### Why NOT to Do It (Counter-Arguments)

1. **Snapshot-Driven Philosophy**:
   - The project follows "snapshot.py is sacred" principle
   - Current architecture is snapshot-only by design
   - Adding persistence might violate core principles

2. **Data Consistency**:
   - Persisted state might become stale
   - Hard to invalidate old deployment data
   - Risk of showing incorrect deployment status

3. **Complexity**:
   - Adds state management complexity
   - Requires merge logic
   - Potential for bugs in state synchronization

### Recommendation

**YES, implement in-memory/derived state, but**:

1. **Use Option 3 (Hybrid)**:
   - Load previous ticket deployment state from `prev_snapshot.ticketIndex`
   - Merge with current snapshot deployments
   - Once deployed, stay deployed (unless rollback detected)

2. **Make it Additive**:
   - Don't refactor existing logic
   - Add merge step after `add_env_presence_to_ticket_index()`
   - Preserve all existing functionality

3. **Fail-Safe**:
   - If merge fails, fall back to current snapshot only
   - Log warnings but don't break snapshot pipeline
   - Maintain snapshot-driven core

4. **MVP-Safe**:
   - No mass refactoring
   - No removal of existing functionality
   - Only adds new merge logic

---

## Conclusion

### Current Behavior

- **Snapshot-only architecture**: Each snapshot recomputes deployment correlation from scratch
- **Two independent paths**: Legacy (tag-change) and time-aware (build-driven)
- **No persistence**: Deployment state is not stored between snapshots
- **Non-deterministic**: Deployment visibility depends on tag changes and TeamCity availability

### Intended Behavior

- **History-aware**: Should infer deployments from snapshot history
- **Persistent**: Deployment state should persist between snapshots
- **Deterministic**: Once deployed, should remain visible

### Architectural Gap

- **Missing**: Historical inference logic
- **Missing**: Deployment state persistence
- **Present but limited**: Time-aware correlation (requires TeamCity)

### Recommendation

**Implement hybrid persistence approach**:
- Load previous ticket deployment state from `prev_snapshot`
- Merge with current snapshot deployments
- Once deployed, stay deployed (unless rollback)
- Make it additive and MVP-safe

This would provide deterministic deployment visibility while maintaining the snapshot-driven core architecture.
