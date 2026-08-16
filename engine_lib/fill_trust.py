"""
fill_trust.py
=============
How much can a robot's backtest FILLS be trusted?

Compares each robot's daily P&L in a REAL-tick proxy timeline (a short recent
window, every tick based on real ticks) against the same days in the main
(mostly 1m-OHLC) timeline. Robots whose main-timeline report is itself a
real-tick run get 'real' automatically.

Trust is assigned per FAMILY from the family-level haircut (per-robot samples
over ~3 months are noisy; a family of 8-10 robots is far steadier):

  real      the main-timeline report IS an every-tick-real-tick run
  high      real ticks keep >= 85% of the OHLC profit in the overlap
  medium    50-85% kept
  low       < 50% kept, or OHLC profit turns into a real-tick loss

The per-robot haircut is stored too, for the drill-down. Results are written
into the main timeline's ea_meta.csv (columns fill_trust, fill_haircut_pct,
family_fill_haircut_pct) so every consumer - Data/EA Pool/Regimes pages,
Build a Run, MT5Tools candidates and regime tab - sees the same label.

Usage:  python fill_trust.py [--main main_pool_2018] [--proxy proxy_3m_realticks]
"""
import os
import argparse

import numpy as np
import pandas as pd

ENGINE_DIR = os.path.dirname(os.path.abspath(__file__))
TRUST_ORDER = {'real': 0, 'high': 1, 'medium': 2, 'low': 3, 'unknown': 4}
TRUST_ICON  = {'real': '✅', 'high': '🟢', 'medium': '🟡', 'low': '🔴', 'unknown': '⚪'}


def _load(tl):
    d = pd.read_csv(os.path.join(ENGINE_DIR, 'timeline', tl, 'daily_pnl.csv'),
                    index_col='date', parse_dates=['date'])
    m = pd.read_csv(os.path.join(ENGINE_DIR, 'timeline', tl, 'ea_meta.csv'))
    return d, m


def _band(haircut):
    if pd.isna(haircut):
        return 'unknown'
    if haircut < 15:
        return 'high'
    if haircut < 50:
        return 'medium'
    return 'low'


def compute_trust(main='main_pool_2018', proxy='proxy_3m_realticks'):
    md, mm = _load(main)
    pd_, pm = _load(proxy)
    p_by = dict(zip(pm.strategy, pm.ea_id))
    lo, hi = pd_.index.min(), pd_.index.max()
    mw = md.loc[lo:hi]

    per = {}
    for r in mm.itertuples():
        if r.ea_id.endswith('REALTICKS'):
            per[r.ea_id] = ('real', np.nan)
            continue
        pe = p_by.get(r.strategy)
        if pe is None or r.ea_id not in mw.columns:
            per[r.ea_id] = ('unknown', np.nan)
            continue
        o = mw[r.ea_id]
        rl = pd_[pe].reindex(mw.index).fillna(0.0)
        on = o.sum()
        hc = (on - rl.sum()) / abs(on) * 100 if abs(on) > 500 else np.nan
        per[r.ea_id] = ('', hc)   # band assigned at family level below

    mm['fill_haircut_pct'] = [per[e][1] for e in mm.ea_id]
    mm['_pre'] = [per[e][0] for e in mm.ea_id]

    # Family-level haircut on OHLC-sourced robots only (sum of P&L, not mean
    # of ratios, so a $50 robot can't swing the family label)
    fam_hc = {}
    for fam, grp in mm[mm['_pre'] == ''].groupby('family'):
        ids = [e for e in grp.ea_id if e in mw.columns]
        pids = [p_by.get(s) for s in grp.strategy]
        pairs = [(e, p) for e, p in zip(ids, pids) if p is not None]
        if not pairs:
            fam_hc[fam] = np.nan
            continue
        on = sum(mw[e].sum() for e, _ in pairs)
        rn = sum(pd_[p].reindex(mw.index).fillna(0.0).sum() for _, p in pairs)
        fam_hc[fam] = (on - rn) / abs(on) * 100 if abs(on) > 1000 else np.nan

    mm['family_fill_haircut_pct'] = mm.family.map(fam_hc)
    mm['fill_trust'] = [
        pre if pre else _band(fam_hc.get(f, np.nan))
        for pre, f in zip(mm['_pre'], mm.family)]
    mm = mm.drop(columns=['_pre'])
    mm['fill_haircut_pct'] = mm['fill_haircut_pct'].round(0)
    mm['family_fill_haircut_pct'] = mm['family_fill_haircut_pct'].round(0)
    return mm, (lo, hi)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--main', default='main_pool_2018')
    ap.add_argument('--proxy', default='proxy_3m_realticks')
    args = ap.parse_args()
    mm, (lo, hi) = compute_trust(args.main, args.proxy)
    out = os.path.join(ENGINE_DIR, 'timeline', args.main, 'ea_meta.csv')
    mm.to_csv(out, index=False)
    print(f'fill trust vs {args.proxy} ({lo.date()} -> {hi.date()}) written to {out}')
    print(mm.fill_trust.value_counts().to_string())
    print()
    fam = (mm.groupby('family')
             .agg(trust=('fill_trust', lambda s: s.mode().iloc[0]),
                  haircut=('family_fill_haircut_pct', 'first'), robots=('ea_id', 'size'))
             .sort_values('haircut', ascending=False))
    print(fam.to_string())


if __name__ == '__main__':
    main()
