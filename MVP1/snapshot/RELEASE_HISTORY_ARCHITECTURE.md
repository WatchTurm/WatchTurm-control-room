# Enterprise-Grade Release History Architecture

## Overview

This document describes the evolution of the Release History system from a simple JSON file to an enterprise-grade, scalable solution that supports real operational questions while preserving all existing functionality.

## Core Principle

**Release History is NOT a raw log. It is an interface for answering questions about past deployments.**

### Example Questions Users Must Answer

- "Who last deployed to QA for repo X?"
- "Were there any releases to QA between Jan 13–15?"
- "What changed since the last deployment to PROD?"
- "Which environments were affected by this release?"

## Architecture Constraints (Non-Negotiable)

1. ✅ **Snapshot pipeline must remain incremental** - No full reprocessing
2. ✅ **Release events must remain append-only** - No destructive migrations
3. ✅ **Existing event details view must keep working** - All metadata preserved
4. ✅ **No performance degradation** - Snapshot runtime, memory, disk usage

## Current State

### Storage
- **Format**: Single JSON file `data/release_history.json`
- **Structure**: `{ projects: { projectKey: { events: [], meta: {} } } }`
- **Max Events**: 2000 per project (hard limit)
- **Collection**: Incremental (compares prev vs current snapshot)
- **UI**: Loads entire file, filters in memory

### Limitations
- ❌ Entire history loaded into memory
- ❌ No retention policy (grows indefinitely until 2000 limit)
- ❌ UI renders all events (can be hundreds/thousands)
- ❌ No date range filtering
- ❌ Performance degrades as history grows

## New Architecture

### 1. Append-Only Storage (JSONL)

**Location**: `data/release_history/`

**Structure**:
```
data/release_history/
  ├── events.jsonl          # Append-only event stream (one JSON object per line)
  ├── index.json            # Metadata index for fast filtering
  └── archive/              # Archived chunks (optional, for retention)
      └── events-2025-01.jsonl
```

**Event Format** (one line per event):
```json
{"id":"...","kind":"TAG_CHANGE","projectKey":"PO1_B2C","envKey":"qa","at":"2026-01-23T09:01:40Z",...}
```

**Index Format** (`index.json`):
```json
{
  "version": "2.0",
  "generatedAt": "2026-01-23T11:17:47Z",
  "retention": {
    "days": 90,
    "lastCleanup": "2026-01-23T11:17:47Z"
  },
  "stats": {
    "totalEvents": 15234,
    "oldestEvent": "2025-10-25T08:12:00Z",
    "newestEvent": "2026-01-23T11:17:47Z"
  },
  "projects": {
    "PO1_B2C": {
      "eventCount": 3421,
      "firstEventAt": "2025-10-25T08:12:00Z",
      "lastEventAt": "2026-01-23T11:17:47Z",
      "environments": ["qa", "red", "uat", "prod"]
    }
  }
}
```

### 2. Collection Logic (Backend)

**Function**: `update_release_history_append_only()`

**Behavior**:
- ✅ Only appends new events (no reprocessing)
- ✅ Updates index.json metadata
- ✅ Runs retention cleanup (if needed)
- ✅ Never loads entire history into memory
- ✅ Snapshot runtime: O(new_events), not O(total_events)

**Implementation**:
```python
def update_release_history_append_only(current_payload, prev_snapshot, github_token):
    # 1. Compute new events (unchanged logic)
    new_events_by_project = compute_tag_change_events(prev_snapshot, current_payload)
    
    # 2. Append to JSONL file (one line per event)
    events_file = _release_history_events_path()
    with open(events_file, 'a', encoding='utf-8') as f:
        for project_key, events in new_events_by_project.items():
            for event in events:
                f.write(json.dumps(event) + '\n')
    
    # 3. Update index.json (lightweight, only metadata)
    _update_release_history_index(new_events_by_project)
    
    # 4. Retention cleanup (if needed, runs periodically)
    _apply_retention_policy()
```

### 3. Retention Policy

**Default**: 90 days

**Configuration**: `RELEASE_HISTORY_RETENTION_DAYS` (env var)

**Behavior**:
- Runs periodically (not every snapshot)
- Archives events older than retention period
- Updates index.json to reflect archived data
- Never deletes data silently (archives to `archive/` directory)
- Logs retention actions

**Implementation**:
```python
def _apply_retention_policy():
    retention_days = int(os.getenv("RELEASE_HISTORY_RETENTION_DAYS", "90"))
    cutoff_date = datetime.now(timezone.utc) - timedelta(days=retention_days)
    
    # Read events.jsonl, filter by date, write to archive
    # Update index.json
    # Log: "Archived 1234 events older than 90 days"
```

### 4. UI: Default View (Last 10-20 Events)

**Behavior**:
- Loads only `index.json` initially (lightweight)
- Fetches last N events from `events.jsonl` (tail operation)
- Shows "Recent Activity" section
- Answers: "What happened recently?"

**Implementation**:
```javascript
// Load index.json (fast, ~1KB)
const index = await fetch('data/release_history/index.json').then(r => r.json());

// Load last 20 events (tail operation, efficient)
const events = await fetchLastNEvents(20);

// Render
renderRecentActivity(events);
```

### 5. UI: Advanced Search Mode

**Behavior**:
- User clicks "Advanced Search" button
- Shows filter panel:
  - Date range (from/to)
  - Environment (DEV/QA/UAT/PROD)
  - Platform/Project
  - Repository
  - Tag
  - Deployer
- Fetches matching events from `events.jsonl` (streaming read)
- Answers: "What happened then?"

**Implementation**:
```javascript
async function searchReleaseHistory(filters) {
  const { dateFrom, dateTo, project, env, repo, tag, deployer } = filters;
  
  // Stream events.jsonl, filter in-memory (or use index for date range)
  const events = await streamEventsFromJSONL({
    dateFrom,
    dateTo,
    project,
    env,
    repo,
    tag,
    deployer
  });
  
  return events;
}
```

### 6. Backward Compatibility

**Migration Strategy**:
- On first run with new code:
  1. Check if `data/release_history.json` exists (old format)
  2. If yes, convert to JSONL format
  3. Generate `index.json`
  4. Keep old file as backup (`release_history.json.backup`)
  5. Continue with new format

**Code**:
```python
def _migrate_legacy_release_history():
    old_path = _repo_root() / 'data' / 'release_history.json'
    if not old_path.exists():
        return  # No migration needed
    
    # Read old format
    old_data = json.loads(old_path.read_text())
    
    # Convert to JSONL
    events_file = _release_history_events_path()
    with open(events_file, 'w', encoding='utf-8') as f:
        for project_key, project_data in old_data.get('projects', {}).items():
            events = project_data.get('events', [])
            for event in events:
                f.write(json.dumps(event) + '\n')
    
    # Generate index
    _generate_index_from_events()
    
    # Backup old file
    backup_path = old_path.with_suffix('.json.backup')
    old_path.rename(backup_path)
```

## Performance Guarantees

### Snapshot Runtime
- **Before**: O(total_events) - loads entire history, sorts, trims
- **After**: O(new_events) - only processes new events
- **Improvement**: Constant time regardless of history size

### UI Load Time
- **Before**: Loads entire JSON file (can be MBs)
- **After**: Loads index.json (~1KB) + last 20 events (~50KB)
- **Improvement**: 10-100x faster initial load

### Memory Usage
- **Before**: Entire history in memory
- **After**: Only active events in memory (default: 20, search: filtered set)
- **Improvement**: Constant memory usage

## Configuration

### Environment Variables

```bash
# Retention period (days)
RELEASE_HISTORY_RETENTION_DAYS=90

# Max events in default view
RELEASE_HISTORY_DEFAULT_LIMIT=20

# Enable new append-only storage (default: enabled)
RELEASE_HISTORY_APPEND_ONLY=1
```

## Migration Path

1. **Phase 1**: Add new append-only storage (parallel to existing)
2. **Phase 2**: Migrate existing data (one-time conversion)
3. **Phase 3**: Update UI to use new format
4. **Phase 4**: Remove old format (after verification)

## What Stays the Same

✅ **Event structure** - All fields preserved
✅ **Event details view** - Click to view full metadata
✅ **Collection logic** - `compute_tag_change_events()` unchanged
✅ **Bootstrap logic** - Still runs on first snapshot
✅ **Filtering** - Project, env, search query still work
✅ **Links** - Commit, kustomization, TeamCity URLs preserved

## What's Added

🆕 **Append-only storage** - JSONL format
🆕 **Metadata index** - Fast filtering
🆕 **Retention policy** - Configurable, explicit
🆕 **Default view limit** - Last 10-20 events
🆕 **Advanced search** - Date range, multiple filters
🆕 **Performance** - Constant time operations

## Forward Compatibility

The new architecture supports future enhancements:

- **AI queries**: "Who deployed to QA last Friday?" (natural language)
- **Analytics**: Deployment frequency, trends
- **Alerts**: "No deployments to PROD in 7 days"
- **Export**: CSV, JSON export for specific date ranges

All without changing backend storage or snapshot logic.

## Summary

This architecture transforms Release History from a simple log into an enterprise-grade system that:

- ✅ Answers real operational questions
- ✅ Scales to thousands of events
- ✅ Maintains constant performance
- ✅ Preserves all existing functionality
- ✅ Supports future enhancements
- ✅ Is audit-friendly and trustworthy
