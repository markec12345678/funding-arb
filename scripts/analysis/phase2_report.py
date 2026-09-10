#!/usr/bin/env python3
"""FAZA 2 analyzer — BACKTEST vs PAPER report.

Read-only by construction:

* never writes into ``scripts/data/pure-futures/`` (live runner state)
* never writes into ``data/`` (backtest artifacts / collector meta)
* writes only into ``scripts/data/phase2/`` (its own output dir)
* the only remote interactions are ``git fetch origin paper-data`` and,
  with ``--funding`` (default on), read-only funding-history API calls via
  the project's own ``backtest.funding_providers.get_funding_provider`` —
  the exact module the backtest uses, so numbers stay consistent with the
  engine. Results are cached per position id in ``funding_cache.json``.

Funnel statistics count only cycles at/after the locked baseline start
(worklog BASELINE-LOCK); pre-baseline manual test cycles are excluded and
only surfaced as a count.

Inputs
    scripts/data/pure-futures/journal.jsonl    sandbox cycles (live)
    scripts/data/pure-futures/positions.json   sandbox positions (live)
    paper-data branch (git)                    github-actions cycles + snapshots
    data/backtest_analysis.json                majors 30d funnel
    data/backtest_30d_*.json                   majors 30d variants
    data/paper_runner.meta.json                supervisor heartbeat
    data/paper_snapshot.meta.json              hourly snapshot pusher

Outputs (``scripts/data/phase2/``)
    report-<UTC>.md / report-<UTC>.json        timestamped full report
    report-latest.md / report-latest.json      stable-name copies

Usage
    python3 scripts/analysis/phase2_report.py            # full run, funding recon on
    python3 scripts/analysis/phase2_report.py --no-funding --skip-git
    python3 scripts/analysis/phase2_report.py --refresh-funding

The report is rerunnable at any point of the collection window: it computes
whatever the current data supports and marks itself INTERIM until Day 3–7.
No thresholds, gates or engine code are read for anything other than
reporting — this analyzer is measurement, not intervention.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

REPO = SCRIPTS_DIR.parent

P_JOURNAL = REPO / "scripts" / "data" / "pure-futures" / "journal.jsonl"
P_POSITIONS = REPO / "scripts" / "data" / "pure-futures" / "positions.json"
P_RUNNER_META = REPO / "data" / "paper_runner.meta.json"
P_SNAPSHOT_META = REPO / "data" / "paper_snapshot.meta.json"
P_BACKTEST_ANALYSIS = REPO / "data" / "backtest_analysis.json"
P_BACKTEST_DIR = REPO / "data"
PHASE2_DIR = REPO / "scripts" / "data" / "phase2"
FUNDING_CACHE = PHASE2_DIR / "funding_cache.json"

# Locked baseline (worklog BASELINE-LOCK section): first supervised paper cycle.
BASELINE_START = datetime(2026, 9, 10, 14, 36, 0, tzinfo=timezone.utc)
TARGET_DAYS = (3, 7)  # interim window per plan; final decision after Day 7

MARK_SPREAD_RE = re.compile(r"mark spread\s+([\d.]+)%\s*>\s*([\d.]+)%", re.I)
DEPTH_RE = re.compile(r"depth check", re.I)
QTY_RE = re.compile(r"floored to 0", re.I)


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _fmt_ms(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")


def _fmt_dt(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")


def _parse_ts(ts: str) -> datetime | None:
    try:
        return datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return None


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except json.JSONDecodeError:
        return None


def _round(x: float | int | None, n: int = 2) -> float | int | None:
    if x is None:
        return None
    try:
        return round(float(x), n)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Journal loading
# ---------------------------------------------------------------------------


def read_journal(path: Path) -> dict[str, Any]:
    """Parse a journal.jsonl. Tolerates a partial trailing line (the runner
    appends concurrently): only the *last* line may be incomplete and is then
    ignored, never counted as corruption."""
    lines: list[dict[str, Any]] = []
    raw = 0
    parse_errors = 0
    incomplete_tail = False
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {"lines": [], "raw": 0, "parse_errors": 0, "incomplete_tail": False}
    all_lines = text.splitlines()
    for i, ln in enumerate(all_lines):
        if not ln.strip():
            continue
        raw += 1
        try:
            lines.append(json.loads(ln))
        except json.JSONDecodeError:
            if i == len(all_lines) - 1:
                incomplete_tail = True
            else:
                parse_errors += 1
    return {
        "lines": lines,
        "raw": raw,
        "parse_errors": parse_errors,
        "incomplete_tail": incomplete_tail,
    }


# ---------------------------------------------------------------------------
# Funnel
# ---------------------------------------------------------------------------


def _bucket_reject(logs: list[str]) -> tuple[str, str | None]:
    joined = " | ".join(logs or [])
    m = MARK_SPREAD_RE.search(joined)
    if m:
        spread = float(m.group(1))
        sub = "absurd(>=5%)" if spread >= 5.0 else "real(1-5%)"
        return ("mark_spread_gate/" + sub, joined[:160])
    if DEPTH_RE.search(joined):
        return ("depth_gate", joined[:160])
    if QTY_RE.search(joined):
        return ("qty_too_small", joined[:160])
    if "settle" in joined.lower():
        return ("settle_mismatch", joined[:160])
    return ("other", joined[:160])


@dataclass
class Funnel:
    source: str
    cycles: int = 0
    first_ts: str | None = None
    last_ts: str | None = None
    scan_total_sum: int = 0
    scan_total_median: float | None = None
    candidates_sum: int = 0
    open_attempts: int = 0
    opens_ok: int = 0
    closes_ok: int = 0
    closes_failed: int = 0
    rejects: Counter = field(default_factory=Counter)
    reject_samples: dict[str, str] = field(default_factory=dict)
    pair_appearances: dict[str, list[str]] = field(default_factory=dict)
    pair_entered: set[str] = field(default_factory=set)
    excluded_pre_baseline: int = 0


def parse_funnel(jr: dict[str, Any], source: str, since: datetime | None = None) -> Funnel:
    f = Funnel(source=source)
    scans: list[int] = []
    for d in jr["lines"]:
        ts = d.get("ts")
        ts_dt = _parse_ts(ts) if ts else None
        if since is not None:
            if ts_dt is None or ts_dt < since:
                f.excluded_pre_baseline += 1
                continue
        if ts:
            if f.first_ts is None:
                f.first_ts = ts
            f.last_ts = ts
        f.cycles += 1
        st = d.get("scan_total")
        if isinstance(st, int):
            scans.append(st)
            f.scan_total_sum += st
        f.candidates_sum += d.get("candidates_after_filter") or 0
        for a in d.get("actions", []) or []:
            act = a.get("action")
            res = a.get("result", {}) or {}
            cand = a.get("candidate", {}) or {}
            if act == "open":
                f.open_attempts += 1
                pair = f"{cand.get('base', '?')}|{cand.get('long_venue', '?')}|{cand.get('short_venue', '?')}"
                if ts:
                    f.pair_appearances.setdefault(pair, []).append(ts)
                if res.get("ok"):
                    f.opens_ok += 1
                    f.pair_entered.add(pair)
                else:
                    bucket, sample = _bucket_reject(res.get("logs", []) or [])
                    f.rejects[bucket] += 1
                    f.reject_samples.setdefault(bucket, sample)
            elif act == "close":
                if res.get("ok"):
                    f.closes_ok += 1
                else:
                    f.closes_failed += 1
    if scans:
        scans_sorted = sorted(scans)
        f.scan_total_median = float(scans_sorted[len(scans_sorted) // 2])
    return f


def funnel_reject_total(f: Funnel) -> int:
    return sum(f.rejects.values())


# ---------------------------------------------------------------------------
# Journal action index (opens/closes by position id)
# ---------------------------------------------------------------------------


def index_actions(jr: dict[str, Any]) -> tuple[dict[str, dict], dict[str, dict]]:
    opens: dict[str, dict] = {}
    closes: dict[str, dict] = {}
    for d in jr["lines"]:
        ts = d.get("ts")
        for a in d.get("actions", []) or []:
            res = a.get("result", {}) or {}
            pid = res.get("position_id") or a.get("position_id")
            if not pid:
                continue
            if a.get("action") == "open" and res.get("ok"):
                opens[pid] = {"ts": ts, "candidate": a.get("candidate", {}) or {}, "fills": res.get("executed", []) or []}
            elif a.get("action") == "close" and res.get("ok"):
                closes[pid] = {
                    "ts": ts,
                    "edge": a.get("edge"),
                    "fills": res.get("executed", []) or [],
                    "logs": res.get("logs", []) or [],
                }
    return opens, closes


# ---------------------------------------------------------------------------
# PnL ledger
# ---------------------------------------------------------------------------


def _fill(fills: list[dict], typ: str) -> dict | None:
    for x in fills:
        if x.get("type") == typ:
            return x
    return None


def _fallback_fee_pct(long_venue: str, short_venue: str, base: str, quote: str) -> float | None:
    """Round-trip taker % via the project fee resolver (offline VIP0 defaults).
    Used only when the journal candidate lacks the recorded fee."""
    try:
        from core.fee_providers import resolve_venue_fee  # noqa: WPS433 (lazy on purpose)

        out = 0.0
        for v in (long_venue, short_venue):
            info = resolve_venue_fee(str(v).lower(), leg="futures", symbol=f"{base}{quote}")
            out += float(info.get("taker_pct", 0.0) or 0.0)
        return out * 2.0
    except Exception:
        return None


def exit_reason(close: dict | None) -> str:
    if close is None:
        return "no_close_action"
    edge = close.get("edge")
    if edge is not None and float(edge) <= -999.0:
        return "pair_disappeared"
    return "edge_below_exit"


def pnl_rows(
    positions: list[dict[str, Any]],
    opens: dict[str, dict],
    closes: dict[str, dict],
) -> list[dict[str, Any]]:
    """Closed-position ledger. ``fees_usd`` is a NEGATIVE PnL contribution
    (round-trip taker fees), so net = price + fees + funding everywhere."""
    rows: list[dict[str, Any]] = []
    for p in positions:
        if p.get("status") != "closed" and not p.get("closed_at"):
            continue
        pid = p.get("id")
        o = opens.get(pid)
        c = closes.get(pid)
        trade_usd = float(p.get("trade_usd") or 0.0)
        cand = o.get("candidate", {}) if o else {}
        fills_o = o.get("fills", []) if o else []
        fills_c = c.get("fills", []) if c else []
        lo, so = _fill(fills_o, "open_long"), _fill(fills_o, "open_short")
        lc, sc = _fill(fills_c, "close_long"), _fill(fills_c, "close_short")
        if lo and so and lc and sc:
            price = (lc["amount_usdt"] - lo["amount_usdt"]) + (so["amount_usdt"] - sc["amount_usdt"])
            price_est = False
        else:
            # Fallback: direction-aware mark-spread move (positions.json only).
            ci = p.get("close_info", {}) or {}
            opened = float(p.get("mark_spread_pct") or 0.0)
            closed = float(ci.get("close_mark_spread") or 0.0)
            move = closed - opened
            price = trade_usd * (move if p.get("direction") == "reverse" else -move) / 100.0
            price_est = True
        rt = cand.get("round_trip_fee_pct")
        if rt is None:
            rt = _fallback_fee_pct(
                p.get("long_venue", ""), p.get("short_venue", ""), p.get("base", ""), p.get("quote", "USDT")
            )
        fees = -trade_usd * float(rt) / 100.0 if rt is not None else None
        hold_min = None
        if p.get("opened_at") and p.get("closed_at"):
            hold_min = (int(p["closed_at"]) - int(p["opened_at"])) / 60000.0
        rows.append(
            {
                "id": pid,
                "base": p.get("base"),
                "pair": f"{p.get('long_venue')}/{p.get('short_venue')}",
                "direction": p.get("direction"),
                "notional_usd": trade_usd,
                "hold_min": _round(hold_min, 1),
                "expected_net_edge_pct": cand.get("net_edge_pct"),
                "entry_spread_pct": cand.get("spread_pct"),
                "entry_interval_h": cand.get("long_interval_h"),
                "round_trip_fee_pct": rt,
                "price_pnl_usd": _round(price, 2),
                "price_pnl_estimated": price_est,
                "fees_usd": _round(fees, 2),
                "exit_reason": exit_reason(c),
                "opened_at": p.get("opened_at"),
                "closed_at": p.get("closed_at"),
                "_position": p,
                "_open": o,
                "_close": c,
            }
        )
    rows.sort(key=lambda r: r.get("closed_at") or 0)
    return rows


# ---------------------------------------------------------------------------
# Funding reconstruction (project's own providers; cached)
# ---------------------------------------------------------------------------


def reconstruct_funding(
    row: dict[str, Any],
    cache: dict[str, Any],
    refresh: bool = False,
) -> dict[str, Any] | None:
    pid = row["id"]
    if not refresh and pid in cache:
        return cache[pid]
    p = row["_position"]
    o = row.get("_open") or {}
    fills = o.get("fills", []) or []
    lo, so = _fill(fills, "open_long"), _fill(fills, "open_short")
    long_notional = float(lo["amount_usdt"]) if lo else float(p.get("trade_usd") or 0.0)
    short_notional = float(so["amount_usdt"]) if so else float(p.get("trade_usd") or 0.0)
    opened = int(p["opened_at"])
    closed = int(p.get("closed_at") or time.time() * 1000)
    quote = p.get("quote", "USDT")
    symbol = f"{p.get('base')}{quote}"
    try:
        from backtest.funding_providers import get_funding_provider  # noqa: WPS433 (lazy on purpose)

        legs = {}
        total = 0.0
        for venue, sign, notional in (
            (p.get("long_venue"), -1.0, long_notional),
            (p.get("short_venue"), +1.0, short_notional),
        ):
            prov = get_funding_provider(str(venue))
            settlements = [
                s for s in prov.fetch_since(symbol, opened) if opened <= int(s.get("ts", 0)) <= closed
            ]
            cash = sum(sign * notional * float(s.get("rate_pct", 0.0)) / 100.0 for s in settlements)
            legs[str(venue)] = {
                "settlements": [
                    {"ts": _fmt_ms(int(s["ts"])), "rate_pct": _round(float(s.get("rate_pct")), 4)}
                    for s in settlements
                ],
                "cash_usd": _round(cash, 2),
            }
            total += cash
        result = {
            "symbol": symbol,
            "hold_window_utc": [_fmt_ms(opened), _fmt_ms(closed)],
            "legs": legs,
            "funding_usd": _round(total, 2),
        }
    except Exception as exc:  # network/venue failure must not kill the report
        result = {"error": f"{type(exc).__name__}: {exc}"}
    cache[pid] = result
    return result


def funding_persistence(row: dict[str, Any], recon: dict[str, Any] | None) -> dict[str, Any] | None:
    """Did the funding gap keep its entry sign/magnitude during the hold?"""
    if not recon or recon.get("error") or "legs" not in recon:
        return None
    p = row["_position"]
    o = row.get("_open") or {}
    cand = o.get("candidate", {}) or {}
    entry_gap = None
    if cand.get("short_rate_pct") is not None and cand.get("long_rate_pct") is not None:
        entry_gap = float(cand["short_rate_pct"]) - float(cand["long_rate_pct"])
    by_ts: dict[int, dict[str, float]] = {}
    for venue, leg in recon["legs"].items():
        for s in leg.get("settlements", []):
            ts = int(
                datetime.strptime(s["ts"], "%Y-%m-%d %H:%M:%SZ")
                .replace(tzinfo=timezone.utc)
                .timestamp()
                * 1000
            )
            by_ts.setdefault(ts, {})[venue] = float(s["rate_pct"])
    long_v, short_v = str(p.get("long_venue")), str(p.get("short_venue"))
    common = []
    for ts, rates in sorted(by_ts.items()):
        if long_v in rates and short_v in rates:
            common.append((ts, rates[short_v] - rates[long_v]))
    n_pos = sum(1 for _, g in common if g > 0)
    return {
        "entry_gap_pct": _round(entry_gap, 4),
        "n_common_settlements": len(common),
        "n_gap_kept_sign": n_pos if entry_gap is None or entry_gap >= 0 else len(common) - n_pos,
        "gaps_pct": [_round(g, 4) for _, g in common],
        "settlements_long": len(recon["legs"].get(long_v, {}).get("settlements", [])),
        "settlements_short": len(recon["legs"].get(short_v, {}).get("settlements", [])),
    }


# ---------------------------------------------------------------------------
# Expected vs realized (edge retention)
# ---------------------------------------------------------------------------


def edge_metrics(
    rows: list[dict[str, Any]],
    funding: dict[str, Any],
    persistence_f: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    """Per closed position: realized net, expected lifetime edge, retention.

    expected_lifetime_pct = entry funding gap x settlements_in_hold − round-trip fees
    (apples-to-apples with realized net, which pays RT fees once and collects
    per settlement). Falls back to interval-based settlement estimate when
    reconstruction is unavailable.
    """
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        fu = (funding.get(r["id"]) or {}).get("funding_usd")
        net = (r["price_pnl_usd"] or 0.0) + (r["fees_usd"] or 0.0) + (fu or 0.0)
        net_pct = 100.0 * net / max(r["notional_usd"], 1e-9)
        fp = persistence_f.get(r["id"]) or {}
        n_settle = fp.get("n_common_settlements") or 0
        if not n_settle and r.get("hold_min") and r.get("entry_interval_h"):
            n_settle = max(1, round((r["hold_min"] / 60.0) / max(float(r["entry_interval_h"]), 1e-9)))
        exp_gap = r.get("entry_spread_pct")
        rt = r.get("round_trip_fee_pct")
        exp_lifetime = None
        if exp_gap is not None and rt is not None and n_settle:
            exp_lifetime = float(exp_gap) * n_settle - float(rt)
        retention = None
        if exp_lifetime and exp_lifetime > 0:
            retention = 100.0 * net_pct / exp_lifetime
        out[r["id"]] = {
            "net_usd": _round(net, 2),
            "net_pct": _round(net_pct, 3),
            "funding_usd": _round(fu, 2) if fu is not None else None,
            "n_settlements": n_settle,
            "expected_lifetime_pct": _round(exp_lifetime, 4),
            "retention_pct": _round(retention, 1),
        }
    return out


# ---------------------------------------------------------------------------
# Opportunity persistence
# ---------------------------------------------------------------------------


def opportunity_persistence(f: Funnel) -> list[dict[str, Any]]:
    out = []
    n = max(f.cycles, 1)
    for pair, tss in sorted(f.pair_appearances.items()):
        streak = best = 1
        prev: datetime | None = None
        for ts in tss:
            dt = _parse_ts(ts)
            if prev is not None and dt and (dt - prev).total_seconds() <= 420:
                streak += 1
            else:
                streak = 1
            best = max(best, streak)
            prev = dt
        out.append(
            {
                "pair": pair.replace("|", " "),
                "pair_display": pair.split("|")[0] + f" long@{pair.split('|')[1]} short@{pair.split('|')[2]}",
                "appearances": len(tss),
                "cycles_pct": _round(100.0 * len(tss) / n, 1),
                "longest_streak_cycles": best,
                "first_seen": tss[0],
                "last_seen": tss[-1],
                "entered": pair in f.pair_entered,
            }
        )
    out.sort(key=lambda x: -x["appearances"])
    return out


# ---------------------------------------------------------------------------
# Continuity / duplicate audit (user-mandated: not just push counts)
# ---------------------------------------------------------------------------


def _git(args: list[str]) -> tuple[int, str]:
    proc = subprocess.run(["git", *args], cwd=REPO, capture_output=True, text=True, timeout=120)
    return proc.returncode, (proc.stdout or proc.stderr or "").strip()


def audit_sandbox(jr: dict[str, Any], positions: list[dict[str, Any]], opens: dict, closes: dict) -> dict[str, Any]:
    tss = [_parse_ts(d.get("ts")) for d in jr["lines"] if d.get("ts")]
    tss = [t for t in tss if t]
    back_jumps = sum(1 for a, b in zip(tss, tss[1:]) if (b - a).total_seconds() < 0)
    dup_ts = sum(1 for a, b in zip(tss, tss[1:]) if (b - a).total_seconds() == 0)
    post = [t for t in tss if t >= BASELINE_START]
    gaps15 = [
        f"{_fmt_dt(a)} -> {_fmt_dt(b)} ({(b - a).total_seconds()/60:.0f}m)"
        for a, b in zip(post, post[1:])
        if 900 < (b - a).total_seconds()
    ]
    ids = [p.get("id") for p in positions]
    dup_ids = [k for k, v in Counter(ids).items() if v > 1]
    id_set = set(ids)
    missing_open = [pid for pid in opens if pid not in id_set]
    missing_close = [
        p.get("id") for p in positions if p.get("status") == "closed" and p.get("id") not in closes
    ]
    last_cycle = jr["lines"][-1] if jr["lines"] else {}
    n_open_now = sum(1 for p in positions if p.get("status") == "open")
    return {
        "journal_lines": jr["raw"],
        "parse_errors": jr["parse_errors"],
        "incomplete_tail_ignored": jr["incomplete_tail"],
        "ts_back_jumps": back_jumps,
        "duplicate_ts": dup_ts,
        "gaps_over_15min": gaps15[:10],
        "positions_total": len(positions),
        "duplicate_position_ids": dup_ids,
        "journal_opens_without_position": missing_open,
        "closed_positions_without_close_action": missing_close,
        "open_now": n_open_now,
        "last_cycle_open_positions_field": last_cycle.get("open_positions"),
    }


def audit_actions(jr: dict[str, Any]) -> dict[str, Any]:
    tss = [_parse_ts(d.get("ts")) for d in jr["lines"] if d.get("ts")]
    tss = [t for t in tss if t]
    if not tss:
        return {"cycles": 0, "note": "no actions cycles readable"}
    dup = sum(1 for a, b in zip(tss, tss[1:]) if a == b)
    gaps = [
        f"{_fmt_dt(a)} -> {_fmt_dt(b)} ({(b - a).total_seconds()/60:.0f}m)"
        for a, b in zip(tss, tss[1:])
        if (b - a).total_seconds() > 600
    ]
    span_min = (tss[-1] - tss[0]).total_seconds() / 60.0
    expected = span_min / 5.0 + 1 if span_min > 0 else 1
    scans = [d.get("scan_total") for d in jr["lines"] if isinstance(d.get("scan_total"), int)]
    return {
        "cycles": len(tss),
        "first": _fmt_dt(tss[0]),
        "last": _fmt_dt(tss[-1]),
        "duplicate_ts": dup,
        "gaps_over_10min": gaps[:10],
        "coverage_pct": _round(100.0 * len(tss) / expected, 1),
        "scan_rows_per_cycle_median": _round(float(sorted(scans)[len(scans) // 2]) if scans else None, 0),
    }


def audit_paper_data_branch(limit: int = 200) -> dict[str, Any]:
    """Continuity of snapshots ON the branch: line counts must never go
    backwards between touching commits (no resets / data loss), and the last
    journal ts must be monotonic. Duplicates inside the latest file are
    counted. This is the 'prove the data is not lost' check, not a push count."""
    rc, _ = _git(["fetch", "origin", "paper-data"])
    if rc != 0:
        return {"available": False, "note": "git fetch failed (offline?)"}
    out: dict[str, Any] = {"available": True, "files": {}}
    for label, path in (
        ("sandbox_snapshots", "paper-data/journal.jsonl"),
        ("github_actions", "paper-data/github-actions/journal.jsonl"),
    ):
        rc, log = _git(["log", f"-n{limit}", "--format=%H|%cI|%s", "origin/paper-data", "--", path])
        commits = [l for l in log.splitlines() if l.count("|") >= 2] if rc == 0 else []
        history = []
        for line in commits:
            h, when, subject = line.split("|", 2)
            rc2, content = _git(["show", f"{h}:{path}"])
            if rc2 != 0:
                continue
            cl = [x for x in content.splitlines() if x.strip()]
            last_ts = None
            for ln in reversed(cl):
                try:
                    last_ts = json.loads(ln).get("ts")
                except json.JSONDecodeError:
                    last_ts = None
                break
            history.append(
                {
                    "commit": h[:8],
                    "time": when,
                    "subject": subject[:60],
                    "line_count": len(cl),
                    "last_ts": last_ts,
                }
            )
        history.reverse()  # chronological (git log is newest-first)
        line_resets = [
            f"{history[i-1]['commit']}->{history[i]['commit']}: {history[i-1]['line_count']}->{history[i]['line_count']}"
            for i in range(1, len(history))
            if history[i]["line_count"] < history[i - 1]["line_count"]
        ]
        ts_resets = [
            f"{history[i-1]['commit']}->{history[i]['commit']}: {history[i-1]['last_ts']} -> {history[i]['last_ts']}"
            for i in range(1, len(history))
            if history[i]["last_ts"]
            and history[i - 1]["last_ts"]
            and history[i]["last_ts"] < history[i - 1]["last_ts"]
        ]
        dup_lines = 0
        latest_count = None
        rc3, content = _git(["show", f"origin/paper-data:{path}"])
        if rc3 == 0:
            seen = set()
            for ln in content.splitlines():
                if not ln.strip():
                    continue
                key = ln.strip()[:200]
                if key in seen:
                    dup_lines += 1
                seen.add(key)
            latest_count = len([x for x in content.splitlines() if x.strip()])
        out["files"][label] = {
            "snapshot_commits": len(history),
            "first_snapshot": history[0]["time"] if history else None,
            "latest_snapshot": history[-1]["time"] if history else None,
            "latest_line_count": latest_count if latest_count is not None else (history[-1]["line_count"] if history else None),
            "latest_last_ts": history[-1]["last_ts"] if history else None,
            "line_count_resets": line_resets,
            "ts_resets": ts_resets,
            "duplicate_lines_in_latest": dup_lines,
        }
    return out


def audit_meta() -> dict[str, Any]:
    rm = _read_json(P_RUNNER_META) or {}
    sm = _read_json(P_SNAPSHOT_META) or {}
    now = time.time() * 1000
    runner_age_s = _round((now - rm.get("last_check", 0)) / 1000.0, 0) if rm.get("last_check") else None
    return {
        "runner": {
            "started_at": _fmt_ms(rm["started_at"]) if rm.get("started_at") else None,
            "respawns": rm.get("respawns"),
            "last_check_age_s": runner_age_s,
            "supervisor_alive": bool(runner_age_s is not None and runner_age_s < 120),
        },
        "snapshot_pusher": {
            "runs": sm.get("runs"),
            "ok": sm.get("ok"),
            "last_result": sm.get("last_result"),
            "last_ok_age_s": _round((now - sm.get("last_ok", 0)) / 1000.0, 0) if sm.get("last_ok") else None,
        },
    }


# ---------------------------------------------------------------------------
# Backtest side
# ---------------------------------------------------------------------------


def backtest_side() -> dict[str, Any]:
    a = _read_json(P_BACKTEST_ANALYSIS) or {}
    variants = []
    for p in sorted(P_BACKTEST_DIR.glob("backtest_30d_*.json")):
        d = _read_json(p)
        if isinstance(d, dict):
            variants.append(
                {
                    "file": p.name,
                    "trade_count": d.get("trade_count"),
                }
            )
    return {
        "window": a.get("window"),
        "bases": a.get("bases"),
        "venues": a.get("venues"),
        "rows_total": a.get("rows_total"),
        "rows_pass_entry_thresholds": a.get("rows_pass_entry_thresholds"),
        "trades": a.get("trades"),
        "fee_gate": a.get("fee_gate"),
        "thresholds": a.get("thresholds"),
        "variants": variants,
        "small_caps_note": (
            "small-cap/event segment is NOT covered by the 30d majors backtest — "
            "paper is the only evidence tier for it"
        ),
    }


# ---------------------------------------------------------------------------
# Report assembly
# ---------------------------------------------------------------------------


def _md_table(headers: list[str], rows: list[list[Any]]) -> str:
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join(["---"] * len(headers)) + "|"]
    for r in rows:
        out.append("| " + " | ".join(str(x) if x is not None else "" for x in r) + " |")
    return "\n".join(out)


def build_report(
    sandbox: Funnel,
    actions: Funnel | None,
    cont_sandbox: dict,
    cont_actions: dict,
    cont_branch: dict,
    meta: dict,
    rows: list[dict],
    funding: dict[str, Any],
    persistence_f: dict[str, Any],
    em: dict[str, dict[str, Any]],
    bt: dict,
    opp: list[dict],
) -> tuple[str, dict]:
    now = _now_utc()
    day = (now - BASELINE_START).total_seconds() / 86400.0
    stage = "INTERIM" if day < TARGET_DAYS[1] else "FINAL-WINDOW"
    total_price = sum(r["price_pnl_usd"] or 0 for r in rows)
    total_fees = sum(r["fees_usd"] or 0 for r in rows)
    total_funding = sum(v["funding_usd"] or 0 for v in em.values() if v.get("funding_usd") is not None)
    funded_n = sum(1 for v in em.values() if v.get("funding_usd") is not None)
    total_net = sum(v["net_usd"] or 0 for v in em.values())
    wins = sum(1 for v in em.values() if (v["net_usd"] or 0) > 0)
    retentions = [v["retention_pct"] for v in em.values() if v.get("retention_pct") is not None]
    holds = [r["hold_min"] for r in rows if r["hold_min"] is not None]
    gap_kept = sum(fp.get("n_gap_kept_sign") or 0 for fp in persistence_f.values() if fp)
    gap_tot = sum(fp.get("n_common_settlements") or 0 for fp in persistence_f.values() if fp)

    L: list[str] = []
    L.append(f"# FAZA 2 — BACKTEST vs PAPER report ({stage})")
    L.append("")
    L.append(
        f"* generated: **{_fmt_dt(now)}** · baseline start: {_fmt_dt(BASELINE_START)} · "
        f"day **{day:.2f}** of 7"
    )
    L.append(
        f"* stage: **{stage}** — decision A/B/C only after Day {TARGET_DAYS[1]} "
        f"(interim reads after Day {TARGET_DAYS[0]})"
    )
    L.append(
        f"* read-only analyzer · funnel counted from baseline only "
        f"({sandbox.excluded_pre_baseline} pre-baseline cycles excluded)"
    )
    L.append("")

    L.append("## 1. Data integrity / continuity audit")
    L.append("")
    L.append("*(continuity of snapshots and duplicates, not push counts)*")
    L.append("")
    L.append(
        _md_table(
            ["check", "value", "verdict"],
            [
                ["sandbox journal lines", cont_sandbox["journal_lines"], "ok" if not cont_sandbox["parse_errors"] else "PARSE ERRORS"],
                ["sandbox parse errors", cont_sandbox["parse_errors"], "ok" if not cont_sandbox["parse_errors"] else "CORRUPT"],
                ["incomplete tail ignored (concurrent append)", cont_sandbox["incomplete_tail_ignored"], "ok"],
                ["ts back-jumps", cont_sandbox["ts_back_jumps"], "ok" if not cont_sandbox["ts_back_jumps"] else "NON-MONOTONIC"],
                ["duplicate ts (sandbox)", cont_sandbox["duplicate_ts"], "inspect" if cont_sandbox["duplicate_ts"] else "ok"],
                ["post-baseline journal gaps > 15 min", len(cont_sandbox["gaps_over_15min"]), "inspect"],
                ["duplicate position ids", len(cont_sandbox["duplicate_position_ids"]), "ok" if not cont_sandbox["duplicate_position_ids"] else "DUPES"],
                ["journal opens w/o position", len(cont_sandbox["journal_opens_without_position"]), "ok" if not cont_sandbox["journal_opens_without_position"] else "GAP"],
                ["closed w/o close action", len(cont_sandbox["closed_positions_without_close_action"]), "ok" if not cont_sandbox["closed_positions_without_close_action"] else "GAP"],
                [
                    "open now vs last cycle field",
                    f"{cont_sandbox['open_now']} vs {cont_sandbox['last_cycle_open_positions_field']}",
                    "ok" if cont_sandbox["open_now"] == cont_sandbox["last_cycle_open_positions_field"] else "RACE/inspect",
                ],
            ],
        )
    )
    L.append("")
    if cont_sandbox["gaps_over_15min"]:
        L.append("gaps: " + "; ".join(cont_sandbox["gaps_over_15min"][:5]))
        L.append("")
    if cont_actions.get("cycles"):
        L.append(
            _md_table(
                ["github actions collector", "value"],
                [
                    ["cycles", cont_actions["cycles"]],
                    ["window", f"{cont_actions['first']} -> {cont_actions['last']}"],
                    ["coverage vs 5-min cadence", f"{cont_actions['coverage_pct']}%"],
                    ["duplicate ts", cont_actions["duplicate_ts"]],
                    ["gaps > 10 min", len(cont_actions.get("gaps_over_10min", []))],
                    ["scan rows / cycle (median)", cont_actions["scan_rows_per_cycle_median"]],
                ],
            )
        )
        L.append("")
    if cont_branch.get("available"):
        for label, f in cont_branch["files"].items():
            verdict = "ok" if not f["line_count_resets"] and not f["ts_resets"] else "RESET DETECTED"
            L.append(
                _md_table(
                    [f"paper-data: {label}", "value"],
                    [
                        ["snapshot commits (file touches)", f["snapshot_commits"]],
                        ["window", f"{f['first_snapshot']} -> {f['latest_snapshot']}"],
                        ["latest line count", f["latest_line_count"]],
                        ["latest last-ts", f["latest_last_ts"]],
                        ["line-count resets (data loss)", len(f["line_count_resets"])],
                        ["ts resets", len(f["ts_resets"])],
                        ["duplicate lines in latest", f["duplicate_lines_in_latest"]],
                        ["verdict", verdict],
                    ],
                )
            )
            if f["line_count_resets"]:
                L.append("resets: " + "; ".join(f["line_count_resets"][:5]))
            L.append("")
    else:
        L.append(f"paper-data branch audit: unavailable ({cont_branch.get('note')})")
        L.append("")
    L.append(
        _md_table(
            ["collector meta", "value"],
            [
                [
                    "runner started / respawns",
                    f"{meta['runner']['started_at']} / {meta['runner']['respawns']}",
                ],
                ["supervisor last check age (s)", meta["runner"]["last_check_age_s"]],
                ["supervisor alive", meta["runner"]["supervisor_alive"]],
                [
                    "snapshot pusher runs/ok/last",
                    f"{meta['snapshot_pusher']['runs']}/{meta['snapshot_pusher']['ok']}/{meta['snapshot_pusher']['last_result']}",
                ],
            ],
        )
    )
    L.append("")

    L.append("## 2. PAPER funnel (reject funnel = information, not failure)")
    L.append("")
    L.append(
        _md_table(
            ["source", "cycles", "scan rows Σ", "fee-gate candidates Σ", "rejected", "entered", "exited"],
            [
                [
                    "sandbox (full universe)",
                    sandbox.cycles,
                    sandbox.scan_total_sum,
                    sandbox.candidates_sum,
                    funnel_reject_total(sandbox),
                    sandbox.opens_ok,
                    sandbox.closes_ok,
                ]
            ]
            + (
                [
                    [
                        "github actions (~290 rows/cycle)",
                        actions.cycles,
                        actions.scan_total_sum,
                        actions.candidates_sum,
                        funnel_reject_total(actions),
                        actions.opens_ok,
                        actions.closes_ok,
                    ]
                ]
                if actions
                else []
            ),
        )
    )
    L.append("")
    L.append("sandbox reject funnel breakdown:")
    L.append("")
    L.append(
        _md_table(
            ["reject bucket", "count"],
            [[k, v] for k, v in sorted(sandbox.rejects.items(), key=lambda kv: -kv[1])],
        )
    )
    L.append("")
    if sandbox.reject_samples:
        for k in list(sorted(sandbox.rejects, key=lambda x: -sandbox.rejects[x]))[:3]:
            L.append(f"sample `{k}`: {sandbox.reject_samples[k][:140]}")
        L.append("")
    if sandbox.scan_total_sum:
        per1k = 1000.0 * sandbox.candidates_sum / sandbox.scan_total_sum
        extra = ""
        if actions and actions.scan_total_sum:
            extra = f" (actions: {1000.0 * actions.candidates_sum / actions.scan_total_sum:.3f} per 1k rows)"
        L.append(
            f"normalization: {sandbox.candidates_sum} candidates / {sandbox.scan_total_sum} scan rows = "
            f"**{per1k:.3f} per 1k rows**{extra}"
        )
        L.append("")

    L.append("## 3. PAPER positions — PnL ledger (closed)")
    L.append("")
    if rows:
        L.append(
            _md_table(
                [
                    "position",
                    "pair",
                    "hold(min)",
                    "expected edge %/settle",
                    "price PnL $",
                    "fees $",
                    "funding $ (recon)",
                    "net $",
                    "net %",
                    "exit",
                ],
                [
                    [
                        r["id"],
                        f"{r['base']} {r['pair']} {r['direction']}",
                        r["hold_min"],
                        r["expected_net_edge_pct"],
                        r["price_pnl_usd"],
                        r["fees_usd"],
                        (funding.get(r["id"]) or {}).get("funding_usd", "n/a"),
                        em[r["id"]]["net_usd"],
                        em[r["id"]]["net_pct"],
                        r["exit_reason"],
                    ]
                    for r in rows
                ],
            )
        )
        L.append("")
        L.append(
            f"**totals (closed, n={len(rows)}; funding reconstructed for {funded_n}):** "
            f"price **{_round(total_price, 2)}** · fees **{_round(total_fees, 2)}** · "
            f"funding **{_round(total_funding, 2)}** · net **{_round(total_net, 2)}**"
        )
        if any(r["price_pnl_estimated"] for r in rows):
            L.append("")
            L.append("note: some price PnL estimated from mark spreads (journal fills missing)")
        L.append("")
    else:
        L.append("no closed positions yet.")
        L.append("")

    L.append("## 4. Funding reconstruction detail")
    L.append("")
    for r in rows:
        fr = funding.get(r["id"])
        if not fr:
            continue
        if fr.get("error"):
            L.append(f"- `{r['id']}`: recon failed — {fr['error']}")
            continue
        legs = " · ".join(
            f"{v} settle {' / '.join(s['ts'][11:16] + ' ' + str(s['rate_pct']) + '%' for s in leg['settlements']) or '—'}"
            f" -> {leg['cash_usd']}$"
            for v, leg in fr["legs"].items()
        )
        L.append(f"- `{r['id']}`: {legs}")
    L.append("")

    L.append("## 5. Expected vs realized — edge retention")
    L.append("")
    if em:
        L.append(
            _md_table(
                [
                    "position",
                    "entry gap %",
                    "settlements in hold",
                    "expected lifetime %",
                    "realized net %",
                    "retention %",
                ],
                [
                    [
                        r["id"],
                        r["entry_spread_pct"],
                        em[r["id"]]["n_settlements"],
                        em[r["id"]]["expected_lifetime_pct"],
                        em[r["id"]]["net_pct"],
                        em[r["id"]]["retention_pct"],
                    ]
                    for r in rows
                ],
            )
        )
        L.append("")
        if retentions:
            L.append(
                f"avg edge retention: **{_round(sum(retentions) / len(retentions), 1)}%** "
                "(>100% = realized beats entry expectation)"
            )
        L.append("")
    else:
        L.append("no closed positions yet.")
        L.append("")

    L.append("## 6. Persistence")
    L.append("")
    L.append("### funding persistence (does the gap keep its entry sign during hold?)")
    L.append("")
    if persistence_f:
        L.append(
            _md_table(
                ["position", "entry gap %", "common settlements", "kept sign", "gap series %"],
                [
                    [
                        pid,
                        fp.get("entry_gap_pct"),
                        fp.get("n_common_settlements"),
                        f"{fp.get('n_gap_kept_sign')}/{fp.get('n_common_settlements')}",
                        fp.get("gaps_pct"),
                    ]
                    for pid, fp in persistence_f.items()
                    if fp
                ],
            )
        )
        L.append("")
    else:
        L.append("no reconstructed holds yet.")
        L.append("")
    L.append("### opportunity persistence (signal recurrence across cycles)")
    L.append("")
    if opp:
        L.append(
            _md_table(
                ["pair", "appearances", "% of cycles", "longest streak", "entered"],
                [
                    [
                        o["pair_display"],
                        o["appearances"],
                        o["cycles_pct"],
                        o["longest_streak_cycles"],
                        "yes" if o["entered"] else "no",
                    ]
                    for o in opp[:12]
                ],
            )
        )
        L.append("")

    L.append("## 7. BACKTEST side (majors, 30d)")
    L.append("")
    L.append(
        _md_table(
            ["metric", "value"],
            [
                ["window", bt.get("window")],
                ["bases / venues", f"{bt.get('bases')} / {bt.get('venues')}"],
                ["signal rows", bt.get("rows_total")],
                ["rows passing entry thresholds", bt.get("rows_pass_entry_thresholds")],
                ["trades", bt.get("trades")],
                ["fee gate", bt.get("fee_gate")],
            ],
        )
    )
    L.append("")
    L.append(f"_{bt.get('small_caps_note')}._")
    L.append("")

    L.append("## 8. BACKTEST vs PAPER (user metric table)")
    L.append("")
    entry_avg = _round(
        sum(r["expected_net_edge_pct"] for r in rows if r["expected_net_edge_pct"] is not None)
        / max(len([r for r in rows if r["expected_net_edge_pct"] is not None]), 1),
        3,
    ) if rows else "n/a"
    L.append(
        _md_table(
            ["metric", "BACKTEST (majors 30d)", "PAPER (collected so far)"],
            [
                [
                    "signal count",
                    f"{bt.get('rows_total')} rows",
                    f"{sandbox.scan_total_sum} scan rows / {sandbox.cycles} cycles",
                ],
                ["candidate count", bt.get("rows_pass_entry_thresholds"), sandbox.candidates_sum],
                [
                    "reject funnel",
                    "fee-gate: 100% of candidates",
                    "; ".join(f"{k}={v}" for k, v in sorted(sandbox.rejects.items(), key=lambda kv: -kv[1])),
                ],
                ["entry count", bt.get("trades"), sandbox.opens_ok],
                ["exit count", 0, sandbox.closes_ok],
                ["funding PnL $", "n/a (0 positions)", _round(total_funding, 2)],
                ["price PnL $", "n/a", _round(total_price, 2)],
                ["fees $", "n/a", _round(total_fees, 2)],
                ["net PnL $", 0, _round(total_net, 2)],
                [
                    "expected edge %",
                    f"best net edge {((bt.get('fee_gate') or {}).get('best_net_edge_pct'))}",
                    f"entry avg {entry_avg}",
                ],
                ["realized edge %", "n/a", _round(100.0 * total_net / max(sum(r['notional_usd'] for r in rows), 1e-9), 3) if rows else "n/a"],
                [
                    "edge retention",
                    "n/a",
                    f"{_round(sum(retentions) / len(retentions), 1)}% ({len(retentions)} pos)" if retentions else "n/a",
                ],
                [
                    "hold duration",
                    "n/a",
                    f"avg {_round(sum(holds) / len(holds), 0)} min (n={len(holds)})" if holds else "n/a",
                ],
                [
                    "funding persistence",
                    "n/a",
                    f"{gap_kept}/{gap_tot} settlements kept sign" if gap_tot else "n/a",
                ],
                [
                    "opportunity persistence",
                    "n/a",
                    "; ".join(
                        f"{o['pair_display']} {o['cycles_pct']}%/{o['longest_streak_cycles']}streak"
                        for o in opp[:4]
                    ) or "n/a",
                ],
            ],
        )
    )
    L.append("")

    L.append("## 9. A / B / C decision support")
    L.append("")
    L.append(
        _md_table(
            ["signal", "current value"],
            [
                ["closed positions net>0 / net<0", f"{wins} / {len(em) - wins}"],
                ["total net PnL $ (closed)", _round(total_net, 2)],
                [
                    "avg edge retention %",
                    _round(sum(retentions) / len(retentions), 1) if retentions else "n/a",
                ],
                [
                    "funding share of gross PnL %",
                    _round(100.0 * total_funding / max(total_funding + total_price, 1e-9), 1)
                    if (total_funding + total_price)
                    else "n/a",
                ],
                [
                    "exits by reason",
                    "; ".join(f"{k}={v}" for k, v in Counter(r["exit_reason"] for r in rows).items()) or "n/a",
                ],
            ],
        )
    )
    L.append("")
    L.append("decision rules (locked framework):")
    L.append("")
    L.append("- **A** net PnL > 0 across enough lifecycles -> minimal live")
    L.append("- **B** signal present but eaten by execution friction -> execution-layer changes only")
    L.append("- **C** no executable edge -> close strategy, stop feature development")
    L.append("")
    L.append(
        f"**status: {stage} — NO DECISION YET.** Current data (day {day:.2f}) is indicative only."
    )
    L.append("")

    payload = {
        "generated_at": _fmt_dt(now),
        "stage": stage,
        "day": _round(day, 3),
        "baseline_start": _fmt_dt(BASELINE_START),
        "pre_baseline_cycles_excluded": sandbox.excluded_pre_baseline,
        "continuity": {"sandbox": cont_sandbox, "actions": cont_actions, "branch": cont_branch, "meta": meta},
        "funnel": {
            "sandbox": {
                "cycles": sandbox.cycles,
                "scan_sum": sandbox.scan_total_sum,
                "scan_median": sandbox.scan_total_median,
                "candidates": sandbox.candidates_sum,
                "opens": sandbox.opens_ok,
                "closes": sandbox.closes_ok,
                "rejects": dict(sandbox.rejects),
            },
            "actions": {
                "cycles": actions.cycles,
                "scan_sum": actions.scan_total_sum,
                "candidates": actions.candidates_sum,
                "opens": actions.opens_ok,
                "closes": actions.closes_ok,
                "rejects": dict(actions.rejects),
            }
            if actions
            else None,
        },
        "pnl": [
            {k: v for k, v in r.items() if not k.startswith("_")}
            | {
                "funding_usd": (funding.get(r["id"]) or {}).get("funding_usd"),
                **em.get(r["id"], {}),
            }
            for r in rows
        ],
        "funding_recon": funding,
        "funding_persistence": persistence_f,
        "opportunity_persistence": opp,
        "backtest": bt,
        "totals": {
            "price": _round(total_price, 2),
            "fees": _round(total_fees, 2),
            "funding": _round(total_funding, 2),
            "net": _round(total_net, 2),
            "retention_avg_pct": _round(sum(retentions) / len(retentions), 1) if retentions else None,
            "wins": wins,
            "closed": len(em),
        },
    }
    return "\n".join(L), payload


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(description="FAZA 2 read-only analyzer (BACKTEST vs PAPER)")
    ap.add_argument("--no-funding", action="store_true", help="skip network funding reconstruction")
    ap.add_argument("--refresh-funding", action="store_true", help="ignore funding cache")
    ap.add_argument("--skip-git", action="store_true", help="skip git fetch / paper-data branch audit")
    ap.add_argument("--out-dir", default=str(PHASE2_DIR))
    args = ap.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    jr = read_journal(P_JOURNAL)
    sandbox = parse_funnel(jr, "sandbox", since=BASELINE_START)
    positions = _read_json(P_POSITIONS) or []
    opens, closes = index_actions(jr)
    rows = pnl_rows(positions, opens, closes)

    actions: Funnel | None = None
    cont_actions: dict[str, Any] = {"cycles": 0}
    if not args.skip_git:
        rc, content = _git(["show", "origin/paper-data:paper-data/github-actions/journal.jsonl"])
        if rc == 0 and content:
            ajr_lines = []
            errs = 0
            for ln in content.splitlines():
                if not ln.strip():
                    continue
                try:
                    ajr_lines.append(json.loads(ln))
                except json.JSONDecodeError:
                    errs += 1
            ajr = {"lines": ajr_lines, "raw": len(ajr_lines), "parse_errors": errs, "incomplete_tail": False}
            actions = parse_funnel(ajr, "github-actions")
            cont_actions = audit_actions(ajr)

    cont_sandbox = audit_sandbox(jr, positions, opens, closes)
    cont_branch = audit_paper_data_branch() if not args.skip_git else {"available": False, "note": "skipped"}
    meta = audit_meta()

    funding: dict[str, Any] = {}
    if not args.refresh_funding:
        funding = _read_json(FUNDING_CACHE) or {}
    persistence_f: dict[str, Any] = {}
    if not args.no_funding:
        for r in rows:
            fr = reconstruct_funding(r, funding, refresh=args.refresh_funding)
            if fr and not fr.get("error"):
                fp = funding_persistence(r, fr)
                if fp:
                    persistence_f[r["id"]] = fp
        FUNDING_CACHE.write_text(json.dumps(funding, indent=1), encoding="utf-8")

    em = edge_metrics(rows, funding, persistence_f)
    bt = backtest_side()
    opp = opportunity_persistence(sandbox)
    md, payload = build_report(
        sandbox, actions, cont_sandbox, cont_actions, cont_branch, meta, rows, funding, persistence_f, em, bt, opp
    )

    stamp = _now_utc().strftime("%Y%m%d-%H%M")
    md_path = out_dir / f"report-{stamp}.md"
    json_path = out_dir / f"report-{stamp}.json"
    md_path.write_text(md, encoding="utf-8")
    json_path.write_text(json.dumps(payload, indent=1), encoding="utf-8")
    (out_dir / "report-latest.md").write_text(md, encoding="utf-8")
    (out_dir / "report-latest.json").write_text(json.dumps(payload, indent=1), encoding="utf-8")

    # compact stdout summary
    print(f"[phase2] stage={payload['stage']} day={payload['day']}")
    print(
        f"[phase2] sandbox: cycles={sandbox.cycles} (pre-baseline excluded={sandbox.excluded_pre_baseline}) "
        f"scan={sandbox.scan_total_sum} cand={sandbox.candidates_sum} "
        f"entered={sandbox.opens_ok} exited={sandbox.closes_ok}"
    )
    if actions:
        print(
            f"[phase2] actions: cycles={actions.cycles} scan={actions.scan_total_sum} "
            f"cand={actions.candidates_sum} entered={actions.opens_ok}"
        )
    t = payload["totals"]
    print(
        f"[phase2] closed PnL: price={t['price']} fees={t['fees']} funding={t['funding']} "
        f"net={t['net']} wins={t['wins']}/{t['closed']} retention_avg={t['retention_avg_pct']}%"
    )
    resets = 0
    if cont_branch.get("available"):
        for f in cont_branch["files"].values():
            resets += len(f["line_count_resets"]) + len(f["ts_resets"])
    print(
        f"[phase2] integrity: sandbox_parse_errors={cont_sandbox['parse_errors']} "
        f"branch_resets={resets} supervisor_alive={meta['runner']['supervisor_alive']}"
    )
    print(f"[phase2] report: {md_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
