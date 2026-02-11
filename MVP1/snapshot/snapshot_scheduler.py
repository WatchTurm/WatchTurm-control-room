"""
Snapshot Scheduler for Auto-Running Snapshots.

Handles:
- Scheduled snapshots (every N minutes)
- Manual trigger
- Progress tracking
- Status reporting
"""

import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Optional

from logging_utils import logger


# Configuration
DEFAULT_INTERVAL_MINUTES = 30
PROGRESS_FILE = Path(__file__).parent.parent.parent / "data" / "snapshot_progress.json"
STATUS_FILE = Path(__file__).parent.parent.parent / "data" / "snapshot_status.json"
RUNTIME_HISTORY_FILE = Path(__file__).parent.parent.parent / "data" / "snapshot_runtimes.json"
DEFAULT_ESTIMATED_RUNTIME_SECONDS = 1200  # 20 minutes default


class SnapshotScheduler:
    """Manages snapshot scheduling and execution."""
    
    def __init__(self, interval_minutes: int = DEFAULT_INTERVAL_MINUTES):
        self.interval_minutes = interval_minutes
        self.interval_seconds = interval_minutes * 60
        self.running = False
        self.scheduler_thread: Optional[threading.Thread] = None
        self.current_process: Optional[subprocess.Popen] = None
        self.last_run_at: Optional[datetime] = None
        self.next_run_at: Optional[datetime] = None
        self.manual_trigger_pending = False
        self.manual_trigger_cooldown_seconds = 300  # 5 minutes after manual trigger
        self.snapshot_start_time: Optional[datetime] = None
        
    def start(self) -> None:
        """Start the scheduler in background thread."""
        if self.running:
            logger.warn("scheduler_already_running")
            return
        
        self.running = True
        self.scheduler_thread = threading.Thread(target=self._scheduler_loop, daemon=True)
        self.scheduler_thread.start()
        logger.info("scheduler_started", interval_minutes=self.interval_minutes)
    
    def stop(self) -> None:
        """Stop the scheduler."""
        self.running = False
        if self.scheduler_thread:
            self.scheduler_thread.join(timeout=5)
        logger.info("scheduler_stopped")
    
    def trigger_manual(self) -> bool:
        """Trigger a manual snapshot run.
        
        Returns:
            True if triggered, False if already running
        """
        if self.current_process and self.current_process.poll() is None:
            logger.warn("snapshot_already_running")
            return False
        
        self.manual_trigger_pending = True
        logger.info("manual_trigger_requested")
        return True
    
    def get_status(self) -> Dict:
        """Get current snapshot status.
        
        Returns:
            Dict with status, lastRun, nextRun, progress, etc.
        """
        status = {
            "running": self.current_process is not None and self.current_process.poll() is None,
            "lastRunAt": self.last_run_at.isoformat() if self.last_run_at else None,
            "nextRunAt": self.next_run_at.isoformat() if self.next_run_at else None,
            "intervalMinutes": self.interval_minutes,
            "manualTriggerPending": self.manual_trigger_pending,
        }
        
        # Load progress if available
        if PROGRESS_FILE.exists():
            try:
                with open(PROGRESS_FILE, "r", encoding="utf-8") as f:
                    progress = json.load(f)
                    status["progress"] = progress
            except Exception as e:
                logger.warn("failed_to_load_progress", error=str(e))
        
        # Calculate time until next run
        if status["nextRunAt"]:
            next_dt = datetime.fromisoformat(status["nextRunAt"].replace("Z", "+00:00"))
            now = datetime.now(timezone.utc)
            if next_dt > now:
                seconds_until = int((next_dt - now).total_seconds())
                status["secondsUntilNextRun"] = seconds_until
                status["minutesUntilNextRun"] = seconds_until // 60
            else:
                status["secondsUntilNextRun"] = 0
                status["minutesUntilNextRun"] = 0
        
        return status
    
    def _scheduler_loop(self) -> None:
        """Main scheduler loop (runs in background thread)."""
        logger.info("scheduler_loop_started")
        
        while self.running:
            try:
                # Check if manual trigger is pending
                if self.manual_trigger_pending:
                    self.manual_trigger_pending = False
                    self._run_snapshot()
                    # After manual trigger, wait cooldown before next auto-run
                    time.sleep(self.manual_trigger_cooldown_seconds)
                    continue
                
                # Calculate next run time
                now = datetime.now(timezone.utc)
                if self.last_run_at:
                    # Next run is last run + interval
                    self.next_run_at = self.last_run_at.replace(tzinfo=timezone.utc) + \
                        timedelta(seconds=self.interval_seconds)
                else:
                    # First run - run immediately
                    self.next_run_at = now
                
                # Wait until next run time
                if self.next_run_at > now:
                    wait_seconds = (self.next_run_at - now).total_seconds()
                    logger.info("scheduler_waiting", wait_seconds=int(wait_seconds))
                    
                    # Sleep in small chunks to allow for manual triggers
                    sleep_chunk = min(60, wait_seconds)  # Check every minute
                    elapsed = 0
                    while elapsed < wait_seconds and self.running:
                        time.sleep(sleep_chunk)
                        elapsed += sleep_chunk
                        # Check if manual trigger came in
                        if self.manual_trigger_pending:
                            break
                
                # Run snapshot if not interrupted
                if self.running and not self.manual_trigger_pending:
                    self._run_snapshot()
                
            except Exception as e:
                logger.error("scheduler_loop_error", error=str(e), exc_info=True)
                time.sleep(60)  # Wait a minute before retrying
    
    def _get_average_runtime(self) -> float:
        """Get average snapshot runtime from history."""
        if not RUNTIME_HISTORY_FILE.exists():
            return DEFAULT_ESTIMATED_RUNTIME_SECONDS
        
        try:
            with open(RUNTIME_HISTORY_FILE, "r", encoding="utf-8") as f:
                history = json.load(f)
            runtimes = history.get("runtimes", [])
            if not runtimes:
                return DEFAULT_ESTIMATED_RUNTIME_SECONDS
            # Use last 10 runtimes for average
            recent = runtimes[-10:]
            avg = sum(recent) / len(recent)
            return max(60, min(3600, avg))  # Clamp between 1 min and 1 hour
        except Exception:
            return DEFAULT_ESTIMATED_RUNTIME_SECONDS
    
    def _record_runtime(self, runtime_seconds: float) -> None:
        """Record snapshot runtime to history."""
        try:
            if RUNTIME_HISTORY_FILE.exists():
                with open(RUNTIME_HISTORY_FILE, "r", encoding="utf-8") as f:
                    history = json.load(f)
            else:
                history = {"runtimes": []}
            
            runtimes = history.get("runtimes", [])
            runtimes.append(runtime_seconds)
            # Keep only last 50 runtimes
            if len(runtimes) > 50:
                runtimes = runtimes[-50:]
            history["runtimes"] = runtimes
            
            RUNTIME_HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
            with open(RUNTIME_HISTORY_FILE, "w", encoding="utf-8") as f:
                json.dump(history, f, indent=2)
        except Exception as e:
            logger.warn("failed_to_record_runtime", error=str(e))
    
    def _calculate_eta(self) -> Optional[int]:
        """Calculate estimated time remaining in seconds."""
        if not self.snapshot_start_time:
            return None
        
        elapsed = (datetime.now(timezone.utc) - self.snapshot_start_time).total_seconds()
        avg_runtime = self._get_average_runtime()
        remaining = max(0, avg_runtime - elapsed)
        return int(remaining)
    
    def _run_snapshot(self) -> None:
        """Run snapshot in subprocess."""
        if self.current_process and self.current_process.poll() is None:
            logger.warn("snapshot_already_running_skip")
            return
        
        logger.info("snapshot_run_starting")
        self.snapshot_start_time = datetime.now(timezone.utc)
        avg_runtime = self._get_average_runtime()
        self._update_progress({
            "status": "running",
            "startedAt": self.snapshot_start_time.isoformat(),
            "step": "Initializing...",
            "progress": 0,
            "estimatedRuntimeSeconds": int(avg_runtime),
            "estimatedRuntimeMinutes": int(avg_runtime / 60),
        })
        
        # Get snapshot.py path
        snapshot_script = Path(__file__).parent / "snapshot.py"
        if not snapshot_script.exists():
            logger.error("snapshot_script_not_found", path=str(snapshot_script))
            self._update_progress({
                "status": "error",
                "error": "Snapshot script not found",
            })
            return
        
        try:
            # Run snapshot in subprocess
            self.current_process = subprocess.Popen(
                [sys.executable, str(snapshot_script)],
                cwd=str(snapshot_script.parent),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            
            # Start progress update thread
            progress_thread = threading.Thread(target=self._update_progress_loop, daemon=True)
            progress_thread.start()
            
            # Wait for completion (with timeout)
            timeout_seconds = 3600  # 1 hour max
            try:
                stdout, stderr = self.current_process.communicate(timeout=timeout_seconds)
                return_code = self.current_process.returncode
                
                if return_code == 0:
                    logger.info("snapshot_run_completed")
                    self.last_run_at = datetime.now(timezone.utc)
                    
                    # Record runtime
                    if self.snapshot_start_time:
                        runtime_seconds = (self.last_run_at - self.snapshot_start_time).total_seconds()
                        self._record_runtime(runtime_seconds)
                    
                    self._update_progress({
                        "status": "completed",
                        "completedAt": self.last_run_at.isoformat(),
                        "step": "Completed",
                        "progress": 100,
                    })
                    self.snapshot_start_time = None
                else:
                    logger.error("snapshot_run_failed", return_code=return_code, stderr=stderr[:500])
                    self._update_progress({
                        "status": "error",
                        "error": f"Snapshot failed with return code {return_code}",
                        "stderr": stderr[:500] if stderr else None,
                    })
            except subprocess.TimeoutExpired:
                logger.error("snapshot_run_timeout", timeout=timeout_seconds)
                self.current_process.kill()
                self._update_progress({
                    "status": "error",
                    "error": f"Snapshot timed out after {timeout_seconds} seconds",
                })
            
        except Exception as e:
            logger.error("snapshot_run_exception", error=str(e), exc_info=True)
            self._update_progress({
                "status": "error",
                "error": str(e),
            })
        finally:
            self.current_process = None
            self.snapshot_start_time = None
    
    def _update_progress_loop(self) -> None:
        """Background thread to update progress while snapshot is running."""
        while self.current_process and self.current_process.poll() is None:
            # Update progress every 30 seconds
            time.sleep(30)
            if self.current_process and self.current_process.poll() is None:
                # Still running - update progress with current ETA
                self._update_progress({
                    "status": "running",
                    "startedAt": self.snapshot_start_time.isoformat() if self.snapshot_start_time else datetime.now(timezone.utc).isoformat(),
                    "step": "Running...",
                    "progress": 0,  # Will be calculated in _update_progress
                })
    
    def _update_progress(self, progress: Dict) -> None:
        """Update progress file."""
        try:
            # If running, calculate and add ETA
            if progress.get("status") == "running" and self.snapshot_start_time:
                eta_seconds = self._calculate_eta()
                if eta_seconds is not None:
                    progress["etaSeconds"] = eta_seconds
                    # Only show positive ETA; if negative/zero, set to None so UI can show "Taking longer than expected"
                    if eta_seconds > 0:
                        progress["etaMinutes"] = max(1, int(eta_seconds / 60))
                    else:
                        progress["etaMinutes"] = None
                    # Calculate progress percentage based on elapsed time
                    elapsed = (datetime.now(timezone.utc) - self.snapshot_start_time).total_seconds()
                    avg_runtime = self._get_average_runtime()
                    if avg_runtime > 0:
                        progress_pct = min(95, int((elapsed / avg_runtime) * 100))
                        progress["progress"] = progress_pct
            
            PROGRESS_FILE.parent.mkdir(parents=True, exist_ok=True)
            with open(PROGRESS_FILE, "w", encoding="utf-8") as f:
                json.dump(progress, f, indent=2)
        except Exception as e:
            logger.warn("failed_to_write_progress", error=str(e))


# Global scheduler instance
_scheduler: Optional[SnapshotScheduler] = None


def get_scheduler(interval_minutes: int = DEFAULT_INTERVAL_MINUTES) -> SnapshotScheduler:
    """Get or create global scheduler instance."""
    global _scheduler
    if _scheduler is None:
        _scheduler = SnapshotScheduler(interval_minutes=interval_minutes)
    return _scheduler


if __name__ == "__main__":
    # CLI entry point for testing
    import argparse
    
    parser = argparse.ArgumentParser(description="Snapshot Scheduler")
    parser.add_argument("--interval", type=int, default=DEFAULT_INTERVAL_MINUTES,
                        help="Snapshot interval in minutes (default: 30)")
    parser.add_argument("--trigger", action="store_true",
                        help="Trigger manual snapshot")
    parser.add_argument("--status", action="store_true",
                        help="Show status and exit")
    
    args = parser.parse_args()
    
    scheduler = get_scheduler(interval_minutes=args.interval)
    
    if args.status:
        status = scheduler.get_status()
        print(json.dumps(status, indent=2))
    elif args.trigger:
        if scheduler.trigger_manual():
            print("Manual snapshot triggered")
        else:
            print("Snapshot already running")
    else:
        print(f"Starting scheduler with {args.interval} minute interval...")
        scheduler.start()
        try:
            while True:
                time.sleep(60)
        except KeyboardInterrupt:
            print("\nStopping scheduler...")
            scheduler.stop()
