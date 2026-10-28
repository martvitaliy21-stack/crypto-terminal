# -*- coding: utf-8 -*-
"""Telegram-бот сигналов ГРОШ (@martallen_bot).

Читает события паперного бота (docs/bot/state.json → events) и отправляет владельцу:
сигнал с монетой, входом, стоп-лоссом, тейк-профитом и размером позиции; сообщение о закрытии сделки;
утреннюю сводку (то, что случилось ночью) и итог дня. Рабочее время 08:00–23:30 по Киеву.
Команды: /status /deposit /risk /pause /resume /help. Подключение — только по персональной ссылке
t.me/<бот>?start=<код> (код — секрет TELEGRAM_OWNER_CODE).
Приватные данные (чат владельца, депозит, настройки, что уже отправлено) хранятся в signals/tg.enc,
зашифрованные ключом из токена бота. В логи GitHub не пишется ничего личного — только счётчики.
Режимы TG_MODE: send (по умолчанию), mark (отметить события отправленными без отправки), dry (печатать вместо отправки).
"""
import base64
import hashlib
import html
import json
import math
import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo
import requests
from cryptography.fernet import Fernet, InvalidToken

ROOT = os.path.dirname(os.path.abspath(__file__))
STATE = os.environ.get("GROSH_STATE") or os.path.join(ROOT, "docs", "bot", "state.json")
PRIV = os.environ.get("GROSH_TG_PRIV") or os.path.join(ROOT, "signals", "tg.enc")
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
OWNER_CODE = os.environ.get("TELEGRAM_OWNER_CODE", "").strip()
MODE = os.environ.get("TG_MODE", "send")
KYIV = ZoneInfo("Europe/Kyiv")
WORK = (8 * 60, 23 * 60 + 30)
EVENING = (23 * 60, 23 * 60 + 30)
SITE = "https://martvitaliy21-stack.github.io/crypto-terminal/"
TICK = {"BTC": 2, "ETH": 2, "SOL": 2, "BNB": 2, "XRP": 4, "LINK": 3, "AVAX": 3}          # знаков после запятой (шаг цены Binance)
STEP = {"BTC": 5, "ETH": 4, "SOL": 3, "BNB": 3, "XRP": 1, "LINK": 2, "AVAX": 2}          # знаков в количестве (шаг лота Binance)
FEE = 0.001
RISK_MIN, RISK_MAX = 0.25, 2.0


def now_ms():
    return int(os.environ["GROSH_NOW"]) if os.environ.get("GROSH_NOW") else int(time.time() * 1000)


def kyiv(ms):
    return datetime.fromtimestamp(ms / 1000, KYIV)


def minutes(ms):
    t = kyiv(ms); return t.hour * 60 + t.minute


def in_work(ms):
    return WORK[0] <= minutes(ms) <= WORK[1]


def hm(ms):
    return kyiv(ms).strftime("%H:%M")


def dm(ms):
    return kyiv(ms).strftime("%d.%m %H:%M")


# ---------- приватное состояние ----------
def fernet():
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(("grosh-tg:" + TOKEN).encode()).digest()))


def load_priv():
    base = dict(owner=None, deposit=None, risk=1.0, paused=False, offset=0, sent=[], held=[], taken={}, last_morning=None, last_evening=None)
    if os.path.exists(PRIV):
        try:
            data = json.loads(fernet().decrypt(open(PRIV, "rb").read()).decode())
            return {**base, **data}, json.dumps(data, sort_keys=True)
        except (InvalidToken, ValueError):
            print("tg: private state unreadable (token changed?) — starting fresh")
    return base, None


def save_priv(p, before):
    p["sent"] = p["sent"][-1000:]; p["held"] = p["held"][-40:]
    now = json.dumps(p, sort_keys=True)
    if now == before:
        return False
    os.makedirs(os.path.dirname(PRIV), exist_ok=True)
    tmp = PRIV + ".tmp"
    with open(tmp, "wb") as f:
        f.write(fernet().encrypt(now.encode()))
    os.replace(tmp, PRIV)
    return True


# ---------- Telegram API ----------
SENT = 0


def tg(method, **params):
    for attempt in range(2):
        try:
            r = requests.post(f"https://api.telegram.org/bot{TOKEN}/{method}", json=params, timeout=20)
            j = r.json()
            if j.get("ok"):
                return j["result"]
            print(f"tg: {method} not ok: {j.get('error_code')} {str(j.get('description'))[:80]}")
            if j.get("error_code") == 429:
                time.sleep(min(30, j.get("parameters", {}).get("retry_after", 5)))
                continue
            return None
        except Exception as e:
            print(f"tg: {method} error {type(e).__name__}")
            time.sleep(2)
    return None


def send(chat_id, text):
    global SENT
    if MODE == "mark":
        return True
    if MODE == "dry":
        print("\n----- сообщение -----\n" + html.unescape(text.replace("<b>", "").replace("</b>", "").replace("<i>", "").replace("</i>", "").replace("<code>", "").replace("</code>", "")))
        SENT += 1
        return True
    ok = tg("sendMessage", chat_id=chat_id, text=text, parse_mode="HTML", disable_web_page_preview=True) is not None
    SENT += ok
    return ok


# ---------- форматирование ----------
def px(sym, v):
    d = TICK.get(sym, 4)
    q = math.floor(v * 10 ** d + 1e-9) / 10 ** d
    return f"{q:.{d}f}"


def qty(sym, q):
    d = STEP.get(sym, 4)
    v = math.floor(q * 10 ** d) / 10 ** d
    s = f"{v:.{d}f}"
    return s.rstrip("0").rstrip(".") if "." in s else s


def rr(v):
    return f"{'+' if v >= 0 else '−'}{abs(v):.1f}R"


def pct(v, d=1):
    return f"{'+' if v >= 0 else '−'}{abs(v) * 100:.{d}f}%"


def usd(v, sign=False):
    s = f"{abs(v):,.2f}".replace(",", " ")
    return (("+" if v >= 0 else "−") if sign else ("−" if v < 0 else "")) + s + " $"


def dur(h):
    d = int(h // 24); hh = int(round(h - d * 24))
    return (f"{d} дн " if d else "") + f"{hh} ч"


def plural(n, a, b, c):
    m, k = n % 10, n % 100
    return c if 11 <= k <= 14 else a if m == 1 else b if 2 <= m <= 4 else c


# ---------- размер позиции пользователя ----------
def user_size(p, ev):
    risk = p["risk"] / 100.0
    frac = risk * ev["entry"] / ev["R"]                 # доля депозита в позиции
    out = dict(frac=frac, risk_pct=p["risk"])
    D = p.get("deposit")
    if D:
        used = sum(t.get("value") or 0 for t in p["taken"].values())
        free = max(0.0, D - used)
        value = D * frac
        out["capped"] = value > free
        value = min(value, free)
        out.update(value=value, qty=value / ev["entry"], risk_usd=value / ev["entry"] * ev["R"])
    return out


def msg_open(p, ev, sz):
    s = ev["sym"]; e = ev["entry"]
    sl, tp, sl_lim, zone = ev["stop"], ev["take"], ev["stop"] * 0.995, ev["chase_max"]
    head = f"<i>свеча 4h закрылась в {hm(ev['signal_ts'])}" + (" ночью — сигнал перепроверен утром" if ev.get("night") else "") + f" · сейчас {hm(ev['ts'])}</i>"
    L = [f"🟢 <b>СИГНАЛ · {s}/USDT</b> · покупка на споте", head, "",
         f"Вход: <b>по рынку</b>, цена ≈ <code>{px(s, e)}</code>",
         f"Не входить, если цена уже выше <code>{px(s, zone)}</code>", "",
         f"🛑 Стоп-лосс: <code>{px(s, sl)}</code>  ({pct(sl / e - 1)})",
         f"🎯 Тейк-профит: <code>{px(s, tp)}</code>  ({pct(tp / e - 1)})",
         "Риск : прибыль = 1 : 4", ""]
    if sz.get("value") is not None:
        if sz["value"] < 5:
            L.append(f"💰 Свободных средств почти нет — по правилам риска эту сделку лучше пропустить.")
        else:
            L += [f"💰 Риск {sz['risk_pct']:g}% депозита = <b>{usd(sz['risk_usd'])}</b>",
                  f"Купить на <b>{sz['value']:.2f} USDT</b> ≈ {qty(s, sz['qty'])} {s} ({sz['frac'] * 100:.0f}% депозита)" + (" — урезано до свободных средств" if sz.get("capped") else "")]
    else:
        L += [f"💰 Риск {sz['risk_pct']:g}% депозита → позиция <b>{sz['frac'] * 100:.0f}% депозита</b>",
              "Чтобы бот считал сумму в долларах, пришли депозит: <code>/deposit 1000</code>"]
    L += ["", "На Binance: купить по рынку, затем сразу <b>OCO на продажу</b> всего купленного:",
          f"· тейк-профит (лимит): <code>{px(s, tp)}</code>",
          f"· стоп-лосс: триггер <code>{px(s, sl)}</code>, лимит <code>{px(s, sl_lim)}</code>",
          "Дальше ничего не двигать — бот напишет, когда сделка закроется.",
          f"<i>#{s} · сигнал механической стратегии v3, не персональная рекомендация</i>"]
    return "\n".join(L)


def close_line(ev, t):
    s = ev["sym"]; win = ev["pnl"] > 0
    label = {"take": "тейк-профит", "stop": "стоп-лосс", "trail": "подтянутый стоп"}.get(ev["reason"], ev["reason"])
    r = f" · {rr(ev['r'])}" if ev.get("r") is not None else ""
    user = ""
    if t and t.get("value"):
        pnl = t["qty"] * ev["exit"] * (1 - FEE) - t["value"] * (1 + FEE)
        user = f" · у тебя {usd(pnl, sign=True)}"
    return win, f"{'✅' if win else '❌'} {s}: {label} {px(s, ev['exit'])} · {pct(ev['exit'] / ev['entry'] - 1)}{r}{user}"


def msg_close(ev, t):
    s = ev["sym"]; win, line = close_line(ev, t)
    label = {"take": "тейк-профит", "stop": "стоп-лосс", "trail": "подтянутый стоп"}.get(ev["reason"], ev["reason"])
    L = [f"{'✅' if win else '❌'} <b>{s}/USDT · {label}</b>",
         f"{px(s, ev['entry'])} → {px(s, ev['exit'])} · {pct(ev['exit'] / ev['entry'] - 1)}" + (f" · {rr(ev['r'])}" if ev.get("r") is not None else "")]
    if t and t.get("value"):
        pnl = t["qty"] * ev["exit"] * (1 - FEE) - t["value"] * (1 + FEE)
        L.append(f"По твоему размеру: <b>{usd(pnl, sign=True)}</b> (вход был на {t['value']:.2f} USDT)")
    L.append(f"Сделка длилась {dur(ev.get('hold_h') or 0)}.")
    L.append("Если OCO-ордер стоит на бирже, позиция уже закрыта автоматически.")
    if not win:
        L.append("<i>Стоп — нормальная часть стратегии: выигрывает примерно каждая четвёртая сделка, зато тейк вчетверо больше стопа.</i>")
    return "\n".join(L)


def positions_block(p, st, live=None, exclude=()):
    marks = {**(st.get("marks") or {}), **(live or {})}
    L = []
    used = 0.0; risk = 0.0
    for key, t in p["taken"].items():
        if key in exclude:
            continue
        s = t["sym"]; pos = st["positions"].get(s)
        m = marks.get(s) or t["entry"]
        R = t["entry"] - t["stop"]
        L.append(f"• {s}: вход {px(s, t['entry'])}, сейчас {px(s, m)} ({pct(m / t['entry'] - 1)}, {rr((m - t['entry']) / R)}) · "
                 f"стоп {px(s, t['stop'])} · тейк {px(s, t['take'])} · {dur((now_ms() - t['ts']) / 3600000)}")
        used += t.get("value") or 0; risk += (t.get("value") or 0) / t["entry"] * R
    return L, used, risk


def market_line(st):
    sig = st.get("signals") or {}
    up = [s for s, g in sig.items() if g.get("trend")]
    if not sig:
        return ""
    if not up:
        return "Рынок: ни одна из 7 монет не в восходящем тренде — стратегия ждёт и сигналов не даёт."
    return f"Рынок: в восходящем тренде {len(up)} из 7 ({', '.join(up)}) — сигнал придёт на откате."


# ---------- команды ----------
HELP = ("<b>Как работает бот</b>\n"
        "Следит за BTC, ETH, SOL, BNB, XRP, LINK и AVAX на 4-часовых свечах и присылает сигнал на покупку на споте "
        "с точным стоп-лоссом и тейк-профитом. Одна сделка — один OCO-ордер, дальше ничего двигать не нужно.\n\n"
        "<b>Рабочее время</b>: 08:00–23:30 по Киеву. Ночные сигналы бот перепроверяет и присылает в 08:00, если они ещё в силе. "
        "Закрытия сделок за ночь — в утренней сводке.\n\n"
        "<b>Риск</b>: по умолчанию 1% депозита на сделку, одновременно не больше двух сделок. Максимум — 2% на сделку.\n\n"
        "<b>Команды</b> (бот проверяет сообщения при каждом запуске — обычно в течение 15–30 минут):\n"
        "/status — открытые сделки и счёт стратегии\n"
        "/deposit 1000 — депозит в USDT для расчёта суммы\n"
        "/risk 1 — риск на сделку, от 0.25 до 2%\n"
        "/pause и /resume — остановить и возобновить новые сигналы\n\n"
        f"Все сделки стратегии видны на сайте: {SITE}\n"
        "<i>Сигналы механической стратегии, не персональная инвестиционная рекомендация. Решение и риск — твои.</i>")


def welcome(p, st, name):
    L = [f"👋 Привет{', ' + html.escape(name) if name else ''}! Это бот сигналов ГРОШ.", "",
         "<b>Что он делает.</b> Присылает сигнал на покупку на споте Binance: монета, вход, стоп-лосс и тейк-профит, "
         "плюс размер позиции под твой депозит. Сделка ставится одним OCO-ордером, дальше бот сам сообщит о закрытии.", "",
         "<b>Рабочее время</b> 08:00–23:30 по Киеву. Ночью сигналы не приходят — бот перепроверит их утром.", "",
         f"<b>Риск</b>: {p['risk']:g}% депозита на сделку, одновременно не больше двух сделок. Изменить: /risk 2 (максимум 2%).", "",
         "<b>Чего ждать</b> (бэктест с декабря 2024 по октябрь 2026 с комиссиями и задержкой входа): примерно 2 сигнала в месяц, "
         "сделка длится около двух недель, выигрывает примерно каждая четвёртая, бывали серии до 7 стопов подряд. "
         "Итог за 22 месяца при риске 1%: от +10% до +20%, максимальная просадка около −10%. BTC за то же время: −18%. "
         "Прошлые результаты не гарантируют будущих.", "",
         "⚠️ Это сигналы механической стратегии, а не персональная инвестиционная рекомендация. Решение о сделке и риск — твои.", ""]
    if st["positions"]:
        L.append("Сейчас у стратегии открыто (до твоего подключения, бот не будет сообщать об их закрытии):")
        for s, pos in st["positions"].items():
            L.append(f"• {s}: вход {px(s, pos['entry'])} · стоп {px(s, pos['stop'])} · тейк {px(s, pos['take'])}")
        L.append("")
    ml = market_line(st)
    if ml:
        L += [ml, ""]
    L.append("Пришли сумму депозита в USDT одним числом, например <code>1000</code> — бот будет считать сумму покупки и риск в долларах.")
    L.append("Команды: /status · /deposit · /risk · /pause · /resume · /help")
    return "\n".join(L)


def status_text(p, st):
    live = {}
    L = [f"📊 <b>Статус</b> · {dm(now_ms())}"]
    dep = f"{usd(p['deposit'])}" if p.get("deposit") else "не указан (/deposit 1000)"
    L.append(f"Депозит: {dep} · риск {p['risk']:g}% на сделку · сигналы: {'⏸ на паузе' if p['paused'] else 'включены'}")
    rows, used, risk = positions_block(p, st, live)
    if rows:
        L.append(f"\nОткрыто у тебя: {len(rows)} {plural(len(rows), 'сделка', 'сделки', 'сделок')}" +
                 (f", под риском {usd(risk)}" + (f" ({risk / p['deposit'] * 100:.1f}%)" if p.get("deposit") else "") if used else ""))
        L += rows
    else:
        L.append("\nОткрытых сделок у тебя нет.")
    pend = st.get("pending") or []
    if pend:
        L.append("Ждут 08:00: " + ", ".join(f"{q['sym']} (сигнал {hm(q['T'])})" for q in pend))
    ml = market_line(st)
    if ml:
        L.append("\n" + ml)
    eq = st["equity"][-1][1] if st.get("equity") else st.get("cash")
    tr = st.get("trades") or []
    v3 = [t for t in tr if t.get("rules") == 3]
    L.append(f"\nСчёт стратегии на сайте: {eq:.2f} $ из 500 · сделок по правилам v3: {len(v3)}" +
             (f", выигрышных {sum(1 for t in v3 if t['pnl'] > 0)}" if v3 else ""))
    lr = (st.get("status") or {}).get("last_run")
    if lr:
        L.append(f"Последний расчёт: {dm(lr)} · {SITE}")
    return "\n".join(L)


def handle_updates(p, st):
    if MODE == "mark":
        return 0
    if MODE == "dry":
        fake = os.environ.get("GROSH_FAKE_UPDATES")
        ups = [u for u in json.load(open(fake)) if u["update_id"] >= p["offset"]] if fake else []
    else:
        ups = tg("getUpdates", offset=p["offset"], timeout=0, allowed_updates=["message"]) or []
    n = 0
    for u in ups:
        p["offset"] = u["update_id"] + 1
        m = u.get("message") or {}
        chat = m.get("chat") or {}
        if chat.get("type") != "private":
            continue
        n += 1
        cid = chat["id"]; text = (m.get("text") or "").strip(); name = (m.get("from") or {}).get("first_name") or ""
        parts = text.split(); cmd = parts[0].split("@")[0].lower() if parts else ""; arg = parts[1] if len(parts) > 1 else ""
        owner = p.get("owner")
        if cmd == "/start":
            if OWNER_CODE and arg == OWNER_CODE:
                if owner and owner["chat_id"] != cid:
                    send(owner["chat_id"], "Бот переподключён к другому аккаунту по персональной ссылке.")
                p["owner"] = dict(chat_id=cid, name=name, since=now_ms())
                send(cid, welcome(p, st, name))
            elif owner and owner["chat_id"] == cid:
                send(cid, welcome(p, st, name))
            else:
                send(cid, "Это приватный бот. Подключение — только по персональной ссылке владельца.")
            continue
        if not owner or owner["chat_id"] != cid:
            continue
        if cmd in ("/help", "help"):
            send(cid, HELP)
        elif cmd == "/status":
            send(cid, status_text(p, st))
        elif cmd == "/pause":
            p["paused"] = True
            send(cid, "⏸ Новые сигналы на паузе. Об уже открытых сделках бот продолжит сообщать. Включить: /resume")
        elif cmd == "/resume":
            p["paused"] = False
            send(cid, "▶️ Сигналы снова включены.")
        elif cmd == "/risk":
            try:
                v = float(arg.replace(",", ".").rstrip("%"))
                if not RISK_MIN <= v <= RISK_MAX:
                    raise ValueError
                p["risk"] = round(v, 2)
                extra = f" = {usd(p['deposit'] * v / 100)} на сделку" if p.get("deposit") else ""
                send(cid, f"Риск на сделку: {v:g}% депозита{extra}. Одновременно не больше двух сделок — под риском до {2 * v:g}%."
                          + ("\n<i>По бэктесту с декабря 2024: при 1% — около +20% и просадка до −9%, при 2% — около +34% и просадка до −15%. "
                             "Доходность и просадка растут вместе.</i>" if v > 1 else ""))
            except ValueError:
                send(cid, f"Укажи риск числом от {RISK_MIN:g} до {RISK_MAX:g}, например: <code>/risk 1</code>")
        elif cmd == "/deposit" or (cmd and cmd.replace(".", "", 1).replace(",", "", 1).isdigit() and not p.get("deposit")):
            raw = arg if cmd == "/deposit" else cmd
            try:
                v = float(raw.replace(",", ".").replace(" ", ""))
                if not 10 <= v <= 10_000_000:
                    raise ValueError
                p["deposit"] = round(v, 2)
                send(cid, f"Депозит: {usd(v)}. Риск {p['risk']:g}% = {usd(v * p['risk'] / 100)} на сделку. Бот будет считать сумму покупки в каждом сигнале.")
            except ValueError:
                send(cid, "Укажи депозит в USDT числом, например: <code>/deposit 1000</code>")
        else:
            send(cid, "Не понял команду. Список команд: /help")
    return n


# ---------- события ----------
def process_events(p, st, now):
    """События паперного бота → сообщения для отправки сейчас (по порядку) и строки для утренней сводки."""
    owner = p.get("owner"); work = in_work(now)
    sent = set(p["sent"])
    new = [e for e in (st.get("events") or []) if e["id"] not in sent and e["ts"] <= now + 60_000]
    msgs = []
    for ev in sorted(new, key=lambda e: e["ts"]):
        p["sent"].append(ev["id"])
        if not owner or ev["ts"] < owner.get("since", 0):
            continue
        k = ev["kind"]
        if k == "open":
            key = f"{ev['sym']}|{ev['entry_ts']}"
            if p["paused"]:
                p["held"].append(f"⏸ пропущен сигнал {ev['sym']} от {hm(ev['signal_ts'])} (пауза)")
                continue
            if not (work and now - ev["ts"] <= 30 * 60_000):
                p["held"].append(f"⌛ сигнал {ev['sym']} от {hm(ev['ts'])} не доставлен вовремя — вход по нему уже неактуален")
                continue
            sz = user_size(p, ev)
            p["taken"][key] = dict(sym=ev["sym"], ts=ev["ts"], entry=ev["entry"], stop=ev["stop"], take=ev["take"],
                                   value=sz.get("value"), qty=sz.get("qty"))
            msgs.append(msg_open(p, ev, sz)); p.setdefault("_new", []).append(key)
        elif k == "close":
            t = p["taken"].pop(f"{ev['sym']}|{ev['entry_ts']}", None)
            if t is None:
                continue
            if work:
                msgs.append(msg_close(ev, t))
            else:
                p["held"].append(close_line(ev, t)[1] + f" ({hm(ev['ts'])})")
        elif k == "cancel" and ev.get("night"):
            p["held"].append(f"⏭ {ev['sym']}: ночной сигнал от {hm(ev['T'])} не прислан — {html.escape(ev['reason'])}")
    return msgs


def digests(p, st, now):
    """→ (утренняя сводка или None, вечерний итог или None)."""
    owner = p.get("owner")
    if not owner:
        return None, None
    day = kyiv(now).strftime("%Y-%m-%d"); morning = evening = None
    if in_work(now) and p.get("last_morning") != day:
        p["last_morning"] = day
        L = [f"☀️ <b>Доброе утро</b> · {kyiv(now).strftime('%d.%m')}"]
        if p["held"]:
            L.append("За ночь:"); L += [f"• {x}" for x in p["held"]]; p["held"] = []
        else:
            L.append("Ночью сделки не закрывались.")
        rows, used, risk = positions_block(p, st, exclude=set(p.get("_new", [])))
        if rows:
            L.append(f"\nОткрыто у тебя: {len(rows)}" + (f", под риском {usd(risk)}" if used else "")); L += rows
        if p.get("_new"):
            L.append(f"\nНовые сигналы — следующим сообщением.")
        ml = market_line(st)
        if ml:
            L.append("\n" + ml)
        if p["paused"]:
            L.append("⏸ Новые сигналы на паузе (/resume).")
        morning = "\n".join(L)
    elif EVENING[0] <= minutes(now) <= EVENING[1] and p.get("last_evening") != day:
        p["last_evening"] = day
        start = int(kyiv(now).replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)
        today = [t for t in (st.get("trades") or []) if t["exit_ts"] >= start]
        rows, used, risk = positions_block(p, st)
        if today or rows:
            L = [f"🌙 <b>Итоги дня</b> · {kyiv(now).strftime('%d.%m')}"]
            if today:
                L.append(f"Стратегия закрыла сегодня: {len(today)} · " + ", ".join(f"{t['symbol']} {rr(t['r'])}" for t in today if t.get("r") is not None))
            if rows:
                L.append(f"Открыто у тебя: {len(rows)}" + (f", под риском {usd(risk)}" if used else "")); L += rows
            L.append("Ночью сигналы не приходят — следующие после 08:00. Стопы и тейки на бирже работают круглосуточно.")
            evening = "\n".join(L)
    return morning, evening


def main():
    if not TOKEN:
        print("tg: no TELEGRAM_BOT_TOKEN — skipped"); return
    st = json.load(open(STATE))
    p, before = load_priv()
    now = now_ms()
    n_up = handle_updates(p, st)
    msgs = process_events(p, st, now)
    morning, evening = digests(p, st, now) if MODE != "mark" else (None, None)
    queue = ([morning] if morning else []) + msgs + ([evening] if evening else [])
    if p.get("owner"):
        for text in queue:
            send(p["owner"]["chat_id"], text)
    p.pop("_new", None)
    changed = save_priv(p, before)
    print(f"tg: owner={'yes' if p.get('owner') else 'no'} updates={n_up} sent={SENT} queued={len(queue)} held={len(p['held'])} "
          f"taken={len(p['taken'])} state_changed={changed}")


if __name__ == "__main__":
    main()
