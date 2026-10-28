# -*- coding: utf-8 -*-
"""Сборщик новостей для раздела «Мир»: docs/news.json.
Источники: мировые ленты (BBC, CNBC, CoinDesk, Cointelegraph, WSJ, MarketWatch, FT) + Google News по странам
в местных языках + украинские издания напрямую. Каждая новость привязывается к хабу на карте и категории.
Сбой одной ленты не мешает остальным; при сбое старые новости хаба сохраняются (до 72 часов).
"""
import html
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone
import xml.etree.ElementTree as ET
import requests

ROOT = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(ROOT, "docs", "news.json")
UA = {"User-Agent": "Mozilla/5.0 (compatible; GroshTerminal/1.0; +https://martvitaliy21-stack.github.io/crypto-terminal/)"}
KEEP_H = 72
PER_HUB, WORLD_POOL = 24, 80


def gnews(hl, gl, ceid):
    return f"https://news.google.com/rss/headlines/section/topic/BUSINESS?hl={hl}&gl={gl}&ceid={ceid}"


def gsearch(q, hl="en-US", gl="US", ceid="US:en"):
    return f"https://news.google.com/rss/search?q={requests.utils.quote(q)}&hl={hl}&gl={gl}&ceid={ceid}"


# id → (ключевые слова для мировых лент, локальные ленты)
HUBS = {
    "nyc":      (r"wall street|nasdaq|s&p|dow jones|nyse|u\.?s\.? stocks?|goldman|jpmorgan|morgan stanley|blackrock|nvidia|apple|microsoft|tesla|amazon|alphabet|meta\b|openai|us markets?",
                 [("Google News · США", gnews("en-US", "US", "US:en"))]),
    "dc":       (r"\bfed\b|federal reserve|powell|\bsec\b|white house|trump|congress|tariff|treasury|cftc|senate|u\.?s\.? economy|us economy|washington",
                 [("Google News · Вашингтон", gsearch("Federal Reserve OR SEC OR tariffs OR \"White House\" economy"))]),
    "toronto":  (r"canada|canadian|toronto|bank of canada|ottawa", [("Google News · Канада", gnews("en-CA", "CA", "CA:en"))]),
    "mexico":   (r"mexic|peso", [("Google News · México", gnews("es-419", "MX", "MX:es-419"))]),
    "saopaulo": (r"brazil|brasil|petrobras|\breal\b", [("Google News · Brasil", gnews("pt-BR", "BR", "BR:pt-419"))]),
    "buenosaires": (r"argentin|milei", [("Google News · Argentina", gnews("es-419", "AR", "AR:es-419"))]),
    "london":   (r"\buk\b|britain|british|bank of england|ftse|sterling|pound|london|hsbc|barclays|rishi|starmer|reeves",
                 [("Google News · UK", gnews("en-GB", "GB", "GB:en"))]),
    "brussels": (r"\beu\b|european union|european commission|brussels|eurozone|von der leyen|sanctions",
                 [("Google News · EU", gsearch("\"European Union\" OR \"European Commission\" OR eurozone economy"))]),
    "frankfurt":(r"germany|german|\becb\b|lagarde|euro\b|dax|deutsche|volkswagen|siemens|bundesbank|berlin",
                 [("Google News · Deutschland", gnews("de", "DE", "DE:de"))]),
    "paris":    (r"france|french|paris|macron|lvmh|cac", [("Google News · France", gnews("fr", "FR", "FR:fr"))]),
    "zurich":   (r"swiss|switzerland|zurich|\bubs\b|nestl|franc\b", [("Google News · Schweiz", gnews("de", "CH", "CH:de"))]),
    "warsaw":   (r"poland|polish|warsaw|zloty|złoty", [("Google News · Polska", gnews("pl", "PL", "PL:pl"))]),
    "kyiv":     (r"ukrain|kyiv|kiev|zelensk|\bnbu\b|hryvnia|укра[їи]н|ки[їе]в|нбу|гривн",
                 [("Google News · Україна", gnews("uk", "UA", "UA:uk")), ("Економічна правда", "https://www.epravda.com.ua/rss/"), ("Google News · Економічна правда", gsearch("site:epravda.com.ua", "uk", "UA", "UA:uk")),
                  ("Інтерфакс-Україна", "https://interfax.com.ua/news/economic.rss"), ("AIN", "https://ain.ua/feed/"),
                  ("Google News · крипто UA", gsearch("криптовалюта OR біткоїн OR блокчейн", "uk", "UA", "UA:uk"))]),
    "istanbul": (r"turk|istanbul|erdogan|lira", [("Google News · Türkiye", gnews("tr", "TR", "TR:tr"))]),
    "dubai":    (r"dubai|\buae\b|emirates|abu dhabi|binance|bybit|okx|tether",
                 [("Google News · Dubai", gsearch("Dubai OR UAE economy OR \"Abu Dhabi\" OR Binance"))]),
    "riyadh":   (r"saudi|aramco|opec|riyadh|gulf|hormuz|brent|crude|oil prices?|oil\b",
                 [("Google News · Gulf", gsearch("Saudi Arabia economy OR Aramco OR OPEC oil"))]),
    "mumbai":   (r"india|indian|mumbai|rupee|sensex|nifty|modi|reliance|adani|\brbi\b", [("Google News · India", gnews("en-IN", "IN", "IN:en"))]),
    "singapore":(r"singapore|asean|malaysia|indonesia|thailand|vietnam|philippin", [("Google News · Singapore", gnews("en-SG", "SG", "SG:en"))]),
    "hongkong": (r"hong kong|hang seng|hsbc", [("Google News · Hong Kong", gnews("en-HK", "HK", "HK:en"))]),
    "shanghai": (r"shanghai|shenzhen|alibaba|tencent|byd\b|csi 300", [("Google News · 中国", gnews("zh-CN", "CN", "CN:zh-Hans"))]),
    "beijing":  (r"china|chinese|beijing|yuan|renminbi|pboc|xi jinping|deepseek|huawei",
                 [("Google News · China", gsearch("China economy OR PBOC OR yuan OR Beijing trade"))]),
    "tokyo":    (r"japan|japanese|tokyo|yen\b|nikkei|\bboj\b|toyota|softbank|sony", [("Google News · 日本", gnews("ja", "JP", "JP:ja"))]),
    "seoul":    (r"korea|korean|seoul|samsung|hyundai|\bwon\b|kospi", [("Google News · 한국", gnews("ko", "KR", "KR:ko"))]),
    "sydney":   (r"australia|aussie|sydney|\brba\b|\basx\b|bhp|rio tinto", [("Google News · Australia", gnews("en-AU", "AU", "AU:en"))]),
    "johannesburg": (r"south africa|johannesburg|rand\b|\bsarb\b", [("Google News · South Africa", gnews("en-ZA", "ZA", "ZA:en"))]),
    "lagos":    (r"nigeria|lagos|naira|africa", [("Google News · Nigeria", gnews("en-NG", "NG", "NG:en"))]),
}
GLOBAL = [
    ("BBC Business", "https://feeds.bbci.co.uk/news/business/rss.xml"),
    ("BBC Technology", "https://feeds.bbci.co.uk/news/technology/rss.xml"),
    ("CNBC Economy", "https://www.cnbc.com/id/20910258/device/rss/rss.html"),
    ("CNBC Finance", "https://www.cnbc.com/id/10000664/device/rss/rss.html"),
    ("CNBC", "https://www.cnbc.com/id/100003114/device/rss/rss.html"),
    ("CoinDesk", "https://www.coindesk.com/arc/outboundfeeds/rss/"),
    ("Cointelegraph", "https://cointelegraph.com/rss"),
    ("MarketWatch", "https://feeds.content.dowjones.io/public/rss/mw_topstories"),
    ("WSJ Markets", "https://feeds.a.dj.com/rss/RSSMarketsMain.xml"),
    ("Financial Times", "https://www.ft.com/rss/home"),
    ("Google News · crypto", gsearch("bitcoin OR ethereum OR crypto OR stablecoin")),
]
CATS = [  # порядок = приоритет
    ("crypto", r"bitcoin|\bbtc\b|crypto|ethereum|\beth\b|blockchain|stablecoin|\btoken|binance|coinbase|defi|\bnft|solana|\bxrp\b|altcoin|биткоин|біткоїн|крипт|блокчейн|стейблко|仮想通貨|暗号資産|비트코인|암호화폐|加密|比特币|krypto|cripto|kripto|tether|\bsec\b.*(coin|crypto)"),
    ("commodities", r"\boil\b|crude|opec|\bgold\b|natural gas|\blng\b|copper|wheat|brent|нафт|нефт|\bгаз|золот|原油|石油|黄金|金価格|petróleo|petrol|\böl\b|erdgas|ropa|altın"),
    ("stocks", r"stocks?\b|shares?\b|equit|nasdaq|s&p|dow\b|ftse|\bdax\b|nikkei|hang seng|earnings|\bipo\b|wall street|rally|sell-?off|акци|акці|бирж|бірж|індекс|индекс|株|증시|股|aktien|börse|bourse|ações|bolsa|borsa|giełd|dividend"),
    ("macro", r"inflation|interest rate|\brates?\b|\bfed\b|\becb\b|central bank|\bgdp\b|recession|economy|jobs report|unemployment|\bcpi\b|tariff|trade deficit|інфляц|инфляц|ставк|нбу|\bввп\b|економік|экономик|курс|景気|金利|경제|金融|通胀|经济|zinsen|konjunktur|inflación|economía|gospodark|ekonomi|enflasyon|budget|debt"),
    ("tech", r"\bai\b|artificial intelligence|\bchips?\b|semiconductor|nvidia|openai|\btech\b|technology|startup|стартап|\bші\b|\bии\b|技术|半導体|\bki\b|software|robot"),
]
CAT_RX = [(c, re.compile(rx, re.I)) for c, rx in CATS]
HUB_RX = [(h, re.compile(v[0], re.I)) for h, v in HUBS.items()]


def fetch(url):
    r = requests.get(url, headers=UA, timeout=20)
    r.raise_for_status()
    return r.content


def parse(xml_bytes, source, hub, lang):
    """RSS 2.0 / Atom → список новостей."""
    root = ET.fromstring(xml_bytes)
    items = []
    ns = {"a": "http://www.w3.org/2005/Atom", "dc": "http://purl.org/dc/elements/1.1/"}
    nodes = root.findall(".//item") or root.findall(".//a:entry", ns)
    for it in nodes:
        title = (it.findtext("title") or it.findtext("a:title", namespaces=ns) or "").strip()
        link = (it.findtext("link") or "").strip()
        if not link:
            ln = it.find("a:link", ns)
            link = (ln.get("href") if ln is not None else "") or ""
        date = it.findtext("pubDate") or it.findtext("dc:date", namespaces=ns) or it.findtext("a:published", namespaces=ns) or it.findtext("a:updated", namespaces=ns) or ""
        src = source
        s_el = it.find("source")
        if s_el is not None and (s_el.text or "").strip():
            src = s_el.text.strip()
            title = re.sub(r"\s+-\s+" + re.escape(src) + r"\s*$", "", title)
        title = html.unescape(re.sub(r"<[^>]+>", "", title)).strip()
        if not title or not link:
            continue
        try:
            dt = parsedate_to_datetime(date) if "," in date or date[:3].isalpha() else datetime.fromisoformat(date.replace("Z", "+00:00"))
            ts = int(dt.timestamp() * 1000)
        except Exception:
            ts = int(time.time() * 1000)
        items.append(dict(t=title[:220], u=link, s=src[:40], p=ts, h=hub, l=lang))
    return items


def classify(item):
    txt = item["t"]
    item["c"] = next((c for c, rx in CAT_RX if rx.search(txt)), "economy")
    if item["h"] is None:
        item["h"] = next((h for h, rx in HUB_RX if rx.search(txt)), None)
    return item


def norm(t):
    return re.sub(r"[^\w]+", "", t.lower())[:80]


def main():
    jobs = [(name, url, None, "en") for name, url in GLOBAL]
    for hub, (_, feeds) in HUBS.items():
        for name, url in feeds:
            lang = "uk" if hub == "kyiv" else "local"
            jobs.append((name, url, hub, lang))

    def work(job):
        name, url, hub, lang = job
        try:
            return name, parse(fetch(url), name, hub, lang), None
        except Exception as e:
            return name, [], f"{name}: {str(e)[:80]}"

    with ThreadPoolExecutor(10) as ex:
        results = list(ex.map(work, jobs))
    now = int(time.time() * 1000)
    fresh, errors, ok_sources = [], [], 0
    for name, items, err in results:
        if err:
            errors.append(err); print("fail", err)
        else:
            ok_sources += 1; fresh += items
    old = []
    if os.path.exists(OUT):
        try:
            old = json.load(open(OUT)).get("items", [])
        except Exception:
            old = []
    seen, merged = set(), []
    for it in sorted(fresh + old, key=lambda x: -x["p"]):
        if now - it["p"] > KEEP_H * 3600_000 or it["p"] > now + 3600_000:
            continue
        k = norm(it["t"])
        if k in seen:
            continue
        seen.add(k); merged.append(classify(it))
    # лимиты: на хаб и на мировой пул
    per, out = {}, []
    for it in merged:
        key = it["h"] or "_world"
        cap = WORLD_POOL if key == "_world" else PER_HUB
        if per.get(key, 0) >= cap:
            continue
        per[key] = per.get(key, 0) + 1; out.append(it)
    counts = {h: sum(1 for it in out if it["h"] == h and now - it["p"] < 86400_000) for h in HUBS}
    data = dict(generated=now, sources_ok=ok_sources, sources_total=len(jobs), errors=errors[:10], counts=counts, items=out)
    tmp = OUT + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, OUT)
    print(f"sources {ok_sources}/{len(jobs)}, items {len(out)}, {os.path.getsize(OUT)//1024} KB; by hub: " +
          ", ".join(f"{h}:{n}" for h, n in sorted(counts.items(), key=lambda x: -x[1]) if n))


if __name__ == "__main__":
    main()
