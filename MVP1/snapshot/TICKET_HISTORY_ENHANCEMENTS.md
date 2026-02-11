# Ticket History Intelligence Layer

## Overview

This document describes the **additive, isolated, feature-flagged** ticket history intelligence layer that reconstructs the full lifecycle of Jira tickets across GitHub, TeamCity, and deployments.

## Architecture

### 1. Event Abstraction Layer

**Location**: `MVP1/snapshot/snapshot.py`

The system normalizes raw snapshot data into ticket-centric events:

- **PR Merge Events**: `{stage: "PR merged", at: mergedAt, ref: baseBranch, source: repo#PR}`
- **Branch/Release Events**: `{stage: "Included in release/0.20.0", type: "branch", ...}`
- **Tag Events**: `{stage: "Tagged as v0.0.121", type: "tag", ...}`
- **Deployment Events**: `{stage: "Deployed to QA", type: "deployment", at: deployedAt, ...}`

All events are stored in `ticket.timeline[]` and `ticket.envPresenceMeta{}`.

### 2. Deterministic Correlation Engine

**Location**: `MVP1/snapshot/snapshot.py`

**Functions**:
- `github_check_commit_in_branch()`: Determines if a PR merge commit is in a branch
- `github_list_branches()`: Lists branches for a repository
- `github_list_tags()`: Lists tags with commit dates
- `enrich_ticket_index_with_branches_and_tags()`: Correlates PRs with branches/tags

**Deterministic Rules**:
- Ticket → PRs: Via regex `TICKET_KEY_RE` on PR title/body (unchanged)
- PR → Branches: Uses GitHub `/compare/{branch}...{sha}` API
- PR → Tags: Checks if merge commit is reachable from tag
- Only tracks release-like branches (`release/*`, `main`, `master`) and version-like tags

### 3. Deployment Inference

**Location**: `MVP1/snapshot/snapshot.py` → `add_env_presence_to_ticket_index()`

**Confidence Scoring**:
- **High**: Exact branch match + deployment timestamp
- **Medium**: Branch mismatch but significant time gap (promotion scenario) OR missing branch info
- **Low**: Missing timestamp or conflicting evidence

Confidence is stored in `envPresenceMeta[stage].confidence`.

### 4. AI Reasoning Layer (Non-Authoritative)

**Location**: `web/app.js`

**Functions**:
- `buildTicketNarrative()`: Rule-based summarization of ticket lifecycle
- `buildTicketNarrativeSection()`: UI rendering of narrative

**Behavior**:
- **Never overwrites factual data** - only interprets existing `timeline` and `envPresence`
- **Validates claims** against raw data before making assertions
- **Exposes confidence levels** and validation warnings
- **Shows evidence** in expandable details section

## Feature Flags

### Snapshot (Backend)

**Environment Variable**: `TICKET_HISTORY_ADVANCED`

**Default**: `1` (enabled)

**Usage**:
```bash
# Enable (default)
export TICKET_HISTORY_ADVANCED=1

# Disable
export TICKET_HISTORY_ADVANCED=0
```

**Effect**: Controls branch/tag correlation enrichment. When disabled, ticket index is built without branch/tag tracking (falls back to basic PR tracking only).

**Location**: `MVP1/snapshot/snapshot.py` line ~3025

### UI (Frontend)

**Global Variable**: `window.TICKET_AI_ENABLED`

**Default**: `false` (disabled)

**Usage**:
```javascript
// Enable AI interpretation
window.TICKET_AI_ENABLED = true;

// Disable (default)
window.TICKET_AI_ENABLED = false;
```

**Effect**: Controls display of "AI interpretation" section in ticket cards. When disabled, ticket cards show only raw timeline (existing behavior).

**Location**: `web/app.js` line ~4383

## Data Model Extensions

### Ticket Index Structure

**Existing fields** (unchanged):
- `ticket.key`
- `ticket.prs[]`
- `ticket.envPresence{}`
- `ticket.timeline[]`

**New fields** (additive):
- `ticket.prs[].branches[]`: List of branches containing the PR
- `ticket.prs[].tags[]`: List of tags containing the PR
- `ticket.envPresenceMeta[stage].confidence`: "high" | "medium" | "low"

### Timeline Event Structure

**Existing**:
- `event.stage`: Event description
- `event.at`: Timestamp
- `event.ref`: Reference (branch/tag/version)
- `event.source`: Source repository

**New**:
- `event.type`: "branch" | "tag" | "deployment" | undefined (for PR merges)

## Safety & Validation

### Isolation

- All new logic is in isolated functions
- Feature flags allow complete disable without side effects
- Existing ticket index structure is preserved (only additive changes)

### Error Handling

- GitHub API failures are caught and logged as `[WARN]` messages
- Snapshot generation continues even if enrichment fails
- UI gracefully handles missing data (shows "No timeline" if empty)

### Validation

The AI reasoning layer validates its conclusions:

1. **Checks timeline events** before claiming deployments
2. **Compares envPresence flags** with deployment events
3. **Marks confidence levels** based on evidence quality
4. **Exposes validation warnings** when claims don't match raw data

## Usage Examples

### Example 1: Enable All Features

**Backend** (`.env`):
```
TICKET_HISTORY_ADVANCED=1
```

**Frontend** (browser console or config):
```javascript
window.TICKET_AI_ENABLED = true;
```

**Result**: Full ticket lifecycle tracking with AI interpretation.

### Example 2: Disable AI, Keep Correlation

**Backend**:
```
TICKET_HISTORY_ADVANCED=1
```

**Frontend**:
```javascript
window.TICKET_AI_ENABLED = false;
```

**Result**: Branch/tag correlation enabled, but no AI summary shown.

### Example 3: Minimal Mode (Existing Behavior)

**Backend**:
```
TICKET_HISTORY_ADVANCED=0
```

**Frontend**:
```javascript
window.TICKET_AI_ENABLED = false;
```

**Result**: Only basic PR tracking (original behavior).

## Testing

### Verify Feature Flags

1. **Backend**: Check snapshot logs for `[WARN] Ticket tracker: failed to enrich branches/tags` (should not appear if enabled)
2. **Frontend**: Check browser console for `TICKET_AI_ENABLED` value

### Verify Data Quality

1. Check `latest.json` → `ticketIndex[KEY].prs[].branches` (should exist if enabled)
2. Check `latest.json` → `ticketIndex[KEY].envPresenceMeta[STAGE].confidence` (should exist)
3. Check UI ticket cards for "AI interpretation" section (only if `TICKET_AI_ENABLED=true`)

## Future Enhancements

### LLM Integration

The `buildTicketNarrative()` function is designed to be easily replaced with an LLM call:

```javascript
// Current: Rule-based
function buildTicketNarrative(ticket) { ... }

// Future: LLM-based (same interface)
async function buildTicketNarrative(ticket) {
  const prompt = buildPromptFromTimeline(ticket.timeline);
  const response = await llmClient.complete(prompt);
  return parseLLMResponse(response);
}
```

The UI integration remains unchanged - only the narrative generation logic changes.

### Webhook Integration

The event abstraction layer is designed to work with future webhook ingestion:

- Webhook events → Normalized event model → Same `timeline[]` structure
- No changes needed to correlation or AI layers

## Troubleshooting

### No Branch/Tag Data

**Symptom**: `ticket.prs[].branches` is empty

**Possible causes**:
1. `TICKET_HISTORY_ADVANCED=0` (disabled)
2. GitHub API rate limiting
3. Repository not found or access denied

**Check**: Snapshot logs for `[WARN]` messages

### AI Interpretation Not Showing

**Symptom**: No "AI interpretation" section in ticket cards

**Possible causes**:
1. `window.TICKET_AI_ENABLED` is not `true`
2. Ticket has no timeline data
3. Narrative function returned `null`

**Check**: Browser console, verify `TICKET_AI_ENABLED` value

### Low Confidence Warnings

**Symptom**: AI interpretation shows "Low confidence" or validation warnings

**Meaning**: System detected inconsistencies between timeline events and envPresence flags

**Action**: Review raw timeline data to understand discrepancy

## Summary

This enhancement adds an **intelligence layer** on top of existing ticket tracking without breaking any existing functionality. All new features are:

- ✅ **Additive**: Only adds new fields/functions
- ✅ **Isolated**: Clear separation from existing code
- ✅ **Feature-flagged**: Can be disabled completely
- ✅ **Safe**: Graceful degradation on errors
- ✅ **Validated**: AI conclusions checked against raw data

The system is production-ready and can be enabled/disabled per environment as needed.
