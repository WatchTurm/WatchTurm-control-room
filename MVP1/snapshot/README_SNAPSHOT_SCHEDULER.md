# Snapshot Scheduler - Auto-Snapshots & Manual Trigger

## Overview

The snapshot scheduler automatically runs snapshots at configurable intervals and provides a manual trigger API. This solves the "20-minute wait" problem by running snapshots in the background.

## Features

- ✅ **Auto-snapshots**: Runs every 30 minutes (configurable)
- ✅ **Manual trigger**: API endpoint to trigger snapshots on demand
- ✅ **Progress tracking**: Real-time progress updates
- ✅ **Status API**: Check when next snapshot will run
- ✅ **Web UI integration**: Shows "Next snapshot in X minutes" and "Run Now" button

## Quick Start

### 1. Install Dependencies

```bash
pip install flask
```

### 2. Start the API Server

```bash
cd MVP1/snapshot
python snapshot_api_server.py --port 8001 --interval 30
```

This will:
- Start the API server on `http://localhost:8001`
- Start the scheduler with 30-minute intervals
- Run the first snapshot immediately (if no previous run)

### 3. Access the Web UI

Open `web/index.html` in your browser. The top bar will show:
- "Next snapshot in X minutes" (when idle)
- "Snapshot: Running..." with progress bar (when running)
- "Run Now" button to trigger manually

## API Endpoints

### GET `/api/snapshot/status`
Get current snapshot status.

**Response**:
```json
{
  "running": false,
  "lastRunAt": "2024-01-20T10:00:00Z",
  "nextRunAt": "2024-01-20T10:30:00Z",
  "intervalMinutes": 30,
  "minutesUntilNextRun": 15,
  "progress": {
    "status": "completed",
    "step": "Completed",
    "progress": 100
  }
}
```

### POST `/api/snapshot/trigger`
Trigger a manual snapshot.

**Response**:
```json
{
  "success": true,
  "message": "Snapshot triggered"
}
```

### GET `/api/snapshot/progress`
Get snapshot progress (alias for `status.progress`).

### GET `/health`
Health check endpoint.

## Configuration

### Command Line Options

```bash
python snapshot_api_server.py [OPTIONS]

Options:
  --port PORT        Port to listen on (default: 8001)
  --interval MINUTES Snapshot interval in minutes (default: 30)
  --host HOST        Host to bind to (default: 127.0.0.1)
```

### Examples

```bash
# Run every 15 minutes
python snapshot_api_server.py --interval 15

# Run on different port
python snapshot_api_server.py --port 8080

# Run on all interfaces
python snapshot_api_server.py --host 0.0.0.0
```

## Standalone Scheduler (Without API Server)

If you only need the scheduler without the API:

```bash
python snapshot_scheduler.py --interval 30
```

Or trigger manually:

```bash
python snapshot_scheduler.py --trigger
```

Or check status:

```bash
python snapshot_scheduler.py --status
```

## How It Works

1. **Scheduler Loop**: Runs in background thread, checks every minute
2. **Manual Triggers**: Interrupts scheduled runs, runs immediately
3. **Cooldown**: After manual trigger, waits 5 minutes before next auto-run
4. **Progress Tracking**: Writes progress to `data/snapshot_progress.json`
5. **Subprocess Execution**: Runs `snapshot.py` in separate process (non-blocking)

## Progress File

Progress is written to `data/snapshot_progress.json`:

```json
{
  "status": "running",
  "startedAt": "2024-01-20T10:00:00Z",
  "step": "Fetching GitHub PRs... (5/15 repos)",
  "progress": 45
}
```

**Status values**:
- `running`: Snapshot is currently running
- `completed`: Snapshot completed successfully
- `error`: Snapshot failed

## Web UI Integration

The web app (`web/app.js`) automatically:
- Polls `/api/snapshot/status` every 30 seconds
- Shows "Next snapshot in X minutes" in the top bar
- Shows progress bar when running
- Provides "Run Now" button for manual triggers

**Note**: If the API server is not running, the UI gracefully handles it (no errors, just no status shown).

## Production Deployment

### As a Service (systemd)

Create `/etc/systemd/system/snapshot-scheduler.service`:

```ini
[Unit]
Description=Snapshot Scheduler
After=network.target

[Service]
Type=simple
User=your-user
WorkingDirectory=/path/to/MVP1/snapshot
ExecStart=/usr/bin/python3 snapshot_api_server.py --port 8001 --interval 30
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Then:
```bash
sudo systemctl enable snapshot-scheduler
sudo systemctl start snapshot-scheduler
```

### Docker

```dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY MVP1/snapshot/ /app/
RUN pip install flask
CMD ["python", "snapshot_api_server.py", "--host", "0.0.0.0", "--port", "8001"]
```

### Environment Variables

You can also set interval via environment variable:

```bash
export SNAPSHOT_INTERVAL=30
python snapshot_api_server.py
```

## Troubleshooting

### API Server Not Responding

1. Check if server is running: `curl http://localhost:8001/health`
2. Check logs for errors
3. Verify port is not in use: `netstat -an | grep 8001`

### Snapshots Not Running

1. Check scheduler status: `python snapshot_scheduler.py --status`
2. Check `data/snapshot_progress.json` for errors
3. Verify `snapshot.py` works manually: `python snapshot.py`

### Progress Not Updating

1. Check `data/snapshot_progress.json` exists and is writable
2. Check file permissions
3. Verify scheduler has write access to `data/` directory

## Next Steps

- **Phase 2**: Add detailed progress steps (which repo, which API call, etc.)
- **Phase 3**: Add performance optimizations (parallel API calls)
- **Phase 4**: Add email/notification on completion/failure

## See Also

- `PERFORMANCE_ANALYSIS.md` - Why snapshots take 20 minutes
- `snapshot.py` - Main snapshot generation script
