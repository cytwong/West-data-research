#!/usr/bin/env python3
"""Market monitor: fetch Yahoo Finance OHLCV, compute indicators, write data/signals.json.

Runs hourly on GitHub Actions. No third-party dependencies (stdlib only).
Watchlist lives in watchlist.txt (one Yahoo symbol per line, # for comments).
"""
import json
import math
import pathlib
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).parent
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}


def load_watchlist():
    syms = []
    for line in (ROOT / "watchlist.txt").read_text().splitlines():
        s = line.strip()
        if s and not s.startswith("#"):
            syms.append(s.split()[0])
    return syms


def fetch_chart(sym, interval, rng, retries=3):
    url = (
        "https://query1.finance.yahoo.com/v8/finance/chart/"
        f"{urllib.parse.quote(sym)}?interval={interval}&range={rng}"
    )
    last_err = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=30) as r:
                j = json.load(r)
            res = (j.get("chart", {}).get("result") or [None])[0]
            if not res or not res.get("timestamp"):
                return None
            q = res["indicators"]["quote"][0]
            bars = []
            for i, t in enumerate(res["timestamp"]):
                if q["close"][i] is None:
                    continue
                bars.append({
                    "t": t,
                    "o": q["open"][i], "h": q["high"][i],
                    "l": q["low"][i], "c": q["close"][i],
                    "v": q["volume"][i] or 0,
                })
            return {"meta": res["meta"], "bars": bars}
        except Exception as e:  # noqa: BLE001
            last_err = e
            time.sleep(2 * (attempt + 1))
    print(f"fetch failed {sym}: {last_err}", file=sys.stderr)
    return None


def sma(a, n, i=None):
    if i is None:
        i = len(a) - 1
    if i + 1 < n or i >= len(a):
        return None
    return sum(a[i - n + 1: i + 1]) / n


def rsi_at(closes, end, n=14):
    if end < n:
        return None
    g = l = 0.0
    for i in range(1, n + 1):
        d = closes[i] - closes[i - 1]
        if d > 0:
            g += d
        else:
            l -= d
    g /= n
    l /= n
    for i in range(n + 1, end + 1):
        d = closes[i] - closes[i - 1]
        g = (g * (n - 1) + max(d, 0)) / n
        l = (l * (n - 1) + max(-d, 0)) / n
    if l == 0:
        return 100.0
    return 100 - 100 / (1 + g / l)


def swings(bars, w):
    highs, lows = [], []
    for i in range(w, len(bars) - w):
        hi = lo = True
        for k in range(i - w, i + w + 1):
            if k == i:
                continue
            if bars[k]["h"] >= bars[i]["h"]:
                hi = False
            if bars[k]["l"] <= bars[i]["l"]:
                lo = False
        if hi:
            highs.append({"i": i, "p": bars[i]["h"]})
        if lo:
            lows.append({"i": i, "p": bars[i]["l"]})
    return highs, lows


def align_pattern(closes, i, periods):
    ms = [sma(closes, p, i) for p in periods]
    if any(m is None for m in ms):
        return None
    return "".join("+" if ms[k] > ms[k + 1] else "-" for k in range(len(ms) - 1))


def rnd(x, nd=4):
    return None if x is None else round(x, nd)


def analyze_bars(sym, h, d, now_ts=None):
    """Pure computation from fetched chart dicts (testable offline)."""
    now_ts = now_ts or time.time()
    hb, db = h["bars"], d["bars"]
    if len(hb) < 30 or len(db) < 60:
        return {"sym": sym, "error": "insufficient data"}
    hc = [b["c"] for b in hb]
    dc = [b["c"] for b in db]

    # effective last completed bar for volume/range (skip trailing zero-volume bar)
    li = len(hb) - 1
    has_vol = any(b["v"] > 0 for b in hb)
    if has_vol and hb[li]["v"] == 0 and li > 0:
        li -= 1
    last = hb[li]
    price = h["meta"].get("regularMarketPrice") or hb[-1]["c"]

    rsi1h = rsi_at(hc, len(hc) - 1)
    rsi1h_prev = rsi_at(hc, len(hc) - 2)
    rsi1d = rsi_at(dc, len(dc) - 1)

    vol_window = [b["v"] for b in hb[max(0, li - 20): li]]
    vol_x = None
    if vol_window and sum(vol_window) > 0:
        vol_x = last["v"] / (sum(vol_window) / len(vol_window))

    tr_sum, tr_n = 0.0, 0
    for i in range(max(1, li - 14), li):
        b, pb = hb[i], hb[i - 1]
        tr_sum += max(b["h"] - b["l"], abs(b["h"] - pb["c"]), abs(b["l"] - pb["c"]))
        tr_n += 1
    range_x = ((last["h"] - last["l"]) / (tr_sum / tr_n)) if tr_n and tr_sum else None

    ma_h_now = align_pattern(hc, len(hc) - 1, [20, 50])
    ma_h_prev = align_pattern(hc, len(hc) - 2, [20, 50])
    ma_d_now = align_pattern(dc, len(dc) - 1, [20, 50, 200])
    ma_d_prev = align_pattern(dc, len(dc) - 2, [20, 50, 200])

    d_highs, d_lows = swings(db[-150:], 3)
    res_levels = sorted(p["p"] for p in d_highs if p["p"] > price)
    sup_levels = sorted((p["p"] for p in d_lows if p["p"] < price), reverse=True)
    resistance = res_levels[0] if res_levels else None
    support = sup_levels[0] if sup_levels else None

    _, h_lows = swings(hb, 2)
    struct_break = None
    if h_lows:
        last_low = h_lows[-1]
        for i in range(last_low["i"] + 3, len(hb)):
            if hb[i]["c"] < last_low["p"]:
                struct_break = {
                    "level": rnd(last_low["p"]),
                    "bars_ago": len(hb) - 1 - i,
                }
                break

    prev_day_close = db[-2]["c"]
    chg_pct = (price / prev_day_close - 1) * 100
    stale_hours = (now_ts - hb[-1]["t"]) / 3600
    prev_price = hb[-2]["c"] if len(hb) >= 2 else None

    out = {
        "sym": sym,
        "price": rnd(price),
        "chg_pct": rnd(chg_pct, 2),
        "rsi_1h": rnd(rsi1h, 1),
        "rsi_1h_prev": rnd(rsi1h_prev, 1),
        "rsi_1d": rnd(rsi1d, 1),
        "vol_x": rnd(vol_x, 2),
        "range_x": rnd(range_x, 2),
        "support": rnd(support),
        "resistance": rnd(resistance),
        "dist_sup_pct": rnd((price / support - 1) * 100, 2) if support else None,
        "dist_res_pct": rnd((resistance / price - 1) * 100, 2) if resistance else None,
        "prev_dist_sup_pct": rnd((prev_price / support - 1) * 100, 2)
        if (support and prev_price) else None,
        "prev_dist_res_pct": rnd((resistance / prev_price - 1) * 100, 2)
        if (resistance and prev_price) else None,
        "ma_1h": {"now": ma_h_now, "prev": ma_h_prev, "changed": ma_h_now != ma_h_prev},
        "ma_1d": {"now": ma_d_now, "prev": ma_d_prev, "changed": ma_d_now != ma_d_prev},
        "struct_break": struct_break,
        "stale_hours": rnd(stale_hours, 1),
        "last_bar_utc": datetime.fromtimestamp(hb[-1]["t"], tz=timezone.utc).isoformat(),
    }
    out["signals"] = build_signals(out)
    return out


def build_signals(m):
    """Fresh-trigger signal strings; empty list = quiet."""
    sig = []
    if m["stale_hours"] is not None and m["stale_hours"] > 2:
        return sig  # market closed for this symbol; suppress everything
    r, rp = m["rsi_1h"], m["rsi_1h_prev"]
    if r is not None and rp is not None:
        if r >= 70 and rp < 70:
            sig.append(f"RSI(1h) 進入超買區 {r}")
        if r <= 30 and rp > 30:
            sig.append(f"RSI(1h) 進入超賣區 {r}")
    if m["ma_1h"]["changed"] and m["ma_1h"]["prev"] is not None:
        sig.append(f"1h 均線排列轉變 {m['ma_1h']['prev']}→{m['ma_1h']['now']}")
    if m["ma_1d"]["changed"] and m["ma_1d"]["prev"] is not None:
        sig.append(f"日線均線排列轉變 {m['ma_1d']['prev']}→{m['ma_1d']['now']}")
    vol_spike = m["vol_x"] is not None and m["vol_x"] >= 2.5
    range_spike = m["range_x"] is not None and m["range_x"] >= 2.0
    if vol_spike:
        sig.append(f"成交量異常放大 {m['vol_x']}x")
    if range_spike:
        sig.append(f"波動幅度異常 {m['range_x']}x ATR")
    big_move = m["chg_pct"] is not None and abs(m["chg_pct"]) >= (
        8 if m["sym"].endswith("-USD") else 5
    )
    if (vol_spike and range_spike) or big_move:
        sig.append(f"⚡異常活動 (日內 {m['chg_pct']}%)")
    # near/cross S-R fires only on fresh entry into the 1% zone (or a cross), not while lingering
    ds, pds = m["dist_sup_pct"], m["prev_dist_sup_pct"]
    if ds is not None and (ds <= 1 or ds < 0) and (pds is None or pds > 1):
        verb = "跌破支撐" if ds < 0 else "逼近支撐"
        sig.append(f"{verb} {m['support']} (距 {ds}%)")
    dr, pdr = m["dist_res_pct"], m["prev_dist_res_pct"]
    if dr is not None and (dr <= 1 or dr < 0) and (pdr is None or pdr > 1):
        verb = "突破壓力" if dr < 0 else "逼近壓力"
        sig.append(f"{verb} {m['resistance']} (距 {dr}%)")
    sb = m["struct_break"]
    if sb and sb["bars_ago"] <= 1:
        sig.append(f"跌破結構低點 {sb['level']}")
    return sig


def analyze(sym):
    h = fetch_chart(sym, "1h", "1mo")
    d = fetch_chart(sym, "1d", "1y")
    if not h or not d:
        return {"sym": sym, "error": "fetch failed"}
    try:
        return analyze_bars(sym, h, d)
    except Exception as e:  # noqa: BLE001
        return {"sym": sym, "error": f"analyze error: {e}"}


def main():
    syms = load_watchlist()
    results = []
    for s in syms:
        results.append(analyze(s))
        time.sleep(1)  # be polite to Yahoo
    alerts = {r["sym"]: r["signals"] for r in results if r.get("signals")}
    out = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "watchlist_count": len(syms),
        "alert_count": sum(len(v) for v in alerts.values()),
        "alerts": alerts,
        "symbols": results,
    }
    dest = ROOT / "data"
    dest.mkdir(exist_ok=True)
    (dest / "signals.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    errs = [r["sym"] for r in results if r.get("error")]
    print(f"ok: {len(results) - len(errs)}/{len(results)} symbols, "
          f"{out['alert_count']} signals" + (f", errors: {errs}" if errs else ""))


if __name__ == "__main__":
    main()
