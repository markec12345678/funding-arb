# Partial-Fill / One-Leg Recovery Engine — Design Spec (FAZA 3, first upgrade)

> STATUS: **DESIGN ONLY — NOT IMPLEMENTED.** This lands only after the FAZA 2
> (backtest ↔ paper) comparison and only if the decision is **A** (edge exists →
> prerequisites for real capital) or **B** (signal exists, execution eats it →
> exactly this layer). If decision **C**, this spec is archived unimplemented.
>
> Scope discipline: no new strategies, no new venues, no threshold changes.
> This document is the implementation contract for the single P0 item the user
> ranked #1: formal one-leg/partial-fill recovery, replacing best-effort
> rollback with an explicit state machine.

## 1. Problem

Today a two-leg entry has three outcomes per leg: filled, not filled, or
partially filled. The executor handles the first two (rollback on failure) but
a *partial* fill of one leg leaves a qty mismatch that is recorded
(`long_qty`/`short_qty`) yet not acted upon at entry time — the excess stays
unhedged until the watcher's rebalance pass notices it. For real capital this
is the largest single risk in the system: an unhedged tail on a thin book can
lose more than the whole spread edge.

## 2. Current behavior (verified against code, commit e071f9a)

| Aspect | Location | Behavior |
|---|---|---|
| Actual-fill extraction | `pure_futures_executor.py` `_exec_qty(res, fallback)` | Per-leg fill qty already parsed from every venue response |
| Parallel entry (default) | same, `parallel_legs` block ~L458-560 | Both legs submitted concurrently; on both-filled records `qty=min(long,short)`, `long_qty`, `short_qty`; **excess unhedged at entry**; on one-leg failure → best-effort rollback → `rolled_back` or `naked` + alert |
| Sequential entry | same, ~L575-650 | Short leg sized to long's actual fill (`exec_qty`); partial-short mismatch still possible; short failure → rollback long |
| Qty-mismatch repair (delayed) | `pure_futures_watcher.py` `check_rebalance` + `rebalance_pure_futures_pair` | Watcher trims oversized leg on QUANTITY mismatch (real delta from partial fill/ADL); `autoRebalance` defaults false (alert-only) |
| Result states | `CrossVenueResult.state` | `aborted / rolled_back / naked / filled / simulated` — no intermediate repair states |
| Position record | `_record_position` | Flat fields; no per-leg fill detail (fee, avg px, ts), no repair history, no lifecycle state field |

Conclusion: the accounting foundation exists; the missing piece is a formal,
immediate, entry-time recovery path with explicit states.

## 3. State machine

```
                ┌────────────────────────────────────────────┐
                │ (pre-order gates: depth → recheck → margin)│
                └───────────────────┬────────────────────────┘
                                    ▼ submit both legs
                              PAIR_PENDING
                     ┌────────────────┴─────────────────┐
              long filled                     long not filled
                     ▼                                   ▼
               LEG_A_FILLED                      (rollback short / abort)
        ┌──────────┴───────────┐
   short filled          short partial / failed
        ▼                        ▼
   LEG_B_FILLED            LEG_B_PARTIAL
        │                        │ policy decision (§4)
        ▼                        ▼
      HEDGED  ◄── REPAIR ──┘ (trim oversized leg | retry undersized leg)
                    │ repair fails / mismatch intolerable
                    ▼
           EMERGENCY_UNWIND ──► UNWOUND (+ alert, quarantine-style ledger note)

Terminal states: HEDGED, ROLLED_BACK, UNWOUND. NAOKED must become impossible
as a *resting* state — it may only appear transiently inside EMERGENCY_UNWIND
execution and must terminate in UNWOUND or an escalated alert.
```

Rules:
- Transitions are persisted atomically with the position record (same locked
  write as today — the P0 atomic-persistence layer).
- The watcher must not run exit logic on non-`HEDGED` positions (exit evaluates
  funding edge, which is undefined for an unhedged pair); it MAY run repair
  escalation if a position rests too long in `LEG_B_PARTIAL`.
- `close_pure_futures_pair` refuses non-`HEDGED` positions unless forced via
  the unwind path.

## 4. Repair policy (the core decision)

Given `mismatch = |long_qty − short_qty|` after both legs settle:

1. **mismatch ≤ epsilon** (epsilon = qty precision floor, i.e. the exchange's
   minimum step): already balanced → `HEDGED`.
2. **mismatch > epsilon, policy `trim` (DEFAULT)**: partially close the
   OVERSIZED leg down to the smaller qty. One deterministic order, no new
   price exposure beyond the leg's own book, fee cost known. This mirrors the
   watcher's proven `rebalance_pure_futures_pair` trim logic — reuse its
   order-construction, moved to entry time.
3. **policy `retry`** (opt-in only): submit the remainder for the undersized
   leg. Exposure to further drift + second fee; only sensible when the book is
   deep and the mismatch is large relative to epsilon.
4. **Emergency**: repair order itself fails, or mismatch > `maxMismatchPct`
   (default 25% of target qty) → close the filled portion of BOTH legs
   (unwind), state `EMERGENCY_UNWIND` → `UNWOUND`, alert fires. An unrecoverable
   unwind failure leaves the alert + ledger note — never a silent naked state.

## 5. Ledger changes (position record)

```jsonc
{
  "id": "pf-...",
  "state": "HEDGED",                  // §3 machine state (new, required)
  "qty": 214408,                       // balanced qty (= min after repair)
  "leg_fills": {                       // (new) per-leg ground truth
    "long":  {"qty": 214408, "avg_px": 0.002332, "fee_usd": 0.276, "ts": 1789...},
    "short": {"qty": 213900, "avg_px": 0.002318, "fee_usd": 0.25,  "ts": 1789...}
  },
  "repairs": [                         // (new) audit trail
    {"ts": 1789..., "action": "trim_long", "qty": 508, "state": "HEDGED"}
  ],
  ...existing fields unchanged...
}
```

Backward compatibility: positions written by the current engine get `state:
"HEDGED"` synthesized on read when `status == "open"` and both `long_qty` /
`short_qty` are present and equal (or absent → pre-parallel era = sequential =
balanced by construction). A migration is NOT required.

## 6. Paper-mode parity

- Gates already run in paper mode (post-P0 restructure). The state machine
  must also run in paper: simulated fills are all-or-nothing today, so paper
  exercises `PAIR_PENDING → LEG_A_FILLED → LEG_B_FILLED → HEDGED` plus the
  failure paths (abort/rollback) — the REPAIR and EMERGENCY paths are covered
  by tests that inject partial fills via the existing fake venues.
- No behavioral change to thresholds or candidate selection.

## 7. Test plan (extends the 539-test suite)

1. Unit (state machine): every legal transition; refusal of illegal ones;
   epsilon boundary; emergency thresholds.
2. Integration (fake venues, hermetic — same harness as
   `test_e2e_paper_flow.py`): long full / short 74% → trim path → `HEDGED`
   with balanced qty + repair record; retry path; repair failure → unwind →
   `UNWOUND` + alert; watcher ignores non-HEDGED for exit; close refusal on
   non-HEDGED.
3. Regression: all existing executor/watcher/CLI tests unchanged (state field
   additive; `min()` semantics preserved post-repair).
4. Offline proof: full suite must pass with sockets blocked (project standard).

## 8. Acceptance criteria

- No resting non-terminal state without a persisted reason and (for
  EMERGENCY_UNWIND) an alert.
- Every `HEDGED` position satisfies `long_qty == short_qty` (within epsilon).
- `naked` as a terminal CrossVenueResult state is eliminated in the parallel
  path (replaced by the machine's unwind path).
- Zero changes to: thresholds, venue set, strategy selection, fee model.
- Suite green offline; new coverage ≥ ~25 tests for the machine + paths.

## 9. Sequencing

1. FAZA 2 report + A/B/C decision (data collection running now).
2. If A or B: implement per this spec — executor integration first (entry-time
   trim), then ledger fields, then watcher escalation, then tests.
3. Re-run paper validation for a shortened confirmation window before any
   live consideration (per the user's minimal-live checklist).
