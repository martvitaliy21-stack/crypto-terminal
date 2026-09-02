"""ГРОШ TERMINAL: корреляции, беты, регрессионные каналы, проекции → docs/data.json.
Источник цен: зеркало Binance (data-api.binance.vision), запасной — CoinGecko.
Запуск: python analyze.py
"""
import json
import math
import os
import time
import warnings
import numpy as np
import pandas as pd
import requests

warnings.filterwarnings("ignore")
ROOT = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(ROOT, "docs", "data.json")

UNIVERSE = {  # тикер: id на CoinGecko (для запасного источника)
    "BTC": "bitcoin", "ETH": "ethereum", "SOL": "solana", "BNB": "binancecoin", "XRP": "ripple",
    "ADA": "cardano", "DOGE": "dogecoin", "AVAX": "avalanche-2", "LINK": "chainlink", "DOT": "polkadot",
    "LTC": "litecoin", "TRX": "tron", "ATOM": "cosmos", "NEAR": "near", "SUI": "sui",
    "UNI": "uniswap", "AAVE": "aave", "POL": "polygon-ecosystem-token", "PEPE": "pepe", "TON": "the-open-network", "ZEC": "zcash",
}
WINDOWS = [30, 90, 180]
CORR_WINDOWS = [30, 90, 365]
HORIZON = 30
HIST = 365


def binance_daily(sym: str, days: int) -> pd.Series:
    since = int((time.time() - days * 86400) * 1000)
    rows = []
    for host in ("https://data-api.binance.vision", "https://api.binance.com"):
        try:
            while True:
                r = requests.get(f"{host}/api/v3/klines", params=dict(symbol=f"{sym}USDT", interval="1d", startTime=since, limit=1000), timeout=20)
                r.raise_for_status()
                batch = r.json()
                rows += batch
                if len(batch) < 1000:
                    break
                since = batch[-1][0] + 1
            break
        except Exception as e:
            print("  binance", host, "fail:", str(e)[:80])
            rows = []
    if not rows:
        raise RuntimeError("binance unavailable")
    s = pd.Series([float(r[4]) for r in rows], index=pd.to_datetime([r[0] for r in rows], unit="ms", utc=True).normalize())
    return s.iloc[:-1]  # последняя свеча не закрыта


def coingecko_daily(cg_id: str, days: int) -> pd.Series:
    r = requests.get(f"https://api.coingecko.com/api/v3/coins/{cg_id}/market_chart",
                     params=dict(vs_currency="usd", days=min(days, 365), interval="daily"), timeout=30)
    r.raise_for_status()
    pr = r.json()["prices"]
    s = pd.Series([p[1] for p in pr], index=pd.to_datetime([p[0] for p in pr], unit="ms", utc=True).normalize())
    s = s[~s.index.duplicated(keep="last")]
    return s.iloc[:-1] if len(s) > 1 else s


def load_prices() -> pd.DataFrame:
    closes, use_cg = {}, False
    for sym, cg in UNIVERSE.items():
        try:
            if use_cg:
                raise RuntimeError("cg mode")
            closes[sym] = binance_daily(sym, HIST + 40)
            print("ok", sym, len(closes[sym]), flush=True)
        except Exception:
            use_cg = True
            try:
                closes[sym] = coingecko_daily(cg, HIST + 40)
                print("ok (coingecko)", sym, len(closes[sym]), flush=True)
                time.sleep(2.5)  # лимит CoinGecko ~30 запросов/мин
            except Exception as e:
                print("skip", sym, e)
    return pd.DataFrame(closes)


def regression_channel(logp: np.ndarray, horizon: int):
    """МНК по log-цене: линия, канал ±2σ и 95% интервал предсказания на horizon дней вперёд."""
    n = len(logp)
    x = np.arange(n, dtype=float)
    xbar = x.mean()
    sxx = ((x - xbar) ** 2).sum()
    b = ((x - xbar) * (logp - logp.mean())).sum() / sxx
    a = logp.mean() - b * xbar
    fit = a + b * x
    resid = logp - fit
    sigma = math.sqrt((resid ** 2).sum() / max(n - 2, 1))
    ss_tot = ((logp - logp.mean()) ** 2).sum()
    r2 = 1 - (resid ** 2).sum() / ss_tot if ss_tot > 0 else 0.0
    z = resid[-1] / sigma if sigma > 0 else 0.0
    xf = np.arange(n, n + horizon, dtype=float)
    mid = a + b * xf
    half = 1.96 * sigma * np.sqrt(1 + 1 / n + (xf - xbar) ** 2 / sxx)
    return dict(n=n, slope_daily_pct=(math.exp(b) - 1) * 100, slope_ann_pct=(math.exp(b * 365) - 1) * 100,
                r2=r2, sigma_pct=(math.exp(sigma) - 1) * 100, z=z,
                fit=np.exp(fit).round(6).tolist(), lo=np.exp(fit - 2 * sigma).round(6).tolist(), hi=np.exp(fit + 2 * sigma).round(6).tolist(),
                proj_mid=np.exp(mid).round(6).tolist(), proj_lo=np.exp(mid - half).round(6).tolist(), proj_hi=np.exp(mid + half).round(6).tolist())


def vol_cone(logret: np.ndarray, last_price: float, horizon: int):
    mu, sd = logret.mean(), logret.std(ddof=1)
    h = np.arange(1, horizon + 1)
    return dict(mu_daily_pct=mu * 100, vol_ann_pct=sd * math.sqrt(365) * 100,
                mid=(last_price * np.exp(mu * h)).round(6).tolist(),
                lo=(last_price * np.exp(mu * h - 1.96 * sd * np.sqrt(h))).round(6).tolist(),
                hi=(last_price * np.exp(mu * h + 1.96 * sd * np.sqrt(h))).round(6).tolist())


def beta_stats(y: pd.Series, x: pd.Series):
    d = pd.concat([y, x], axis=1).dropna()
    if len(d) < 20:
        return dict(beta=None, alpha_daily_pct=None, r2=None, corr=None)
    xv, yv = d.iloc[:, 1].values, d.iloc[:, 0].values
    b = np.cov(xv, yv, ddof=1)[0, 1] / np.var(xv, ddof=1)
    r = np.corrcoef(xv, yv)[0, 1]
    return dict(beta=b, alpha_daily_pct=(yv.mean() - b * xv.mean()) * 100, r2=r * r, corr=r)


def main():
    px = load_prices().ffill().iloc[-HIST:].dropna(axis=1)
    stale = [c for c in px.columns if (px[c].diff().iloc[-10:] == 0).all()]
    if stale:
        print("stale, dropped:", stale)
    px = px.drop(columns=stale)
    syms = list(px.columns)
    lr = np.log(px).diff().dropna()
    dates = [d.strftime("%Y-%m-%d") for d in px.index]
    last_date = px.index[-1]
    proj_dates = [(last_date + pd.Timedelta(days=i)).strftime("%Y-%m-%d") for i in range(1, HORIZON + 1)]
    out = dict(generated=time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime()), symbols=syms, dates=dates, proj_dates=proj_dates,
               windows=WINDOWS, corr_windows=CORR_WINDOWS, horizon=HORIZON,
               prices={s: px[s].round(8).tolist() for s in syms}, stats={}, regression={}, cone={}, corr={}, roll_corr={}, leadlag={}, links={}, edges=[])
    for s in syms:
        p = px[s]
        st = dict(price=float(p.iloc[-1]), chg1=float(p.iloc[-1] / p.iloc[-2] - 1) * 100, chg7=float(p.iloc[-1] / p.iloc[-8] - 1) * 100,
                  chg30=float(p.iloc[-1] / p.iloc[-31] - 1) * 100, vol30=float(lr[s].iloc[-30:].std(ddof=1) * math.sqrt(365) * 100),
                  dd90=float((p.iloc[-90:] / p.iloc[-90:].cummax() - 1).min() * 100))
        b90, b30, be = beta_stats(lr[s].iloc[-90:], lr["BTC"].iloc[-90:]), beta_stats(lr[s].iloc[-30:], lr["BTC"].iloc[-30:]), beta_stats(lr[s].iloc[-90:], lr["ETH"].iloc[-90:])
        st.update(beta90=b90["beta"], alpha90=b90["alpha_daily_pct"], r2_90=b90["r2"], corr90=b90["corr"], corr30=b30["corr"], beta_eth=be["beta"], corr_eth=be["corr"])
        out["stats"][s] = st
        out["regression"][s] = {str(w): regression_channel(np.log(p.iloc[-w:].values), HORIZON) for w in WINDOWS}
        out["cone"][s] = vol_cone(lr[s].iloc[-90:].values, float(p.iloc[-1]), HORIZON)
        out["roll_corr"][s] = lr[s].rolling(30).corr(lr["BTC"]).iloc[-180:].round(4).where(lambda x: x.notna(), None).tolist()
        out["leadlag"][s] = {str(k): float(lr["BTC"].iloc[-180:].corr(lr[s].shift(-k).iloc[-180:])) for k in range(-3, 4)}
    for w in CORR_WINDOWS:
        out["corr"][str(w)] = lr.iloc[-w:].corr().round(3).values.tolist()
    c90 = lr.iloc[-90:].corr()
    for s in syms:
        top = c90[s].drop(s).sort_values(ascending=False)
        out["links"][s] = [dict(s=k, c=round(float(v), 3)) for k, v in top.head(4).items()] + [dict(s=k, c=round(float(v), 3), low=True) for k, v in top.tail(2).items()]
    for i, a in enumerate(syms):
        for b in syms[i + 1:]:
            if c90.loc[a, b] >= 0.75:
                out["edges"].append([a, b, round(float(c90.loc[a, b]), 3)])
    idx = lr.index[-180:]
    avg = []
    for d in idx:
        m = lr.loc[:d].iloc[-30:].corr().values
        avg.append(round(float(m[np.triu_indices_from(m, 1)].mean()), 4))
    out["avg_corr"] = dict(dates=[d.strftime("%Y-%m-%d") for d in idx], values=avg)
    out["roll_dates"] = out["avg_corr"]["dates"]
    with open(OUT, "w") as f:
        json.dump(out, f, separators=(",", ":"))
    print("written", OUT, os.path.getsize(OUT) // 1024, "KB", len(syms), "symbols")


if __name__ == "__main__":
    main()
