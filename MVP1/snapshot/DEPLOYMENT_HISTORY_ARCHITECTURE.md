# Deterministic Ticket Tracker Deployment Detection Architecture

## Overview

This document describes the implementation of a **deterministic, persistent deployment detection system** for Ticket Tracker that ensures deployment visibility remains stable across snapshots.

## Core Principles

1. **Source of Truth**: GitHub infra kustomization tag-change history (tag changes = deployments)
2. **Deterministic Correlation**: PR merge commits must be reachable from deployed tags
3. **Persistent State**: Once deployed, stay deployed (unless rollback detected)
4. **History-Driven**: Compute environment presence from deployment history, not just current snapshot

## Architecture

### 1. Deployment Event Log (Append-Only)

**Storage**: `data/deployment_history/events.jsonl`

**Format**: One JSON object per line (same pattern as release_history)

```json
{
  "id": "deploy:sha:project:env:component:tag",
  "kind": "DEPLOYMENT",
  "projectKey": "PO1_B2C",
  "envKey": "qa",
  "envName": "QA",
  "component": "component-name",
  "repo": "repo-name",
  "fromTag": "v1.0.0",
  "toTag": "v1.0.1",
  "deployedAt": "2026-01-23T10:00:00Z",
  "deployer": "username",
  "commitUrl": "https://github.com/...",
  "kustomizationUrl": "https://github.com/...",
  "tagSha": "abc123...",  // SHA of the tag/commit that was deployed
  "at": "2026-01-23T10:00:00Z"
}
```

**Collection**: 
- Reuse `compute_tag_change_events()` from release history
- Store deployment events separately (focused on ticket correlation)
- Append-only, never delete

### 2. Ticket → Deployment Correlation

**Function**: `correlate_tickets_to_deployments()`

**Logic**:
- For each deployment event, determine which tickets are included
- For each ticket PR:
  - Get PR merge commit SHA
  - Check if merge commit is reachable from deployed tag SHA
  - Use GitHub API: `/repos/{owner}/{repo}/compare/{tag}...{mergeSha}`
- Store correlation: `deployment_id -> [ticket_keys]`

**Deterministic Rules**:
- PR merge commit must be in deployed tag's commit history
- Time validation: `deployedAt >= mergedAt` (deployment must be after merge)
- Branch validation: If branch info available, prefer exact match

### 3. Environment Presence Calculation

**Function**: `compute_ticket_environment_presence_from_history()`

**Logic**:
1. Load all deployment events from history (from `events.jsonl`)
2. For each ticket:
   - Find all deployments that include this ticket (from correlation)
   - Group by environment
   - For each environment, find the **latest** deployment
   - Mark as present if deployment exists
   - Only mark as absent if rollback detected (tag goes backwards)

**Rollback Detection**:
- If current tag < previous tag (semantic versioning or build number)
- Or if tag SHA goes backwards in commit history
- Mark environment as absent (rollback occurred)

### 4. Integration with Current Snapshot

**Function**: `add_env_presence_to_ticket_index()` (enhanced)

**Flow**:
1. Compute current snapshot deployments (existing logic)
2. Load deployment history and compute historical presence
3. **Merge Strategy**:
   - If current snapshot shows deployment → use it (latest)
   - If current snapshot doesn't show deployment but history does → keep historical
   - Only remove if rollback detected

**Result**: Deterministic, persistent deployment visibility

## Data Flow

```
Snapshot Run:
├─ Compute tag changes (existing: compute_tag_change_events)
├─ Store deployment events to events.jsonl (append-only)
├─ Correlate tickets to deployments (new: correlate_tickets_to_deployments)
│  └─ For each deployment: find which tickets are included
├─ Load deployment history (from events.jsonl)
├─ Compute environment presence from history (new: compute_ticket_environment_presence_from_history)
│  └─ For each ticket: find latest deployment per environment
├─ Merge with current snapshot deployments
│  └─ Once deployed, stay deployed (unless rollback)
└─ Write to latest.json (ticket data includes persistent deployment state)
```

## Implementation Details

### File Structure

```
data/
├── deployment_history/
│   ├── events.jsonl          # Append-only deployment events
│   ├── index.json            # Metadata index (optional, for performance)
│   └── archive/              # Archived events (optional)
└── release_history/          # Existing release history (separate)
```

### Key Functions

1. **`store_deployment_events()`**: Store tag changes as deployment events
2. **`load_deployment_history()`**: Load all deployment events from history
3. **`correlate_tickets_to_deployments()`**: Determine which tickets are in which deployments
4. **`compute_ticket_environment_presence_from_history()`**: Compute persistent environment presence
5. **`merge_deployment_presence()`**: Merge historical and current snapshot presence

### Performance Considerations

- **Lazy Loading**: Only load deployment history when needed
- **Caching**: Cache ticket→deployment correlations (computed once per snapshot)
- **Incremental**: Only process new deployment events (append-only)
- **Index**: Optional index.json for fast filtering (similar to release_history)

## Acceptance Criteria

### Scenario 1: First Snapshot
- **Input**: First snapshot run, no previous history
- **Expected**: Deployment events stored, environment presence computed from current snapshot
- **Result**: ✅ Meaningful deployment state shown

### Scenario 2: No New Deployments
- **Input**: Snapshot run with no tag changes
- **Expected**: Previous deployment state preserved
- **Result**: ✅ Deployments do not disappear

### Scenario 3: TeamCity Down
- **Input**: TeamCity unavailable, but GitHub data available
- **Expected**: Deployment detection still works (uses GitHub tag changes)
- **Result**: ✅ Correctness maintained (TeamCity is enrichment only)

### Scenario 4: Rollback Detection
- **Input**: Tag goes backwards (v1.0.1 → v1.0.0)
- **Expected**: Environment marked as absent (rollback)
- **Result**: ✅ Rollback correctly detected

### Scenario 5: Multiple Deployments
- **Input**: Ticket deployed to DEV, then QA, then UAT
- **Expected**: All environments show as present
- **Result**: ✅ Full deployment history visible

## Migration Strategy

1. **Backward Compatible**: Existing snapshot logic remains unchanged
2. **Additive**: New functions added, existing functions enhanced
3. **Gradual**: Can be enabled via feature flag if needed
4. **No Data Loss**: Existing deployment data preserved

## Testing

- Unit tests for correlation logic
- Integration tests for history loading
- Acceptance tests for all scenarios above
- Performance tests for large history files
