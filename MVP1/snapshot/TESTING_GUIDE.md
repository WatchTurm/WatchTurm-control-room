# Testing Guide - Step by Step

## 1. Test Auto-Snapshots

### Step 1.1: Install Flask (if not already installed)
```bash
pip install flask
```

### Step 1.2: Start the API Server
Open a **new terminal window** (keep the current one open):

```bash
cd C:\Users\mateu\OneDrive\Pulpit\merge-checker\release-ops-control-room-main\MVP1\snapshot
python snapshot_api_server.py --port 8001 --interval 5
```

**What this does**:
- Starts API server on `http://localhost:8001`
- Starts scheduler with 5-minute intervals (for testing)
- Runs first snapshot immediately

**You should see**:
```
Starting snapshot API server on http://127.0.0.1:8001
Snapshot interval: 5 minutes

Endpoints:
  GET  http://127.0.0.1:8001/api/snapshot/status
  POST http://127.0.0.1:8001/api/snapshot/trigger
  GET  http://127.0.0.1:8001/api/snapshot/progress
  GET  http://127.0.0.1:8001/health

Press Ctrl+C to stop
```

**Keep this terminal open** - the server needs to keep running.

### Step 1.3: Test the API (Optional)
In your **original terminal**, test the API:

```bash
# Check status
curl http://localhost:8001/api/snapshot/status

# Or trigger manually
curl -X POST http://localhost:8001/api/snapshot/trigger
```

### Step 1.4: Test the Web UI
1. Open `web/index.html` in your browser
2. Look at the **top bar** (top right corner)
3. You should see:
   - "Next snapshot in X minutes" (when idle)
   - OR "Snapshot: Running..." with progress bar (when running)
   - "Run Now" button (to trigger manually)

4. Click "Run Now" to test manual trigger
5. Watch the progress bar update

**Expected result**: Status updates every 30 seconds, shows next run time, manual trigger works.

---

## 2. Test Onboarding Wizard

### Step 2.1: Set Environment Variables
Make sure you have integration credentials set:

```bash
# Windows PowerShell
$env:DATADOG_API_KEY="your-key"
$env:DATADOG_APP_KEY="your-key"
$env:TEAMCITY_BASE_URL="your-url"
$env:TEAMCITY_TOKEN="your-token"
$env:GITHUB_ORG="your-org"
$env:GITHUB_TOKEN="your-token"
$env:JIRA_BASE_URL="your-url"
$env:JIRA_EMAIL="your-email"
$env:JIRA_TOKEN="your-token"
```

### Step 2.2: Run the Wizard
```bash
cd C:\Users\mateu\OneDrive\Pulpit\merge-checker\release-ops-control-room-main\MVP1\snapshot
python snapshot.py --onboard-select
```

**What this does**:
1. Discovers all resources from integrations
2. Shows you lists to select from
3. Auto-generates project configs

**Follow the prompts**:
- Select repositories (check/uncheck)
- Select build types
- Select namespaces
- Select Jira projects
- Confirm auto-detected projects

**Expected result**: New YAML configs created in `MVP1/snapshot/configs/`

---

## 3. Verify Pod Count Fix

### Step 3.1: Check the Data
Open `data/latest.json` and search for `"pods"`:

```bash
# Windows PowerShell
Get-Content data\latest.json | Select-String "pods"
```

Or open the file and search for `"pods"` - you should see actual pod counts (not just "1").

### Step 3.2: Check the Web UI
1. Open `web/index.html`
2. Go to Overview
3. Check environment cards
4. Pod counts should show **total pods**, not just "1"

**Expected result**: Pod counts are correct (e.g., "15 pods" instead of "1 pod").

---

## 4. What to Do Next

### If Everything Works ✅
Move to **Production Readiness**:
- Add error notifications
- Create deployment guides
- Add security hardening

### If Something Doesn't Work ❌
1. Check error messages
2. Verify environment variables are set
3. Check if API server is running
4. Check file permissions

### If You Want Better Performance 🚀
Move to **Performance Optimizations**:
- Parallel API calls
- Cache bootstrap events
- Reduce runtime to 5-10 min

---

## Quick Test Checklist

- [ ] API server starts without errors
- [ ] Web UI shows snapshot status
- [ ] "Run Now" button works
- [ ] Auto-snapshot runs after interval
- [ ] Onboarding wizard discovers resources
- [ ] Pod counts are correct in UI

---

## Troubleshooting

### API Server Won't Start
- Check if port 8001 is in use: `netstat -an | findstr 8001`
- Try different port: `--port 8002`

### Web UI Shows No Status
- Check if API server is running
- Check browser console for errors
- Verify `SNAPSHOT_API_BASE` in `web/app.js` matches your port

### Onboarding Wizard Fails
- Check environment variables are set
- Check API credentials are valid
- Check network connectivity to APIs

### Pod Counts Still Wrong
- Verify the fix was applied (check `snapshot.py` line 542)
- Run snapshot again
- Clear browser cache
