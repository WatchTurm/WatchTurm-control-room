# Snapshot Performance Analysis

## Current Runtime: ~20 minutes

### Root Causes

#### 1. Sequential API Calls (Biggest Bottleneck) ⚠️
**Problem**: All API calls are made sequentially, not in parallel.

**Examples**:
- GitHub: Fetching PRs, commits, branches for each repo (one at a time)
- TeamCity: Fetching build details for each component (one at a time)
- Datadog: Querying metrics for each environment/component (one at a time)
- Jira: Fetching ticket details (one at a time)

**Impact**: If you have:
- 15 repos × 3 API calls each = 45 sequential calls
- 15 components × 1 TeamCity call = 15 sequential calls
- 4 environments × 5 Datadog queries = 20 sequential calls
- **Total**: ~80 sequential API calls

**Time per call**: ~1-2 seconds (network latency + processing)
**Total time**: 80 × 1.5s = **~120 seconds = 2 minutes** (just for API calls)

**But wait...** There's more:

#### 2. Bootstrap Events (Major Time Consumer) ⚠️
**Problem**: Fetching commit history for each component to reconstruct tag changes.

**Code**: `_bootstrap_events_for_component()` and `compute_bootstrap_events()`

**What it does**:
- For each component, fetches commits from kustomization.yaml file
- Spans ~60 days of history
- Compares tag signatures between commits
- Can fetch 10-20 pages of commits per component

**Impact**: 
- 15 components × 15 pages × 2 seconds = **~450 seconds = 7.5 minutes**

#### 3. Ticket Correlation (Time Consumer) ⚠️
**Problem**: Complex logic checking if commits are in branches.

**Code**: `correlate_tickets_to_deployments()` and `github_check_commit_in_branch()`

**What it does**:
- For each deployment, checks if PR merge commits are in deployed tags
- Makes individual GitHub API calls for each check
- Can be 50-100+ checks per snapshot

**Impact**:
- 50 checks × 1 second = **~50 seconds**

#### 4. Rate Limiting Delays
**Problem**: When rate limits are hit, we wait with exponential backoff.

**Code**: `_api_request_with_retry()` with `time.sleep()`

**Impact**: 
- If rate limited 5-10 times: **~30-60 seconds** of waiting

#### 5. Data Processing (Minor)
**Problem**: Processing large amounts of data in memory.

**Impact**: 
- Usually < 30 seconds
- Not a major bottleneck

---

## Total Time Breakdown (Estimated)

| Component | Time | % of Total |
|-----------|------|-----------|
| Bootstrap Events | 7.5 min | 37% |
| Sequential API Calls | 2 min | 10% |
| Ticket Correlation | 1 min | 5% |
| Rate Limiting | 1 min | 5% |
| Data Processing | 0.5 min | 2.5% |
| **Other/Overhead** | **8 min** | **40%** |

**Wait, where's the other 8 minutes?**

Likely:
- More API calls we haven't counted
- Network latency variations
- Retry logic adding delays
- File I/O operations
- JSON parsing/serialization

---

## Would It Be Faster on a Server?

### Short Answer: **Slightly, but not dramatically**

### Why?

**Network Latency**:
- Local machine → GitHub API: ~100-200ms per call
- Server → GitHub API: ~50-100ms per call (if server is closer to API)
- **Savings**: ~50-100ms per call × 100 calls = **5-10 seconds**

**Processing Speed**:
- Local machine: Usually fast enough (not the bottleneck)
- Server: Might be faster, but API calls are the bottleneck
- **Savings**: Minimal (maybe 1-2 minutes)

**Total Potential Savings**: **~5-15 minutes** (from 20 min to 5-15 min)

**But**: The real bottleneck is **sequential API calls**, not network speed.

---

## Optimization Opportunities

### 1. Parallel API Calls (Biggest Win) 🚀
**Current**: Sequential (one at a time)
**Optimized**: Parallel (10-20 at a time)

**Implementation**: Use `concurrent.futures.ThreadPoolExecutor` or `asyncio`

**Expected Improvement**: 
- API calls: 2 min → **~15 seconds** (8x faster)
- **Total time**: 20 min → **~13 min** (35% faster)

**Risk**: Rate limiting (need to respect API limits)

### 2. Cache Bootstrap Events (Big Win) 🚀
**Current**: Fetches commit history every time
**Optimized**: Cache bootstrap events, only fetch new commits

**Expected Improvement**:
- Bootstrap: 7.5 min → **~30 seconds** (15x faster)
- **Total time**: 20 min → **~13 min** (35% faster)

**Risk**: Need to handle cache invalidation

### 3. Optimize Ticket Correlation (Medium Win)
**Current**: Individual API calls for each check
**Optimized**: Batch checks, use GraphQL, cache results

**Expected Improvement**:
- Correlation: 1 min → **~10 seconds** (6x faster)
- **Total time**: 20 min → **~19 min** (5% faster)

**Risk**: More complex code

### 4. Incremental Processing (Medium Win)
**Current**: Processes everything every time
**Optimized**: Only process what changed

**Expected Improvement**:
- **Total time**: 20 min → **~5-10 min** (50-75% faster)

**Risk**: Need to track what changed

---

## Recommended Approach: Auto-Snapshots + Manual Trigger

### ChatGPT's Suggestion: ✅ **Good, with modifications**

**Original**:
- Auto-snapshot every 15 minutes
- Manual trigger available
- Progress indicator

**Recommended Modifications**:

### 1. Configurable Interval (Not Fixed 15 Minutes)
**Why**: 
- 15 minutes might be too frequent (hits rate limits)
- 15 minutes might be too slow (for active teams)
- Different customers have different needs

**Recommendation**:
- **Default**: 30 minutes (safer for rate limits)
- **Configurable**: Per-project or global setting
- **Smart**: Adjust based on activity (more frequent if many changes)

### 2. Progress Indicator (Essential)
**What to show**:
- Current step: "Fetching GitHub PRs... (5/15 repos)"
- Progress bar: `[████████░░] 80%`
- ETA: "Estimated time remaining: 3 minutes"
- Or: "Next snapshot in 5 minutes" (when idle)

**Implementation**:
- Add progress callbacks to long-running functions
- Store progress in a file (`data/snapshot_progress.json`)
- Web UI polls this file to show progress

### 3. Manual Trigger (Essential)
**Why**: 
- User wants fresh data immediately
- Debugging/testing
- After configuration changes

**Implementation**:
- API endpoint: `POST /api/snapshot/trigger`
- Queue system: Prevent concurrent snapshots
- Status endpoint: `GET /api/snapshot/status`

### 4. Smart Scheduling
**Recommendation**: 
- **During business hours**: Every 30 minutes
- **Off hours**: Every 2 hours
- **After manual trigger**: Wait 5 minutes before next auto-run

### 5. Background Processing
**Recommendation**:
- Run snapshots in background thread/process
- Don't block web UI
- Show "Snapshot in progress" indicator

---

## Implementation Plan

### Phase 1: Basic Auto-Snapshots (MVP)
1. ✅ Add configurable interval (default: 30 min)
2. ✅ Add manual trigger endpoint
3. ✅ Add progress tracking (simple file-based)
4. ✅ Show "Next snapshot in X minutes" in UI

**Time**: 2-3 days
**Risk**: Low

### Phase 2: Progress Indicators
1. ✅ Add progress callbacks to long-running functions
2. ✅ Store progress in JSON file
3. ✅ Web UI polls and displays progress bar
4. ✅ Show current step and ETA

**Time**: 3-5 days
**Risk**: Medium

### Phase 3: Performance Optimizations
1. ✅ Parallel API calls (ThreadPoolExecutor)
2. ✅ Cache bootstrap events
3. ✅ Optimize ticket correlation

**Time**: 5-7 days
**Risk**: High (rate limiting, complexity)

---

## Realistic Expectations

### Current State
- **Runtime**: ~20 minutes
- **Frequency**: Manual only
- **User Experience**: Wait and hope

### After Phase 1 (Auto-Snapshots)
- **Runtime**: ~20 minutes (same)
- **Frequency**: Every 30 minutes (automatic)
- **User Experience**: Much better (always fresh data)

### After Phase 2 (Progress Indicators)
- **Runtime**: ~20 minutes (same)
- **Frequency**: Every 30 minutes (automatic)
- **User Experience**: Excellent (see progress, know when next run)

### After Phase 3 (Optimizations)
- **Runtime**: ~5-10 minutes (50-75% faster)
- **Frequency**: Every 15-30 minutes (can be more frequent)
- **User Experience**: Excellent (fast + visible progress)

---

## Recommendations

### For MVP1 (Now)
1. ✅ **Implement Phase 1**: Auto-snapshots + manual trigger
2. ✅ **Add simple progress**: "Next snapshot in X minutes"
3. ⏸️ **Defer optimizations**: Focus on UX first

### For MVP2 (Next)
1. ✅ **Implement Phase 2**: Full progress indicators
2. ✅ **Start Phase 3**: Parallel API calls (carefully)

### For Production
1. ✅ **Complete Phase 3**: All optimizations
2. ✅ **Monitor performance**: Track actual runtimes
3. ✅ **Tune intervals**: Based on real usage

---

## Conclusion

**Is 20 minutes too long?** 
- **For manual runs**: Yes, feels long
- **For auto-runs**: Acceptable (user doesn't wait)

**Will server be faster?**
- **Slightly** (5-15 min savings), but not dramatically
- **Real win**: Parallel API calls (can save 50-75% of time)

**Is auto-snapshot approach good?**
- ✅ **Yes, with modifications**:
  - Configurable interval (default 30 min)
  - Progress indicators
  - Manual trigger
  - Smart scheduling

**Priority**:
1. **Phase 1** (Auto-snapshots) - High value, low risk
2. **Phase 2** (Progress) - High value, medium risk
3. **Phase 3** (Optimizations) - High value, high risk (do later)
