"""Structured logging utilities for snapshot generation.

This module provides a structured logger that outputs JSON-formatted logs
for production environments and human-readable logs for development.
"""
import os
import sys
import json
from datetime import datetime
from typing import Any, Optional


class StructuredLogger:
    """Structured logger that outputs JSON in production, readable text in dev."""
    
    def __init__(self, level: str = 'INFO'):
        """Initialize logger.
        
        Args:
            level: Minimum log level (DEBUG, INFO, WARN, ERROR)
        """
        self.level = level.upper()
        self.levels = {'DEBUG': 0, 'INFO': 1, 'WARN': 2, 'ERROR': 3}
        self._is_tty = sys.stdout.isatty()
    
    def _should_log(self, level: str) -> bool:
        """Check if message at given level should be logged."""
        level_num = self.levels.get(level.upper(), 1)
        min_level_num = self.levels.get(self.level, 1)
        return level_num >= min_level_num
    
    def _log(self, level: str, message: str, **kwargs) -> None:
        """Internal logging method.
        
        Args:
            level: Log level (DEBUG, INFO, WARN, ERROR)
            message: Log message
            **kwargs: Additional structured fields
        """
        if not self._should_log(level):
            return
        
        entry = {
            'ts': datetime.utcnow().isoformat() + 'Z',
            'level': level.upper(),
            'msg': message,
            **kwargs
        }
        
        # Output to stderr for WARN/ERROR, stdout for DEBUG/INFO
        output_stream = sys.stderr if level.upper() in ('WARN', 'ERROR') else sys.stdout
        
        # JSON format for non-TTY (production), readable format for TTY (dev)
        if self._is_tty:
            # Human-readable format for development
            level_color = {
                'DEBUG': '\033[36m',  # Cyan
                'INFO': '\033[32m',   # Green
                'WARN': '\033[33m',   # Yellow
                'ERROR': '\033[31m',  # Red
            }.get(level.upper(), '')
            reset = '\033[0m'
            
            # Build readable message
            parts = [f"{level_color}[{level.upper()}]{reset} {message}"]
            if kwargs:
                # Add key-value pairs
                kv_parts = []
                for k, v in kwargs.items():
                    if isinstance(v, (dict, list)):
                        v = json.dumps(v)[:100]  # Truncate long values
                    kv_parts.append(f"{k}={v}")
                if kv_parts:
                    parts.append(" | " + " ".join(kv_parts))
            
            print(" ".join(parts), file=output_stream)
        else:
            # JSON format for production (parseable by log aggregators)
            print(json.dumps(entry), file=output_stream)
    
    def debug(self, message: str, **kwargs) -> None:
        """Log debug message."""
        self._log('DEBUG', message, **kwargs)
    
    def info(self, message: str, **kwargs) -> None:
        """Log info message."""
        self._log('INFO', message, **kwargs)
    
    def warn(self, message: str, **kwargs) -> None:
        """Log warning message."""
        self._log('WARN', message, **kwargs)
    
    def error(self, message: str, **kwargs) -> None:
        """Log error message."""
        self._log('ERROR', message, **kwargs)


# Global logger instance
# Log level can be set via LOG_LEVEL environment variable
_log_level = os.getenv('LOG_LEVEL', 'INFO').upper()
logger = StructuredLogger(level=_log_level)
