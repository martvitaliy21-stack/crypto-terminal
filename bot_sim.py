"""Паперный бот ГРОШ (правила v2): торгует виртуальными 500 USDT по 4h-стратегии.
Вход: тренд (close>EMA200, EMA50>EMA200), откат RSI≤50 с разворотом, объём выше среднего, бычья свеча.
Стоп 3×ATR. Тейка нет: после движения +1.5R включается трейлинг-стоп на 3.5×ATR от максимума.
Риск 1% на сделку, не больше 14% капитала в монету, суммарный риск портфеля (до стопов) не больше 3%.
Запускается по расписанию на GitHub Actions, состояние хранит в docs/bot/state.json.
Устойчивость: обрабатывает все закрытые свечи с последнего запуска (пропуски расписания не теряют сделок),
ошибки по одной монете не роняют остальных, состояние пишется атомарно.
"""
import json
import math
import os
import time
import traceback
import numpy as np
import pandas as pd
import requests

ROOT = os.path.dirname(os.path.abspath(__file__))
STATE = os.environ.get("GROSH_STATE") or os.path.join(ROOT, "docs", "bot", "state.json")
SYMBOLS = ["BTC", "ETH", "SOL", "BNB", "XRP", "LINK", "AVAX"]
TF, TF_MS = "4h", 4 * 3600 * 1000
CFG = dict(start_equity=500.0, risk_per_trade=0.01, max_positions=7, max_position_pct=0.14, daily_loss_limit=0.03,
           fee=0.001, slippage=0.0005, ema_fast=50, ema_slow=200, rsi_len=14, rsi_entry=50, rsi_lookback=3,
           atr_len=14, atr_stop_mult=3.0, vol_len=20,
           rules_version=2, take_mode="trail", trail_atr=3.5, trail_start_r=1.5, max_heat=0.03, zwin=540)
RULES_V2_NOTE = "правила v2: тейк 2R заменён трейлинг-стопом 3.5×ATR после +1.5R; суммарный риск портфеля ограничен 3% (по бэктесту за 2 года: +34% при просадке −14% вместо +12% при −23%)"
HOSTS = ["https://data-api.binance.vision", "https://api.binance.com"]


def now_ms():
    return int(time.time() * 1000)


def klines(sym: str, limit: int = 760) -> pd.DataFrame:
    last_err = None
    for host in HOSTS:
        try:
            r = requests.get(f"{host}/api/v3/klines", params=dict(symbol=f"{sym}USDT", interval=TF, limit=limit), timeout=20)
            r.raise_for_status()
            rows = r.json()
            df = pd.DataFrame([[int(x[0])] + [float(v) for v in x[1:6]] for x in rows], columns=["ts", "open", "high", "low", "close", "volume"])
            return df[df["ts"] + TF_MS <= now_ms()].reset_index(drop=True)   # только закрытые свечи
        except Exception as e:
            last_err = e
    raise RuntimeError(f"{sym}: {last_err}")


def price(sym: str) -> float:
    for host in HOSTS:
        try:
            r = requests.get(f"{host}/api/v3/ticker/price", params=dict(symbol=f"{sym}USDT"), timeout=10)
            r.raise_for_status()
            return float(r.json()["price"])
        except Exception:
            continue
    raise RuntimeError(f"{sym}: price unavailable")


def rolling_z(close: np.ndarray, w: int) -> np.ndarray:
    """z-score к МНК-линии по log-цене в скользящем окне w."""
    y = np.log(close.astype(float)); n = len(y); j = np.arange(n, dtype=float)
    z = np.full(n, np.nan)
    if n < w:
        return z
    cs = lambda a: np.concatenate([[0.0], np.cumsum(a)])
    S1, S2, SY, SJY, SYY = cs(j), cs(j * j), cs(y), cs(j * y), cs(y * y)
    i = np.arange(w - 1, n); lo = i - w + 1
    sx = S1[i + 1] - S1[lo]; sxx = S2[i + 1] - S2[lo]; sy = SY[i + 1] - SY[lo]; sxy = SJY[i + 1] - SJY[lo]; syy = SYY[i + 1] - SYY[lo]
    b = (w * sxy - sx * sy) / (w * sxx - sx * sx); a = (sy - b * sx) / w
    sse = syy - 2 * a * sy - 2 * b * sxy + w * a * a + 2 * a * b * sx + b * b * sxx
    sigma = np.sqrt(np.maximum(sse, 0) / max(w - 2, 1))
    z[i] = np.where(sigma > 0, (y[i] - (a + b * i)) / sigma, 0.0)
    return z


def indicators(df: pd.DataFrame) -> pd.DataFrame:
    c = df["close"]
    d = df.copy()
    d["ema_fast"] = c.ewm(span=CFG["ema_fast"], adjust=False, min_periods=CFG["ema_fast"]).mean()
    d["ema_slow"] = c.ewm(span=CFG["ema_slow"], adjust=False, min_periods=CFG["ema_slow"]).mean()
    delta = c.diff(); up = delta.clip(lower=0); dn = -delta.clip(upper=0)
    au = up.ewm(alpha=1 / CFG["rsi_len"], adjust=False, min_periods=CFG["rsi_len"]).mean()
    ad = dn.ewm(alpha=1 / CFG["rsi_len"], adjust=False, min_periods=CFG["rsi_len"]).mean()
    d["rsi"] = 100 - 100 / (1 + au / ad.replace(0, np.nan))
    pc = c.shift(1)
    tr = pd.concat([d["high"] - d["low"], (d["high"] - pc).abs(), (d["low"] - pc).abs()], axis=1).max(axis=1)
    d["atr"] = tr.ewm(alpha=1 / CFG["atr_len"], adjust=False, min_periods=CFG["atr_len"]).mean()
    d["vol_ma"] = d["volume"].rolling(CFG["vol_len"]).mean()
    d["rsi_min"] = d["rsi"].shift(1).rolling(CFG["rsi_lookback"]).min()
    d["trend"] = (c > d["ema_slow"]) & (d["ema_fast"] > d["ema_slow"])
    d["pullback"] = (d["rsi_min"] <= CFG["rsi_entry"]) & (d["rsi"] > d["rsi"].shift(1))
    d["vol_ok"] = d["volume"] > d["vol_ma"]
    d["bullish"] = c > d["open"]
    d["entry"] = d["trend"] & d["pullback"] & d["vol_ok"] & d["bullish"] & d["atr"].notna()
    d["z"] = rolling_z(c.values, CFG["zwin"])
    return d


def load_state() -> dict:
    if os.path.exists(STATE):
        with open(STATE) as f:
            st = json.load(f)
        if st.get("cash") is not None:
            return st
    t0 = now_ms()
    return dict(version=1, started=t0, live_from=t0, start_equity=CFG["start_equity"], cash=CFG["start_equity"],
                positions={}, trades=[], equity=[], last_bar={}, log=[], paused_until=None, day=None, day_start_eq=CFG["start_equity"],
                day_pnl=0.0, btc_start=None, config=CFG, status=dict(runs=0, errors=0, last_run=None, last_error=None))


def log(st, ts, msg):
    st["log"].append([ts, msg]); st["log"] = st["log"][-80:]
    print(time.strftime("%Y-%m-%d %H:%M", time.gmtime(ts / 1000)), msg)


def equity_value(st, last_close: dict) -> float:
    return st["cash"] + sum(p["qty"] * last_close.get(s, p["entry"]) for s, p in st["positions"].items())


def heat(st, eq: float) -> float:
    """Доля капитала, которую портфель потеряет, если все стопы сработают сейчас."""
    return sum(max(0.0, p["entry"] - p["stop"]) * p["qty"] for p in st["positions"].values()) / eq if eq > 0 else 0.0


def snap(st, ts, closes):
    eq = equity_value(st, closes)
    bh = CFG["start_equity"] * closes["BTC"] / st["btc_start"] if st.get("btc_start") and "BTC" in closes else None
    if st["equity"] and st["equity"][-1][0] >= ts:
        return
    st["equity"].append([ts, round(eq, 4), round(bh, 4) if bh else None])
    st["equity"] = st["equity"][-3000:]


def close_position(st, sym, px, ts, reason):
    p = st["positions"].pop(sym)
    proceeds = p["qty"] * px * (1 - CFG["fee"])
    pnl = proceeds - p["cost"]
    st["cash"] += proceeds; st["day_pnl"] += pnl
    risk0 = p.get("risk0") or (p["entry"] - p.get("stop0", p["stop"])) * p["qty"]
    st["trades"].append(dict(symbol=sym, entry_ts=p["entry_ts"], exit_ts=ts, entry=p["entry"], exit=px, qty=p["qty"],
                             pnl=round(pnl, 4), pnl_pct=round(pnl / p["cost"], 5), r=round(pnl / risk0, 3) if risk0 > 0 else None,
                             reason=reason, max_r=round((p.get("max_high", p["entry"]) - p["entry"]) / p["R"], 2) if p.get("R") else None))
    label = {"take": "тейк", "stop": "стоп", "trail": "трейлинг"}.get(reason, reason)
    log(st, ts, f"{'✅' if pnl > 0 else '❌'} {sym} закрыта [{label}] {p['entry']:.4g} → {px:.4g}  {pnl:+.2f} $ ({pnl / p['cost'] * 100:+.2f}%"
                + (f", {pnl / risk0:+.2f}R" if risk0 > 0 else "") + ")")


def open_position(st, sym, row, ts, eq):
    entry = float(row["close"]) * (1 + CFG["slippage"])
    stop = entry - CFG["atr_stop_mult"] * float(row["atr"])
    if stop <= 0 or stop >= entry:
        return
    R = entry - stop
    qty = min(eq * CFG["risk_per_trade"] / R, eq * CFG["max_position_pct"] / entry)
    room = CFG["max_heat"] - heat(st, eq)
    if room <= 0.002:
        log(st, ts, f"{sym}: сигнал есть, но лимит риска портфеля {CFG['max_heat']*100:.0f}% исчерпан (сейчас {heat(st, eq)*100:.1f}%)"); return
    qty = min(qty, room * eq / R)
    cost = qty * entry * (1 + CFG["fee"])
    if qty * entry < 10 or cost > st["cash"]:
        log(st, ts, f"{sym}: сигнал есть, но не хватает средств (нужно {cost:.2f}, есть {st['cash']:.2f})"); return
    st["cash"] -= cost
    take = None if CFG["take_mode"] == "trail" else entry + 2.0 * R
    st["positions"][sym] = dict(entry_ts=ts, entry=entry, stop=stop, stop0=stop, take=take, qty=qty, cost=cost, R=R, risk0=R * qty,
                                max_high=entry, trailing=False, rsi=round(float(row["rsi"]), 1))
    log(st, ts, f"🟢 {sym} куплено {qty:.5g} @ {entry:.4g} на {cost:.2f} $  стоп {stop:.4g} ({(stop / entry - 1) * 100:.1f}%)  "
                f"трейлинг после {entry + CFG['trail_start_r'] * R:.4g} (+{CFG['trail_start_r'] * R / entry * 100:.1f}%)  RSI {row['rsi']:.0f}  риск {R * qty:.2f} $")


def migrate_v2(st, frames, ts):
    """Переход на правила v2 для позиций, открытых по v1: тейк снимается, трейлинг включается от максимума с момента входа."""
    if st.get("rules_version", 1) >= 2:
        return
    for sym, p in st["positions"].items():
        f = frames.get(sym)
        p["R"] = p["entry"] - p["stop"]; p["stop0"] = p["stop"]; p["risk0"] = p["R"] * p["qty"]
        p["max_high"] = float(max(p["entry"], f.loc[f["ts"] >= p["entry_ts"], "high"].max())) if f is not None and (f["ts"] >= p["entry_ts"]).any() else p["entry"]
        p["take"] = None; p["trailing"] = False
    st["rules_version"] = 2; st["config"] = CFG
    st.setdefault("rules_changes", []).append([ts, RULES_V2_NOTE])
    log(st, ts, "⚙️ " + RULES_V2_NOTE + f"; {len(st['positions'])} открытых позиций переведены на трейлинг")


def manage_position(st, sym, p, row, ts):
    """Сопровождение на закрытой свече: стоп по low, затем обновление трейлинга по максимуму."""
    lo, hi, atr = float(row["low"]), float(row["high"]), float(row["atr"])
    if lo <= p["stop"]:
        reason = "trail" if p["stop"] >= p["entry"] * (1 + 2 * CFG["fee"]) else "stop"
        close_position(st, sym, p["stop"] * (1 - CFG["slippage"]), ts, reason); return
    if p.get("take") and hi >= p["take"]:
        close_position(st, sym, p["take"], ts, "take"); return
    p["max_high"] = max(p.get("max_high", p["entry"]), hi)
    R = p.get("R") or (p["entry"] - p["stop"])
    if CFG["take_mode"] == "trail" and p["max_high"] >= p["entry"] + CFG["trail_start_r"] * R and not math.isnan(atr):
        new_stop = p["max_high"] - CFG["trail_atr"] * atr
        if new_stop > p["stop"]:
            if not p.get("trailing"):
                log(st, ts, f"🔒 {sym}: цена прошла +{CFG['trail_start_r']}R, включён трейлинг — стоп {p['stop']:.4g} → {new_stop:.4g}")
            p["stop"] = new_stop; p["trailing"] = True


def signals(st, frames, eq):
    """Сканер: состояние каждого условия входа по последней закрытой свече."""
    out = {}
    h = heat(st, eq)
    for s, f in frames.items():
        r = f.iloc[-1]
        prev_rsi = float(f["rsi"].iloc[-2]) if len(f) > 1 else float("nan")
        g = lambda v: None if (isinstance(v, float) and math.isnan(v)) else (float(v) if isinstance(v, (float, np.floating)) else v)
        out[s] = dict(ts=int(r["ts"]) + TF_MS, close=g(r["close"]), ema_fast=g(r["ema_fast"]), ema_slow=g(r["ema_slow"]),
                      trend=bool(r["trend"]), dist_ema_slow_pct=g((r["close"] / r["ema_slow"] - 1) * 100) if not math.isnan(r["ema_slow"]) else None,
                      rsi=g(r["rsi"]), rsi_prev=g(prev_rsi), rsi_min3=g(r["rsi_min"]), pullback=bool(r["pullback"]),
                      vol_ratio=g(r["volume"] / r["vol_ma"]) if r["vol_ma"] else None, vol_ok=bool(r["vol_ok"]), bullish=bool(r["bullish"]),
                      atr_pct=g(r["atr"] / r["close"] * 100), z=g(r["z"]), entry=bool(r["entry"]), in_position=s in st["positions"],
                      stop_pct=g(-CFG["atr_stop_mult"] * r["atr"] / r["close"] * 100))
    st["signals"] = out
    st["heat"] = round(h, 5); st["heat_cap"] = CFG["max_heat"]


def run():
    st = load_state()
    st["status"]["runs"] += 1
    frames, last_close, errors = {}, {}, []
    for s in SYMBOLS:
        try:
            frames[s] = indicators(klines(s))
            last_close[s] = float(frames[s]["close"].iloc[-1])
        except Exception as e:
            errors.append(str(e)[:120]); print("skip", s, e)
    if not frames:
        st["status"].update(errors=st["status"]["errors"] + 1, last_error="нет данных ни по одной монете", last_run=now_ms())
        save(st); return
    if not st["last_bar"]:
        for s, f in frames.items():
            st["last_bar"][s] = int(f["ts"].iloc[-1])
        st["live_from"] = st["started"] = now_ms()
        try:
            st["btc_start"] = price("BTC")
        except Exception:
            st["btc_start"] = last_close.get("BTC")
        st["rules_version"] = CFG["rules_version"]
        log(st, now_ms(), f"старт симуляции: {CFG['start_equity']:.0f} $, без предыстории — все сделки только в реальном времени")
    migrate_v2(st, frames, now_ms())

    events = []
    for s, f in frames.items():
        lb = st["last_bar"].get(s, int(f["ts"].iloc[0]))
        for k in np.where(f["ts"].values > lb)[0]:
            events.append((int(f["ts"].iloc[k]), s, int(k)))
    events.sort()

    def closes_at(ts):
        return {x: float(frames[x].loc[frames[x]["ts"] + TF_MS <= ts, "close"].iloc[-1]) for x in frames if (frames[x]["ts"] + TF_MS <= ts).any()}
    prev_ts = None
    for bar_ts, s, k in events:
        ts = bar_ts + TF_MS   # момент закрытия свечи
        if prev_ts is not None and ts != prev_ts:
            snap(st, prev_ts, closes_at(prev_ts))
        prev_ts = ts
        row = frames[s].iloc[k]
        day = time.strftime("%Y-%m-%d", time.gmtime(ts / 1000))
        if st["day"] != day:
            st["day"], st["day_pnl"] = day, 0.0
            st["day_start_eq"] = equity_value(st, closes_at(ts))
        if s in st["positions"]:
            manage_position(st, s, st["positions"][s], row, ts)
        if st["paused_until"] and ts >= st["paused_until"]:
            st["paused_until"] = None; log(st, ts, "▶️ пауза после дневного лимита снята")
        if not st["paused_until"] and st["day_pnl"] <= -CFG["daily_loss_limit"] * st["day_start_eq"]:
            st["paused_until"] = ts + 86400_000; log(st, ts, f"🛑 дневной убыток {st['day_pnl']:+.2f} $: пауза на 24 часа")
        if s not in st["positions"] and not st["paused_until"] and len(st["positions"]) < CFG["max_positions"] and bool(row["entry"]):
            open_position(st, s, row, ts, equity_value(st, closes_at(ts)))
        st["last_bar"][s] = bar_ts
    if prev_ts is not None:
        snap(st, prev_ts, closes_at(prev_ts))

    marks = dict(last_close)
    for s in set(list(st["positions"]) + ["BTC"]):
        try:
            marks[s] = price(s)
        except Exception as e:
            errors.append(str(e)[:120])
    eq = equity_value(st, marks)
    btc_px = marks.get("BTC") or last_close.get("BTC")
    bh = CFG["start_equity"] * btc_px / st["btc_start"] if st.get("btc_start") and btc_px else None
    if not st["equity"] or st["equity"][-1][0] < now_ms():
        st["equity"].append([now_ms(), round(eq, 4), round(bh, 4) if bh else None])
        st["equity"] = st["equity"][-3000:]
    st["marks"] = {s: marks.get(s) for s in st["positions"]}
    signals(st, frames, eq)
    st["status"].update(last_run=now_ms(), last_error="; ".join(errors) if errors else None,
                        errors=st["status"]["errors"] + (1 if errors else 0), symbols_ok=len(frames), events=len(events))
    log(st, now_ms(), f"капитал {eq:.2f} $  позиций {len(st['positions'])}  риск портфеля {heat(st, eq)*100:.1f}%  новых свечей {len(events)}" + (f"  ⚠ {len(errors)} ошибок" if errors else ""))
    save(st)


def save(st):
    tmp = STATE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, STATE)


if __name__ == "__main__":
    try:
        run()
    except Exception:
        traceback.print_exc()
        st = load_state()
        st["status"].update(errors=st["status"].get("errors", 0) + 1, last_error=traceback.format_exc().strip().splitlines()[-1][:200], last_run=now_ms())
        save(st)
        raise SystemExit(0)
