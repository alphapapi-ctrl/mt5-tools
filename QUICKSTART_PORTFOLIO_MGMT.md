# Live EA Portfolio Management — quick start

The **Live EA Portfolio Mgmt** page is a rules-based review layer for a team
of trading robots (EAs). It works standalone: everything it needs ships in
this repo. Nothing here is tied to any particular account or portfolio — you
configure yours once and the page adapts.

## What it does, in one paragraph

You write **benching rules** (bench a robot after N losing days, after its
losing streak has cost $X, after its drawdown hits $Y, or when it beats its
own historical worst). Those rules are checked against your **benchmark
accounts** — demo accounts running the whole robot pool at the standard size
($100k balance, lot step = HistoricalMaxDD / 5%), so every threshold means the
same thing for every robot. Any robot that trips a rule on the bench is then
**matched to the live accounts running it** — that is your action list for the
day. A separate size-free check flags a live copy doing *worse than the same
robot on the bench* (an account problem: fills, VPS, set-file — not the
robot's form). **Swap-in candidates** are ranked on recent form.

## Setup (once)

1. **Live data feed** — on the *Live MT5 EAs* page: attach the ReportExporter
   EA to each MT5 account, point it at your FTP server, add the FTP details
   and your accounts (label, balance, live/demo). Refresh so the cache fills.
2. **Benchmark accounts** — *Live EA Portfolio Mgmt → 🧪 Benchmark accounts*:
   tick the demo accounts that run your candidate pool at the standard size
   (one per bucket works well — FX / Gold / Indices / Crypto). Save.
3. **Rules** — *⚙️ Benching rules*: the defaults are the set validated in
   backtest (streak-cost rule + drawdown insurance, relative rules on, 21-day
   cooling-off). Adjust, save.
4. **Set files** (optional) — if your UBS set files are somewhere other than
   the batch backtester's folder, run `python build_ea_name_map.py --sets
   <folder>` once so live EA comments map to strategy names.

That's it. The compiled datasets ship in `engine_data/timeline/`:

- `main_pool_2018` — 140 UBS robots, 2018–2026, the long-history baseline
  (streak baselines for the relative rules, fill-trust badges).
- `proxy_3m_realticks` — the same pool over the last 3 months on real ticks,
  the recent-form source for swap-in candidates.

## Weekly review

1. Refresh accounts on *Live MT5 EAs*.
2. *🎛 Management*: read **1 · rules checked on the bench** → **2 · matched to
   live accounts** (act on these; press **Mark as benched** so the 21-day
   cooling-off applies) → **3 · live copies behaving worse than the bench**.
3. **Swap-in candidates**: a shortlist by recent form, with each robot's
   fill-trust badge (✅ real-tick / 🟢 / 🟡 / 🔴 how far its backtest fills can
   be believed). Check the Market column against what the account already
   holds before adding — the list does not know your book.
4. Every couple of weeks, refresh the recent-form proxy: batch-backtest your
   set files over the last 3 months at **Model 4 — REALTICKS**, then on the
   *🧪 Benchmark accounts* tab point the compiler at the reports folder and
   compile. Trust badges refresh automatically.

## Where things live (per install, never committed)

- `ea_rules_config.json` — your accounts, rules, dataset choices
- `ea_bench_log.json` — robots you marked as benched (cooling-off)
- `ea_name_map.json` — comment → strategy map built from *your* set files
- `engine_data/timeline/proxy_*` — proxies you compile yourself
