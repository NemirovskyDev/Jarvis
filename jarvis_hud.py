"""Jarvis HUD: dados do painel azul (clima, sistema, uso do Claude) e envelope de volume da fala.
Funções puras (parse_*, envelope, wmo_desc) testáveis sem rede/janela + buscadores finos (fetch_weather, probe_usage, sys_sample)."""
import datetime
import json
import subprocess
import time
import urllib.parse
import urllib.request

import numpy as np

import jarvis_config as conf

# ===================== CONFIGURAÇÃO =====================
_CFG = conf.get()               # config.json (fora do git): "location" e "timezone"
LAT, LON = _CFG["location"]["lat"], _CFG["location"]["lon"]
TZ = _CFG["timezone"]
SYS_EVERY = 2                   # s: CPU/RAM/disco/rede
WEATHER_EVERY = 15 * 60         # s
USAGE_EVERY = 10 * 60           # s
LEVEL_FRAME_S = 0.065           # janela do envelope de volume (~15 emissões/s)
LEVEL_FULL_RMS = 0.18           # RMS que vale 1.0 (fala normal chega a 0.6~1.0)

WEATHER_URL = (
    "https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}"
    "&current=temperature_2m,apparent_temperature,weather_code"
    "&hourly=temperature_2m,precipitation_probability,weather_code"
    "&daily=temperature_2m_max,temperature_2m_min,precipitation_probability_max"
    "&timezone={tz}&forecast_days=2"
)
USAGE_CMD = ["-p", "--model", "haiku", "--tools", "", "--setting-sources", "", "--strict-mcp-config",
             "--disable-slash-commands", "--no-session-persistence", "--output-format", "stream-json", "--verbose"]


# ===================== CLIMA =====================
def wmo_desc(code):
    """Código WMO -> descrição curta em pt-BR."""
    table = [({0}, "céu limpo"), ({1}, "predominantemente limpo"), ({2}, "parcialmente nublado"), ({3}, "nublado"),
             ({45, 48}, "neblina"), (range(51, 58), "garoa"), (range(61, 68), "chuva"), (range(71, 78), "neve"),
             (range(80, 83), "pancadas de chuva"), (range(95, 100), "trovoada")]
    return next((d for codes, d in table if code in codes), "—")


def parse_weather(j, now):
    """JSON do Open-Meteo + `now` (datetime local) -> dict do HUD. Próximas 8 horas a partir da hora atual (atravessa a meia-noite)."""
    cur, hr, day = j["current"], j["hourly"], j["daily"]
    start = now.strftime("%Y-%m-%dT%H:00")
    i0 = next((i for i, t in enumerate(hr["time"]) if t >= start), 0)  # strings ISO locais comparam como texto
    hourly = [{"h": int(hr["time"][i][11:13]), "temp": round(hr["temperature_2m"][i]),
               "rain": hr["precipitation_probability"][i], "code": hr["weather_code"][i]}
              for i in range(i0, min(i0 + 8, len(hr["time"])))]
    code = cur["weather_code"]
    return {"temp": round(cur["temperature_2m"]), "feels": round(cur["apparent_temperature"]),
            "desc": wmo_desc(code), "code": code,
            "min": round(day["temperature_2m_min"][0]), "max": round(day["temperature_2m_max"][0]),
            "rain": hourly[0]["rain"] if hourly else day["precipitation_probability_max"][0],
            "hourly": hourly, "updated": now.isoformat(timespec="seconds")}


def fetch_weather():
    req = urllib.request.Request(WEATHER_URL.format(lat=LAT, lon=LON, tz=urllib.parse.quote(TZ, safe="")), headers={"User-Agent": "JarvisDesktop/5.0"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return parse_weather(json.loads(r.read().decode("utf-8")), datetime.datetime.now())


# ===================== SISTEMA =====================
def sys_sample(prev=None):
    """(dict do HUD, estado). `prev` = estado da chamada anterior (None na primeira: rede 0)."""
    import psutil
    now, io = time.time(), psutil.net_io_counters()
    down = up = 0.0
    if prev:
        dt = max(now - prev[0], 1e-3)
        down, up = max(0.0, (io.bytes_recv - prev[1]) / dt), max(0.0, (io.bytes_sent - prev[2]) / dt)
    d = {"cpu": psutil.cpu_percent(interval=None), "ram": psutil.virtual_memory().percent,
         "disk": psutil.disk_usage("C:\\").percent, "net_down": down, "net_up": up}
    return d, (now, io.bytes_recv, io.bytes_sent)


# ===================== USO DO CLAUDE =====================
def parse_usage(lines, now=None):
    """Linhas stream-json (com ruído) -> {five_hour, five_hour_resets, seven_day, seven_day_resets, checked}; ausente = None."""
    wins = {}
    for ln in lines:
        try:
            ev = json.loads(ln)
        except ValueError:
            continue
        if isinstance(ev, dict) and ev.get("type") == "rate_limit_event":
            w = (ev.get("rate_limit_info") or {}).get("unifiedWindows") or {}
            wins.update({k: v for k, v in w.items() if isinstance(v, dict)})
    out = {"checked": (now or datetime.datetime.now()).isoformat(timespec="seconds")}
    for k in ("five_hour", "seven_day"):
        w, u, r = wins.get(k, {}), None, None
        if isinstance(w.get("utilization"), (int, float)):
            u = float(w["utilization"])
        if isinstance(w.get("resetsAt"), (int, float)):
            r = datetime.datetime.fromtimestamp(w["resetsAt"]).astimezone().isoformat(timespec="seconds")
        out[k], out[k + "_resets"] = u, r
    return out


def probe_usage(claude, cwd, no_window=0, run=subprocess.run):
    """Uma chamada mínima ao claude -p só para ler o evento rate_limit_event. Levanta erro se não vier nada."""
    r = run([claude, *USAGE_CMD], input=b"Say ok.", capture_output=True, timeout=60, cwd=cwd, creationflags=no_window)
    u = parse_usage(r.stdout.decode("utf-8", "replace").splitlines())
    if u["five_hour"] is None and u["seven_day"] is None:
        raise RuntimeError("sem rate_limit_event (código %s)" % r.returncode)
    return u


# ===================== VOLUME DA FALA =====================
def envelope(audio, sr, frame_s=LEVEL_FRAME_S):
    """Float mono -> lista de níveis 0..1 (RMS por janela de frame_s, normalizado por LEVEL_FULL_RMS)."""
    n = max(1, int(sr * frame_s))
    a = np.asarray(audio, dtype=np.float32).ravel()
    return [min(1.0, float(np.sqrt(np.mean(a[i:i + n] ** 2))) / LEVEL_FULL_RMS) for i in range(0, len(a), n)]


# ===================== TESTES =====================
def hud_selftest():
    # clima: 48 h a partir de 00:00, agora 23:40 -> 23h, 0h, 1h...
    times = [(datetime.datetime(2026, 10, 13) + datetime.timedelta(hours=i)).strftime("%Y-%m-%dT%H:%M") for i in range(48)]
    j = {"current": {"temperature_2m": 23.6, "apparent_temperature": 25.4, "weather_code": 2},
         "hourly": {"time": times, "temperature_2m": [20 + i / 10 for i in range(48)],
                    "precipitation_probability": list(range(48)), "weather_code": [3] * 48},
         "daily": {"temperature_2m_max": [27.2, 28], "temperature_2m_min": [19.4, 20], "precipitation_probability_max": [60, 10]}}
    w = parse_weather(j, datetime.datetime(2026, 10, 13, 23, 40))
    assert [h["h"] for h in w["hourly"]] == [23, 0, 1, 2, 3, 4, 5, 6] and w["hourly"][0]["rain"] == 23, w
    assert (w["temp"], w["feels"], w["min"], w["max"], w["desc"], w["rain"]) == (24, 25, 19, 27, "parcialmente nublado", 23)
    w = parse_weather(j, datetime.datetime(2026, 10, 14, 20, 5))  # fim da série: lista encurta, não quebra
    assert [h["h"] for h in w["hourly"]] == [20, 21, 22, 23]
    assert [wmo_desc(c) for c in (0, 3, 48, 53, 65, 75, 81, 96, 1234)] == \
        ["céu limpo", "nublado", "neblina", "garoa", "chuva", "neve", "pancadas de chuva", "trovoada", "—"]
    # uso: ruído + evento + janela faltando
    ev = {"type": "rate_limit_event", "rate_limit_info": {"unifiedWindows": {
        "five_hour": {"utilization": 0.69, "resetsAt": 1791926400}, "seven_day": {"utilization": 0.59, "resetsAt": 1792400000}}}}
    u = parse_usage(["aviso solto", json.dumps({"type": "system"}), "{quebrado", json.dumps(ev), ""])
    assert u["five_hour"] == 0.69 and u["seven_day"] == 0.59
    assert datetime.datetime.fromisoformat(u["five_hour_resets"]).timestamp() == 1791926400
    ev["rate_limit_info"]["unifiedWindows"].pop("seven_day")
    u = parse_usage([json.dumps(ev)])
    assert u["seven_day"] is None and u["seven_day_resets"] is None and u["five_hour"] == 0.69
    assert parse_usage(["nada"])["five_hour"] is None
    # sistema
    d, st = sys_sample()
    time.sleep(0.2)
    d2, _ = sys_sample(st)
    assert d["net_down"] == 0 and all(0 <= d2[k] <= 100 for k in ("cpu", "ram", "disk")) and d2["net_down"] >= 0 <= d2["net_up"]
    # envelope
    e = envelope(np.zeros(24000), 24000)
    assert len(e) == -(-24000 // int(24000 * LEVEL_FRAME_S)) and max(e) == 0
    tone = 0.5 * np.sin(np.linspace(0, 2000, 24000))
    assert min(envelope(tone, 24000)[:-1]) > 0.95 and max(envelope(tone, 24000)) <= 1.0
    print("hud: ok")
