"""
build_ea_name_map.py
====================
Regenerate ea_name_map.json from the deployed UBS set files — run after any
set-file comment changes or when new sets are added.

The map is deliberately flat: {EA_Comment: set-file stem}. The stem is also
the report stem, and therefore the strategy name in any timeline compiled
from those reports — one identity everywhere, no migration shims.

Usage: python build_ea_name_map.py [--sets <folder>]
"""

import os
import re
import json
import argparse

MODULE_DIR   = os.path.dirname(os.path.abspath(__file__))


def _default_sets():
    """Per-install: the UBS set-file folder. Prefer the batch backtester's
    saved folder, else the standard layout, else prompt via --sets."""
    cfg = os.path.join(MODULE_DIR, 'mt5_batch_config.json')
    if os.path.isfile(cfg):
        try:
            with open(cfg, encoding='utf-8') as f:
                c = json.load(f)
            sf = c.get('set_folder') or c.get('sets_folder')
            if sf and os.path.isdir(sf):
                return sf
        except Exception:
            pass
    for cand in (r'C:\BulkBackTest\Updated set files',
                 os.path.join(os.path.dirname(MODULE_DIR), 'Updated set files')):
        if os.path.isdir(cand):
            return cand
    return ''


DEFAULT_SETS = _default_sets()


def read_comment(path):
    with open(path, 'rb') as f:
        raw = f.read()
    text = raw[2:].decode('utf-16-le') if raw[:2] == b'\xff\xfe' \
        else raw.decode('utf-8', 'replace')
    m = re.search(r'^EA_Comment=(.*)$', text, re.MULTILINE)
    return m.group(1).strip() if m else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--sets', default=DEFAULT_SETS)
    args = ap.parse_args()

    mapping, dupes, missing = {}, [], []
    for root, _, files in os.walk(args.sets):
        for fn in sorted(files):
            if not fn.lower().endswith('.set'):
                continue
            stem = os.path.splitext(fn)[0]
            comment = read_comment(os.path.join(root, fn))
            if not comment:
                missing.append(fn)
                continue
            if comment in mapping:
                dupes.append(comment)
                continue
            mapping[comment] = stem

    # Optional transitional bridging as DATA, not code: entries in
    # ea_name_map_overrides.json ({legacy live comment: stem}) are merged in,
    # survive regeneration, and the file is simply deleted once every account
    # is migrated to the standardised comments.
    ov_path = os.path.join(MODULE_DIR, 'ea_name_map_overrides.json')
    if os.path.isfile(ov_path):
        with open(ov_path, encoding='utf-8') as f:
            overrides = json.load(f)
        # keys starting with '_' are notes; empty values are unfilled slots
        overrides = {k: v for k, v in overrides.items()
                     if not k.startswith('_') and v}
        mapping.update(overrides)
        print(f"merged {len(overrides)} override(s) from ea_name_map_overrides.json")

    out = os.path.join(MODULE_DIR, 'ea_name_map.json')
    with open(out, 'w') as f:
        json.dump(mapping, f, indent=2)

    print(f"mapped {len(mapping)} comments -> {out}")
    if missing:
        print(f"  WARNING no EA_Comment in: {missing}")
    if dupes:
        print(f"  WARNING duplicate comments skipped: {dupes}")


if __name__ == '__main__':
    main()
