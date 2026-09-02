"""Паперный бот ГРОШ: торгует виртуальными 500 USDT по 4h-стратегии (EMA-тренд + откат RSI, стоп 3×ATR, тейк 2:1).
Запускается по расписанию на GitHub Actions, состояние хранит в docs/bot/state.json.
Устойчивость: обрабатывает все закрытые свечи с последнего запуска (пропуски расписания не теряют сделок),
ошибки по одной монете не роняют остальных, состояние никогда не перезаписывается пустым.
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
STATE = os.path.join(ROOT, "docs", "bot", "state.json")
SYMBOLS = ["BTC", "ETH", "SOL", "BNB", "XRP", "LINK", "AVAX"]
TF, TF_MS = "4h", 4 * 3600 * 1000
CFG = dict(start_equity=500.0, risk_per_trade=0.01, max_positions=3, max_position_pct=0.33, daily_loss_limit=0.03,
           fee=0.001, slippage=0.0005, ema_fast=50, ema_slow=200, rsi_len=14, rsi_entry=50, rsi_lookback=3,
           atr_len=14, atr_stop_mult=3.0, rr=2.0, vol_len=20, warm_start_days=30)
HOSTS = ["https://data-api.binance.vision", "https://api.binance.com"]


def now_ms():
    return int(time.time() * 1000)


def klines(sym: str, limit: int = 400) -> pd.DataFrame:
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
    rsi_min = d["rsi"].shift(1).rolling(CFG["rsi_lookback"]).min()
    d["entry"] = ((c > d["ema_slow"]) & (d["ema_fast"] > d["ema_slow"]) & (rsi_min <= CFG["rsi_entry"])
                  & (d["rsi"] > d["rsi"].shift(1)) & (d["volume"] > d["vol_ma"]) & (c > d["open"]) & d["atr"].notna())
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
    st["log"].append([ts, msg]); st["log"] = st["log"][-60:]
    print(time.strftime("%Y-%m-%d %H:%M", time.gmtime(ts / 1000)), msg)


def equity_value(st, last_close: dict) -> float:
    return st["cash"] + sum(p["qty"] * last_close.get(s, p["entry"]) for s, p in st["positions"].items())


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
    st["trades"].append(dict(symbol=sym, entry_ts=p["entry_ts"], exit_ts=ts, entry=p["entry"], exit=px, qty=p["qty"],
                             pnl=round(pnl, 4), pnl_pct=round(pnl / p["cost"], 5), reason=reason))
    log(st, ts, f"{'✅' if pnl > 0 else '❌'} {sym} закрыта [{reason}] {p['entry']:.4g} → {px:.4g}  {pnl:+.2f} $ ({pnl / p['cost'] * 100:+.2f}%)")


def open_position(st, sym, row, ts, eq):
    entry = float(row["close"]) * (1 + CFG["slippage"])
    stop = entry - CFG["atr_stop_mult"] * float(row["atr"])
    if stop <= 0 or stop >= entry:
        return
    take = entry + CFG["rr"] * (entry - stop)
    qty = min(eq * CFG["risk_per_trade"] / (entry - stop), eq * CFG["max_position_pct"] / entry)
    cost = qty * entry * (1 + CFG["fee"])
    if qty * entry < 10 or cost > st["cash"]:
        log(st, ts, f"{sym}: сигнал есть, но не хватает средств (нужно {cost:.2f}, есть {st['cash']:.2f})"); return
    st["cash"] -= cost
    st["positions"][sym] = dict(entry_ts=ts, entry=entry, stop=stop, take=take, qty=qty, cost=cost, rsi=round(float(row["rsi"]), 1))
    log(st, ts, f"🟢 {sym} куплено {qty:.5g} @ {entry:.4g} на {cost:.2f} $  стоп {stop:.4g} ({(stop / entry - 1) * 100:.1f}%)  тейк {take:.4g} ({(take / entry - 1) * 100:+.1f}%)  RSI {row['rsi']:.0f}")


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
    first_run = not st["last_bar"]
    if first_run:
        warm_from = now_ms() - CFG["warm_start_days"] * 86400_000
        for s, f in frames.items():
            st["last_bar"][s] = int(f["ts"][f["ts"] < warm_from].iloc[-1]) if (f["ts"] < warm_from).any() else int(f["ts"].iloc[0])
        st["live_from"] = now_ms(); st["started"] = warm_from
        if "BTC" in frames:
            st["btc_start"] = float(frames["BTC"].loc[frames["BTC"]["ts"] > warm_from, "close"].iloc[0])
        log(st, warm_from, f"старт симуляции: {CFG['start_equity']:.0f} $, первые {CFG['warm_start_days']} дней рассчитаны по истории")

    # все новые закрытые свечи по всем монетам — в хронологическом порядке
    events = []
    for s, f in frames.items():
        lb = st["last_bar"].get(s, int(f["ts"].iloc[0]))
        for k in np.where(f["ts"].values > lb)[0]:
            events.append((int(f["ts"].iloc[k]), s, int(k)))
    events.sort()
    def closes_at(ts):
        return {x: float(frames[x].loc[frames[x]["ts"] <= ts, "close"].iloc[-1]) for x in frames if (frames[x]["ts"] <= ts).any()}
    prev_ts = None
    for ts, s, k in events:
        if prev_ts is not None and ts != prev_ts:
            snap(st, prev_ts, closes_at(prev_ts))
        prev_ts = ts
        row = frames[s].iloc[k]
        day = time.strftime("%Y-%m-%d", time.gmtime(ts / 1000))
        if st["day"] != day:
            st["day"], st["day_pnl"] = day, 0.0
            st["day_start_eq"] = equity_value(st, closes_at(ts))
        if s in st["positions"]:
            p = st["positions"][s]
            if row["low"] <= p["stop"]:
                close_position(st, s, p["stop"] * (1 - CFG["slippage"]), ts, "stop")
            elif row["high"] >= p["take"]:
                close_position(st, s, p["take"], ts, "take")
        if st["paused_until"] and ts >= st["paused_until"]:
            st["paused_until"] = None; log(st, ts, "▶️ пауза после дневного лимита снята")
        if not st["paused_until"] and st["day_pnl"] <= -CFG["daily_loss_limit"] * st["day_start_eq"]:
            st["paused_until"] = ts + 86400_000; log(st, ts, f"🛑 дневной убыток {st['day_pnl']:+.2f} $: пауза на 24 часа")
        if s not in st["positions"] and not st["paused_until"] and len(st["positions"]) < CFG["max_positions"] and bool(row["entry"]):
            open_position(st, s, row, ts, equity_value(st, closes_at(ts)))
        st["last_bar"][s] = ts
    if prev_ts is not None:
        snap(st, prev_ts, closes_at(prev_ts))

    # текущая оценка по последним ценам
    marks = dict(last_close)
    for s in list(st["positions"]):
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
    st["status"].update(last_run=now_ms(), last_error="; ".join(errors) if errors else None,
                        errors=st["status"]["errors"] + (1 if errors else 0), symbols_ok=len(frames), events=len(events))
    log(st, now_ms(), f"капитал {eq:.2f} $  позиций {len(st['positions'])}  новых свечей {len(events)}" + (f"  ⚠ {len(errors)} ошибок" if errors else ""))
    save(st)


def save(st):
    tmp = STATE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, STATE)   # атомарная запись: никогда не остаётся полфайла


if __name__ == "__main__":
    try:
        run()
    except Exception:
        traceback.print_exc()
        st = load_state()
        st["status"].update(errors=st["status"].get("errors", 0) + 1, last_error=traceback.format_exc().strip().splitlines()[-1][:200], last_run=now_ms())
        save(st)
        raise SystemExit(0)   # состояние сохранено с пометкой об ошибке, workflow не падает
