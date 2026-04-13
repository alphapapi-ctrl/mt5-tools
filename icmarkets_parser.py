"""
icmarkets_parser.py
===================
Parser for IC Markets MT5 Position History XLSX exports.

The file contains one sheet ("MT5 Position history List") with:
  - Row 0: "Report"
  - Row 1: Name / Produced At metadata
  - Row 2: Column headers
  - Row 3+: Deal rows (one row per leg — In or Out)

Each closed trade has two legs sharing the same Position ID:
  - "Trade Buy In" / "Trade Sell In"  → entry leg  (Profit = 0)
  - "Trade Buy Out" / "Trade Sell Out" → exit leg   (Profit = actual P/L)

Open positions have only an In leg (no Out yet).

Usage
-----
    from icmarkets_parser import parse_icmarkets_xlsx, get_icmarkets_accounts

    # Get list of accounts in the file
    accounts = get_icmarkets_accounts(file_bytes)
    # → ['11586098', '11586099']

    # Parse all accounts (returns combined DataFrame)
    df = parse_icmarkets_xlsx(file_bytes)

    # Parse a specific account
    df = parse_icmarkets_xlsx(file_bytes, account="11586098")

Output columns (normalised to match mt5_parser schema)
-------------------------------------------------------
    symbol, type, open_time, close_time, open_price, close_price,
    volume, net_profit, win, commission, swap, comment,
    _account, _strategy, position_id, position_status,
    open_date, close_date, day_of_week, hour, duration_min,
    symbol_base
"""

import io
import pandas as pd
import numpy as np


# ─────────────────────────────────────────────────────────────────────────────
# Public helpers
# ─────────────────────────────────────────────────────────────────────────────

def get_icmarkets_accounts(file_bytes: bytes) -> list[str]:
    """Return sorted list of account numbers found in the file."""
    raw = _read_raw(file_bytes)
    if raw is None:
        return []
    accounts = raw["Account Number"].dropna().unique().tolist()
    return sorted([str(a) for a in accounts if str(a).strip()])


def parse_icmarkets_xlsx(file_bytes: bytes, account: str = None) -> pd.DataFrame:
    """
    Parse IC Markets position history XLSX into a normalised trade DataFrame.

    Parameters
    ----------
    file_bytes : bytes
        Raw bytes of the .xlsx file.
    account : str, optional
        If provided, only trades for this account number are returned.
        If None, all accounts are combined.

    Returns
    -------
    pd.DataFrame with normalised columns ready for use in mt5_parser-compatible
    analysis pages.
    """
    raw = _read_raw(file_bytes)
    if raw is None or raw.empty:
        return pd.DataFrame()

    # Filter by account if requested
    if account:
        raw = raw[raw["Account Number"].astype(str) == str(account)].copy()
    if raw.empty:
        return pd.DataFrame()

    # ── Separate In (entry) and Out (exit) legs ────────────────────────────
    ins  = raw[raw["Transaction Type"].str.contains("In",  na=False)].copy()
    outs = raw[raw["Transaction Type"].str.contains("Out", na=False)].copy()

    for df in (ins, outs):
        df["Position"] = pd.to_numeric(df["Position"], errors="coerce")

    # ── Pair by Position ID ────────────────────────────────────────────────
    paired = ins.merge(
        outs[["Position", "Date Time", "Open Price", "Profit",
              "Transaction Type"]].rename(columns={
            "Date Time":        "Date Time_close",
            "Open Price":       "Close Price",
            "Profit":           "Profit_close",
            "Transaction Type": "TT_close",
        }),
        on="Position",
        how="left",   # keep open positions too (no Out leg yet)
    )

    # ── Build normalised columns ───────────────────────────────────────────
    out = pd.DataFrame()
    out["position_id"]     = paired["Position"]
    out["symbol"]          = paired["Symbol"].astype(str)
    out["symbol_base"]     = out["symbol"].str.split(".").str[0]
    out["_account"]        = paired["Account Number"].astype(str)
    out["position_status"] = paired["Position Status"].astype(str)

    # Direction from the In leg transaction type
    out["type"] = paired["Transaction Type"].apply(
        lambda x: "buy" if "Buy" in str(x) else "sell"
    )

    out["open_time"]   = pd.to_datetime(paired["Date Time"],       errors="coerce")
    out["close_time"]  = pd.to_datetime(paired["Date Time_close"], errors="coerce")
    out["open_price"]  = pd.to_numeric(paired["Open Price"],       errors="coerce")
    out["close_price"] = pd.to_numeric(paired["Close Price"],      errors="coerce")
    out["volume"]      = pd.to_numeric(paired["Trade Volume Lots"],errors="coerce")
    out["net_profit"]  = pd.to_numeric(paired["Profit_close"],     errors="coerce").fillna(0)
    out["commission"]  = 0.0   # IC Markets file doesn't separate commission
    out["swap"]        = 0.0
    out["comment"]     = ""
    out["_strategy"]   = out["symbol_base"]  # use symbol as strategy label

    # ── Derived columns ────────────────────────────────────────────────────
    out["win"]          = out["net_profit"] > 0
    out["open_date"]    = out["open_time"].dt.date
    out["close_date"]   = out["close_time"].dt.date
    out["day_of_week"]  = out["open_time"].dt.day_name()
    out["hour"]         = out["open_time"].dt.hour
    out["duration_min"] = ((out["close_time"] - out["open_time"])
                           .dt.total_seconds() / 60).round(1)

    return out.reset_index(drop=True)


# ─────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────────────────

def _read_raw(file_bytes: bytes) -> pd.DataFrame | None:
    """Read the raw sheet and return a DataFrame with named columns."""
    try:
        buf = io.BytesIO(file_bytes)
        df  = pd.read_excel(buf, sheet_name=0, header=None, dtype=str)
    except Exception:
        return None

    # Find the header row — it contains "Symbol" in column 0
    header_row = None
    for i, row in df.iterrows():
        if str(row.iloc[0]).strip() == "Symbol":
            header_row = i
            break

    if header_row is None:
        return None

    df.columns = df.iloc[header_row].tolist()
    df = df.iloc[header_row + 1:].copy()
    df.columns.name = None

    # Keep only real data rows (Position Status = Open or Closed)
    if "Position Status" in df.columns:
        df = df[df["Position Status"].isin(["Open", "Closed"])].copy()

    return df.reset_index(drop=True)