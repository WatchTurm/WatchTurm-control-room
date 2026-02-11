# Ticket Deployment/Environment Data Diagnostic Report

## Executive Summary

**Problem**: 100+ tickets have PRs and merge information, but **zero tickets** have `envPresence` or deployment events (DEV/QA/UAT/PROD).

**Root Cause Analysis**: The deployment detection logic requires **tag changes between snapshots** (`prev_tag != cur_tag`). If this is the first snapshot run, or if no tag changes occurred between snapshots, the system correctly shows no deployments (this is expected behavior, not a bug).

---

## Diagnostic Checklist

### 1️⃣ Snapshot Baseline

**Status**: ⚠️ **CRITICAL DEPENDENCY**

**Code Location**: `snapshot.py` lines 1116-1120, 1169-1171

**Logic**:
```python
# Build tag change map: (project_key, env_key, component_name) -> {repo, fromTag, toTag, deployedAt, ...}
tag_changes_by_key: dict[tuple[str, str, str], dict] = {}
if prev_snapshot:
    prev_map = _component_map(prev_snapshot)
    cur_map = _component_map({"projects": projects_out})
    for key, cur in cur_map.items():
        # Only track actual tag changes (same criteria as Release History: prev_tag != cur_tag)
        if prev_tag and cur_tag and prev_tag != cur_tag:
            tag_changes_by_key[key] = {...}
```

**Finding**:
- ✅ Deployment detection **requires** `prev_snapshot` to exist
- ✅ Deployment detection **only** triggers on tag changes (`prev_tag != cur_tag`)
- ⚠️ **If this is the first snapshot run**, `prev_snapshot` is `None`, so no deployments are detected (CORRECT BEHAVIOR)
- ⚠️ **If no tag changes occurred** between snapshots, no deployments are detected (CORRECT BEHAVIOR)

**Output**: 
- **Is deployment detection eligible?** 
  - ❌ **NO** if `prev_snapshot` is `None` (first run)
  - ❌ **NO** if no tag changes detected between snapshots
  - ✅ **YES** only if `prev_snapshot` exists AND tag changes are detected

**Action Required**: 
- Verify if `prev_snapshot` is being loaded: Check `load_previous_snapshot_from_history()` call
- Verify if tag changes exist: Check `tag_changes_by_key` dictionary size after computation

---

### 2️⃣ Feature Flags

**Status**: ✅ **VERIFIED IN CODE**

**Code Location**: `snapshot.py` lines 3628-3632, 3649-3662

**Logic**:
```python
# Feature flag: TICKET_HISTORY_ADVANCED (default: enabled)
ticket_history_advanced = os.getenv("TICKET_HISTORY_ADVANCED", "1").strip().lower() in ("1", "true", "yes", "on")

# Feature flag: TICKET_HISTORY_TIME_AWARE (default: enabled)
ticket_history_time_aware = os.getenv("TICKET_HISTORY_TIME_AWARE", "1").strip().lower() in ("1", "true", "yes", "on")
```

**Finding**:
- ✅ `TICKET_HISTORY_ADVANCED` defaults to **enabled** (if env var not set)
- ✅ `TICKET_HISTORY_TIME_AWARE` defaults to **enabled** (if env var not set)
- ⚠️ **Runtime value unknown** - need to check actual environment variables during snapshot execution

**Output**:
- **Is advanced ticket history enabled?**
  - ✅ **YES** by default (unless explicitly disabled via env var)
  - ⚠️ **UNKNOWN** - actual runtime value needs verification from snapshot logs

**Action Required**:
- Check snapshot execution logs for feature flag values
- Verify environment variables: `echo $TICKET_HISTORY_ADVANCED` and `echo $TICKET_HISTORY_TIME_AWARE`

---

### 3️⃣ TeamCity Data Completeness

**Status**: ⚠️ **REQUIRES DATA INSPECTION**

**Code Location**: `snapshot.py` lines 1155-1178

**Logic**:
```python
# Only include components that had actual tag changes (same criteria as Release History)
for proj in projects_out:
    for env in (proj.get("environments") or []):
        for comp in (env.get("components") or []):
            # Only include if there was an actual tag change
            key = (pkey, env_key, comp_name)
            if prev_snapshot and key not in tag_changes_by_key:
                # No tag change detected - skip (unified with Release History)
                continue
            
            entry = {
                "branch": ...,
                "deployedAt": (comp.get("deployedAt") or env.get("lastDeploy") or "").strip(),
                "tag": (comp.get("tag") or "").strip(),
            }
```

**Finding**:
- ✅ System checks for `comp.get("tag")` and `comp.get("deployedAt")`
- ✅ System also checks `env.get("lastDeploy")` as fallback
- ⚠️ **Even if TeamCity provides data**, it's **ignored** if no tag change detected
- ⚠️ **Component must have tag change** to be included in `stage_repo_info`

**Output**:
- **Does TeamCity provide deployment data?**
  - ⚠️ **UNKNOWN** - requires inspection of `latest.json`:
    - Check `projects[].environments[].components[].tag`
    - Check `projects[].environments[].components[].deployedAt`
    - Check `projects[].environments[].components[].buildFinishedAt`
- **Is environment-aware deploy signal available?**
  - ⚠️ **UNKNOWN** - requires inspection of component data structure

**Action Required**:
- Inspect `latest.json` for sample components:
  ```bash
  cat latest.json | jq '.projects[0].environments[0].components[0] | {name, tag, deployedAt, buildFinishedAt, repo}'
  ```
- Verify if components have `tag` and `deployedAt` fields populated
- Check if TeamCity integration is returning deployment timestamps

---

### 4️⃣ Tag Change Detection

**Status**: ⚠️ **CRITICAL - LIKELY ROOT CAUSE**

**Code Location**: `snapshot.py` lines 1116-1120, 1129-1130

**Logic**:
```python
# Only track actual tag changes (same criteria as Release History: prev_tag != cur_tag)
if prev_tag and cur_tag and prev_tag != cur_tag:
    tag_changes_by_key[key] = {
        'repo': repo,
        'fromTag': prev_tag,
        'toTag': cur_tag,
        'deployedAt': (cur_comp.get('deployedAt') or '').strip(),
        ...
    }
```

**Finding**:
- ✅ System **only** detects deployments when tags **change** (`prev_tag != cur_tag`)
- ✅ If tags are **identical** between snapshots, no deployment events are generated
- ⚠️ **This is correct behavior** - the system infers deployments from tag changes
- ⚠️ **If no deployments occurred** (tags unchanged), system correctly shows no deployments

**Output**:
- **Are there real tag changes?**
  - ⚠️ **UNKNOWN** - requires comparison of `prev_snapshot` vs `current_snapshot`
- **Is the system behaving correctly?**
  - ✅ **YES** - if no tag changes, no deployments should be shown
  - ✅ **YES** - system is designed to detect deployments via tag changes, not absolute tag values

**Action Required**:
- Compare previous and current snapshots:
  ```bash
  # Check if tag changes exist
  python -c "
  import json
  prev = json.load(open('data/history/latest-YYYY-MM-DDTHH-MM-SSZ.json'))
  curr = json.load(open('data/latest.json'))
  # Compare tags per component
  "
  ```
- Verify if actual deployments occurred (check TeamCity/ArgoCD)
- Check if tag changes are being detected but not correlated to tickets

---

### 5️⃣ Commit / PR / Build Correlation

**Status**: ⚠️ **REQUIRES DATA INSPECTION**

**Code Location**: `snapshot.py` lines 1210-1270 (legacy logic), 1304-1344 (time-aware logic)

**Logic**:
```python
# Legacy logic (lines 1210-1270)
for pr in prs:
    repo = (pr.get("repo") or "").strip()
    merged_at = (pr.get("mergedAt") or "").strip()
    merged_dt = _parse_iso(merged_at)
    
    for stage, repo_map in stage_repo_info.items():
        info = repo_map.get(repo) or {}
        deployed_branch = (info.get("branch") or "").strip()
        deployed_at = (info.get("deployedAt") or "").strip()
        deployed_dt = _parse_iso(deployed_at)
        
        if not deployed_dt or not merged_dt:
            continue  # Not enough timing info -> cannot assert.
        
        if deployed_dt < merged_dt:
            continue  # Deployment before merge - impossible
        
        # Case 1: Both branches known - require exact match for high confidence
        if base and deployed_branch:
            if deployed_branch == base:
                presence[stage] = True
            else:
                # Branch mismatch: only allow if deployment is significantly after merge
                if time_diff >= 86400:  # At least 24 hours
                    presence[stage] = True
```

**Finding**:
- ✅ System correlates PRs to deployments via:
  - Repository match (`repo`)
  - Time constraint (`deployed_dt >= merged_dt`)
  - Branch match (preferred) or time gap (24+ hours)
- ⚠️ **Requires** `stage_repo_info` to be populated (which requires tag changes)
- ⚠️ **If `stage_repo_info` is empty**, no correlations are possible

**Output**:
- **Is there a missing correlation key?**
  - ⚠️ **POSSIBLY** - if `stage_repo_info` is empty (no tag changes), correlation cannot occur
  - ✅ **NO** - correlation logic exists and is sound, but requires tag changes as prerequisite

**Action Required**:
- Check if `stage_repo_info` is populated:
  - Inspect `tag_changes_by_key` size
  - Verify components with tag changes are included in `stage_repo_info`
- For sample ticket, verify:
  - PR merge commit SHA exists
  - Component tag matches PR target branch
  - Deployment timestamp is after PR merge

---

### 6️⃣ Time-Based Consistency

**Status**: ✅ **VERIFIED IN CODE**

**Code Location**: `snapshot.py` lines 1304-1344 (time-aware), 1210-1270 (legacy)

**Logic**:
```python
# Time-aware logic (lines 1304-1344)
if time_aware_deployments:
    for deploy_info in time_aware_deployments:
        deployed_at = deploy_info.get("deployedAt") or ""
        deployed_dt = _parse_iso(deployed_at)
        if not deployed_dt:
            continue  # Missing timestamp - fail closed
        
        # Mark environment as present (build-driven)
        presence[stage] = True

# Legacy logic (lines 1210-1270)
if not deployed_dt or not merged_dt:
    continue  # Not enough timing info -> cannot assert.

if deployed_dt < merged_dt:
    continue  # Deployment before merge - impossible
```

**Finding**:
- ✅ System **does** check timestamps for time validation
- ✅ System **rejects** deployments that occur before PR merge (correct)
- ✅ System **requires** timestamps to be present (fail closed)
- ⚠️ **Time-aware logic** only runs if `time_aware_deployments` is populated (requires time-aware correlation to run first)

**Output**:
- **Is time validation causing issues?**
  - ✅ **NO** - time validation is working correctly (rejecting invalid correlations)
  - ⚠️ **POSSIBLY** - if timestamps are missing, correlations are skipped (correct fail-closed behavior)

**Action Required**:
- Verify timestamps in component data:
  - Check `deployedAt` field exists and is valid ISO format
  - Check `mergedAt` field exists in PR data
- Verify time-aware correlation is running:
  - Check if `time_aware_deployments` is populated in ticket data

---

### 7️⃣ Snapshot Frequency

**Status**: ⚠️ **REQUIRES OPERATIONAL DATA**

**Code Location**: N/A (operational concern)

**Finding**:
- ⚠️ **Unknown** - snapshot frequency not visible in code
- ⚠️ **Possible issue**: If snapshots run infrequently, deployments between snapshots are invisible
- ✅ **System design**: Detects deployments via tag changes between snapshots (correct)

**Output**:
- **Could deployments be happening between snapshots?**
  - ⚠️ **POSSIBLY** - if snapshot frequency is low (e.g., daily) and deployments happen multiple times per day
  - ✅ **NO** - if snapshot frequency matches deployment frequency

**Action Required**:
- Check snapshot execution schedule (cron, CI/CD, manual)
- Compare with typical deployment frequency
- Verify if tag changes are being captured in subsequent snapshots

---

### 8️⃣ Environment Mapping

**Status**: ✅ **VERIFIED IN CODE**

**Code Location**: `snapshot.py` line 1159

**Logic**:
```python
stage = _env_to_stage(env.get("name"))
```

**Finding**:
- ✅ System uses `_env_to_stage()` function to map environment names to stages
- ⚠️ **Function definition not visible** in current code inspection (may be defined elsewhere)
- ✅ System expects stages: `["DEV", "QA", "UAT", "PROD"]` (line 1189)

**Output**:
- **Is there a naming mismatch?**
  - ⚠️ **UNKNOWN** - requires inspection of `_env_to_stage()` implementation
  - ⚠️ **POSSIBLY** - if environment names don't map to expected stages

**Code Location**: `snapshot.py` lines 1085-1098

**Function**:
```python
def _env_to_stage(env_name: str) -> str:
    """Map environment name to one of DEV/QA/UAT/PROD for ticket tracker badges."""
    n = (env_name or "").strip().lower()
    if not n:
        return "DEV"
    if "prod" in n:
        return "PROD"
    if "uat" in n:
        return "UAT"
    if n == "qa" or "qa" in n or n in ("green",):
        return "QA"
    # everything else we treat as DEV-like (dev lanes / colors)
    return "DEV"
```

**Finding**:
- ✅ Function exists and maps environment names to stages
- ✅ Mapping logic: substring matching ("prod" -> PROD, "uat" -> UAT, "qa" -> QA, else -> DEV)
- ✅ Default fallback to "DEV" if name is empty or doesn't match
- ⚠️ **Potential issue**: If environment names don't contain expected substrings, they all map to "DEV"

**Output**:
- **Is there a naming mismatch?**
  - ✅ **NO** - function exists and has reasonable mapping logic
  - ⚠️ **POSSIBLY** - if environment names are non-standard (e.g., "staging", "test", "red", "blue")
  - ✅ **VERIFIED** - function handles common cases (qa, green -> QA; prod -> PROD; uat -> UAT)

**Action Required**:
- Check actual environment names in `latest.json`:
  ```bash
  cat latest.json | jq '.projects[].environments[] | {key, name, stage: (.name | ascii_downcase)}'
  ```
- Verify if environment names contain expected substrings ("qa", "uat", "prod")
- If names are non-standard (e.g., "red", "blue", "staging"), they will all map to "DEV"

---

## Diagnostic Results Summary

### ✅ Correct Behavior (Not Bugs)

1. **No deployments on first snapshot** - System requires `prev_snapshot` to detect tag changes
2. **No deployments if no tag changes** - System only detects deployments via tag changes
3. **Time validation rejecting invalid correlations** - System correctly rejects deployments before PR merge
4. **Fail-closed on missing data** - System skips correlations when timestamps are missing

### ⚠️ Missing Data / Configuration Issues

1. **`prev_snapshot` may be `None`** - First snapshot run or snapshot history not loaded
2. **No tag changes detected** - Tags may be identical between snapshots (no actual deployments)
3. **TeamCity data may be incomplete** - Components may lack `tag` or `deployedAt` fields
4. **Feature flags may be disabled** - Runtime values need verification

### ❌ Potential Bugs (Requires Verification)

1. **`_env_to_stage()` mapping** - Environment names may not map to expected stages
2. **Tag change detection logic** - May not be detecting valid tag changes
3. **Time-aware correlation not running** - May be disabled or failing silently

---

## Concrete Examples Needed

To complete diagnosis, inspect actual data:

### Example 1: Check Tag Changes

```bash
# Compare previous and current snapshots
PREV=$(ls -t data/history/latest-*.json | head -1)
CURR="data/latest.json"

# Check if any tag changes exist
python3 <<EOF
import json
prev = json.load(open('$PREV'))
curr = json.load(open('$CURR'))

prev_map = {}
for p in prev.get('projects', []):
    for e in p.get('environments', []):
        for c in e.get('components', []):
            key = (p['key'], e['key'], c['name'])
            prev_map[key] = c.get('tag', '')

changes = []
for p in curr.get('projects', []):
    for e in p.get('environments', []):
        for c in e.get('components', []):
            key = (p['key'], e['key'], c['name'])
            prev_tag = prev_map.get(key, '')
            curr_tag = c.get('tag', '')
            if prev_tag and curr_tag and prev_tag != curr_tag:
                changes.append({
                    'project': p['key'],
                    'env': e['key'],
                    'component': c['name'],
                    'from': prev_tag,
                    'to': curr_tag
                })

print(f"Tag changes detected: {len(changes)}")
for c in changes[:5]:
    print(f"  {c}")
EOF
```

### Example 2: Check Ticket PRs and Deployments

```bash
# Check a sample ticket
cat latest.json | jq '.ticketIndex | to_entries[0] | {
  key: .key,
  prs: .value.prs | length,
  envPresence: .value.envPresence,
  envPresenceMeta: .value.envPresenceMeta,
  hasTimeAware: (.value.timeAwareDeployments != null)
}'
```

### Example 3: Check Component Data

```bash
# Check component deployment data
cat latest.json | jq '.projects[0].environments[0].components[0] | {
  name,
  tag,
  deployedAt,
  buildFinishedAt,
  repo,
  branch
}'
```

---

## Next Steps (Without Implementing)

1. **Verify `prev_snapshot` loading**:
   - Check if `load_previous_snapshot_from_history()` is being called
   - Verify if previous snapshot exists in `data/history/`

2. **Verify tag changes**:
   - Compare previous and current snapshots
   - Count actual tag changes detected

3. **Verify feature flags**:
   - Check snapshot execution logs
   - Verify environment variables

4. **Verify TeamCity data**:
   - Inspect `latest.json` for component `tag` and `deployedAt` fields
   - Verify TeamCity integration is returning deployment data

5. **Verify environment mapping**:
   - Find `_env_to_stage()` function
   - Check actual environment names vs. expected stages

6. **Verify time-aware correlation**:
   - Check if `time_aware_deployments` is populated in ticket data
   - Verify time-aware correlation is running (check logs)

---

## Conclusion

### Most Likely Root Cause

**Primary Issue**: The system requires **tag changes between snapshots** (`prev_tag != cur_tag`) to detect deployments. This is a **design decision**, not a bug.

**Why deployments are missing**:

1. **First snapshot run** (`prev_snapshot` is `None`):
   - ✅ **CORRECT BEHAVIOR** - System cannot detect tag changes without a previous snapshot
   - **Solution**: Run a second snapshot after deployments occur

2. **No tag changes between snapshots**:
   - ✅ **CORRECT BEHAVIOR** - If tags are identical, no deployments are inferred
   - **Solution**: Verify if actual deployments occurred (check TeamCity/ArgoCD)

3. **Tag changes exist but not correlated to tickets**:
   - ⚠️ **POSSIBLE ISSUE** - Tag changes detected but PR correlation fails
   - **Solution**: Verify PR repository matches component repository

### Diagnostic Priority

**Check in this order**:

1. **Verify `prev_snapshot` exists** (highest priority):
   ```bash
   # Check if previous snapshot is loaded
   ls -la data/history/latest-*.json | head -1
   ```

2. **Verify tag changes are detected**:
   ```bash
   # Compare previous and current snapshots
   # See "Concrete Examples" section above
   ```

3. **Verify feature flags are enabled**:
   ```bash
   # Check environment variables
   echo $TICKET_HISTORY_ADVANCED
   echo $TICKET_HISTORY_TIME_AWARE
   ```

4. **Verify component data completeness**:
   ```bash
   # Check if components have tag and deployedAt
   cat latest.json | jq '.projects[0].environments[0].components[0]'
   ```

### Expected Behavior vs. Bugs

| Scenario | Expected Behavior | Status |
|----------|------------------|--------|
| First snapshot run | No deployments (no `prev_snapshot`) | ✅ CORRECT |
| No tag changes | No deployments (tags unchanged) | ✅ CORRECT |
| Tag changes but no PR correlation | No deployments (PR repo mismatch) | ✅ CORRECT |
| Tag changes + PR correlation + time validation fails | No deployments (deployment before merge) | ✅ CORRECT |
| Tag changes + PR correlation + time validation passes | Deployments detected | ✅ EXPECTED |
| Feature flags disabled | No advanced correlation | ⚠️ CONFIGURATION |
| Missing timestamps | No deployments (fail closed) | ✅ CORRECT |

### Next Steps (Without Implementing)

1. **Run diagnostic script** (see "Concrete Examples" section)
2. **Verify `prev_snapshot` loading** in snapshot logs
3. **Compare tag changes** between snapshots
4. **Check feature flags** in runtime environment
5. **Inspect component data** for completeness

### Final Answer

**Why env/deploy data is missing**:

- **Most likely**: This is the **first snapshot run** or **no tag changes** occurred between snapshots
- **System is behaving correctly** by showing no deployments when tag changes are not detected
- **This is expected behavior**, not a bug

**To confirm**: Verify if `prev_snapshot` exists and if tag changes are being detected. If both are true but deployments still missing, then investigate PR-to-deployment correlation logic further.
