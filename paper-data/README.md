# Paper validation data (append-only snapshots)

This branch preserves the paper-trading data collection for the
BACKTEST ↔ PAPER comparison milestone. The live collector runs
`scripts/execution/run_pure_futures_spread.py --config templates/config.pure_futures.spread.json --watch 5 --verbose`
(paper mode = public market data only, dry_run, no API keys).

Files:
- `journal.jsonl` — one line per 5-min cycle: scan totals, candidate funnel,
  per-attempt gate results (depth / funding re-check / execution state).
  Append-only; each commit = a later snapshot.
- `positions.json` — current paper positions ledger (atomic writes).
- `strategy_config.json` — REQUIRED local override (trade_usd 500). Without it
  DEFAULT_STRATEGY silently raises notional to 5000 USD per pair.
- `backtest_analysis.json`, `backtest_30d_btc_eth_sol.json` — backtest side of
  the comparison (majors, 30d, fee-gate verdict).
- `paper_runner.log` — collector stdout tail.

To contribute as an external tester: run the collector on your own machine for
3-7 days, then send/PR your `scripts/data/pure-futures/journal.jsonl`.

## Collectors writing to this branch (3 independent samples)

- `journal.jsonl` + `positions.json` (top level) — snapshots pushed from the
  SANDBOX collector (manual periodic snapshots; append-only).
- `github-actions/journal.jsonl` + `github-actions/positions.json` — continuous
  collection by the scheduled `paper-collector` workflow (cron every 5 min on
  GitHub runners; each cycle appends one journal line). CAVEAT: GitHub runners
  see a reduced symbol universe (~290 vs ~2500 scan rows per cycle, shared-IP
  venue rate limiting) — every line records its own `scan_total`, so normalize
  per-row rates when comparing samples. Highest-edge signals (RVN, KR200) still
  surface in the reduced universe.
- (planned) `tester/` — journal from an external crypto-user tester running the
  same commands on their own machine for 3-7 days.

All three run the identical collector, identical thresholds, identical gates.

## github-actions collector specifics (data caveats)

- Each Actions run starts with an EMPTY workspace: the github-actions sample is
  STATELESS — it measures the signal funnel (scan -> candidates -> gate
  rejects) but cannot hold paper positions across cycles, so it contributes
  no entry/exit/hold/PnL lifecycle data. Position lifecycle data comes only
  from persistent collectors (sandbox / external tester).
- The heartbeat trigger fires repository_dispatch every ~5 min from the
  sandbox dev-server (fork cron-schedule registration lags; dispatch is
  immediate). If the sandbox dies, GitHub-side cycles stop until the cron
  schedule arms itself or the sandbox returns.
