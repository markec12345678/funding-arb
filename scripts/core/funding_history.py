#!/usr/bin/env python3
"""Funding-rate history persistence + pair stability metrics.

Every scan of the pure-futures (or shared) rate matrix can be recorded to one
append-only JSONL line:

    {"ts": <unix sec>, "rates": {base: {venue: {"r": <rate_pct>, "ih": <interval_h>}}}}

Recording is throttled to one line per hour (funding intervals are 1h/4h/8h —
a 5-minute scan loop would only bloat the file with near-duplicate snapshots).

On top of the stored snapshots, pairs get trailing metrics — spread mean /
std / z-score and the share of snapshots where the spread cleared the entry
threshold ("stable_pct") — so opportunities can be ranked by *sustained*
differentials instead of instantaneous ones. Rows keep their exact shape when
insufficient history exists (metrics attach only when samples >= min_samples).

All IO is failure-tolerant: a broken/corrupt file never crashes a scan.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any

from core.config import runs_base

# 1 snapshot/hour by default; scans closer together are skipped.
DEFAULT_MIN_INTERVAL_SEC = 3600.0
DEFAULT_WINDOW_DAYS = 7.0
# Metrics need a meaningful sample: 12 hourly snapshots ≈ half a day.
DEFAULT_MIN_SAMPLES = 12
# Tail-read bound: ~30 days of hourly snapshots (~40KB/line) ≈ 30MB — cap the
# read so metric computation stays fast even on long-running installs.
_TAIL_MAX_BYTES = 8 * 1024 * 1024


def history_enabled() -> bool:
    """Recording can be turned off with FARB_FUNDING_HISTORY=0."""
    return os.environ.get("FARB_FUNDING_HISTORY", "1").strip() not in ("0", "false", "off")


def history_path() -> Path:
    return runs_base() / "funding_history.jsonl"


def record_scan_rates(
    by_base: dict[str, dict[str, dict[str, Any]]],
    *,
    min_interval_sec: float = DEFAULT_MIN_INTERVAL_SEC,
    now: float | None = None,
) -> bool:
    """Append the current rate matrix as one compact JSONL line (throttled).

    Returns True when a line was written. Never raises: history is a nice-to-
    have and must not break the scan path.
    """
    if not history_enabled() or not by_base:
        return False
    try:
        path = history_path()
        ts = time.time() if now is None else float(now)

        last = _last_snapshot_ts(path)
        if last is not None and ts - last < min_interval_sec:
            return False

        rates: dict[str, dict[str, dict[str, float]]] = {}
        for base, venues in by_base.items():
            row: dict[str, dict[str, float]] = {}
            for venue, info in venues.items():
                try:
                    r = float(info.get("rate_pct", 0.0) or 0.0)
                    ih = float(info.get("interval_h", 8.0) or 8.0)
                except (TypeError, ValueError):
                    continue
                row[str(venue)] = {"r": round(r, 6), "ih": round(ih, 2)}
            if row:
                rates[str(base)] = row

        if not rates:
            return False

        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": round(ts, 3), "rates": rates}, separators=(",", ":")) + "\n")
        return True
    except Exception:
        return False


def _read_last_line(path: Path) -> str | None:
    """Last complete line of the file, read backwards in chunks.

    Snapshot lines can exceed 100KB (972 bases x venues), so a fixed-size tail
    peek would land mid-line and fail to parse. Walking back to the last
    newline boundary is size-independent.
    """
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            if size == 0:
                return None
            pos = size
            tail = b""
            while pos > 0 and tail.count(b"\n") < 2:
                step = min(65536, pos)
                pos -= step
                f.seek(pos)
                tail = f.read(step) + tail
            for cand in reversed(tail.split(b"\n")):
                if cand.strip():
                    return cand.decode("utf-8", errors="replace")
            return None
    except Exception:
        return None


def _last_snapshot_ts(path: Path) -> float | None:
    """ts of the last line (throttle check); None when file missing/corrupt.

    ts sits at the head of our own line format, so the cheap prefix regex
    covers the hot path; full json.loads is only the fallback.
    """
    line = _read_last_line(path)
    if not line:
        return None
    m = re.match(r'\{"ts":([0-9]+(?:\.[0-9]+)?)', line[:64])
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            return None
    try:
        return float(json.loads(line).get("ts", 0.0) or 0.0)
    except (json.JSONDecodeError, ValueError, AttributeError):
        return None


def load_recent_snapshots(
    window_days: float = DEFAULT_WINDOW_DAYS,
    *,
    now: float | None = None,
    path: Path | None = None,
) -> list[dict[str, Any]]:
    """Tail-bounded read of snapshots inside the trailing window.

    Returns [{"ts": float, "rates": {base: {venue: {"r": pct, "ih": h}}}}, ...]
    in chronological order. Corrupt lines are skipped silently.
    """
    p = history_path() if path is None else path
    try:
        if not p.exists():
            return []
        with open(p, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - _TAIL_MAX_BYTES))
            raw = f.read().decode("utf-8", errors="replace")
        cutoff = (time.time() if now is None else float(now)) - window_days * 86400.0
        out: list[dict[str, Any]] = []
        for ln in raw.splitlines():
            ln = ln.strip()
            if not ln:
                continue
            try:
                obj = json.loads(ln)
                ts = float(obj.get("ts", 0.0) or 0.0)
            except (json.JSONDecodeError, ValueError, AttributeError):
                continue
            if ts >= cutoff and isinstance(obj.get("rates"), dict):
                out.append({"ts": ts, "rates": obj["rates"]})
        return out
    except Exception:
        return []


def pair_spread_series(
    base: str,
    long_venue: str,
    short_venue: str,
    snapshots: list[dict[str, Any]],
) -> list[float]:
    """Trailing spread series (short_rate - long_rate, scanner convention)."""
    series: list[float] = []
    b, lv, sv = str(base).upper(), str(long_venue), str(short_venue)
    for snap in snapshots:
        try:
            r = snap["rates"][b]
            short_rate = float(r[sv]["r"])
            long_rate = float(r[lv]["r"])
        except (KeyError, TypeError, ValueError):
            continue
        series.append(short_rate - long_rate)
    return series


def pair_history_metrics(
    base: str,
    long_venue: str,
    short_venue: str,
    current_spread_pct: float,
    entry_threshold_pct: float,
    snapshots: list[dict[str, Any]],
    *,
    min_samples: int = DEFAULT_MIN_SAMPLES,
) -> dict[str, Any] | None:
    """Trailing metrics for one pair, or None when history is insufficient.

    - spread_mean/std: trailing spread distribution
    - spread_z: (current - mean) / std — how stretched the entry is vs history
    - stable_pct: share of snapshots where spread >= entry threshold
      (sustained differential, not a one-scan spike)
    """
    series = pair_spread_series(base, long_venue, short_venue, snapshots)
    if len(series) < min_samples:
        return None

    n = len(series)
    mean = sum(series) / n
    var = sum((x - mean) ** 2 for x in series) / n
    std = var ** 0.5

    above = sum(1 for x in series if x >= entry_threshold_pct)
    stable_pct = round(above / n * 100.0, 1)

    spread_z = None
    if std > 1e-9:
        spread_z = round((current_spread_pct - mean) / std, 2)

    last_ts = None
    if snapshots:
        last_ts = snapshots[-1].get("ts")

    return {
        "samples": n,
        "spread_mean_pct": round(mean, 6),
        "spread_std_pct": round(std, 6),
        "spread_z": spread_z,
        "stable_pct": stable_pct,
        "window_last_ts": last_ts,
    }
