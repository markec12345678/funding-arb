# FAZA 2 — BACKTEST vs PAPER report (INTERIM)

* generated: **2026-09-10 17:19:12Z** · baseline start: 2026-09-10 14:36:00Z · day **0.11** of 7
* stage: **INTERIM** — decision A/B/C only after Day 7 (interim reads after Day 3)
* read-only analyzer · funnel counted from baseline only (18 pre-baseline cycles excluded)

## 1. Data integrity / continuity audit

*(continuity of snapshots and duplicates, not push counts)*

| check | value | verdict |
|---|---|---|
| sandbox journal lines | 53 | ok |
| sandbox parse errors | 0 | ok |
| incomplete tail ignored (concurrent append) | False | ok |
| ts back-jumps | 0 | ok |
| duplicate ts (sandbox) | 0 | ok |
| post-baseline journal gaps > 15 min | 0 | inspect |
| duplicate position ids | 0 | ok |
| journal opens w/o position | 0 | ok |
| closed w/o close action | 0 | ok |
| open now vs last cycle field | 2 vs 2 | ok |

| github actions collector | value |
|---|---|
| cycles | 12 |
| window | 2026-09-10 15:53:08Z -> 2026-09-10 17:17:49Z |
| coverage vs 5-min cadence | 66.9% |
| duplicate ts | 0 |
| gaps > 10 min | 2 |
| scan rows / cycle (median) | 302.0 |

| paper-data: sandbox_snapshots | value |
|---|---|
| snapshot commits (file touches) | 3 |
| window | 2026-09-10T15:11:33Z -> 2026-09-10T16:57:33Z |
| latest line count | 49 |
| latest last-ts | 2026-09-10T16:56:43.090156+00:00 |
| line-count resets (data loss) | 0 |
| ts resets | 0 |
| duplicate lines in latest | 0 |
| verdict | ok |

| paper-data: github_actions | value |
|---|---|
| snapshot commits (file touches) | 12 |
| window | 2026-09-10T15:53:09Z -> 2026-09-10T17:17:50Z |
| latest line count | 12 |
| latest last-ts | 2026-09-10T17:17:49.779214+00:00 |
| line-count resets (data loss) | 0 |
| ts resets | 0 |
| duplicate lines in latest | 0 |
| verdict | ok |

| collector meta | value |
|---|---|
| runner started / respawns | 2026-09-10 14:36:36Z / 4 |
| supervisor last check age (s) | 14.0 |
| supervisor alive | True |
| snapshot pusher runs/ok/last | 1/1/pushed |

## 2. PAPER funnel (reject funnel = information, not failure)

| source | cycles | scan rows Σ | fee-gate candidates Σ | rejected | entered | exited |
|---|---|---|---|---|---|---|
| sandbox (full universe) | 35 | 79837 | 303 | 41 | 4 | 2 |
| github actions (~290 rows/cycle) | 12 | 3607 | 13 | 12 | 1 | 0 |

sandbox reject funnel breakdown:

| reject bucket | count |
|---|---|
| depth_gate | 24 |
| mark_spread_gate/absurd(>=5%) | 10 |
| other | 7 |

sample `depth_gate`: depth check: short@okx: only $128 within ±0.3% window (need $1500 = 3.0×$500)
sample `mark_spread_gate/absurd(>=5%)`: Inter-venue perp mark spread 28.85% > 1.0%, rejecting open
sample `other`: perp price unavailable long=0.01657 short=0.0

normalization: 303 candidates / 79837 scan rows = **3.795 per 1k rows** (actions: 3.604 per 1k rows)

## 3. PAPER positions — PnL ledger (closed)

| position | pair | hold(min) | expected edge %/settle | price PnL $ | fees $ | funding $ (recon) | net $ | net % | exit |
|---|---|---|---|---|---|---|---|---|---|
| pf-RVN-okx-bybit-1789053113-c4b85d | RVN okx/bybit reverse | 50.0 | 0.529327 | 0.64 | -1.05 | 3.94 | 3.53 | 0.706 | pair_disappeared |
| pf-RVN-okx-bitget-1789050960-1f2651 | RVN okx/bitget reverse | 105.7 | 0.3239 | -1.07 | -1.1 | 3.74 | 1.57 | 0.314 | edge_below_exit |

**totals (closed, n=2; funding reconstructed for 2):** price **-0.43** · fees **-2.15** · funding **7.68** · net **5.1**

## 4. Funding reconstruction detail

- `pf-RVN-okx-bybit-1789053113-c4b85d`: okx settle 16:00 -1.0% -> 5.0$ · bybit settle 16:00 -0.2121% -> -1.06$
- `pf-RVN-okx-bitget-1789050960-1f2651`: okx settle 16:00 -1.0% -> 5.0$ · bitget settle 16:00 -0.2538% -> -1.26$

## 5. Expected vs realized — edge retention

| position | entry gap % | settlements in hold | expected lifetime % | realized net % | retention % |
|---|---|---|---|---|---|
| pf-RVN-okx-bybit-1789053113-c4b85d | 0.634327 | 1 | 0.4243 | 0.706 | 166.4 |
| pf-RVN-okx-bitget-1789050960-1f2651 | 0.4339 | 1 | 0.2139 | 0.314 | 146.8 |

avg edge retention: **156.6%** (>100% = realized beats entry expectation)

## 6. Persistence

### funding persistence (does the gap keep its entry sign during hold?)

| position | entry gap % | common settlements | kept sign | gap series % |
|---|---|---|---|---|
| pf-RVN-okx-bybit-1789053113-c4b85d | 0.6343 | 1 | 1/1 | [0.7879] |
| pf-RVN-okx-bitget-1789050960-1f2651 | 0.4339 | 1 | 1/1 | [0.7462] |

### opportunity persistence (signal recurrence across cycles)

| pair | appearances | % of cycles | longest streak | entered |
|---|---|---|---|---|
| KR200 long@bitget short@okx | 21 | 60.0 | 21 | yes |
| STORJ long@bybit short@binance | 10 | 28.6 | 7 | no |
| PUFFER long@bybit short@binance | 7 | 20.0 | 7 | no |
| RVN long@okx short@bybit | 3 | 8.6 | 2 | yes |
| GPRO long@bitget short@bybit | 2 | 5.7 | 2 | no |
| RVN long@okx short@bitget | 1 | 2.9 | 1 | yes |
| SOPH long@bybit short@bitget | 1 | 2.9 | 1 | yes |

## 7. BACKTEST side (majors, 30d)

| metric | value |
|---|---|
| window | 2026-08-11T15:00Z -> 2026-09-10T14:00Z (30.0 days) |
| bases / venues | ['BTC', 'ETH', 'SOL'] / ['binance', 'bitget', 'bybit', 'okx', 'hyperliquid'] |
| signal rows | 27876 |
| rows passing entry thresholds | 0 |
| trades | 0 |
| fee gate | {'round_trip_taker_pct': 0.11, 'best_spread_pct': 0.0218, 'best_net_edge_pct': -0.0882, 'median_abs_net_edge_pct': 0.1084, 'breakeven_settlements_at_max_spread': 5.05, 'note': 'Best per-settlement spread of the whole 30d window was 5x below the two-leg taker fee; spikes mean-revert within one settlement (p99 = 0.0000%).'} |

_small-cap/event segment is NOT covered by the 30d majors backtest — paper is the only evidence tier for it._

## 8. BACKTEST vs PAPER (user metric table)

| metric | BACKTEST (majors 30d) | PAPER (collected so far) |
|---|---|---|
| signal count | 27876 rows | 79837 scan rows / 35 cycles |
| candidate count | 0 | 303 |
| reject funnel | fee-gate: 100% of candidates | depth_gate=24; mark_spread_gate/absurd(>=5%)=10; other=7 |
| entry count | 0 | 4 |
| exit count | 0 | 2 |
| funding PnL $ | n/a (0 positions) | 7.68 |
| price PnL $ | n/a | -0.43 |
| fees $ | n/a | -2.15 |
| net PnL $ | 0 | 5.1 |
| expected edge % | best net edge -0.0882 | entry avg 0.427 |
| realized edge % | n/a | 0.51 |
| edge retention | n/a | 156.6% (2 pos) |
| hold duration | n/a | avg 78.0 min (n=2) |
| funding persistence | n/a | 2/2 settlements kept sign |
| opportunity persistence | n/a | KR200 long@bitget short@okx 60.0%/21streak; STORJ long@bybit short@binance 28.6%/7streak; PUFFER long@bybit short@binance 20.0%/7streak; RVN long@okx short@bybit 8.6%/2streak |

## 9. A / B / C decision support

| signal | current value |
|---|---|
| closed positions net>0 / net<0 | 2 / 0 |
| total net PnL $ (closed) | 5.1 |
| avg edge retention % | 156.6 |
| funding share of gross PnL % | 105.9 |
| exits by reason | pair_disappeared=1; edge_below_exit=1 |

decision rules (locked framework):

- **A** net PnL > 0 across enough lifecycles -> minimal live
- **B** signal present but eaten by execution friction -> execution-layer changes only
- **C** no executable edge -> close strategy, stop feature development

**status: INTERIM — NO DECISION YET.** Current data (day 0.11) is indicative only.
