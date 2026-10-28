# ГРОШ TERMINAL (beta)

Связи и корреляции крипторынка: беты к BTC/ETH, тепловая карта корреляций, регрессионные каналы с
интервалом предсказания, конус дрейф/волатильность, скользящая корреляция и lead/lag. Под каждым
индикатором есть пояснение на человеческом языке.

- `analyze.py` считает всё и пишет `docs/data.json` (Binance, запасной источник CoinGecko).
- `docs/index.html` — сайт, публикуется через GitHub Pages из папки `/docs`.
- `bot_sim.py` — паперный бот на виртуальные 500 $, правила v3 (с 28.10.2026): вход на закрытии 4h + фильтр «EMA50 5д ≥ −1.5%», ≤2 сигнала за свечу; стоп 3 ATR, тейк 4R (OCO); риск 1%, под риском ≤2% (≤2 сделок); сигналы 08:00–23:30 Киев, ночные перепроверяются утром; выходы по часовым свечам. Пишет события в `docs/bot/state.json → events`.
- `tg_bot.py` — Telegram-бот сигналов @martallen_bot: шлёт владельцу сигналы, закрытия, утреннюю сводку и итог дня; команды /status /deposit /risk /pause /resume /help. Секреты: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_OWNER_CODE` (подключение по ссылке t.me/martallen_bot?start=<код>). Приватные данные — `signals/tg.enc` (Fernet, ключ из токена). Тест без отправки: `TG_MODE=dry`.
- `.github/workflows/update.yml` каждые 30 минут запускает бота и пересчитывает данные; push с повтором при гонке.
- `news.py` — новости для раздела «Мир»: 41 лента (BBC, CNBC, CoinDesk, Cointelegraph, WSJ, MarketWatch, FT, Економічна правда, Інтерфакс-Україна, AIN, Google News по 20 странам) → `docs/news.json`, 26 хабов, категории.
- `worldmap.py` — точечная карта суши из Natural Earth 1:110m → `docs/world.json` (запускается один раз).
- `build_artifact.py out.html` — одностраничная версия с инлайн-данными.

Локально: `pip install -r requirements.txt && python analyze.py && python -m http.server 8766 --directory docs`

Не финансовая рекомендация.
