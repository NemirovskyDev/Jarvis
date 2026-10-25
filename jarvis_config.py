"""Configuração pessoal do Jarvis.

Tudo o que é seu (nome, cidade, ativos acompanhados, notas iniciais) fica em arquivos que o git ignora:
  config.json  -> preferências (veja config.example.json)
  seed.json    -> notas iniciais do Second Brain (veja seed.example.json)
Se o arquivo não existe, valem os padrões genéricos abaixo. Valor inválido não derruba o Jarvis: usa o padrão
e registra o aviso em PROBLEMS (o jarvis_desktop.py escreve esses avisos no log ao iniciar).
Teste: python jarvis_config.py
"""
import copy
import json
import os
import re
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PROBLEMS = []  # avisos de configuração (pt-BR), para o log

DEFAULTS = {
    "owner_name": "the user",                              # como o Jarvis chama o dono nos prompts (ex.: "Maria")
    "location": {"lat": -23.5505, "lon": -46.6333},        # clima (padrão: São Paulo)
    "timezone": "America/Sao_Paulo",
    "tickers": [["^BVSP", "Ibovespa"]],                    # [símbolo do Yahoo Finance, nome exibido]
    "news_topics": [                                       # [chave, rótulo, consulta do Google News]
        ["ia", "IA e tecnologia", "inteligência artificial OR OpenAI OR Anthropic OR Claude OR Gemini"],
        ["mercado", "Mercado", 'Ibovespa OR "fundos imobiliários"'],
    ],
    "forwarded_domain": "",                                # e-mails ao Gmail cujo destinatário contém isto viram a conta "encaminhada"; vazio = desligado
}


def _is_pair_list(v, n):
    return isinstance(v, list) and bool(v) and all(
        isinstance(x, list) and len(x) == n and all(isinstance(s, str) and s.strip() for s in x) for x in v)


def _valid_location(v):
    return (isinstance(v, dict) and all(isinstance(v.get(k), (int, float)) and not isinstance(v.get(k), bool) for k in ("lat", "lon"))
            and -90 <= v["lat"] <= 90 and -180 <= v["lon"] <= 180)


CHECKS = {
    "owner_name": lambda v: isinstance(v, str) and bool(v.strip()) and len(v) <= 60,
    "location": _valid_location,
    "timezone": lambda v: isinstance(v, str) and re.fullmatch(r"[A-Za-z_]+(/[A-Za-z_+\-0-9]+)*", v) is not None,
    "tickers": lambda v: _is_pair_list(v, 2),
    "news_topics": lambda v: _is_pair_list(v, 3),
    "forwarded_domain": lambda v: isinstance(v, str) and len(v) <= 100,
}


def load_config(base=HERE):
    """Padrões + config.json (se existir e for válido, campo a campo)."""
    cfg = copy.deepcopy(DEFAULTS)
    path = os.path.join(base, "config.json")
    if not os.path.exists(path):
        return cfg
    try:
        with open(path, encoding="utf-8") as f:
            user = json.load(f)
    except (OSError, ValueError) as e:
        PROBLEMS.append(f"config.json ilegível ({type(e).__name__}); usando os padrões")
        return cfg
    if not isinstance(user, dict):
        PROBLEMS.append("config.json precisa ser um objeto JSON; usando os padrões")
        return cfg
    for key, value in user.items():
        if key not in CHECKS:
            PROBLEMS.append(f"config.json: campo desconhecido '{key}' (ignorado)")
        elif CHECKS[key](value):
            cfg[key] = value
        else:
            PROBLEMS.append(f"config.json: valor inválido em '{key}'; usando o padrão")
    return cfg


_cfg = None


def get():
    """Configuração do usuário, lida uma vez só (todos os módulos compartilham o mesmo resultado e os avisos não se repetem)."""
    global _cfg
    if _cfg is None:
        _cfg = load_config()
    return _cfg


def _valid_seed(d):
    if not isinstance(d, dict) or not isinstance(d.get("notes"), list) or not isinstance(d.get("relations"), list):
        return False
    ids = set()
    for n in d["notes"]:
        if not (isinstance(n, dict) and all(isinstance(n.get(k), str) and n[k].strip() for k in ("id", "area", "title", "body"))):
            return False
        ids.add(n["id"])
    if len(ids) != len(d["notes"]):
        return False  # ids repetidos
    return all(isinstance(r, list) and len(r) == 2 and r[0] in ids and r[1] in ids for r in d["relations"])


def _read_seed(path):
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError) as e:
        PROBLEMS.append(f"{os.path.basename(path)} ilegível ({type(e).__name__})")
        return None
    if not _valid_seed(d):
        PROBLEMS.append(f"{os.path.basename(path)} inválido (notas com id/area/title/body, ids únicos, relações entre ids existentes)")
        return None
    return d


def load_seed(base=HERE, use_example=False):
    """(notas, relações) iniciais do Second Brain: seed.json (pessoal) ou seed.example.json (genérico). Só servem para criar o cofre."""
    for name in (() if use_example else ("seed.json",)) + ("seed.example.json",):
        path = os.path.join(base, name)
        if os.path.exists(path):
            d = _read_seed(path)
            if d:
                return copy.deepcopy(d["notes"]), copy.deepcopy(d["relations"])
    return [], []


def selftest():
    with tempfile.TemporaryDirectory() as t:
        w = lambda name, data: open(os.path.join(t, name), "w", encoding="utf-8").write(data if isinstance(data, str) else json.dumps(data))  # noqa: E731
        # sem arquivos: padrões, sem avisos
        del PROBLEMS[:]
        assert load_config(t) == DEFAULTS and load_seed(t) == ([], []) and not PROBLEMS
        # config válida e parcial; campo inválido e desconhecido caem no padrão com aviso
        w("config.json", {"owner_name": "Ana", "tickers": [["ITUB4.SA", "ITUB4"]], "location": {"lat": 999, "lon": 0}, "xyz": 1,
                          "forwarded_domain": "uni.example.edu", "news_topics": [["a", "b"]]})
        c = load_config(t)
        assert c["owner_name"] == "Ana" and c["tickers"] == [["ITUB4.SA", "ITUB4"]] and c["forwarded_domain"] == "uni.example.edu"
        assert c["location"] == DEFAULTS["location"] and c["news_topics"] == DEFAULTS["news_topics"]
        assert len(PROBLEMS) == 3, PROBLEMS
        # JSON quebrado e tipo errado
        del PROBLEMS[:]
        w("config.json", "{ nao é json")
        assert load_config(t) == DEFAULTS and len(PROBLEMS) == 1
        w("config.json", "[1]")
        assert load_config(t) == DEFAULTS and len(PROBLEMS) == 2
        # semente: example vale sem seed.json; seed.json válido prevalece; inválido cai no example com aviso
        ex = {"notes": [{"id": "a", "area": "meta", "title": "A", "body": "x"}, {"id": "b", "area": "metas", "title": "B", "body": "y"}],
              "relations": [["a", "b"]]}
        w("seed.example.json", ex)
        assert load_seed(t) == (ex["notes"], ex["relations"])
        mine = {"notes": [{"id": "z", "area": "meta", "title": "Z", "body": "w"}], "relations": []}
        w("seed.json", mine)
        assert load_seed(t) == (mine["notes"], []) and load_seed(t, use_example=True)[0] == ex["notes"]
        del PROBLEMS[:]
        for bad in ({"notes": [{"id": "a"}], "relations": []},
                    {"notes": mine["notes"] * 2, "relations": []},          # ids repetidos
                    {"notes": mine["notes"], "relations": [["z", "nao_existe"]]}):
            w("seed.json", bad)
            assert load_seed(t)[0] == ex["notes"]
        assert len(PROBLEMS) == 3
        w("seed.json", "lixo")
        assert load_seed(t)[0] == ex["notes"] and len(PROBLEMS) == 4
        # os padrões embutidos passam nas próprias validações
        assert all(CHECKS[k](v) for k, v in DEFAULTS.items())
    del PROBLEMS[:]
    print("config: ok")


if __name__ == "__main__":
    selftest()
