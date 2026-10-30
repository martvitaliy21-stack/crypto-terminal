#!/usr/bin/env bash
# Живой цикл: бот и Telegram раз в минуту, сайт (analyze/news) раз в 30 минут.
# Работает ~5ч45м, потом запускает следующий прогон — разрыв между прогонами около минуты.
# GitHub cron не умеет чаще 5 минут и часто опаздывает, поэтому полагаться на него нельзя.
set -u
RUN_FOR=${RUN_FOR:-20700}       # 5ч45м (лимит job — 6ч)
TICK=${TICK:-60}
FULL_EVERY=${FULL_EVERY:-1800}
start=$(date +%s); last_full=0; last_push=$start

git config user.name "grosh-bot"
git config user.email "actions@users.noreply.github.com"

key() {   # что важно отдать сразу: события, позиции, ожидающие сигналы, приватное состояние TG
  python - <<'P'
import json, hashlib, os
st = json.load(open("docs/bot/state.json"))
k = json.dumps([[e["id"] for e in st.get("events", [])], st.get("positions"), st.get("pending"), len(st.get("trades", []))], sort_keys=True)
tg = open("signals/tg.enc", "rb").read() if os.path.exists("signals/tg.enc") else b""
print(hashlib.sha1(k.encode() + tg).hexdigest())
P
}

push() {
  for i in 1 2 3 4; do
    git add docs/data.json docs/bot/state.json docs/news.json signals/
    if git diff --cached --quiet; then return 0; fi
    git commit -qm "update: $(date -u +'%Y-%m-%d %H:%M UTC')"
    git push -q && return 0
    # кто-то запушил параллельно: наша копия свежее (она живёт в цикле) — при конфликте берём свою
    git pull -q --rebase -X theirs && git push -q && return 0
    git rebase --abort 2>/dev/null || true
    sleep 5
  done
  echo "push failed"; return 1
}

prev=$(key)
while :; do
  t0=$(date +%s)
  python bot_sim.py || echo "bot_sim failed"
  python tg_bot.py || echo "telegram step failed"
  force=0
  if (( t0 - last_full >= FULL_EVERY )); then
    python analyze.py || echo "analyze failed, keeping previous data.json"
    python news.py || echo "news failed, keeping previous news.json"
    last_full=$t0; force=1
  fi
  cur=$(key)
  if [[ "$cur" != "$prev" || $force == 1 ]]; then
    push && { prev=$cur; last_push=$t0; }
  fi
  (( $(date +%s) - start >= RUN_FOR )) && break
  sleep $(( TICK - ($(date +%s) - t0) > 5 ? TICK - ($(date +%s) - t0) : 5 ))
done

python analyze.py || true
python news.py || true
push
echo "loop done after $(( $(date +%s) - start ))s — запускаю следующий прогон"
gh workflow run update.yml --ref main || echo "self-dispatch failed (подхватит cron/Mac)"
