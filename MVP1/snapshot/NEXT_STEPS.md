# What's Next? - Roadmap & Priorities

## ✅ Completed

- **Phase 0**: Safety & Data Integrity (atomic file ops, error handling)
- **Phase 1**: Crash & Error Hardening (safe JSON parsing, structured logging)
- **Phase 2**: Concurrency & Correctness (race conditions, first-run handling)
- **Onboarding**: Selection-based wizard (discovery + selection)
- **Auto-Snapshots**: Phase 1 (scheduler + API + UI)

## 🎯 Immediate Next Steps (Recommended)

### Option 1: Test & Validate Current Work
**Priority**: High | **Time**: 1-2 hours

Test what we just built:
1. **Test auto-snapshots**:
   ```bash
   cd MVP1/snapshot
   pip install flask
   python snapshot_api_server.py --port 8001 --interval 5  # 5 min for testing
   ```
   - Open `web/index.html`
   - Verify status shows in top bar
   - Click "Run Now" to test manual trigger
   - Wait for auto-run (5 minutes)

2. **Test onboarding wizard**:
   ```bash
   python snapshot.py --onboard-select
   ```
   - Verify discovery works
   - Test selection interface
   - Verify config generation

3. **Verify pod count fix**:
   - Run snapshot: `python snapshot.py`
   - Check if pod counts are now correct (should show total, not "1")

**Why**: Ensure everything works before moving forward.

---

### Option 2: Phase 2 - Detailed Progress Indicators
**Priority**: Medium | **Time**: 3-5 days

Enhance progress tracking with detailed steps:

**What to add**:
- Show which repo/component is being processed
- Show which API call is running
- Show ETA based on historical data
- Progress bar with actual steps

**Files to modify**:
- `snapshot.py` - Add progress callbacks
- `snapshot_scheduler.py` - Update progress more frequently
- `web/app.js` - Show detailed progress UI

**Why**: Better UX, users know what's happening.

---

### Option 3: Phase 3 - Performance Optimizations
**Priority**: Medium | **Time**: 5-7 days

Reduce snapshot runtime from 20 min to 5-10 min:

**What to optimize**:
1. **Parallel API calls** (biggest win):
   - Use `concurrent.futures.ThreadPoolExecutor`
   - Process 10-20 API calls in parallel
   - **Expected**: 20 min → ~13 min (35% faster)

2. **Cache bootstrap events**:
   - Don't re-fetch commit history every time
   - Only fetch new commits since last run
   - **Expected**: 7.5 min → ~30 sec (15x faster)

3. **Optimize ticket correlation**:
   - Batch GitHub API calls
   - Use GraphQL where possible
   - **Expected**: 1 min → ~10 sec (6x faster)

**Total expected improvement**: 20 min → **5-10 min** (50-75% faster)

**Why**: Faster snapshots = can run more frequently = fresher data.

---

### Option 4: Production Readiness
**Priority**: High (before customer deployment) | **Time**: 2-3 days

Prepare for production deployment:

**What to add**:
1. **Error notifications**:
   - Email/Slack on snapshot failure
   - Alert if snapshots stop running

2. **Monitoring**:
   - Track snapshot success rate
   - Track runtime trends
   - Alert on anomalies

3. **Deployment guides**:
   - Docker setup
   - systemd service files
   - Environment variable docs

4. **Security hardening**:
   - Secure token storage (httpOnly cookies)
   - Input validation
   - Rate limiting

**Why**: Must be production-ready before customer deployment.

---

### Option 5: Enhanced Onboarding
**Priority**: Medium | **Time**: 3-4 days

Improve the onboarding wizard:

**What to add**:
1. **Better filtering**:
   - Filter repos by language, activity, team
   - Search within lists
   - Group by patterns

2. **Preview before generation**:
   - Show what config will look like
   - Allow editing before saving

3. **Validation**:
   - Test mappings before saving
   - Warn about missing/incomplete mappings

**Why**: Makes onboarding even smoother.

---

## 🎯 Recommended Path

### For MVP1 (Now)
1. **Test current work** (1-2 hours)
   - Verify auto-snapshots work
   - Verify onboarding wizard works
   - Fix any bugs found

2. **Production readiness** (2-3 days)
   - Error notifications
   - Deployment guides
   - Security hardening

### For MVP2 (Next Sprint)
3. **Phase 2 - Progress indicators** (3-5 days)
   - Detailed progress steps
   - Better UX

4. **Phase 3 - Performance** (5-7 days)
   - Parallel API calls
   - Cache bootstrap events
   - Reduce runtime to 5-10 min

### For Future
5. **Enhanced onboarding** (3-4 days)
   - Better filtering/search
   - Preview/validation

---

## 🤔 What Should We Do Next?

**My recommendation**: Start with **Option 1 (Test & Validate)** to ensure everything works, then move to **Option 4 (Production Readiness)** to prepare for customer deployment.

**But you decide**:
- Want to test first? → Option 1
- Want better UX? → Option 2
- Want faster snapshots? → Option 3
- Want production-ready? → Option 4
- Want better onboarding? → Option 5

**What would you like to focus on?**
