# -*- coding: utf-8 -*-
"""Собирает одностраничную версию с инлайн-данными (для артефакта claude.ai)."""
import re, sys
src=open("docs/index.html",encoding="utf-8").read()
def inline(name):
    return open(name,encoding="utf-8").read().replace("</","<\\/")
head=re.search(r"<head>(.*?)</head>",src,re.S).group(1)
body=re.search(r"<body>(.*?)</body>",src,re.S).group(1)
head=re.sub(r'<meta charset="utf-8">\s*','',head); head=re.sub(r'<meta name="viewport"[^>]*>\s*','',head)
body=re.sub(r"/\*DATA\*/fetch\('data\.json[^\n]*", "window.SNAPSHOT=true;D=JSON.parse(document.getElementById('data').textContent);init();splashStep('data',true);", body, count=1)
body=re.sub(r"/\*WORLD\*/fetch\('world\.json[^\n]*", "WORLD=JSON.parse(document.getElementById('worlddata').textContent);splashStep('world',true);worldReady();", body, count=1)
body=re.sub(r"/\*NEWS\*/fetch\('news\.json[^\n]*", "N=JSON.parse(document.getElementById('newsdata').textContent);splashStep('news',true);newsReady();", body, count=1)
import re as _re
body=_re.sub(r"/\*BOT\*/fetch\('bot/state\.json[^\n]*", "B=JSON.parse(document.getElementById('botdata').textContent);bot();splashStep('bot',true);", body, count=1)
body=body.replace("<script>",'<script id="data" type="application/json">'+inline("docs/data.json")+'</script>\n<script id="botdata" type="application/json">'+inline("docs/bot/state.json")+'</script>\n<script id="worlddata" type="application/json">'+inline("docs/world.json")+'</script>\n<script id="newsdata" type="application/json">'+inline("docs/news.json")+'</script>\n<script>',1)
assert not re.search(r"/\*(DATA|BOT|WORLD|NEWS)\*/fetch", body), "data fetch marker left in body"
open(sys.argv[1],"w",encoding="utf-8").write(head.strip()+"\n"+body.strip()+"\n")
print("artifact built")
