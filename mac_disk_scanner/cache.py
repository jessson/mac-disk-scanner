from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional

from .models import ScanNode

CACHE_VERSION = 36
TTL_SECONDS = 30 * 60

# Scan regions; each one is cached independently with its own timestamp.
REGIONS = ("workspaces", "temporary", "caches", "home")


class ScanCache:
    def __init__(self, path: Optional[Path] = None):
        self.path = path or (
            Path.home() / ".cache" / "mac-disk-scanner" / "scan-cache.json"
        )

    def _load_payload(self):
        if not self.path.exists():
            return None
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return None
        if payload.get("version") != CACHE_VERSION:
            return None
        return payload

    def load_region(self, region: str, allow_stale: bool = False):
        if region not in REGIONS:
            return None
        payload = self._load_payload()
        if not payload:
            return None

        entry = (payload.get("regions") or {}).get(region)
        if not entry:
            return None

        created_at = float(entry.get("created_at", 0))
        updated_at = float(entry.get("updated_at", created_at))
        age = max(0.0, time.time() - created_at)
        if age > TTL_SECONDS and not allow_stale:
            return None

        try:
            roots = [ScanNode.from_dict(x) for x in entry.get("roots", [])]
        except Exception:
            return None

        return roots, {
            "created_at": created_at,
            "updated_at": updated_at,
            "age": age,
            "ttl": TTL_SECONDS,
            "stale": age > TTL_SECONDS,
        }

    def region_fresh(self, region: str) -> bool:
        """True when a cached, non-stale result exists for the region."""
        if region not in REGIONS:
            return False
        data = self.load_region(region)
        return bool(data and not data[1]["stale"])

    def region_meta(self, region: str):
        """Region metadata without fresh/stale filtering, or None."""
        if region not in REGIONS:
            return None
        payload = self._load_payload()
        if not payload:
            return None
        entry = (payload.get("regions") or {}).get(region)
        if not entry:
            return None
        created_at = float(entry.get("created_at", 0))
        updated_at = float(entry.get("updated_at", created_at))
        age = max(0.0, time.time() - created_at)
        return {
            "created_at": created_at,
            "updated_at": updated_at,
            "age": age,
            "ttl": TTL_SECONDS,
            "stale": age > TTL_SECONDS,
        }

    def save_region(self, region: str, roots: list, created_at: float | None = None):
        """Atomically persist one region, keeping every other region intact."""
        if region not in REGIONS:
            return
        payload = self._load_payload() or {
            "version": CACHE_VERSION,
            "regions": {},
        }
        now = time.time()
        payload.setdefault("regions", {})[region] = {
            "created_at": created_at if created_at is not None else now,
            "updated_at": now,
            "roots": [x.to_dict() for x in roots],
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def describe_age(self, seconds: float) -> str:
        m = int(seconds // 60)
        s = int(seconds % 60)
        return f"{m}m {s}s" if m else f"{s}s"