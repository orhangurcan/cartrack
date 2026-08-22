#!/usr/bin/env python3
import hashlib
import json
import os
import sys
from pathlib import Path

STATE_FILE = Path("state/state.json")
CONFIG_SCHEMA_VERSION = "portable-v2"


def prepare_state_key():
    raw = os.getenv("SEARCHES_JSON")
    if not raw:
        return

    os.environ["STATE_KEY"] = hashlib.sha256(
        ("cartrack-state-v1\0" + raw).encode("utf-8")
    ).hexdigest()

    mode = sys.argv[1] if len(sys.argv) > 1 else "watch"
    if mode not in {"watch", "dry-run"}:
        return

    fingerprint = hashlib.sha256(
        ("cartrack-config-v1\0" + CONFIG_SCHEMA_VERSION + "\0" + raw).encode("utf-8")
    ).hexdigest()[:24]

    try:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        state = {}

    if state.get("config_fingerprint") == fingerprint:
        return

    fresh = {
        "version": 2,
        "searches": {},
        "startup_notified": False,
        "last_daily_summary_date": None,
        "config_fingerprint": fingerprint,
    }
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(
        json.dumps(fresh, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print("Configuration changed; fresh baseline will be created.")


prepare_state_key()

import watcher

watcher.main()
