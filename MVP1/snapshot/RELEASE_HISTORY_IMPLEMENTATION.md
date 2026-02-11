# Release History Implementation Summary

## What Was Implemented

### Backend (snapshot.py)

1. **Append-Only Storage Functions**:
   - `_release_history_events_path()` - Path to `events.jsonl`
   - `_release_history_index_path()` - Path to `index.json`
   - `_append_events_to_jsonl()` - Append events to JSONL file
   - `_update_release_history_index()` - Update lightweight metadata index
   - `_apply_retention_policy()` - Archive events older than retention period
   - `_migrate_legacy_release_history()` - One-time migration from old format
   - `update_release_history_append_only()` - Main entry point for append-only storage

2. **Configuration**:
   - `RELEASE_HISTORY_RETENTION_DAYS` - Default: 90 days
   - `RELEASE_HISTORY_DEFAULT_LIMIT` - Default: 20 events
   - `RELEASE_HISTORY_APPEND_ONLY` - Feature flag (default: enabled)

3. **Integration**:
   - Main snapshot flow now uses append-only storage when enabled
   - Falls back to legacy format if disabled (backward compatibility)

## What Stays the Same

✅ **Event structure** - All fields preserved (id, kind, projectKey, envKey, component, fromTag, toTag, at, by, links, etc.)
✅ **Collection logic** - `compute_tag_change_events()` unchanged
✅ **Bootstrap logic** - Still runs on first snapshot
✅ **Event details** - Full metadata still accessible

## What's New

🆕 **Append-only storage** - JSONL format (`events.jsonl`)
🆕 **Metadata index** - Fast filtering (`index.json`)
🆕 **Retention policy** - Configurable, explicit (default: 90 days)
🆕 **Performance** - Constant time operations (O(new_events), not O(total_events))
🆕 **Migration** - Automatic one-time migration from legacy format

## File Structure

```
data/release_history/
  ├── events.jsonl          # Append-only event stream
  ├── index.json            # Metadata index
  └── archive/              # Archived events (older than retention period)
      └── events-2025-01.jsonl
```

## Next Steps (UI Updates)

The UI (`web/app.js`) needs to be updated to:

1. **Load index.json first** (lightweight, ~1KB)
2. **Show last 10-20 events by default** (tail operation on events.jsonl)
3. **Add advanced search mode** with:
   - Date range (from/to)
   - Environment filter
   - Project filter
   - Repository filter
   - Tag filter
   - Deployer filter

## Configuration

Set environment variables in `MVP1/.env`:

```bash
# Retention period (days)
RELEASE_HISTORY_RETENTION_DAYS=90

# Default view limit
RELEASE_HISTORY_DEFAULT_LIMIT=20

# Enable append-only storage (default: enabled)
RELEASE_HISTORY_APPEND_ONLY=1
```

## Migration

On first run with new code:
1. Checks if legacy `release_history.json` exists
2. If yes, converts to JSONL format
3. Generates `index.json`
4. Backs up old file to `release_history.json.backup`
5. Continues with new format

## Performance Improvements

- **Snapshot runtime**: O(new_events) instead of O(total_events)
- **UI load time**: ~1KB index.json + ~50KB last 20 events (vs. entire file)
- **Memory usage**: Constant (only active events in memory)

## Backward Compatibility

- Legacy format still supported (if `RELEASE_HISTORY_APPEND_ONLY=0`)
- Existing `release_history.json` automatically migrated
- All existing event data preserved
