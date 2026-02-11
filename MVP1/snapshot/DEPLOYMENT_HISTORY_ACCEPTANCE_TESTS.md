# Deployment History Acceptance Tests

## Overview

This document describes acceptance tests for the deterministic deployment detection system. These tests verify that deployment visibility remains stable across snapshots.

## Test Scenarios

### Scenario 1: First Snapshot

**Setup**:
- First snapshot run (no previous history)
- Some components have tags and deployedAt timestamps
- Some tickets have PRs merged

**Expected Behavior**:
- ✅ Deployment events are stored to `data/deployment_history/events.jsonl`
- ✅ Environment presence is computed from current snapshot
- ✅ Tickets show deployment information if PRs are correlated to current deployments
- ✅ No errors or warnings about missing history

**Verification**:
```bash
# Check deployment history file exists
ls -la data/deployment_history/events.jsonl

# Check events were stored
cat data/deployment_history/events.jsonl | jq -s 'length'

# Check ticket has deployment info
cat data/latest.json | jq '.ticketIndex["TICKET-123"].envPresence'
```

**Acceptance Criteria**:
- [ ] Deployment history file created
- [ ] At least one deployment event stored
- [ ] Tickets with valid PR→deployment correlation show envPresence
- [ ] No errors in snapshot logs

---

### Scenario 2: No New Deployments (Stability Test)

**Setup**:
- Second snapshot run
- No tag changes since previous snapshot
- Previous snapshot had deployments visible

**Expected Behavior**:
- ✅ Previous deployment state is preserved
- ✅ Deployments do NOT disappear
- ✅ Environment presence remains stable
- ✅ Timeline events from history are included

**Verification**:
```bash
# Check deployment history (should have previous events)
cat data/deployment_history/events.jsonl | jq -s 'length'

# Check ticket still shows deployments
cat data/latest.json | jq '.ticketIndex["TICKET-123"].envPresence'

# Compare with previous snapshot
diff <(cat data/history/YYYY-MM-DD*.json | jq '.ticketIndex["TICKET-123"].envPresence') \
     <(cat data/latest.json | jq '.ticketIndex["TICKET-123"].envPresence')
```

**Acceptance Criteria**:
- [ ] Deployment history preserved (no events lost)
- [ ] Ticket envPresence unchanged (or enhanced, not reduced)
- [ ] Timeline includes historical deployment events
- [ ] No false negatives (deployments don't disappear)

---

### Scenario 3: TeamCity Down (Resilience Test)

**Setup**:
- TeamCity API unavailable or returns errors
- GitHub API available
- Previous deployment history exists

**Expected Behavior**:
- ✅ Deployment detection still works (uses GitHub tag changes)
- ✅ Historical deployments remain visible
- ✅ New deployments detected from tag changes (if any)
- ✅ Warnings logged but snapshot succeeds

**Verification**:
```bash
# Simulate TeamCity down (disable in .env or network)
# Run snapshot
python MVP1/snapshot/snapshot.py

# Check warnings (should mention TeamCity but not fail)
grep -i "teamcity" snapshot.log

# Check deployments still visible
cat data/latest.json | jq '.ticketIndex["TICKET-123"].envPresence'
```

**Acceptance Criteria**:
- [ ] Snapshot completes successfully
- [ ] Deployment history processing continues
- [ ] Historical deployments remain visible
- [ ] Warnings logged but no fatal errors

---

### Scenario 4: Rollback Detection

**Setup**:
- Tag goes backwards: v1.0.1 → v1.0.0 (or build number decreases)
- Previous deployment to QA with v1.0.1
- Rollback to v1.0.0

**Expected Behavior**:
- ✅ Rollback detected (tag version decreased)
- ✅ Environment marked as absent (rollback occurred)
- ✅ Previous deployment history preserved
- ✅ Timeline shows rollback event

**Verification**:
```bash
# Check rollback detection
cat data/latest.json | jq '.ticketIndex["TICKET-123"].envPresence.QA'

# Should be false if rollback detected
# Check timeline for rollback event
cat data/latest.json | jq '.ticketIndex["TICKET-123"].timeline[] | select(.stage | contains("Rollback"))'
```

**Acceptance Criteria**:
- [ ] Rollback detected (tag version comparison)
- [ ] Environment presence updated (marked absent)
- [ ] Rollback event in timeline
- [ ] Previous deployment history preserved

**Note**: Rollback detection is currently a TODO. This test documents expected behavior for future implementation.

---

### Scenario 5: Multiple Deployments (Full Lifecycle)

**Setup**:
- Ticket has PR merged to main
- Deployed to DEV (tag v1.0.1)
- Deployed to QA (tag v1.0.2)
- Deployed to UAT (tag v1.0.3)
- Not yet deployed to PROD

**Expected Behavior**:
- ✅ All environments show as present (DEV, QA, UAT)
- ✅ PROD shows as absent
- ✅ Timeline shows all deployment events in chronological order
- ✅ Each environment has correct metadata (when, tag, repo)

**Verification**:
```bash
# Check all environments
cat data/latest.json | jq '.ticketIndex["TICKET-123"].envPresence'

# Check timeline
cat data/latest.json | jq '.ticketIndex["TICKET-123"].timeline[] | select(.type == "deployment")'

# Check metadata
cat data/latest.json | jq '.ticketIndex["TICKET-123"].envPresenceMeta'
```

**Acceptance Criteria**:
- [ ] DEV, QA, UAT all show as present
- [ ] PROD shows as absent
- [ ] Timeline has all deployment events
- [ ] Metadata correct for each environment
- [ ] Events in chronological order

---

### Scenario 6: Ticket Correlation Accuracy

**Setup**:
- Multiple tickets with PRs in same repo
- Deployment includes some PRs but not others
- PR merge commits must be in deployed tag history

**Expected Behavior**:
- ✅ Only tickets with PRs actually in deployment are marked as present
- ✅ Tickets with unrelated PRs are not marked
- ✅ Correlation is deterministic (same result every time)
- ✅ False positives avoided

**Verification**:
```bash
# Check ticket correlation
cat data/latest.json | jq '.ticketIndex | to_entries[] | select(.value.envPresence.QA == true) | .key'

# Verify PR merge commit is in deployed tag
# (Manual verification using GitHub API)
```

**Acceptance Criteria**:
- [ ] Only correct tickets show deployments
- [ ] No false positives
- [ ] Correlation is deterministic
- [ ] GitHub API used correctly for reachability check

---

## Performance Tests

### Large History File

**Setup**:
- 10,000+ deployment events in history
- 1000+ tickets
- Multiple projects and environments

**Expected Behavior**:
- ✅ Snapshot completes in reasonable time (< 5 minutes)
- ✅ Memory usage reasonable (< 2GB)
- ✅ No performance degradation

**Verification**:
```bash
# Measure snapshot time
time python MVP1/snapshot/snapshot.py

# Check memory usage
# (Use system monitoring tools)
```

**Acceptance Criteria**:
- [ ] Snapshot completes in < 5 minutes
- [ ] Memory usage < 2GB
- [ ] No timeout errors

---

## Integration Tests

### End-to-End Flow

**Setup**:
- Full snapshot run with all integrations enabled
- GitHub, TeamCity, Jira all available
- Previous history exists

**Expected Behavior**:
- ✅ All systems integrate correctly
- ✅ Deployment history updated
- ✅ Ticket data includes persistent deployments
- ✅ UI can render deployment information

**Verification**:
```bash
# Run full snapshot
python MVP1/snapshot/snapshot.py

# Check output
cat data/latest.json | jq '.ticketIndex | keys | length'

# Check deployment history
cat data/deployment_history/events.jsonl | jq -s 'length'

# Open UI and verify Ticket Tracker shows deployments
```

**Acceptance Criteria**:
- [ ] Snapshot completes successfully
- [ ] All integrations work
- [ ] Deployment history updated
- [ ] UI shows deployment information correctly

---

## Regression Tests

### Backward Compatibility

**Setup**:
- Existing snapshot data from before deployment history
- Run new snapshot with deployment history enabled

**Expected Behavior**:
- ✅ Existing data preserved
- ✅ New deployment history created
- ✅ No data loss
- ✅ Graceful migration

**Verification**:
```bash
# Backup existing data
cp -r data data.backup

# Run snapshot
python MVP1/snapshot/snapshot.py

# Compare (should be compatible)
diff data.backup/latest.json data/latest.json
```

**Acceptance Criteria**:
- [ ] Existing data preserved
- [ ] New features work
- [ ] No breaking changes
- [ ] Graceful handling of missing history

---

## Test Execution

### Manual Testing

1. Run each scenario manually
2. Verify expected behavior
3. Document results
4. Fix any issues found

### Automated Testing (Future)

- Unit tests for correlation logic
- Integration tests for history loading
- Performance benchmarks
- Regression test suite

---

## Success Criteria

The implementation is considered successful if:

1. ✅ **Scenario 1**: First snapshot shows meaningful deployment state
2. ✅ **Scenario 2**: "No new deploy" snapshot does not wipe deployments
3. ✅ **Scenario 3**: TeamCity down does not break correctness
4. ✅ **Scenario 5**: Multiple deployments show correctly
5. ✅ **Scenario 6**: Ticket correlation is accurate (no false positives)

**Critical**: Scenarios 1, 2, and 3 are **must-pass** for production use.
