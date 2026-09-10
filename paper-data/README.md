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
