# ГРОШ TERMINAL (beta)

Связи и корреляции крипторынка: беты к BTC/ETH, тепловая карта корреляций, регрессионные каналы с
интервалом предсказания, конус дрейф/волатильность, скользящая корреляция и lead/lag. Под каждым
индикатором есть пояснение на человеческом языке.

- `analyze.py` считает всё и пишет `docs/data.json` (Binance, запасной источник CoinGecko).
- `docs/index.html` — сайт, публикуется через GitHub Pages из папки `/docs`.
- `bot_sim.py` — паперный бот на виртуальные 500 $ (та же стратегия, что у основного бота), состояние в `docs/bot/state.json`.
- `.github/workflows/update.yml` каждые 30 минут запускает бота и пересчитывает данные; push с повтором при гонке.
- `build_artifact.py out.html` — одностраничная версия с инлайн-данными.

Локально: `pip install -r requirements.txt && python analyze.py && python -m http.server 8766 --directory docs`

Не финансовая рекомендация.
