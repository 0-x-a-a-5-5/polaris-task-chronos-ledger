"""Structured telemetry helper module."""

import time
from typing import Any, Dict


def format_audit_event(event_type: str, metadata: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "timestamp": time.time(),
        "event_type": event_type,
        "metadata": dict(metadata),
    }
