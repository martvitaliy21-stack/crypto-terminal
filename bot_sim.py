"""Паперный бот ГРОШ (правила v3): виртуальные 500 USDT, те же сделки, что получает Telegram-бот сигналов.

Вход на закрытии 4h-свечи: тренд (close>EMA200, EMA50>EMA200), откат RSI≤50 с разворотом, объём выше среднего,
бычья свеча, EMA50 за 5 дней упала не больше чем на 1.5%; не больше 2 сигналов за свечу (с меньшим RSI — первые).
Исполнение как у человека: сигналы доставляются с 08:00 до 23:30 по Киеву, ночные ждут утра и перепроверяются;
вход по текущей цене; стоп 3×ATR и тейк 4R ставятся одним OCO-ордером и больше не двигаются.
Риск 1% на сделку, под риском одновременно не больше 2% депозита (≤ 2 позиций), вложено не больше 100%.
Выходы проверяются по часовым свечам и по живой цене. Запуск — GitHub Actions каждые ~30 минут.
Устойчивость: пропущенные свечи обрабатываются при следующем запуске, сбой одной монеты не роняет остальные,
состояние пишется атомарно. События (сигнал/закрытие/отмена) копятся в state["events"] для tg_bot.py.
"""
import json
import math
import os
import time
import traceback
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import numpy as np
import pandas as pd
import requests

ROOT = os.path.dirname(os.path.abspath(__file__))
STATE = os.environ.get("GROSH_STATE") or os.path.join(ROOT, "docs", "bot", "state.json")
SYMBOLS = ["BTC", "ETH", "SOL", "BNB", "XRP", "LINK", "AVAX"]
TF_MS = 4 * 3600 * 1000
H_MS = 3600 * 1000
KYIV = ZoneInfo("Europe/Kyiv")
CFG = dict(start_equity=500.0, risk_per_trade=0.01, max_positions=2, max_heat=0.02, exposure=1.0, daily_loss_limit=0.03,
           fee=0.001, slippage=0.0005, ema_fast=50, ema_slow=200, rsi_len=14, rsi_entry=50, rsi_lookback=3,
           atr_len=14, atr_stop_mult=3.0, vol_len=20, zwin=540,
           rules_version=3, take_mode="fixed", tp_r=4.0, slope50_min=-1.5, slope50_bars=30, max_new_per_bar=2,
           work_from=8 * 60, work_to=23 * 60 + 30, chase_atr=1.0, signal_max_age_h=10, tz="Europe/Kyiv")
RULES_V3_NOTE = ("правила v3: вход только если EMA50 за 5 дней упала не больше чем на 1.5% и не больше 2 сигналов за свечу; "
                 "стоп 3 ATR и фиксированный тейк 4R одним OCO-ордером; риск 1% на сделку, под риском не больше 2% депозита; "
                 "сигналы с 08:00 до 23:30 по Киеву, ночные перепроверяются утром")
HOSTS = ["https://data-api.binance.vision", "https://api.binance.com"]


def now_ms():
    return int(os.environ["GROSH_NOW"]) if os.environ.get("GROSH_NOW") else int(time.time() * 1000)


def hour_floor(ms):
    return ms - ms % H_MS


def kyiv(ms):
    return datetime.fromtimestamp(ms / 1000, KYIV)


def fmt_kyiv(ms):
    return kyiv(ms).strftime("%d.%m %H:%M")


def in_work(ms):
    try:
        ext = json.load(open(os.path.join(ROOT, "signals", "schedule.json"))).get("extend_until", 0)
        if ms - 8 * 3600_000 < ext and ms <= ext:      # разовое продление вечера (вечер того же дня → до ext)
            return True
    except Exception:
        pass
    t = kyiv(ms); m = t.hour * 60 + t.minute
    return CFG["work_from"] <= m <= CFG["work_to"]


def klines(sym: str, interval: str = "4h", limit: int = 760) -> pd.DataFrame:
    step = TF_MS if interval == "4h" else H_MS
    last_err = None
    for host in HOSTS:
        try:
            r = requests.get(f"{host}/api/v3/klines", params=dict(symbol=f"{sym}USDT", interval=interval, limit=limit), timeout=20)
            r.raise_for_status()
            rows = r.json()
            df = pd.DataFrame([[int(x[0])] + [float(v) for v in x[1:6]] for x in rows], columns=["ts", "open", "high", "low", "close", "volume"])
            return df[df["ts"] + step <= now_ms()].reset_index(drop=True)   # только закрытые свечи
        except Exception as e:
            last_err = e
    raise RuntimeError(f"{sym} {interval}: {last_err}")


def prices(syms) -> dict:
    q = json.dumps([f"{s}USDT" for s in syms], separators=(",", ":"))
    for host in HOSTS:
        try:
            r = requests.get(f"{host}/api/v3/ticker/price", params=dict(symbols=q), timeout=10)
            r.raise_for_status()
            return {x["symbol"][:-4]: float(x["price"]) for x in r.json()}
        except Exception:
            continue
    return {}


def price(sym: str) -> float:
    p = prices([sym])
    if sym not in p:
        raise RuntimeError(f"{sym}: price unavailable")
    return p[sym]


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
    d["slope50"] = d["ema_fast"].pct_change(CFG["slope50_bars"]) * 100
    d["slope_ok"] = d["slope50"] >= CFG["slope50_min"]
    d["entry"] = d["trend"] & d["pullback"] & d["vol_ok"] & d["bullish"] & d["atr"].notna()
    d["entry_v3"] = d["entry"] & d["slope_ok"]
    d["z"] = rolling_z(c.values, CFG["zwin"])
    return d


def load_state() -> dict:
    if os.path.exists(STATE):
        with open(STATE) as f:
            st = json.load(f)
        if st.get("cash") is not None:
            st.setdefault("pending", []); st.setdefault("events", [])
            return st
    t0 = now_ms()
    return dict(version=1, started=t0, live_from=t0, start_equity=CFG["start_equity"], cash=CFG["start_equity"],
                positions={}, trades=[], equity=[], last_bar={}, log=[], paused_until=None, day=None, day_start_eq=CFG["start_equity"],
                day_pnl=0.0, btc_start=None, config=CFG, pending=[], events=[], rules_version=CFG["rules_version"],
                status=dict(runs=0, errors=0, last_run=None, last_error=None))


def log(st, ts, msg):
    st["log"].append([ts, msg]); st["log"] = st["log"][-80:]
    print(time.strftime("%Y-%m-%d %H:%M", time.gmtime(ts / 1000)), msg)


def emit(st, ev):
    evs = st.setdefault("events", [])
    if any(e.get("id") == ev["id"] for e in evs):
        return
    evs.append(ev); st["events"] = evs[-300:]


def equity_value(st, marks: dict) -> float:
    return st["cash"] + sum(p["qty"] * marks.get(s, p["entry"]) for s, p in st["positions"].items())


def invested(st, marks: dict) -> float:
    return sum(p["qty"] * marks.get(s, p["entry"]) for s, p in st["positions"].items())


def heat(st, eq: float) -> float:
    """Доля капитала, которую портфель потеряет, если все стопы сработают сейчас."""
    return sum(max(0.0, p["entry"] - p["stop"]) * p["qty"] for p in st["positions"].values()) / eq if eq > 0 else 0.0


def compact_equity(st):
    """Старше 3 дней оставляем одну точку на 4 часа, чтобы история не обрезалась."""
    e = st["equity"]
    if len(e) <= 2500:
        return
    cutoff = e[-1][0] - 3 * 86400_000
    thin = {}
    for p in e:
        if p[0] < cutoff:
            thin[p[0] // TF_MS] = p
    st["equity"] = sorted(thin.values()) + [p for p in e if p[0] >= cutoff]


def close_position(st, sym, px, ts, reason):
    p = st["positions"].pop(sym)
    proceeds = p["qty"] * px * (1 - CFG["fee"])
    pnl = proceeds - p["cost"]
    st["cash"] += proceeds; st["day_pnl"] += pnl
    risk0 = p.get("risk0") or (p["entry"] - p.get("stop0", p["stop"])) * p["qty"]
    if reason == "stop" and p["stop"] >= p["entry"] * (1 + 2 * CFG["fee"]):
        reason = "trail"
    R = p.get("R") or (p["entry"] - p.get("stop0", p["stop"]))
    tr = dict(symbol=sym, entry_ts=p["entry_ts"], exit_ts=ts, entry=p["entry"], exit=px, qty=p["qty"],
              pnl=round(pnl, 4), pnl_pct=round(pnl / p["cost"], 5), r=round(pnl / risk0, 3) if risk0 > 0 else None,
              reason=reason, max_r=round((p.get("max_high", p["entry"]) - p["entry"]) / R, 2) if R else None, rules=st.get("rules_version", 1))
    st["trades"].append(tr)
    emit(st, dict(id=f"close|{sym}|{p['entry_ts']}", kind="close", ts=ts, sym=sym, entry=p["entry"], exit=px, stop=p["stop"], take=p.get("take"),
                  pnl=round(pnl, 4), pnl_pct=tr["pnl_pct"], r=tr["r"], reason=reason, entry_ts=p["entry_ts"], hold_h=round((ts - p["entry_ts"]) / H_MS, 1)))
    label = {"take": "тейк", "stop": "стоп", "trail": "трейлинг"}.get(reason, reason)
    log(st, ts, f"{'✅' if pnl > 0 else '❌'} {sym} закрыта [{label}] {p['entry']:.5g} → {px:.5g}  {pnl:+.2f} $ ({pnl / p['cost'] * 100:+.2f}%"
                + (f", {pnl / risk0:+.2f}R" if risk0 > 0 else "") + ")")


def check_exits(st, f1: dict, live: dict, now: int):
    """Стоп и тейк по закрытым часовым свечам после входа (стоп раньше тейка), затем по живой цене."""
    for sym in list(st["positions"]):
        p = st["positions"][sym]
        f = f1.get(sym)
        start = p.get("checked_to") or (hour_floor(p["entry_ts"]) + H_MS)
        closed = False
        if f is not None and len(f):
            bars = f[(f["ts"] >= start) & (f["ts"] + H_MS <= now)]
            for b in bars.itertuples():
                if b.low <= p["stop"]:
                    close_position(st, sym, p["stop"] * (1 - CFG["slippage"]), int(b.ts) + H_MS, "stop"); closed = True; break
                if p.get("take") and b.high >= p["take"]:
                    close_position(st, sym, p["take"], int(b.ts) + H_MS, "take"); closed = True; break
                p["max_high"] = max(p.get("max_high", p["entry"]), float(b.high))
                p["checked_to"] = int(b.ts) + H_MS
        if closed:
            continue
        px = live.get(sym)
        if px is not None:
            if px <= p["stop"]:
                close_position(st, sym, p["stop"] * (1 - CFG["slippage"]), now, "stop")
            elif p.get("take") and px >= p["take"]:
                close_position(st, sym, p["take"], now, "take")
            else:
                p["max_high"] = max(p.get("max_high", p["entry"]), px)


def queue_signals(st, frames: dict):
    """Новые закрытые 4h-свечи → сигналы в очередь (не больше max_new_per_bar на свечу)."""
    by_T = {}
    for s, f in frames.items():
        lb = st["last_bar"].get(s, int(f["ts"].iloc[0]))
        new = f[f["ts"] > lb]
        for row in new.itertuples():
            by_T.setdefault(int(row.ts) + TF_MS, []).append((s, row))
        if len(new):
            st["last_bar"][s] = int(new["ts"].iloc[-1])
    pend = {q["sym"] for q in st["pending"]}
    for T in sorted(by_T):
        cands = [(float(r.rsi), s, r) for s, r in by_T[T] if bool(r.entry_v3) and s not in st["positions"] and s not in pend]
        cands.sort(key=lambda x: x[0])
        for rsi, s, r in cands[: CFG["max_new_per_bar"]]:
            atr, close = float(r.atr), float(r.close)
            sig = dict(id=f"{s}|{T}", sym=s, T=T, close=close, atr=atr, rsi=round(rsi, 1), stop_ref=close - CFG["atr_stop_mult"] * atr,
                       chase_max=close + CFG["chase_atr"] * atr, night=not in_work(T))
            st["pending"].append(sig); pend.add(s)
            log(st, T, f"📡 {s}: сигнал на закрытии свечи, цена {close:.5g}, RSI {rsi:.0f}" + (" — ночь, ждёт 08:00" if sig["night"] else ""))


def open_position(st, sig, px, now, marks):
    s = sig["sym"]
    entry = px * (1 + CFG["slippage"]); R = CFG["atr_stop_mult"] * sig["atr"]; stop = entry - R; take = entry + CFG["tp_r"] * R
    eq = equity_value(st, marks)
    room = CFG["max_heat"] - heat(st, eq)
    if room <= 0.001:
        return f"под риском уже {heat(st, eq) * 100:.1f}% депозита (лимит {CFG['max_heat'] * 100:.0f}%)"
    qty = min(CFG["risk_per_trade"] * eq, room * eq) / R
    qty = min(qty, max(0.0, CFG["exposure"] * eq - invested(st, marks)) / entry, st["cash"] / (entry * (1 + CFG["fee"])))
    if qty * entry < 10:
        return "не хватает свободных средств"
    cost = qty * entry * (1 + CFG["fee"]); st["cash"] -= cost
    st["positions"][s] = dict(entry_ts=now, signal_ts=sig["T"], entry=entry, stop=stop, stop0=stop, take=take, qty=qty, cost=cost, R=R,
                              risk0=R * qty, max_high=entry, trailing=False, rsi=sig["rsi"], atr=sig["atr"], checked_to=hour_floor(now) + H_MS)
    emit(st, dict(id=f"open|{s}|{now}", kind="open", ts=now, sym=s, signal_ts=sig["T"], signal_close=sig["close"], entry=entry, px=px, stop=stop,
                  take=take, R=R, atr=sig["atr"], rsi=sig["rsi"], chase_max=sig["chase_max"], qty=qty, cost=cost, eq=eq, risk_usd=R * qty,
                  heat_after=heat(st, eq), night=sig.get("night", False), entry_ts=now))
    log(st, now, f"🟢 {s} куплено {qty:.5g} @ {entry:.5g} на {cost:.2f} $  стоп {stop:.5g} ({(stop / entry - 1) * 100:.1f}%)  "
                 f"тейк {take:.5g} (+{(take / entry - 1) * 100:.1f}%)  риск {R * qty:.2f} $" + (f"  · сигнал от {fmt_kyiv(sig['T'])}" if sig.get("night") else ""))
    return None


def deliver(st, live: dict, now: int, marks: dict):
    """Доставка сигналов: только в рабочее время, с перепроверкой цены."""
    if not st["pending"] or not in_work(now):
        return
    keep = []
    for sig in sorted(st["pending"], key=lambda q: (q["T"], q["rsi"])):
        s = sig["sym"]; px = live.get(s)
        if px is None:
            keep.append(sig); continue
        reason = None
        if s in st["positions"]:
            reason = "по монете уже открыта сделка"
        elif px <= sig["stop_ref"]:
            reason = "цена уже ниже уровня стопа"
        elif px > sig["chase_max"]:
            reason = "цена ушла выше зоны входа"
        elif (now - sig["T"]) / H_MS > CFG["signal_max_age_h"]:
            reason = "сигнал устарел"
        elif st.get("paused_until"):
            reason = "пауза после дневного лимита убытка"
        elif len(st["positions"]) >= CFG["max_positions"]:
            reason = f"уже открыто {CFG['max_positions']} сделки"
        if reason is None:
            reason = open_position(st, sig, px, now, marks)
        if reason:
            emit(st, dict(id=f"cancel|{sig['id']}", kind="cancel", ts=now, sym=s, T=sig["T"], night=sig.get("night", False), reason=reason, px=px))
            log(st, now, f"⏭ {s}: сигнал от {fmt_kyiv(sig['T'])} не исполнен — {reason}")
    st["pending"] = keep


def migrate_v3(st, ts):
    if st.get("rules_version", 1) >= 3:
        return
    for sym, p in st["positions"].items():
        R = p.get("R") or (p["entry"] - p.get("stop0", p["stop"]))
        p["R"] = R; p.setdefault("stop0", p["stop"]); p.setdefault("risk0", R * p["qty"])
        p["take"] = p["entry"] + CFG["tp_r"] * R; p["trailing"] = False; p["checked_to"] = hour_floor(ts)
    st["rules_version"] = 3; st["config"] = CFG
    st.setdefault("pending", []); st.setdefault("events", [])
    st.setdefault("rules_changes", []).append([ts, RULES_V3_NOTE])
    emit(st, dict(id="rules|v3", kind="rules", ts=ts, note=RULES_V3_NOTE))
    log(st, ts, "⚙️ " + RULES_V3_NOTE + (f"; открытым позициям ({len(st['positions'])}) поставлен тейк 4R" if st["positions"] else ""))


def signals(st, frames, eq):
    """Сканер: состояние каждого условия входа по последней закрытой свече."""
    out = {}
    pend = {q["sym"]: q for q in st["pending"]}
    for s, f in frames.items():
        r = f.iloc[-1]
        prev_rsi = float(f["rsi"].iloc[-2]) if len(f) > 1 else float("nan")
        g = lambda v: None if (isinstance(v, float) and math.isnan(v)) else (float(v) if isinstance(v, (float, np.floating)) else v)
        out[s] = dict(ts=int(r["ts"]) + TF_MS, close=g(r["close"]), ema_fast=g(r["ema_fast"]), ema_slow=g(r["ema_slow"]),
                      trend=bool(r["trend"]), dist_ema_slow_pct=g((r["close"] / r["ema_slow"] - 1) * 100) if not math.isnan(r["ema_slow"]) else None,
                      rsi=g(r["rsi"]), rsi_prev=g(prev_rsi), rsi_min3=g(r["rsi_min"]), pullback=bool(r["pullback"]),
                      vol_ratio=g(r["volume"] / r["vol_ma"]) if r["vol_ma"] else None, vol_ok=bool(r["vol_ok"]), bullish=bool(r["bullish"]),
                      slope50=g(r["slope50"]), slope_ok=bool(r["slope_ok"]),
                      atr_pct=g(r["atr"] / r["close"] * 100), z=g(r["z"]), entry=bool(r["entry_v3"]), in_position=s in st["positions"],
                      pending=s in pend, stop_pct=g(-CFG["atr_stop_mult"] * r["atr"] / r["close"] * 100),
                      take_pct=g(CFG["tp_r"] * CFG["atr_stop_mult"] * r["atr"] / r["close"] * 100))
    st["signals"] = out
    st["heat"] = round(heat(st, eq), 5); st["heat_cap"] = CFG["max_heat"]


def run():
    st = load_state()
    st["status"]["runs"] += 1
    now = now_ms()
    f4, f1, errors = {}, {}, []
    for s in SYMBOLS:
        try:
            f4[s] = indicators(klines(s, "4h", 760))
            f1[s] = klines(s, "1h", 200)
        except Exception as e:
            errors.append(str(e)[:120]); print("skip", s, e)
    if not f4:
        st["status"].update(errors=st["status"]["errors"] + 1, last_error="нет данных ни по одной монете", last_run=now)
        save(st); return
    last_close = {s: float(f["close"].iloc[-1]) for s, f in f4.items()}
    live = prices(SYMBOLS)
    if not live:
        errors.append("живые цены недоступны")
    marks = {**last_close, **live}
    if not st["last_bar"]:
        for s, f in f4.items():
            st["last_bar"][s] = int(f["ts"].iloc[-1])
        st["live_from"] = st["started"] = now
        st["btc_start"] = marks.get("BTC")
        log(st, now, f"старт симуляции: {CFG['start_equity']:.0f} $, без предыстории — все сделки только в реальном времени")
    migrate_v3(st, now)
    day = time.strftime("%Y-%m-%d", time.gmtime(now / 1000))
    if st.get("day") != day:
        st["day"], st["day_pnl"], st["day_start_eq"] = day, 0.0, equity_value(st, marks)
    check_exits(st, f1, live, now)
    if st.get("paused_until") and now >= st["paused_until"]:
        st["paused_until"] = None; log(st, now, "▶️ пауза после дневного лимита снята")
    if not st.get("paused_until") and st["day_pnl"] <= -CFG["daily_loss_limit"] * st["day_start_eq"]:
        st["paused_until"] = now + 86400_000; log(st, now, f"🛑 дневной убыток {st['day_pnl']:+.2f} $: пауза на 24 часа")
    queue_signals(st, f4)
    deliver(st, live, now, marks)
    eq = equity_value(st, marks)
    bh = CFG["start_equity"] * marks["BTC"] / st["btc_start"] if st.get("btc_start") and marks.get("BTC") else None
    if not st["equity"] or st["equity"][-1][0] < now:
        st["equity"].append([now, round(eq, 4), round(bh, 4) if bh else None])
    compact_equity(st)
    st["marks"] = {s: marks.get(s) for s in st["positions"]}
    signals(st, f4, eq)
    st["status"].update(last_run=now, last_error="; ".join(errors) if errors else None,
                        errors=st["status"]["errors"] + (1 if errors else 0), symbols_ok=len(f4), events=len(st["events"]))
    log(st, now, f"капитал {eq:.2f} $  позиций {len(st['positions'])}  ждут {len(st['pending'])}  под риском {heat(st, eq) * 100:.1f}%"
                 + (f"  ⚠ {len(errors)} ошибок" if errors else ""))
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
