# Troubleshooting: Missing Deployment/Environment Data in Ticket Tracker

## Problem Summary

After running a fresh snapshot with advanced ticket tracking enabled, tickets show PRs correctly but **no environment presence data** (DEV/QA/UAT/PROD badges are empty, no deployment information).

## Root Cause Analysis

### 1. Why Deployment Data Might Be Missing

The deployment detection system relies on **tag changes between snapshots**. Here's the critical flow:

```
Previous Snapshot → Current Snapshot → Tag Changes Detected → Deployment Events
```

**Key Requirement**: The system **only** detects deployments when:
- A previous snapshot exists (`prev_snapshot` is not `None`)
- Component tags have **changed** between the previous and current snapshot
- The tag change represents an actual deployment (not just a data refresh)

**If any of these conditions fail, no deployment data will be generated.**

### 2. Most Common Reasons for Missing Data

#### A. First Snapshot Run (No Previous Snapshot)

**Symptom**: First time running the snapshot, or `release_history.json` is missing/empty.

**Why it fails**: 
- `load_previous_snapshot_from_history()` returns `None`
- `add_env_presence_to_ticket_index()` requires `prev_snapshot` to detect tag changes
- Without previous tags, the system cannot determine what changed

**Code location**: `snapshot.py` line ~3041, ~1099-1120

#### B. No Tag Changes Detected

**Symptom**: Tags haven't changed between snapshots (same tags in both).

**Why it fails**:
- The system only tracks deployments when `prev_tag != cur_tag` (line ~1110)
- If tags are identical, no tag changes are recorded
- No tag changes = no deployment events

**Code location**: `snapshot.py` line ~1109-1120

#### C. Feature Flag Disabled

**Symptom**: `TICKET_HISTORY_ADVANCED` environment variable is set to `0`, `false`, `no`, or `off`.

**Why it fails**:
- Branch/tag correlation enrichment is skipped
- Only basic PR tracking runs (no branch/tag correlation)

**Code location**: `snapshot.py` line ~3056

#### D. Missing TeamCity/Deployment Data

**Symptom**: Components don't have `deployedAt` timestamps or `tag` fields populated.

**Why it fails**:
- Deployment detection requires `component.deployedAt` and `component.tag`
- If TeamCity integration isn't fetching this data, tags won't be available
- Without tags, tag changes cannot be detected

**Code location**: `snapshot.py` line ~1117, ~1155

#### E. Silent GitHub API Failures

**Symptom**: Branch/tag correlation fails but errors are only logged as warnings.

**Why it fails**:
- GitHub API rate limiting or access issues
- Errors are caught and logged, but enrichment continues with empty data
- Check logs for `[WARN] Ticket tracker: failed to enrich branches/tags`

**Code location**: `snapshot.py` line ~3070-3071

## Diagnostic Steps

### Step 1: Verify Feature Flag

**Check if `TICKET_HISTORY_ADVANCED` is enabled:**

```bash
# In your environment or .env file
echo $TICKET_HISTORY_ADVANCED

# Should output: 1, true, yes, or on (case-insensitive)
# If empty or 0/false/no/off, the feature is disabled
```

**Fix if disabled:**
```bash
export TICKET_HISTORY_ADVANCED=1
# Or add to your .env file:
# TICKET_HISTORY_ADVANCED=1
```

### Step 2: Check Previous Snapshot Availability

**Verify `release_history.json` exists and contains data:**

```bash
# Check if file exists
ls -la MVP1/snapshot/release_history.json

# Check if it has previous snapshots
cat MVP1/snapshot/release_history.json | jq '.history | length'
# Should output a number > 0

# Check the most recent snapshot
cat MVP1/snapshot/release_history.json | jq '.history[0] | keys'
# Should show: projects, generatedAt, etc.
```

**If missing or empty:**
- This is likely the **first snapshot run**
- **Solution**: Run the snapshot **twice** (first run creates baseline, second run detects changes)
- Or manually create a baseline by copying `latest.json` to `release_history.json` with structure:
  ```json
  {
    "history": [
      {
        "generatedAt": "2026-01-20T00:00:00Z",
        "projects": [...]
      }
    ]
  }
  ```

### Step 3: Verify Tag Changes Between Snapshots

**Check if tags actually changed:**

```python
# Add this diagnostic code to snapshot.py temporarily, or run in Python REPL:

import json

# Load previous snapshot
with open('MVP1/snapshot/release_history.json', 'r') as f:
    history = json.load(f)
    prev = history.get('history', [{}])[0] if history.get('history') else None

# Load current snapshot
with open('MVP1/snapshot/latest.json', 'r') as f:
    current = json.load(f)

# Compare tags
def get_component_tags(snapshot):
    tags = {}
    for proj in snapshot.get('projects', []):
        pkey = proj.get('key', '')
        for env in proj.get('environments', []):
            ekey = env.get('key', '')
            for comp in env.get('components', []):
                cname = comp.get('name', '')
                tag = comp.get('tag', '')
                key = (pkey, ekey, cname)
                tags[key] = tag
    return tags

if prev:
    prev_tags = get_component_tags(prev)
    curr_tags = get_component_tags(current)
    
    changes = []
    for key in set(prev_tags.keys()) | set(curr_tags.keys()):
        prev_tag = prev_tags.get(key, '')
        curr_tag = curr_tags.get(key, '')
        if prev_tag and curr_tag and prev_tag != curr_tag:
            changes.append({
                'key': key,
                'from': prev_tag,
                'to': curr_tag
            })
    
    print(f"Tag changes detected: {len(changes)}")
    for change in changes[:10]:  # Show first 10
        print(f"  {change['key']}: {change['from']} → {change['to']}")
else:
    print("No previous snapshot found - this is the first run")
```

**If no tag changes detected:**
- Tags haven't changed between snapshots
- **Solution**: Wait for actual deployments (tag changes) to occur, or manually trigger a deployment to create tag changes

### Step 4: Verify Component Data Quality

**Check if components have required fields:**

```bash
# Check if components have tags and deployedAt
cat MVP1/snapshot/latest.json | jq '.projects[0].environments[0].components[0] | {name, tag, deployedAt, branch}'

# Should show:
# {
#   "name": "component-name",
#   "tag": "v1.2.3",        # Must exist
#   "deployedAt": "2026-01-20T10:00:00Z",  # Must exist
#   "branch": "release/1.2"  # Optional but helpful
# }
```

**If tags or deployedAt are missing:**
- TeamCity integration might not be fetching this data
- Check TeamCity API configuration and ensure it's returning tag/deployment information
- Verify `teamcity_fetch_builds()` and related functions are working

### Step 5: Check GitHub API Enrichment

**Verify branch/tag correlation is working:**

```bash
# Check snapshot logs for warnings
grep -i "warn.*ticket\|warn.*branch\|warn.*tag" snapshot.log

# Should NOT see:
# [WARN] Ticket tracker: failed to enrich branches/tags: ...
```

**Check if PRs have branch/tag data:**

```bash
# Check a specific ticket in latest.json
cat MVP1/snapshot/latest.json | jq '.ticketIndex["TCBP-4034"].prs[0] | {repo, number, branches, tags}'

# Should show:
# {
#   "repo": "tcbp-mfe-tour",
#   "number": 123,
#   "branches": ["release/0.19.0", "main"],  # Should exist if enrichment worked
#   "tags": ["v0.0.121"]                     # Should exist if enrichment worked
# }
```

**If branches/tags are empty:**
- GitHub API might be rate-limited or failing
- Check GitHub token permissions (needs `repo` scope)
- Verify network connectivity to GitHub API

### Step 6: Verify Environment Presence Logic

**Check if envPresence is being set:**

```bash
# Check a ticket's envPresence
cat MVP1/snapshot/latest.json | jq '.ticketIndex["TCBP-4034"] | {envPresence, envPresenceMeta}'

# Should show:
# {
#   "envPresence": {
#     "DEV": true,
#     "QA": true,
#     "UAT": false,
#     "PROD": false
#   },
#   "envPresenceMeta": {
#     "DEV": {
#       "when": "2026-01-20T10:00:00Z",
#       "repo": "tcbp-mfe-tour",
#       "tag": "v0.0.121",
#       "confidence": "high"
#     },
#     ...
#   }
# }
```

**If envPresence is empty or all false:**
- Tag changes might not match PR merge times
- PR merge times might be after deployment times (logic requires PR merged BEFORE deployment)
- Check the time comparison logic in `add_env_presence_to_ticket_index()` (line ~1185-1230)

## Step-by-Step Fix Guide

### Fix 1: Enable Feature Flag (If Disabled)

```bash
# Set environment variable
export TICKET_HISTORY_ADVANCED=1

# Or add to .env file
echo "TICKET_HISTORY_ADVANCED=1" >> .env

# Re-run snapshot
python MVP1/snapshot/snapshot.py
```

### Fix 2: Create Baseline Snapshot (First Run)

**If this is your first snapshot run:**

```bash
# Run snapshot once to create baseline
python MVP1/snapshot/snapshot.py

# This creates latest.json but no deployment data (expected)

# Run snapshot again (now it has previous snapshot to compare against)
python MVP1/snapshot/snapshot.py

# Now deployment data should appear
```

### Fix 3: Verify Tag Changes Exist

**If tags haven't changed:**

1. **Check actual deployments**: Have there been any deployments since the last snapshot?
2. **Manual trigger**: If needed, trigger a deployment to create tag changes
3. **Wait for next deployment cycle**: The system only detects deployments when tags change

### Fix 4: Fix TeamCity Data Collection

**If components lack tags/deployedAt:**

1. **Check TeamCity integration**:
   ```bash
   # Verify TeamCity credentials
   echo $TEAMCITY_URL
   echo $TEAMCITY_TOKEN
   ```

2. **Check TeamCity API response**: Ensure TeamCity is returning tag and deployment timestamp data

3. **Verify component mapping**: Ensure TeamCity build data is correctly mapped to components

### Fix 5: Fix GitHub API Issues

**If branch/tag correlation fails:**

1. **Check GitHub token**:
   ```bash
   echo $GITHUB_TOKEN
   # Should be a valid token with 'repo' scope
   ```

2. **Test GitHub API access**:
   ```bash
   curl -H "Authorization: token $GITHUB_TOKEN" \
        https://api.github.com/repos/YOUR_ORG/YOUR_REPO/branches
   ```

3. **Check rate limits**: GitHub API has rate limits (5000 requests/hour for authenticated users)

### Fix 6: Add Diagnostic Logging

**Add temporary logging to understand what's happening:**

```python
# Add to snapshot.py around line 3078, before add_env_presence_to_ticket_index:

print(f"[DEBUG] prev_snapshot exists: {prev_snapshot is not None}")
if prev_snapshot:
    prev_projects = prev_snapshot.get('projects', [])
    print(f"[DEBUG] prev_snapshot has {len(prev_projects)} projects")
    
    # Count components with tags
    prev_tag_count = 0
    for proj in prev_projects:
        for env in proj.get('environments', []):
            for comp in env.get('components', []):
                if comp.get('tag'):
                    prev_tag_count += 1
    print(f"[DEBUG] prev_snapshot has {prev_tag_count} components with tags")

# Add to add_env_presence_to_ticket_index around line 1120:
print(f"[DEBUG] tag_changes_by_key has {len(tag_changes_by_key)} entries")
if tag_changes_by_key:
    for key, change in list(tag_changes_by_key.items())[:5]:
        print(f"[DEBUG] Tag change: {key} -> {change.get('fromTag')} -> {change.get('toTag')}")
```

## Expected Behavior

### Successful Deployment Detection

When working correctly, you should see:

1. **In `latest.json`**:
   ```json
   {
     "ticketIndex": {
       "TCBP-4034": {
         "envPresence": {
           "DEV": true,
           "QA": true
         },
         "envPresenceMeta": {
           "DEV": {
             "when": "2026-01-20T10:00:00Z",
             "repo": "tcbp-mfe-tour",
             "tag": "v0.0.121",
             "confidence": "high"
           }
         },
         "timeline": [
           {
             "stage": "Deployed to DEV",
             "at": "2026-01-20T10:00:00Z",
             "type": "deployment"
           }
         ]
       }
     }
   }
   ```

2. **In UI**: Ticket cards show environment badges (DEV/QA/UAT/PROD) with proper colors

3. **In logs**: No `[WARN]` messages about ticket tracker failures

## Quick Diagnostic Checklist

- [ ] `TICKET_HISTORY_ADVANCED=1` is set
- [ ] `release_history.json` exists and has at least one previous snapshot
- [ ] Tags have changed between previous and current snapshot
- [ ] Components have `tag` and `deployedAt` fields populated
- [ ] GitHub token has `repo` scope and is valid
- [ ] No `[WARN]` messages in snapshot logs about ticket tracker
- [ ] At least one ticket has PRs with `mergeSha` populated
- [ ] PR merge times are before deployment times (for deployment detection)

## Common Misconfigurations

1. **Feature flag not set**: Most common - check `TICKET_HISTORY_ADVANCED`
2. **First snapshot run**: Need to run twice to establish baseline
3. **No tag changes**: System only detects deployments when tags change
4. **Missing TeamCity data**: Components need tags and deployedAt from TeamCity
5. **GitHub API failures**: Check token permissions and rate limits

## Next Steps

If after following all diagnostic steps the issue persists:

1. **Enable debug logging** (see Fix 6 above)
2. **Check snapshot logs** for specific error messages
3. **Verify data flow**: Ensure TeamCity → snapshot → ticket index pipeline is working
4. **Test with a known ticket**: Pick a ticket you know was deployed and trace it through the system

## Summary

The deployment detection system requires:
- ✅ Feature flag enabled (`TICKET_HISTORY_ADVANCED=1`)
- ✅ Previous snapshot exists (not first run)
- ✅ Tag changes between snapshots
- ✅ Component tags and deployedAt populated
- ✅ GitHub API working for branch/tag correlation

If any of these are missing, deployment data will not be generated.
