"""
collate_set_baselines.py — scan UBS .set files and collate each strategy's
hard-coded HistoricalMaxDD into ea_baselines.json, keyed by EA_Comment.

The planner and EA size with the same rule (balance-per-0.01-lot =
HistoricalMaxDD / risk%), so these values let the Prop Planner replicate the
EA's live risk behaviour from a 0.01-lot backtest.

Usage:
    python collate_set_baselines.py [sets_folder]

Default folder: C:\\Users\\pc\\Desktop\\EA\\30StrategiesPortfolio_Sets
Re-running refreshes the "UBS set files" group; manually-filled packaged-EA
groups in ea_baselines.json are left untouched.
"""

import sys, os, glob, json

DEFAULT_FOLDER = r"C:\Users\pc\Desktop\EA\UPDATED SETS UBS_V6.3"
BASELINES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ea_baselines.json")


def read_set(path: str) -> dict:
    """Parse an MT5 .set file (UTF-16, `key=value||optimizer-flags` lines)."""
    raw = open(path, "rb").read()
    text = None
    for enc in ("utf-16", "utf-8"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        return {}
    vals = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith(";") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        vals[k.strip()] = v.split("||")[0].strip()
    return vals


def main(folder: str) -> None:
    if not os.path.isdir(folder):
        print(f"Folder not found: {folder}")
        sys.exit(1)

    # Recursive scan — v6.3 organises sets in subfolders per EA family.
    # On duplicate EA_Comment, a set inside a "live portfolio" folder wins
    # (that's the active configuration); otherwise first found is kept.
    paths = sorted(glob.glob(os.path.join(folder, "**", "*.set"), recursive=True))
    entries = {}
    for p in paths:
        rel = os.path.relpath(p, folder)
        vals = read_set(p)
        comment = vals.get("EA_Comment") or vals.get("TradeComment")
        dd = vals.get("HistoricalMaxDD")
        if not comment or not dd:
            print(f"  skip {rel}: no EA_Comment/HistoricalMaxDD")
            continue
        try:
            dd_v = float(dd)
        except ValueError:
            print(f"  skip {rel}: HistoricalMaxDD={dd!r} not numeric")
            continue
        if dd_v <= 0:
            print(f"  skip {rel}: HistoricalMaxDD=0 (main EA settings, not a strategy set)")
            continue
        entry = {"max_dd": dd_v, "set_file": rel}
        if vals.get("ForceSymbol"):
            entry["symbol"] = vals["ForceSymbol"].strip().upper()
        if vals.get("MaxRiskPerStrategy_Value"):
            try:
                entry["risk_value"] = float(vals["MaxRiskPerStrategy_Value"])
            except ValueError:
                pass
        _is_live = "live portfolio" in rel.lower()
        if comment in entries:
            _had_live = "live portfolio" in entries[comment]["set_file"].lower()
            if _is_live and not _had_live:
                print(f"  duplicate {comment!r}: preferring live portfolio ({rel})")
                entries[comment] = entry
            else:
                print(f"  duplicate {comment!r}: keeping {entries[comment]['set_file']} "
                      f"(ignoring {rel})")
            continue
        entries[comment] = entry
        print(f"  {comment:32s} max_dd={dd_v:g}"
              f"{'  risk=' + str(entry['risk_value']) if 'risk_value' in entry else ''}"
              f"  ({rel})")

    data = {}
    if os.path.exists(BASELINES):
        with open(BASELINES, encoding="utf-8") as fh:
            data = json.load(fh)
    grp = data.setdefault("UBS set files",
                          {"risk_input": "MaxRiskPerStrategy_Value", "strategies": {}})
    grp["risk_input"] = "MaxRiskPerStrategy_Value"
    grp["strategies"] = entries   # full refresh of the scanned group only
    grp["_scanned_from"] = folder
    with open(BASELINES, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    print(f"\n{len(entries)} strategies written to {BASELINES} (group 'UBS set files')")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_FOLDER)
