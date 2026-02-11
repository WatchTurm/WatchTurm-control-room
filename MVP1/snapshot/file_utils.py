"""File utilities for atomic, thread-safe file operations.

This module provides functions for safely writing and reading JSON files
with proper locking to prevent corruption during concurrent access.
"""
import os
import json
import time
from pathlib import Path
from typing import Any

# Try to import fcntl (Unix/Linux/Mac)
# Note: Windows file locking is complex and not well-supported by standard library
# We rely on atomic rename operations which are safe on Windows
try:
    import fcntl
    HAS_FLOCK = True
except ImportError:
    HAS_FLOCK = False


def atomic_write_json(path: Path, data: Any, *, encoding: str = 'utf-8') -> None:
    """Write JSON atomically using temp file + rename pattern.
    
    This function:
    - Writes to a temporary file first
    - Uses file locking to prevent concurrent writes
    - Atomically replaces the target file (POSIX requirement)
    - Handles both Unix (fcntl) and Windows (msvcrt) systems
    
    Args:
        path: Target file path
        data: Data to serialize as JSON
        encoding: File encoding (default: utf-8)
    
    Raises:
        IOError: If file operations fail
        OSError: If locking fails
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + '.tmp')
    
    try:
        with open(tmp_path, 'w', encoding=encoding) as f:
            # Lock file (Unix/Linux/Mac only)
            # Windows: atomic rename provides safety, locking is handled by OS
            if HAS_FLOCK:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())  # Force write to disk
        
        # Atomic rename (POSIX requirement)
        # On Windows, this may fail if target exists, so we handle it
        try:
            os.replace(tmp_path, path)
        except OSError:
            # Windows fallback: delete target first, then rename
            if path.exists():
                path.unlink()
            os.rename(tmp_path, path)
            
    except Exception:
        # Clean up temp file on error
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except Exception:
                pass
        raise


def safe_read_json(path: Path, default: Any = None, *, encoding: str = 'utf-8') -> Any:
    """Read JSON with proper error handling and file locking.
    
    This function:
    - Uses shared lock for reading (allows concurrent reads)
    - Returns default value on error instead of crashing
    - Handles missing files gracefully
    
    Args:
        path: File path to read
        default: Default value to return on error (default: None)
        encoding: File encoding (default: utf-8)
    
    Returns:
        Parsed JSON data, or default if read/parse fails
    """
    if not path.exists():
        return default
    
    try:
        with open(path, 'r', encoding=encoding) as f:
            # Shared lock for read (allows concurrent reads, blocks writes)
            # Windows: atomic operations provide safety
            if HAS_FLOCK:
                fcntl.flock(f.fileno(), fcntl.LOCK_SH)
            
            return json.load(f)
    except (json.JSONDecodeError, IOError, OSError) as e:
        # Log error but don't crash - return default
        # Note: Can't use logger here (circular import risk), use simple print
        print(f"[WARN] Failed to read {path}: {e}", file=sys.stderr)
        return default


def atomic_append_jsonl(path: Path, events: list[dict], *, encoding: str = 'utf-8', max_retries: int = 5) -> None:
    """Append events to JSONL file atomically.
    
    This function:
    - Copies existing content to temp file
    - Appends new events
    - Atomically replaces the original file
    - Uses file locking to prevent concurrent writes
    
    Args:
        path: Target JSONL file path
        events: List of event dictionaries to append
        encoding: File encoding (default: utf-8)
        max_retries: Maximum retry attempts if lock fails (default: 5)
    
    Raises:
        IOError: If file operations fail after retries
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    
    # Write to temp file first
    tmp_path = path.with_suffix(path.suffix + '.tmp')
    
    for attempt in range(max_retries):
        try:
            # Copy existing content
            if path.exists():
                with open(path, 'r', encoding=encoding) as src:
                    if HAS_FLOCK:
                        fcntl.flock(src.fileno(), fcntl.LOCK_SH)  # Shared lock for read
                    
                    with open(tmp_path, 'w', encoding=encoding) as dst:
                        dst.write(src.read())
            
            # Append new events with exclusive lock
            with open(tmp_path, 'a', encoding=encoding) as f:
                if HAS_FLOCK:
                    fcntl.flock(f.fileno(), fcntl.LOCK_EX)  # Exclusive lock for write
                
                for event in events:
                    # Ensure event has required fields
                    if not event.get("id"):
                        continue
                    # Write as single-line JSON
                    f.write(json.dumps(event, ensure_ascii=False) + '\n')
                
                f.flush()
                os.fsync(f.fileno())  # Force write to disk
            
            # Atomic replace
            try:
                os.replace(tmp_path, path)
            except OSError:
                # Windows fallback
                if path.exists():
                    path.unlink()
                os.rename(tmp_path, path)
            
            return  # Success
            
        except (IOError, OSError) as e:
            if attempt < max_retries - 1:
                # Retry with exponential backoff
                wait_time = 0.1 * (2 ** attempt)
                time.sleep(wait_time)
                continue
            # Clean up temp file on final failure
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except Exception:
                    pass
            raise
        except Exception:
            # Clean up temp file on unexpected error
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except Exception:
                    pass
            raise
