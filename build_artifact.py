# -*- coding: utf-8 -*-
"""Собирает одностраничную версию с инлайн-данными (для артефакта claude.ai)."""
import re, sys
src=open("docs/index.html",encoding="utf-8").read()
def inline(name):
    return open(name,encoding="utf-8").read().replace("</","<\\/")
head=re.search(r"<head>(.*?)</head>",src,re.S).group(1)
body=re.search(r"<body>(.*?)</body>",src,re.S).group(1)
head=re.sub(r'<meta charset="utf-8">\s*','',head); head=re.sub(r'<meta name="viewport"[^>]*>\s*','',head)
body=body.replace("/*DATA*/fetch('data.json').then(r=>r.json()).then(d=>{D=d;init();});","D=JSON.parse(document.getElementById('data').textContent);init();")
body=body.replace("/*BOT*/fetch('bot/state.json').then(r=>r.json()).then(b=>{B=b;bot();}).catch(()=>{const e=$('#botStatus');e.textContent='нет данных';e.dataset.level='bad';});","B=JSON.parse(document.getElementById('botdata').textContent);bot();")
body=body.replace("<script>",'<script id="data" type="application/json">'+inline("docs/data.json")+'</script>\n<script id="botdata" type="application/json">'+inline("docs/bot/state.json")+'</script>\n<script>',1)
assert "fetch(" not in body, "fetch left in body"
open(sys.argv[1],"w",encoding="utf-8").write(head.strip()+"\n"+body.strip()+"\n")
print("artifact built")
