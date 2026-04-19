"""
ftp_sync_cli.py
===============
CLI tool to pull MT5 published account history from FTP and display stats.
Run from MT5Tools folder with venv activated.

Usage:
    python ftp_sync_cli.py --host 192.168.1.x --user ftpuser --pass ftppass
    python ftp_sync_cli.py --host 192.168.1.x --user ftpuser --pass ftppass --list
    python ftp_sync_cli.py --host 192.168.1.x --user ftpuser --pass ftppass --account 12345
    python ftp_sync_cli.py --config  (use saved config in ftp_config.json)

Config is saved to ftp_config.json after first run (gitignored).
"""

import argparse
import ftplib
import json
import os
import sys
from pathlib import Path
from datetime import datetime

CONFIG_FILE = Path(__file__).parent / "ftp_config.json"
CACHE_DIR   = Path(__file__).parent / "cache"


# ── Config ────────────────────────────────────────────────────────────────────

def load_config() -> dict:
    if CONFIG_FILE.exists():
        return json.loads(CONFIG_FILE.read_text())
    return {}


def save_config(cfg: dict):
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2))
    print(f"Config saved to {CONFIG_FILE}")


# ── FTP helpers ───────────────────────────────────────────────────────────────

def connect_ftp(host: str, user: str, password: str, port: int = 21) -> ftplib.FTP:
    ftp = ftplib.FTP()
    ftp.connect(host, port, timeout=10)
    ftp.login(user, password)
    ftp.set_pasv(True)
    return ftp


def list_accounts(ftp: ftplib.FTP) -> list:
    """List top-level directories on FTP — each should be an account folder."""
    items = []
    ftp.retrlines("LIST", items.append)
    folders = []
    for item in items:
        parts = item.split()
        if item.startswith("d") and parts:
            folders.append(parts[-1])
    return folders


def find_report_file(ftp: ftplib.FTP, account_folder: str) -> str | None:
    """
    Find the HTML report file inside an account folder.
    MT5 typically publishes as: account_folder/report.htm or account_folder/Report.htm
    """
    try:
        ftp.cwd(f"/{account_folder}")
    except ftplib.error_perm:
        try:
            ftp.cwd(account_folder)
        except ftplib.error_perm:
            return None

    files = []
    ftp.retrlines("NLST", files.append)
    for f in files:
        if f.lower().endswith(('.htm', '.html')):
            return f
    return None


def download_report(ftp: ftplib.FTP, account_folder: str,
                    filename: str) -> bytes:
    """Download report file and return raw bytes."""
    buf = []
    ftp.retrbinary(f"RETR {filename}", buf.append)
    return b"".join(buf)


# ── Parse + display ───────────────────────────────────────────────────────────

def display_stats(stats: dict, fmt: str, account_folder: str):
    """Print stats to console in a readable format."""
    sep = "─" * 60
    print(f"\n{sep}")
    print(f"  Account: {account_folder}  |  Format: {fmt}")
    print(sep)

    rows = [
        ("Net Profit",      f"${stats.get('net_profit', 0):,.2f}"),
        ("Total Trades",    stats.get('total_trades', 0)),
        ("Win Rate",        f"{stats.get('win_rate', 0)}%"),
        ("Profit Factor",   stats.get('profit_factor', 0)),
        ("R:R Ratio",       stats.get('rr_ratio', 0)),
        ("Expectancy",      f"${stats.get('expectancy', 0):,.2f}"),
        ("Max Drawdown",    f"${stats.get('max_drawdown', 0):,.2f}"),
        ("Avg Win",         f"${stats.get('avg_win', 0):,.2f}"),
        ("Avg Loss",        f"${stats.get('avg_loss', 0):,.2f}"),
        ("Best Trade",      f"${stats.get('best_trade', 0):,.2f}"),
        ("Worst Trade",     f"${stats.get('worst_trade', 0):,.2f}"),
        ("Max Consec Wins", stats.get('max_consec_wins', 0)),
        ("Max Consec Loss", stats.get('max_consec_losses', 0)),
        ("Trading Days",    stats.get('trading_days', 0)),
        ("Trades/Day",      stats.get('trades_per_day', 0)),
        ("Long Trades",     f"{stats.get('long_trades', 0)} ({stats.get('long_win_rate', 0)}% WR)"),
        ("Short Trades",    f"{stats.get('short_trades', 0)} ({stats.get('short_win_rate', 0)}% WR)"),
    ]

    for label, value in rows:
        print(f"  {label:<22} {value}")
    print(sep)


def display_monthly(df):
    """Print monthly P&L breakdown."""
    import pandas as pd
    if df is None or df.empty:
        return
    tmp = df[['close_time', 'net_profit']].dropna().copy()
    tmp['close_time'] = pd.to_datetime(tmp['close_time'], errors='coerce')
    tmp['ym'] = tmp['close_time'].dt.strftime('%Y-%m')
    monthly = tmp.groupby('ym')['net_profit'].sum().sort_index()

    print("\n  Monthly P&L:")
    print("  " + "─" * 30)
    for ym, pnl in monthly.items():
        bar   = "█" * min(int(abs(pnl) / 10), 30)
        sign  = "+" if pnl >= 0 else ""
        color = "\033[92m" if pnl >= 0 else "\033[91m"
        reset = "\033[0m"
        print(f"  {ym}  {color}{sign}${pnl:>8.2f}  {bar}{reset}")
    print()


def display_recent_trades(df, n=10):
    """Print the most recent N trades."""
    if df is None or df.empty:
        return
    import pandas as pd
    df = df.sort_values('close_time', ascending=False).head(n)
    print(f"\n  Last {n} trades:")
    print("  " + "─" * 70)
    print(f"  {'Date':<22} {'Symbol':<12} {'Type':<6} {'Profit':>10}")
    print("  " + "─" * 70)
    for _, row in df.iterrows():
        pnl   = row.get('net_profit', 0)
        color = "\033[92m" if pnl >= 0 else "\033[91m"
        reset = "\033[0m"
        print(f"  {str(row.get('close_time','')):<22} "
              f"{str(row.get('symbol','')):<12} "
              f"{str(row.get('type','')):<6} "
              f"{color}${pnl:>9.2f}{reset}")
    print()


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Pull MT5 FTP published reports and display stats",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__
    )
    parser.add_argument("--host",    help="FTP host/IP")
    parser.add_argument("--user",    help="FTP username")
    parser.add_argument("--password","--pass", dest="password", help="FTP password")
    parser.add_argument("--port",    type=int, default=21, help="FTP port (default: 21)")
    parser.add_argument("--account", help="Account folder name (default: first found)")
    parser.add_argument("--list",    action="store_true", help="List account folders and exit")
    parser.add_argument("--config",  action="store_true", help="Use saved ftp_config.json")
    parser.add_argument("--save",    action="store_true", help="Save connection details to ftp_config.json")
    parser.add_argument("--trades",  type=int, default=10, metavar="N", help="Show last N trades (default: 10)")
    parser.add_argument("--no-monthly", action="store_true", help="Skip monthly breakdown")
    parser.add_argument("--save-cache", action="store_true", help="Save parsed data to cache/ folder")
    args = parser.parse_args()

    # Load from config if requested
    cfg = {}
    if args.config or (not args.host):
        cfg = load_config()
        if not cfg:
            print("No ftp_config.json found. Run with --host, --user, --password first.")
            sys.exit(1)

    host     = args.host     or cfg.get("host")
    user     = args.user     or cfg.get("user")
    password = args.password or cfg.get("password")
    port     = args.port     or cfg.get("port", 21)

    if not all([host, user, password]):
        parser.print_help()
        sys.exit(1)

    if args.save:
        save_config({"host": host, "user": user, "password": password, "port": port})

    # Connect
    print(f"\nConnecting to {host}:{port}...")
    try:
        ftp = connect_ftp(host, user, password, port)
        print(f"✓ Connected as {user}")
    except Exception as e:
        print(f"✗ Connection failed: {e}")
        sys.exit(1)

    # List accounts
    accounts = list_accounts(ftp)
    if not accounts:
        print("No account folders found on FTP root.")
        ftp.quit()
        sys.exit(1)

    print(f"Found {len(accounts)} folder(s): {', '.join(accounts)}")

    if args.list:
        ftp.quit()
        return

    # Pick account
    target = args.account or accounts[0]
    if target not in accounts:
        print(f"Account folder '{target}' not found. Available: {', '.join(accounts)}")
        ftp.quit()
        sys.exit(1)

    # Find and download report
    print(f"\nLooking for report in '{target}'...")
    report_file = find_report_file(ftp, target)
    if not report_file:
        print(f"No .htm/.html file found in '{target}'")
        ftp.quit()
        sys.exit(1)

    print(f"Downloading {report_file}...")
    try:
        raw = download_report(ftp, target, report_file)
        ftp.quit()
        print(f"✓ Downloaded {len(raw):,} bytes")
    except Exception as e:
        print(f"✗ Download failed: {e}")
        ftp.quit()
        sys.exit(1)

    # Parse
    print("Parsing report...")
    try:
        sys.path.insert(0, str(Path(__file__).parent))
        from mt5_parser import detect_and_parse, calc_stats
        df, fmt = detect_and_parse(raw, f"{target}.htm")
        if df is None or df.empty:
            print("✗ Could not parse report — check file format")
            sys.exit(1)
        print(f"✓ Parsed {len(df)} trades — format: {fmt}")
    except ImportError:
        print("✗ mt5_parser.py not found — run from MT5Tools folder")
        sys.exit(1)

    # Stats
    stats = calc_stats(df)
    display_stats(stats, fmt, target)

    if not args.no_monthly:
        display_monthly(df)

    if args.trades > 0:
        display_recent_trades(df, args.trades)

    # Save cache
    if args.save_cache:
        import pickle
        CACHE_DIR.mkdir(exist_ok=True)
        cache_data = {
            "account_folder": target,
            "df"            : df,
            "stats"         : stats,
            "fmt"           : fmt,
            "fetched_at"    : datetime.now().isoformat(),
        }
        cache_file = CACHE_DIR / f"ftp_{target}.pkl"
        cache_file.write_bytes(pickle.dumps(cache_data))
        print(f"Cache saved to {cache_file}")


if __name__ == "__main__":
    main()