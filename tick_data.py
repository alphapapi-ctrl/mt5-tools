"""
tick_data.py — random-access reader for large, time-sorted tick CSVs.

Built for the EA Stress Tester's Cost Stress tab: a 10-year gold export runs
20 GB+, but we only ever need a few-second window around each trade event.
The file is never loaded — a byte-offset binary search on the time-sorted
file finds each window in a handful of seeks.

Auto-detected formats (delimiter tab/comma/semicolon, header optional):
  - QuantDataManager / MT5 Symbols-window export:
      <DATE>\t<TIME>\t<BID>\t<ASK>\t<LAST>\t<VOLUME>\t<FLAGS>
  - Generic combined-datetime CSVs:  2019.01.01 22:00:00.500,1.14572,1.14612
  - Dukascopy:  Gmt time,Ask,Bid,AskVolume,BidVolume  (day-first dates)

Rows without both bid and ask (e.g. Last/Volume-only ticks) are skipped.
Assumes the file is sorted by time — true of all mainstream tick exports.
"""

from datetime import datetime
import os

import numpy as np

_EPOCH = datetime(1970, 1, 1)
# Bisection stops when the bracket is this small, then scans line-by-line.
# Small enough that per-event scanning stays ~100 lines, large enough that
# a line always fits inside the bracket.
_BISECT_STOP = 4096


class TickFormatError(Exception):
    pass


def _has_alpha(s: str) -> bool:
    # 'T' alone is allowed — ISO datetimes ("2019-01-01T00:00:00") aren't headers
    return any(c.isalpha() and c not in "Tt" for c in s)


class TickFile:
    """Random-access tick CSV. Opening is cheap — only the first and last
    few KB are read to sniff the format and time range."""

    def __init__(self, path: str):
        self.path = path
        self._size = os.path.getsize(path)
        self._fh = open(path, "rb")
        self._daycache = {}
        self._detect()

    def close(self):
        self._fh.close()

    # ── format sniffing ──────────────────────────────────────────────────
    def _detect(self):
        self._fh.seek(0)
        first_raw = self._fh.readline()
        first = first_raw.decode("utf-8", "replace").strip()
        if not first:
            raise TickFormatError("empty file")
        has_header = _has_alpha(first)
        self._data_start = len(first_raw) if has_header else 0

        # Sample data lines for detection
        self._fh.seek(self._data_start)
        sample_raw = self._fh.read(65536).decode("utf-8", "replace")
        sample = [l for l in sample_raw.splitlines() if l.strip()][:200]
        if len(sample) < 2:
            raise TickFormatError("not enough data lines to detect format")
        # Drop the (possibly partial) last line
        sample = sample[:-1]

        self._delim = max(["\t", ",", ";"], key=lambda d: sample[0].count(d))
        if sample[0].count(self._delim) == 0:
            raise TickFormatError("cannot detect delimiter")

        header = [h.strip().strip("<>").strip().lower()
                  for h in first.split(self._delim)] if has_header else []
        parts0 = [p.strip() for p in sample[0].split(self._delim)]

        # Locate datetime column(s)
        self._c_date = self._c_time = None
        self._dt_split = False
        if header:
            for i, h in enumerate(header):
                if h == "date":
                    self._c_date = i
                elif h == "time" and self._c_time is None:
                    self._c_time = i
            if self._c_date is not None and self._c_time is not None:
                self._dt_split = True
            else:
                for i, h in enumerate(header):
                    if "time" in h or "date" in h:
                        self._c_date = i
                        break
        if self._c_date is None:
            # Positional: a field with ':' is time-bearing; one with a date
            # separator but no ':' is a date
            for i, p in enumerate(parts0):
                if ":" in p:
                    if any(s in p for s in "./-") and " " not in self._delim:
                        self._c_date = i          # combined datetime
                    elif self._c_time is None:
                        self._c_time = i
                elif any(s in p.replace(" ", "") for s in "./-") \
                        and self._c_date is None:
                    self._c_date = i
            if self._c_date is not None and self._c_time is not None \
                    and self._c_date != self._c_time:
                self._dt_split = True
            elif self._c_date is None and self._c_time is not None:
                self._c_date = self._c_time      # combined in one field
                self._c_time = None
        if self._c_date is None:
            raise TickFormatError("cannot find a date/time column")

        # Bid/ask columns — by header name, else first two floats after time
        self._c_bid = self._c_ask = None
        if header:
            for i, h in enumerate(header):
                if "bid" in h and "vol" not in h and self._c_bid is None:
                    self._c_bid = i
                if "ask" in h and "vol" not in h and self._c_ask is None:
                    self._c_ask = i
        if self._c_bid is None or self._c_ask is None:
            used = {self._c_date, self._c_time}
            floats = []
            for i, p in enumerate(parts0):
                if i in used or not p:
                    continue
                try:
                    float(p)
                    floats.append(i)
                except ValueError:
                    pass
            if len(floats) < 2:
                raise TickFormatError("cannot find bid/ask columns")
            self._c_bid, self._c_ask = floats[0], floats[1]

        # Date component order: 4-digit first → YMD; else assume year-last.
        # For year-last, any first component >12 proves day-first (Dukascopy);
        # undecided defaults to day-first, which is the year-last convention.
        dstr = parts0[self._c_date].replace("T", " ").split()[0]
        sep = "." if "." in dstr else "-" if "-" in dstr else "/"
        self._dsep = sep
        comps = dstr.split(sep)
        if len(comps) != 3:
            raise TickFormatError(f"unrecognised date format: {dstr!r}")
        if len(comps[0]) == 4:
            self._dorder = "YMD"
        else:
            self._dorder = "DMY"
            for line in sample:
                try:
                    p = line.split(self._delim)[self._c_date]
                    a, b = p.replace("T", " ").split()[0].split(sep)[:2]
                    if int(a) > 12:
                        self._dorder = "DMY"
                        break
                    if int(b) > 12:
                        self._dorder = "MDY"
                        break
                except (ValueError, IndexError):
                    continue

        # Validate on the sample; swap bid/ask if the file is ask-first
        parsed = [r for r in (self._parse_line(l.encode()) for l in sample) if r]
        if len(parsed) < max(2, len(sample) // 2):
            raise TickFormatError("could not parse the sample data lines")
        med_spread = float(np.median([a - b for _, b, a in parsed]))
        if med_spread < 0:
            self._c_bid, self._c_ask = self._c_ask, self._c_bid

    # ── line parsing ─────────────────────────────────────────────────────
    def _parse_line(self, raw: bytes):
        """(epoch_seconds, bid, ask) or None for blank/partial/quote-less rows."""
        try:
            parts = raw.decode("ascii", "replace").split(self._delim)
            bid_s = parts[self._c_bid].strip()
            ask_s = parts[self._c_ask].strip()
            if not bid_s or not ask_s:
                return None
            if self._dt_split:
                dstr, tstr = parts[self._c_date].strip(), parts[self._c_time].strip()
            else:
                dstr, tstr = parts[self._c_date].strip().replace("T", " ").split()
            base = self._daycache.get(dstr)
            if base is None:
                a, b, c = dstr.split(self._dsep)
                if self._dorder == "YMD":
                    y, mo, dy = int(a), int(b), int(c)
                elif self._dorder == "DMY":
                    y, mo, dy = int(c), int(b), int(a)
                else:
                    y, mo, dy = int(c), int(a), int(b)
                base = (datetime(y, mo, dy) - _EPOCH).total_seconds()
                self._daycache[dstr] = base
            hp = tstr.split(":")
            ts = base + int(hp[0]) * 3600 + int(hp[1]) * 60 + \
                (float(hp[2]) if len(hp) > 2 else 0.0)
            return ts, float(bid_s), float(ask_s)
        except (ValueError, IndexError):
            return None

    def _line_at(self, off: int):
        """First parseable line at or after byte offset `off` (skipping any
        partial line), or None at EOF."""
        self._fh.seek(off)
        if off != self._data_start:
            self._fh.readline()
        while True:
            raw = self._fh.readline()
            if not raw:
                return None
            rec = self._parse_line(raw)
            if rec:
                return rec

    # ── time range ───────────────────────────────────────────────────────
    @property
    def first_ts(self):
        rec = self._line_at(self._data_start)
        return rec[0] if rec else None

    @property
    def last_ts(self):
        back = min(self._size, 262144)
        self._fh.seek(self._size - back)
        for raw in reversed(self._fh.read().split(b"\n")):
            rec = self._parse_line(raw)
            if rec:
                return rec[0]
        return None

    # ── random access ────────────────────────────────────────────────────
    def _offset_before(self, ts_target: float, lo: int = None) -> int:
        """Byte offset from which a forward scan starts at (or just before)
        ts_target. File must be time-sorted."""
        lo = self._data_start if lo is None else max(lo, self._data_start)
        hi = self._size
        while hi - lo > _BISECT_STOP:
            mid = (lo + hi) // 2
            rec = self._line_at(mid)
            if rec is None or rec[0] > ts_target:
                hi = mid
            else:
                lo = mid
        return lo

    def window(self, t0: float, t1: float, lo_hint: int = None):
        """All ticks with t0 <= ts <= t1, plus the last tick before t0 (the
        quote in force when the window opens). Returns (ts, bid, ask) float
        arrays and a byte offset to pass as lo_hint for the next (later)
        window — successive sorted lookups get monotonically cheaper."""
        start = self._offset_before(t0, lo_hint)
        self._fh.seek(start)
        if start != self._data_start:
            self._fh.readline()
        prev = None
        ts_l, b_l, a_l = [], [], []
        while True:
            raw = self._fh.readline()
            if not raw:
                break
            rec = self._parse_line(raw)
            if rec is None:
                continue
            if rec[0] < t0:
                prev = rec
                continue
            if rec[0] > t1:
                break
            ts_l.append(rec[0]); b_l.append(rec[1]); a_l.append(rec[2])
        if prev is not None:
            ts_l.insert(0, prev[0]); b_l.insert(0, prev[1]); a_l.insert(0, prev[2])
        return (np.array(ts_l), np.array(b_l), np.array(a_l)), start


# ─────────────────────────────────────────────────────────────────────────────
# Event costing
# ─────────────────────────────────────────────────────────────────────────────
def collect_event_windows(tick: TickFile, event_times, pre_s: float = 5.0,
                          post_s: float = 1.5, progress=None) -> list:
    """Fetch a tick window around each event time (sorted ascending).
    Returns a list of (ts, bid, ask) array tuples, one per event."""
    out, hint = [], None
    n = len(event_times)
    for k, t in enumerate(event_times):
        arrs, hint = tick.window(t - pre_s, t + post_s, hint)
        out.append(arrs)
        if progress and (k % 200 == 0 or k == n - 1):
            progress((k + 1) / n)
    return out


def _quote_at(ts, bid, ask, t, buy_side, stale_s):
    """Quote in force at time t (last tick at or before t), NaN if the
    freshest tick is older than stale_s."""
    i = int(np.searchsorted(ts, t, side="right")) - 1
    if i < 0 or t - ts[i] > stale_s:
        return np.nan
    return float(ask[i]) if buy_side else float(bid[i])


def delay_costs(windows, event_times, buy_action, delay_s: float,
                stale_s: float = 5.0):
    """Per-event execution-delay cost in price units (positive = adverse,
    negative = the market moved in your favour during the delay), plus the
    live spread at each event. NaN where no fresh quote exists.

    buy_action: True where the event pays the ask (buy entries, sell exits),
    False where it receives the bid (sell entries, buy exits).
    """
    n = len(event_times)
    costs = np.full(n, np.nan)
    spreads = np.full(n, np.nan)
    for i in range(n):
        ts, bid, ask = windows[i]
        if len(ts) == 0:
            continue
        t = event_times[i]
        buy = bool(buy_action[i])
        p0 = _quote_at(ts, bid, ask, t, buy, stale_s)
        p1 = _quote_at(ts, bid, ask, t + delay_s, buy, stale_s)
        if np.isnan(p0) or np.isnan(p1):
            continue
        costs[i] = (p1 - p0) if buy else (p0 - p1)
        j = int(np.searchsorted(ts, t, side="right")) - 1
        if j >= 0:
            spreads[i] = ask[j] - bid[j]
    return costs, spreads
