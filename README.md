# MT5 Tools

A standalone Streamlit dashboard for MetaTrader 5 trade analysis, batch backtesting, portfolio construction and EA settings comparison.

> **Windows only** — the Batch Backtest feature requires MetaTrader 5 and Windows. All other pages run on any OS.

---

## Features

### 📊 Trade Analysis
Import and analyse trade history from multiple formats:
- MT5 account history reports (`.htm`)
- MT5 backtest reports (`.htm`)
- Quant Analyzer CSV exports
- **IC Markets** position history XLSX — multi-account support with account selector

Analysis modes: Overall · By Strategy · By Symbol · By Day of Week  
Filters: date range, symbol, strategy/EA, day of week, trade type  
Stats: net profit, win rate, profit factor, R:R, expectancy, max DD, consecutive wins/losses, duration  
Charts: equity curve, P&L by day of week, P&L by hour of day

---

### 📈 Portfolio Builder
Load multiple backtest files and view them as a combined portfolio.

- Tabs: Overview · Trades · Equity Chart · Strategies · What-If · Portfolios
- Equity curve with peak drawdown panel and daily P&L bars
- What-If: scale all stats by a target lot size
- Create and name custom sub-portfolios from loaded strategies

---

### 🏆 Portfolio Master
Automated portfolio construction — searches combinations of loaded strategies and ranks them by a composite score.

**Composite scoring (0–1000):**
- Ret/DD · Stability (R²) · Stagnation · Win Rate · Growth Quality · Diversity
- Six weight sliders — normalised automatically

**Search modes:**
| Mode | Description |
|------|-------------|
| Exhaustive | Every combination — count and estimated time shown before run |
| Greedy (fast) | Incremental build from each strategy seed |
| Monte Carlo | Random sampling with configurable sample count |
| Greedy + Monte Carlo | Both combined, deduplicated |

Cancel button stops the search mid-run and returns results found so far.

**Correlation:**
- Pairwise correlation filter (max threshold slider)
- Average portfolio correlation as output metric
- Conditional correlation (drawdown days only) — how strategies co-move during losses
- Per-result correlation heatmaps in detail expanders

---

### 🔀 Trade Compare
Compare two trade history files to measure slippage and profit variance — typically a backtest vs live account run of the same EA.

- Match tolerance: 1–24 hours
- Slippage summary: avg entry slip, exit slip, profit variance, time difference
- Charts: equity overlay, profit variance bar chart per trade
- CSV export

---

### ⚙️ EA Settings Comparator
Upload 2–10 MT5 `.set` files and compare parameters side-by-side.

- Percentage variation highlighting
- Show differences only toggle
- Inline editing
- Export single file or all as ZIP

---

### 📋 Batch Backtest *(Windows only)*
Streamlit UI wrapper around the CLI batch runner. Runs MT5 backtests sequentially for a folder of `.set` files.

- Reads config from `mt5_batch_config.json`
- Lot size modes: as-is · manual fixed lots · lots per balance
- Report naming: `{Strategy}_{Symbol}_{Period}_{Model}_{Instance}.htm`
  - `.set` filename stem becomes the instance letter (e.g. `A.set` → `_A`)
- Instrument and timeframe: one for all files or auto-detect from filename
- Editable preview table before running
- Live progress bar — cancel mid-run
- Optimisation files excluded automatically (any spelling/case of `optimiz`/`optimis`)
- **Windows check** — shows a clear warning and exits if not running on Windows

---

## Requirements

- Windows 10 / 11
- Python 3.11+
- MetaTrader 5 (for Batch Backtest)

---

## Installation

```bash
git clone https://github.com/alphapapi-ctrl/mt5-tools.git
cd mt5-tools
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
streamlit run app.py
```

### Batch Backtest — first-run setup

Run the CLI script once to create `mt5_batch_config.json`:

```bash
python mt5_batch_backtest.py
```

Follow the prompts to configure your MT5 terminal path, tester folder, EA, dates, deposit and suffix. Config is saved locally and gitignored.

---

## Supported File Formats

| Format | Source |
|--------|--------|
| MT5 account history HTM | MetaTrader 5 → File → Save As Report |
| MT5 backtest HTM | Strategy Tester report tab |
| Quant Analyzer CSV | Trade List → Export CSV |
| IC Markets XLSX | IC Markets portal → Reports → MT5 Position History → Export |

---

## Project Structure

```
mt5-tools/
├── app.py                    # Main app — navigation and routing
├── mt5_parser.py             # MT5 HTM + Quant Analyzer CSV parsers
├── icmarkets_parser.py       # IC Markets XLSX parser
├── mt5_batch_backtest.py     # CLI batch backtest runner
├── set_comparator.py         # EA .set file comparison logic
├── view_trade_analysis.py    # Trade Analysis page
├── view_trade_compare.py     # Trade Compare page
├── view_portfolio_builder.py # Portfolio Builder page
├── view_portfolio_master.py  # Portfolio Master page
├── view_set_comparator.py    # EA Settings Comparator page
├── view_batch_backtest.py    # Batch Backtest page (Streamlit UI)
├── view_settings.py          # Settings page
├── requirements.txt
└── mt5_batch_config.json     # Created on first run (gitignored)
```

---

## Notes

- `mt5_batch_config.json` is gitignored — create it by running `mt5_batch_backtest.py` once
- `set_comparator.py` may need to be copied from your EA project folder if not included
- MT5 `.set` files are UTF-16 LE encoded — handled automatically
- Streamlit's `pages/` folder name is reserved — view files sit at root level prefixed with `view_`
- `git config core.autocrlf true` suppresses LF/CRLF warnings on Windows

---

## License

MIT