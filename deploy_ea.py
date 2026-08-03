"""
deploy_ea.py
============
Copies the ReportExporter EA (source + compiled) into every MT5 terminal
data folder found for the current user, so each terminal sees it in the
Navigator under Expert Advisors.

Terminals are discovered under:
    %APPDATA%\\MetaQuotes\\Terminal\\<instance_hash>\\MQL5\\Experts

Run from MT5Tools with the venv activated:
    python deploy_ea.py           # copy to all terminals found
    python deploy_ea.py --dry-run # show what would be copied, copy nothing

After running, right-click "Expert Advisors" in each terminal's Navigator
and click Refresh (or restart the terminal) to see ReportExporter.
"""

import argparse
import os
import shutil
import sys
from pathlib import Path

EA_DIR   = Path(__file__).parent / "MQL5"
EA_FILES = ["ReportExporter.mq5", "ReportExporter.ex5"]


def find_terminal_expert_dirs() -> list[Path]:
    """Find MQL5\\Experts folders of all MT5 terminals for the current user."""
    appdata = os.environ.get("APPDATA")
    if not appdata:
        print("ERROR: %APPDATA% not set — are you running on Windows?")
        sys.exit(1)

    terminals_root = Path(appdata) / "MetaQuotes" / "Terminal"
    if not terminals_root.exists():
        print(f"ERROR: {terminals_root} not found — no MT5 terminals installed for this user?")
        sys.exit(1)

    found = []
    for inst in sorted(terminals_root.iterdir()):
        # Real terminal data folders are 32-char hex hashes; skip "Common",
        # "Community" etc. and anything without an MQL5 folder.
        if not inst.is_dir() or not (inst / "MQL5").is_dir():
            continue
        found.append(inst / "MQL5" / "Experts")
    return found


def terminal_label(experts_dir: Path) -> str:
    """Human-readable label for a terminal: broker name from origin.txt if present."""
    inst = experts_dir.parent.parent  # <hash>/
    origin = inst / "origin.txt"
    if origin.exists():
        try:
            text = origin.read_text(encoding="utf-16", errors="ignore").strip()
            if text:
                return f"{inst.name[:8]}… ({Path(text).name})"
        except Exception:
            pass
    return f"{inst.name[:8]}…"


def main():
    ap = argparse.ArgumentParser(description="Deploy ReportExporter EA to all MT5 terminals")
    ap.add_argument("--dry-run", action="store_true", help="show targets without copying")
    args = ap.parse_args()

    sources = [EA_DIR / f for f in EA_FILES]
    missing = [s for s in sources if not s.exists()]
    if missing:
        for m in missing:
            print(f"WARNING: {m} not found — skipping")
        sources = [s for s in sources if s.exists()]
    if not sources:
        print("ERROR: no EA files to deploy.")
        sys.exit(1)

    targets = find_terminal_expert_dirs()
    if not targets:
        print("No MT5 terminal data folders found.")
        sys.exit(1)

    print(f"Deploying: {', '.join(s.name for s in sources)}")
    print(f"Found {len(targets)} terminal(s):\n")

    for experts_dir in targets:
        label = terminal_label(experts_dir)
        if args.dry_run:
            print(f"  [dry-run] {label}  ->  {experts_dir}")
            continue
        try:
            experts_dir.mkdir(parents=True, exist_ok=True)
            for src in sources:
                shutil.copy2(src, experts_dir / src.name)
            print(f"  OK        {label}  ->  {experts_dir}")
        except OSError as e:
            print(f"  FAILED    {label}  ->  {experts_dir}  ({e})")

    if not args.dry_run:
        print("\nDone. Refresh the Navigator (right-click Expert Advisors -> Refresh)")
        print("or restart each terminal, then attach ReportExporter to one chart per terminal.")


if __name__ == "__main__":
    main()
