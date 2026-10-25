"""Jarvis Feeds: agenda, e-mails, notícias, cotações e Morning Digest (somente leitura).

Parte pura (parse/normalização/contexto): testável com fixtures, sem rede (ver feeds_selftest).
Parte de rede: Gmail + Google Agenda pelos conectores do claude.ai (via claude -p, só ferramentas de leitura),
Google News RSS, Yahoo Finance. O Outlook do IBMEC chega pelo encaminhamento para o Gmail.
"""
import datetime
import email.utils
import hashlib
import html
import json
import os
import re
import threading
import time
import unicodedata
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from urllib.parse import quote, urlencode

import jarvis_config as conf

# ===================== CONFIGURAÇÃO (ajuste aqui) =====================
_CFG = conf.get()  # preferências pessoais: config.json (fora do git) ou os padrões genéricos
NEWS_TOPICS = _CFG["news_topics"]  # [chave, rótulo, consulta do Google News]
TICKERS = _CFG["tickers"]          # [símbolo do Yahoo Finance, nome exibido]
AGENDA_EMAIL_MIN = 30      # a cada quantos minutos atualiza agenda + e-mails (cada busca pelos conectores leva 1 a 4 min)
NEWS_MARKET_MIN = 30       # a cada quantos minutos atualiza notícias + cotações
AGENDA_DAYS = 31           # eventos de ontem até daqui a N dias
EMAIL_DAYS = 3             # e-mails dos últimos N dias
EMAIL_MAX = 25             # e-mails por busca (mais = busca mais lenta)
NEWS_MAX = 10              # manchetes por tópico
DIGEST_MAX_AGE_H = 3       # digest em cache mais velho que isso é refeito sob demanda
MAX_CONTEXT_CHARS = 4800   # ~1200 tokens: limite do contexto do painel enviado ao Claude
FORWARDED_DOMAIN = _CFG["forwarded_domain"]  # e-mails ao Gmail cujo destinatário contém isto (ex.: o da faculdade, encaminhado) viram a conta "microsoft"; vazio = desligado

# Conectores do claude.ai: só estas ferramentas de LEITURA são liberadas; as de escrita ficam bloqueadas.
# No claude -p as ferramentas dos conectores vêm "adiadas": o ToolSearch (só carrega esquemas) precisa ser liberado.
CONNECTOR_READ = ["ToolSearch", "mcp__claude_ai_Google_Calendar__list_events", "mcp__claude_ai_Gmail__search_threads"]
CONNECTOR_DENY = [
    "mcp__claude_ai_Gmail__" + t for t in (
        "send_message", "reply", "forward", "create_draft", "update_draft", "delete_draft", "trash_message", "trash_thread",
        "untrash_message", "untrash_thread", "label_message", "label_thread", "unlabel_message", "unlabel_thread",
        "update_message_labels", "create_label", "update_label", "delete_label", "mark_message_spam", "mark_thread_spam",
        "unmark_message_spam", "unmark_thread_spam", "apply_sensitive_message_label", "apply_sensitive_thread_label")
] + ["mcp__claude_ai_Google_Calendar__" + t for t in ("create_event", "update_event", "delete_event", "respond_to_event")]

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE_FILE = os.path.join(HERE, "feeds_cache.json")

DIGEST_TAG = "[[DIGEST]]"
DIGEST_OFFER = "Good morning, sir. Shall I read your morning digest?"
DIGEST_RULE = (
    "If sir asks for the morning digest, or accepts your offer to read it, reply with exactly " + DIGEST_TAG +
    " and nothing else."
)
FORWARDED = "via Gmail (encaminhamento)"
UA = {"User-Agent": "Mozilla/5.0"}

FETCH_SYSTEM = (
    "You extract data for a read-only dashboard. Never create, send, reply, forward, delete, label or modify anything.\n"
    "Your connector tools are deferred. FIRST call ToolSearch with query "
    "\"select:mcp__claude_ai_Google_Calendar__list_events,mcp__claude_ai_Gmail__search_threads\" to load them, then use them.\n"
    "1) Google Calendar: list_events on the primary calendar from {start} to {end}.\n"
    "2) Gmail: ONE search_threads call with query \"in:inbox newer_than:{days}d\" and at most {max} results. Do not open threads.\n"
    "Reply with ONLY one JSON object, no prose, no code fences:\n"
    '{{"events":[{{"id":"","title":"","start":"ISO8601","end":"ISO8601","all_day":false,"location":"","link":""}}],'
    '"emails":[{{"id":"","from":"","to":"","subject":"","snippet":"","date":"ISO8601","unread":true,"link":""}}]}}\n'
    "Use the message id (not the thread id) when available. Snippets at most 160 characters."
)

TRIAGE_SYSTEM = (
    "You triage e-mails for a university student. Reply with STRICT JSON only, no prose and no code fences: "
    '[{"id": "...", "cat": "acao"|"info"|"ruido"}] with one entry per input e-mail, using the same ids. '
    "acao = needs his reply or action, or has a deadline; info = useful to know; "
    "ruido = promotions, newsletters, automatic notifications."
)
DIGEST_SYSTEM = (
    "You are JARVIS, a refined British butler. Write sir's morning digest in British English, 120 to 180 words, "
    "in a spoken style (at most 180 words): no markdown, no lists, no emojis, address him as 'sir'. Cover: today's agenda, "
    "e-mails needing action, two or three headlines per news topic, market moves (Ibovespa and any notable portfolio "
    "moves) and relevant Second Brain items (deadlines, goals). End with ONE suggested priority for the day. "
    "Never claim the agenda is clear or that there are no e-mails for a source marked NOT CONNECTED; instead mention briefly, once, "
    "that those accounts are not yet connected. Say 'nothing scheduled' or 'no e-mails' only for sources that are connected. "
    "At most 180 words. Portuguese names and titles may be kept as they are. Output only the digest text."
)


def log(*a):
    print(time.strftime("%H:%M:%S"), "[feeds]", *a, flush=True)


def short_err(e):
    return "erro: " + (type(e).__name__ + ": " + " ".join(str(e).split()))[:90]


# ===================== PARTE PURA: datas e normalização =====================
def parse_dt(s):
    """ISO (com Z, fração longa do Graph, só data...) -> datetime."""
    s = re.sub(r"(\.\d{6})\d+", r"\1", str(s).replace("Z", "+00:00"))
    return datetime.datetime.fromisoformat(s)


def iso(dt):
    """datetime/str -> ISO local sem fuso (o JS interpreta como hora local; ordena como texto)."""
    if isinstance(dt, str):
        dt = parse_dt(dt)
    if dt.tzinfo:
        dt = dt.astimezone().replace(tzinfo=None)
    return dt.isoformat(timespec="seconds")


def sender(raw):
    """'Fulano <f@x.com>' -> 'Fulano' (ou o endereço)."""
    name, addr = email.utils.parseaddr(raw or "")
    return name or addr or "?"


def norm(s):
    return "".join(c for c in unicodedata.normalize("NFD", str(s).lower()) if not unicodedata.combining(c))


def safe_iso(s):
    if not s:
        return ""
    try:
        return iso(s)
    except (TypeError, ValueError):
        return ""


def norm_event(ev):
    """Evento devolvido pelo conector do Google Agenda -> contrato."""
    start = safe_iso(ev.get("start"))
    return {"id": str(ev.get("id") or start + str(ev.get("title"))), "account": "google",
            "title": str(ev.get("title") or "(sem título)"), "start": start, "end": safe_iso(ev.get("end")) or start,
            "all_day": bool(ev.get("all_day")) or len(str(ev.get("start", ""))) == 10,
            "location": str(ev.get("location") or ""), "link": str(ev.get("link") or "")}


def norm_email(m):
    """E-mail devolvido pelo conector do Gmail -> contrato. Encaminhado do IBMEC vira conta "microsoft" (bolinha do Outlook)."""
    date = safe_iso(m.get("date"))
    mid = str(m.get("id") or "h" + hashlib.md5((str(m.get("subject")) + date).encode()).hexdigest()[:12])
    ibmec = bool(FORWARDED_DOMAIN) and FORWARDED_DOMAIN.lower() in str(m.get("to", "")).lower()
    return {"id": mid, "account": "microsoft" if ibmec else "google", "from": sender(m.get("from")),
            "subject": str(m.get("subject") or "(sem assunto)"), "snippet": html.unescape(str(m.get("snippet") or ""))[:200],
            "date": date, "unread": bool(m.get("unread")), "cat": None, "link": str(m.get("link") or "")}


def parse_connectors(text):
    """Resposta do claude -p -> (eventos, e-mails); None se não houver o JSON esperado."""
    m = re.search(r"\{.*\}", text or "", re.S)
    try:
        data = json.loads(m.group(0)) if m else None
    except ValueError:
        return None
    if not isinstance(data, dict) or not ("events" in data or "emails" in data):
        return None
    evs = [norm_event(e) for e in data.get("events") or [] if isinstance(e, dict)]
    mails = [norm_email(e) for e in data.get("emails") or [] if isinstance(e, dict)]
    return [e for e in evs if e["start"]], mails


# ===================== PARTE PURA: notícias e cotações =====================
def parse_rss(xml_text, limit=NEWS_MAX):
    """Google News RSS -> [{title, source, url, published}] (sufixo ' - Fonte' removido do título)."""
    out = []
    for it in ET.fromstring(xml_text).findall("./channel/item")[:limit]:
        title, src = (it.findtext("title") or "").strip(), (it.findtext("source") or "").strip()
        if src and title.endswith(" - " + src):
            title = title[:-len(src) - 3]
        elif not src and " - " in title:
            title, src = title.rsplit(" - ", 1)
        try:
            pub = iso(email.utils.parsedate_to_datetime(it.findtext("pubDate")))
        except (TypeError, ValueError):
            pub = ""
        out.append({"title": title, "source": src, "url": (it.findtext("link") or "").strip(), "published": pub})
    return out


def parse_yahoo(data, symbol, name):
    """Resposta do chart do Yahoo -> cotação. Qualquer falha/ausência -> price None ('sem cotação')."""
    q = {"symbol": symbol, "name": name, "price": None, "change_pct": None, "closes": []}
    try:
        res = data["chart"]["result"][0]
        price = res["meta"]["regularMarketPrice"]
        closes = [round(c, 2) for c in res["indicators"]["quote"][0]["close"] if c is not None][-5:]
        # Variação do dia: contra o fechamento anterior da série (o chartPreviousClose com range=5d é de 5 pregões atrás).
        prev = closes[-2] if len(closes) >= 2 else res["meta"].get("chartPreviousClose")
        q["price"], q["closes"] = round(float(price), 2), closes
        if prev:
            q["change_pct"] = round((price / prev - 1) * 100, 2)
    except (KeyError, IndexError, TypeError, ValueError):
        pass
    return q


# ===================== PARTE PURA: triagem, painel, contexto =====================
def parse_triage(text):
    """Extrai o primeiro array JSON válido de uma resposta bagunçada -> {id: cat}; cat desconhecida vira 'info'."""
    dec = json.JSONDecoder()
    for m in re.finditer(r"\[", text or ""):
        try:
            arr, _ = dec.raw_decode(text[m.start():])
        except ValueError:
            continue
        if isinstance(arr, list):
            out = {}
            for x in arr:
                if isinstance(x, dict) and x.get("id"):
                    c = norm(x.get("cat", ""))
                    out[str(x["id"])] = c if c in ("acao", "info", "ruido") else "info"
            return out
    return {}


def build_dash(cache):
    """Cache -> DASH (contrato com a página)."""
    triage = cache.get("triage", {})
    events, emails = [], []
    for acc in ("google", "microsoft"):
        d = cache.get("data", {}).get(acc, {})
        events += d.get("events", [])
        emails += [dict(e, cat=triage.get(e["id"], {}).get("cat")) for e in d.get("emails", [])]
    default = {"google": "não configurado", "microsoft": FORWARDED, "news": "não configurado", "market": "não configurado"}
    return {"updated": cache.get("updated"), "status": dict(default, **cache.get("status", {})),
            "events": sorted(events, key=lambda e: e["start"]), "emails": sorted(emails, key=lambda e: e["date"], reverse=True),
            "news": cache.get("news", []), "quotes": cache.get("quotes", []), "digest": cache.get("digest")}


def fmt_quote(q):
    if q["price"] is None:
        return f"{q['name']}: sem cotação"
    ch = "" if q["change_pct"] is None else f" ({q['change_pct']:+.2f}%)"
    return f"{q['name']} {q['price']:,.2f}{ch}"


def dashboard_context(cache, now=None):
    """Texto compacto (limitado) do painel para o Claude: agenda hoje/amanhã, e-mails de ação, manchetes, cotações."""
    now = now or datetime.datetime.now()
    d = build_dash(cache)
    today, tomorrow = now.date().isoformat(), (now.date() + datetime.timedelta(days=1)).isoformat()
    L = ["LIVE DASHBOARD (sir's agenda, e-mail, news and portfolio; use it when asked about them):"]
    # "Sem dados" não é "agenda vazia": só afirma "nothing"/"none" se a busca nos conectores deu certo.
    # O Outlook do IBMEC chega encaminhado para o Gmail, então tudo depende do status "google".
    if d["status"]["google"] != "conectado":
        L.append(f"Agenda and e-mail (Google Calendar + Gmail, which also receives the IBMEC Outlook mail): NOT CONNECTED - no data "
                 f"(status: {d['status']['google']})")
    else:
        for label, day in (("Today", today), ("Tomorrow", tomorrow)):
            evs = [e for e in d["events"] if e["start"][:10] == day]
            L.append(f"Agenda {label}: " + ("; ".join(("all day" if e["all_day"] else e["start"][11:16]) + " " + e["title"] for e in evs) or "nothing"))
        acao = [e for e in d["emails"] if e["cat"] == "acao"]
        L.append("E-mails needing action: " + ("; ".join(("[IBMEC] " if e["account"] == "microsoft" else "") + f"{e['from']}: {e['subject']}"
                                                       for e in acao[:8]) or "none"))
    for t in d["news"]:
        L.append(f"Headlines {t['label']}: " + " | ".join(f"{i['title']} ({i['source']})" for i in t["items"][:5]))
    if d["quotes"]:
        L.append("Quotes: " + "; ".join(fmt_quote(q) for q in d["quotes"]))
    return "\n".join(L)[:MAX_CONTEXT_CHARS]


def is_digest_reply(raw):
    return DIGEST_TAG in (raw or "")


# ===================== REDE: notícias e cotações =====================
def http_get(url, headers=None, timeout=15):
    req = urllib.request.Request(url, headers=dict(UA, **(headers or {})))
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def fetch_news(query):
    url = "https://news.google.com/rss/search?" + urlencode({"q": query + " when:1d", "hl": "pt-BR", "gl": "BR", "ceid": "BR:pt-419"})
    return parse_rss(http_get(url))


def fetch_quote(symbol, name):
    try:
        data = json.loads(http_get(f"https://query1.finance.yahoo.com/v8/finance/chart/{quote(symbol)}?range=5d&interval=1d"))
    except (urllib.error.URLError, ValueError, OSError):  # 404 = símbolo desconhecido
        data = {}
    return parse_yahoo(data, symbol, name)


# ===================== ORQUESTRAÇÃO =====================
def load_cache(path=CACHE_FILE):
    try:
        with open(path, encoding="utf-8") as f:
            c = json.load(f)
        if isinstance(c, dict):
            return c
    except (OSError, ValueError):
        pass
    return {}


class Feeds:
    """Guarda o cache, busca cada fonte de forma independente e gera triagem/digest. ask(system, texto, modelo) -> str."""

    def __init__(self, ask, notes_fn=lambda: [], cache_file=CACHE_FILE, on_update=lambda: None, notify=lambda info: None):
        self.ask, self.notes_fn, self.file = ask, notes_fn, cache_file
        self.on_update, self.notify = on_update, notify
        self.lock = threading.Lock()
        self.cache = load_cache(cache_file)

    # ---------- cache ----------
    def update(self, **kv):
        """Altera chaves do cache e grava (atômico) sob o lock."""
        with self.lock:
            self.cache.update(kv)
            tmp = self.file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.cache, f, ensure_ascii=False)
            os.replace(tmp, self.file)

    def set_status(self, key, value):
        self.update(status=dict(self.cache.get("status", {}), **{key: value}))

    def dash(self):
        with self.lock:
            return build_dash(self.cache)

    def context(self):
        with self.lock:
            return dashboard_context(self.cache)

    # ---------- atualizações ----------
    def refresh_agenda_email(self):
        """Gmail + Google Agenda numa chamada ao claude -p com os conectores (1 a 4 min, em segundo plano)."""
        if self.cache.get("status", {}).get("google") != "conectado":
            self.set_status("google", "buscando…")
            self.on_update()
        now = datetime.datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
        system = FETCH_SYSTEM.format(start=(now - datetime.timedelta(days=1)).isoformat(), end=(now + datetime.timedelta(days=AGENDA_DAYS)).isoformat(),
                                     days=EMAIL_DAYS, max=EMAIL_MAX)
        try:
            for tentativa in (1, 2):  # o modelo às vezes não carrega as ferramentas: uma nova tentativa resolve
                try:
                    r = parse_connectors(self.ask(system, "Extract now.", "haiku", tools=CONNECTOR_READ, deny=CONNECTOR_DENY))
                    if r is None:
                        raise ValueError("resposta dos conectores sem o JSON esperado")
                    break
                except Exception as e:
                    if tentativa == 2:
                        raise
                    log("conectores, nova tentativa:", short_err(e))
            self.update(data={"google": {"events": r[0], "emails": r[1]}})
            self.set_status("google", "conectado")
            self.set_status("microsoft", FORWARDED)  # substitui status antigo do Graph que ficou no cache
        except Exception as e:  # mantém os dados antigos
            log("conectores", short_err(e))
            self.set_status("google", short_err(e))
        self.update(updated=iso(datetime.datetime.now()))
        self.on_update()
        self.triage()

    def refresh_news_market(self):
        news = []
        try:
            news = [{"key": k, "label": lab, "items": fetch_news(q)} for k, lab, q in NEWS_TOPICS]
            self.update(news=news)
            self.set_status("news", "conectado")
        except Exception as e:
            log("news", short_err(e))
            self.set_status("news", short_err(e))
        try:
            quotes = [fetch_quote(s, n) for s, n in TICKERS]
            ok = any(q["price"] is not None for q in quotes)
            self.update(quotes=quotes)
            self.set_status("market", "conectado" if ok else "erro: sem cotações")
        except Exception as e:
            log("market", short_err(e))
            self.set_status("market", short_err(e))
        self.update(updated=iso(datetime.datetime.now()))
        self.on_update()

    def triage(self):
        """UMA chamada ao Claude para os e-mails ainda sem categoria (até 40 por rodada)."""
        done = self.cache.get("triage", {})
        todo = [e for e in build_dash(self.cache)["emails"] if e["id"] not in done][:40]
        if not todo:
            return
        try:
            payload = json.dumps([{"id": e["id"], "from": e["from"], "subject": e["subject"], "snippet": e["snippet"][:200]} for e in todo],
                                 ensure_ascii=False)
            cats = parse_triage(self.ask(TRIAGE_SYSTEM, payload, "haiku"))
        except Exception as e:
            log("triagem", short_err(e))
            return
        if not cats:  # resposta ilegível: tenta de novo na próxima rodada
            return
        now, old = time.time(), time.time() - 30 * 86400
        tri = {k: v for k, v in done.items() if v.get("ts", 0) > old}
        tri.update({e["id"]: {"cat": cats.get(e["id"], "info"), "ts": now} for e in todo})
        self.update(triage=tri)
        self.on_update()

    # ---------- digest ----------
    def digest(self, force=False):
        """Digest do dia (do cache se tem menos de DIGEST_MAX_AGE_H h). Levanta exceção se o Claude falhar."""
        d, now = self.cache.get("digest"), datetime.datetime.now()
        google_ok = self.cache.get("status", {}).get("google") == "conectado"
        # um digest feito SEM o Google (ainda carregando ou com erro) não vale depois que o Google conectou
        stale = google_ok and d and not d.get("google_ok")
        if d and not force and not stale and d["date"] == now.date().isoformat() and \
                (now - datetime.datetime.fromisoformat(d["generated_at"])).total_seconds() < DIGEST_MAX_AGE_H * 3600:
            return d
        brain = "\n".join(f"- {n['title']}: {n['body'][:220]}" for n in self.notes_fn() if n.get("area") in ("metas", "projetos", "trabalho"))
        text = self.ask(DIGEST_SYSTEM, self.context() + "\n\nSECOND BRAIN (Portuguese):\n" + brain + f"\n\nNow: {now:%A, %d %B %Y, %H:%M}.", "sonnet").strip()
        d = {"date": now.date().isoformat(), "text": text, "generated_at": iso(now), "google_ok": google_ok}
        self.update(digest=d)
        return d

    def digest_offer_due(self, today=None):
        """True só na primeira chamada de cada dia (e marca o dia)."""
        today = today or datetime.date.today().isoformat()
        if self.cache.get("last_digest_offer") == today:
            return False
        self.update(last_digest_offer=today)
        return True

    # ---------- conexão ----------
    def connect(self, provider):
        """Chamado numa thread pelo botão do painel. Não há login aqui: os conectores são do claude.ai."""
        msg = {"google": "Gmail e Google Agenda vêm dos conectores do Claude. Se continuar com erro, confira em "
                         "claude.ai → Configurações → Conectores. Tentando de novo agora…",
               "microsoft": "O Outlook do IBMEC chega pelo encaminhamento para o Gmail: não precisa conectar."}
        self.notify({"provider": provider, "message": msg.get(provider, "")})
        time.sleep(8)
        self.notify({"provider": provider, "message": ""})
        if provider == "google":
            self.refresh_agenda_email()


# ===================== TESTES (sem rede) =====================
RSS_FIX = """<?xml version="1.0"?><rss><channel>
<item><title>OpenAI lança modelo - Folha - Exame</title><link>https://n.example/1</link><pubDate>Mon, 12 Oct 2026 14:30:00 GMT</pubDate><source url="https://e.example">Exame</source></item>
<item><title>Sem fonte - G1</title><link>https://n.example/2</link><pubDate>lixo</pubDate></item></channel></rss>"""
YAHOO_FIX = {"chart": {"result": [{"meta": {"regularMarketPrice": 9.45, "chartPreviousClose": 9.0},
                                    "indicators": {"quote": [{"close": [9.0, None, 9.2, 9.3, 9.0, 9.45]}]}}]}}


CONNECTOR_FIX = """Here you go:
```json
{"events":[{"id":"e1","title":"Aula","start":"%(day)sT09:00:00-03:00","end":"%(day)sT10:30:00-03:00","link":"https://cal.example/e1"},
 {"id":"e2","title":"Feriado","start":"%(day)s","end":"%(day)s","all_day":true},
 {"id":"e3","title":"Sem data","start":""}],
 "emails":[{"id":"a1","from":"Prof Silva <prof@example.com>","to":"me@example.com","subject":"Prova","snippet":"Prazo &amp; entrega",
  "date":"2026-10-12T14:03:00Z","unread":true,"link":"https://mail.example/a1"},
 {"from":"Secretaria IBMEC <sec@example.edu>","to":"me@students.example.edu","subject":"Matrícula","date":"2026-10-12T15:00:00Z"}]}
```"""


def fixture_cache():
    now = datetime.datetime.now()
    evs, mails = parse_connectors(CONNECTOR_FIX % {"day": now.date().isoformat()})
    quotes = [parse_yahoo(YAHOO_FIX, "VALE3.SA", "VALE3"), parse_yahoo({}, "XXXX.SA", "XXXX")]
    news = [{"key": "ia", "label": "IA e tecnologia", "items": parse_rss(RSS_FIX)}]
    return {"updated": iso(now), "status": {"google": "conectado"}, "news": news, "quotes": quotes, "digest": None,
            "data": {"google": {"events": evs, "emails": mails}}, "triage": {"a1": {"cat": "acao", "ts": time.time()}}}


def feeds_selftest():
    """Roda os testes com um domínio de encaminhamento fictício (e prova que, sem domínio configurado, nada é marcado)."""
    global FORWARDED_DOMAIN
    real, FORWARDED_DOMAIN = FORWARDED_DOMAIN, "students.example.edu"
    try:
        _feeds_selftest()
        FORWARDED_DOMAIN = ""
        assert norm_email({"id": "x", "to": "me@students.example.edu", "date": "2026-10-12T15:00:00Z"})["account"] == "google"
    finally:
        FORWARDED_DOMAIN = real


def _feeds_selftest():
    # RSS: sufixo da fonte removido, data ISO, item sem fonte/data tolerado
    n = parse_rss(RSS_FIX)
    assert n[0]["title"] == "OpenAI lança modelo - Folha" and n[0]["source"] == "Exame" and n[0]["published"].startswith("2026-10-1")
    assert n[1]["title"] == "Sem fonte" and n[1]["source"] == "G1" and n[1]["published"] == "" and n[0]["url"] == "https://n.example/1"
    # Yahoo: preço, variação do dia, closes sem null; 404/vazio -> sem cotação
    q = parse_yahoo(YAHOO_FIX, "VALE3.SA", "VALE3")
    assert q["price"] == 9.45 and q["closes"] == [9.0, 9.2, 9.3, 9.0, 9.45] and q["change_pct"] == 5.0, q
    assert parse_yahoo({}, "X", "X")["price"] is None and parse_yahoo({"chart": {"result": None}}, "X", "X")["closes"] == []
    # Conectores -> dicts normalizados (texto em volta, evento sem data descartado, IBMEC marcado, id estável sem id)
    c = fixture_cache()
    ge, gm = c["data"]["google"]["events"], c["data"]["google"]["emails"]
    assert [e["id"] for e in ge] == ["e1", "e2"] and ge[0]["start"][11:13].isdigit() and not ge[0]["all_day"]
    assert ge[1]["all_day"] and ge[1]["start"].endswith("T00:00:00")
    assert gm[0]["from"] == "Prof Silva" and gm[0]["snippet"] == "Prazo & entrega" and gm[0]["unread"] and gm[0]["account"] == "google"
    assert gm[1]["account"] == "microsoft" and gm[1]["id"].startswith("h") and not gm[1]["unread"] and len(gm[1]["date"]) == 19
    assert gm[1]["id"] == parse_connectors(CONNECTOR_FIX % {"day": "2026-10-13"})[1][1]["id"]  # mesmo id entre buscas
    assert parse_connectors("não consegui") is None and parse_connectors('{"x": 1}') is None
    assert parse_connectors('{"events": [], "emails": []}') == ([], [])
    # Triagem: lixo em volta, categoria desconhecida e acento
    t = parse_triage('Claro! [nota] Aqui:\n```json\n[{"id":"a","cat":"acao"},{"id":"b","cat":"Ruído"},{"id":"c","cat":"xyz"}]\n```')
    assert t == {"a": "acao", "b": "ruido", "c": "info"}, t
    assert parse_triage("sem json") == {} and parse_triage("[1,2]") == {}
    # DASH
    d = build_dash(c)
    assert set(d) == {"updated", "status", "events", "emails", "news", "quotes", "digest"} and d["status"]["microsoft"] == FORWARDED
    assert [e["cat"] for e in d["emails"] if e["id"] == "a1"] == ["acao"] and [e["cat"] for e in d["emails"] if e["account"] == "microsoft"] == [None]
    assert d["events"] == sorted(d["events"], key=lambda e: e["start"]) and json.dumps(d)
    assert build_dash({})["events"] == [] and build_dash({})["status"]["news"] == "não configurado"
    # Contexto limitado; sem conexão não afirma "agenda vazia"
    ctx = dashboard_context(c)
    assert "Prova" in ctx and "Aula" in ctx and "Agenda Today:" in ctx and "NOT CONNECTED" not in ctx and "VALE3 9.45" in ctx and "XXXX: sem cotação" in ctx
    ibmec_id = gm[1]["id"]
    c2 = dict(c, triage=dict(c["triage"], **{ibmec_id: {"cat": "acao", "ts": time.time()}}))
    assert "[IBMEC] Secretaria IBMEC: Matrícula" in dashboard_context(c2)
    off = dict(c, status={"google": "erro: x"})
    ctx0 = dashboard_context(off)
    assert ctx0.count("NOT CONNECTED") == 1 and "nothing" not in ctx0 and "none" not in ctx0 and "Aula" not in ctx0, ctx0
    big = dict(c, news=[{"key": "k", "label": "L", "items": [{"title": "x" * 300, "source": "s", "url": "", "published": ""}] * 5}] * 40)
    assert len(dashboard_context(big)) <= MAX_CONTEXT_CHARS
    # Feeds: triagem em lote, digest em cache, oferta uma vez por dia (arquivo temporário, ask falso)
    import tempfile
    calls = []

    reply = {"fetch": CONNECTOR_FIX % {"day": datetime.date.today().isoformat()}}

    def fake_ask(system, text, model="sonnet", tools=None, deny=()):
        calls.append(model)
        if tools:  # busca nos conectores: só leitura liberada, escrita bloqueada
            assert tools == CONNECTOR_READ and "mcp__claude_ai_Gmail__send_message" in deny and not set(tools) & set(deny)
            return reply["fetch"]
        return '[{"id":"a1","cat":"ruido"}]' if system == TRIAGE_SYSTEM else "Good morning, sir. All quiet."
    tmp = os.path.join(tempfile.gettempdir(), "jarvis_feeds_test.json")
    try:
        f = Feeds(fake_ask, lambda: [{"area": "metas", "title": "Metas", "body": "IA"}], cache_file=tmp)
        f.cache.update(c)
        f.cache["triage"] = {}
        f.triage()
        assert f.cache["triage"]["a1"]["cat"] == "ruido" and f.cache["triage"][ibmec_id]["cat"] == "info" and calls == ["haiku"]
        f.triage()
        assert calls == ["haiku"]  # tudo já classificado: nenhuma chamada nova
        # atualização pelos conectores: ok -> conectado; resposta sem JSON -> erro e mantém os dados antigos
        f.refresh_agenda_email()
        assert f.cache["status"]["google"] == "conectado" and len(f.cache["data"]["google"]["emails"]) == 2 and calls == ["haiku", "haiku"]
        reply["fetch"] = "Sorry, I can't access your calendar."
        f.refresh_agenda_email()
        assert f.cache["status"]["google"].startswith("erro") and len(f.cache["data"]["google"]["emails"]) == 2
        del calls[:]
        calls.append("haiku")
        d1 = f.digest()
        assert d1["text"].startswith("Good morning") and f.digest() is f.cache["digest"] and calls == ["haiku", "sonnet"]
        f.cache["digest"]["generated_at"] = iso(datetime.datetime.now() - datetime.timedelta(hours=4))
        f.digest()
        assert calls[-1] == "sonnet" and len(calls) == 3  # velho demais: refeito
        # digest feito sem o Google não pode continuar valendo quando o Google conecta
        f.cache["status"]["google"] = "erro: x"
        f.digest(force=True)
        assert f.cache["digest"]["google_ok"] is False and f.digest() is f.cache["digest"]  # sem Google: reaproveita o cache
        n = len(calls)
        f.cache["status"]["google"] = "conectado"
        assert f.digest()["google_ok"] is True and len(calls) == n + 1  # Google conectou: refeito na hora
        assert f.digest() is f.cache["digest"] and len(calls) == n + 1  # e agora fica em cache
        assert f.digest_offer_due("2026-01-01") and not f.digest_offer_due("2026-01-01") and f.digest_offer_due("2026-01-02")
        assert load_cache(tmp)["last_digest_offer"] == "2026-01-02"  # persistiu
    finally:
        for p in (tmp, tmp + ".tmp"):
            if os.path.exists(p):
                os.remove(p)
    assert is_digest_reply("  [[DIGEST]] ") and not is_digest_reply("Very good, sir.")
    print("feeds: ok")


if __name__ == "__main__":
    feeds_selftest()
