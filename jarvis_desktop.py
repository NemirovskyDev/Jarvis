"""Jarvis Desktop: janela nativa (pywebview) + motor de áudio em Python, sem navegador.

Uso: python jarvis_desktop.py               -> abre a janela em tela cheia, faz o boot e cumprimenta.
     pythonw jarvis_desktop.py --background -> começa oculto; "Hey Jarvis" ou duas palmas chamam a janela.
     python jarvis_desktop.py --selftest    -> testes sem microfone/janela/rede.
Fluxo: WAKE (openWakeWord + palmas) -> COMMAND (grava) -> THINKING (Whisper + Claude) -> SPEAKING (Kokoro) -> FOLLOW-UP.
"""
import atexit
import copy
import datetime
import json
import os
import queue
import re
import socket
import sys
import threading
import time
import unicodedata
import webbrowser

import shutil
import subprocess
import tempfile

import numpy as np

import jarvis_config as conf
import jarvis_feeds as feeds
import jarvis_hud as hud
import jarvis_vault as vault

# ===================== CONFIGURAÇÃO (ajuste aqui) =====================
VOICE = "bm_george"        # voz do Kokoro (britânica, masculina)
VOICE_SPEED = 0.95         # velocidade da fala
VOICE_FX = True            # True = leve reverberação "de sistema"
SHOW_ANSWER_TEXT = False     # False = o Jarvis só fala (o texto da resposta não aparece na tela; erros aparecem)
HIBERNATE_POLL_S = 1.0     # hibernando: de quanto em quanto tempo os laços de fundo conferem se foram chamados de volta
OUTPUT_DEVICE = "Echo Dot"  # saída de áudio pelo nome (parte do nome, ex.: "Echo"); vazio = a padrão do Windows
WAKE_THRESHOLD = 0.5       # pontuação mínima do "hey_jarvis" (0 a 1)
WHISPER_MODEL = "base"     # tiny / base / small (maior = mais preciso e mais lento)
COMMAND_LANGUAGE = "pt"    # idioma dos comandos falados; None = detecção automática (pouco confiável em frases curtas)
EFFORT = "medium"          # esforço do Claude: low / medium / high
FOLLOW_UP_SECONDS = 8      # após uma resposta, quanto espera por nova fala sem precisar da palavra de ativação
COMMAND_WAIT_SECONDS = 10  # após o despertar, quanto espera o comando começar
SILENCE_SECONDS = 0.9      # silêncio que encerra o comando
MAX_COMMAND_SECONDS = 15   # duração máxima de um comando
CLAP_THRESHOLD = 0.35      # palmas: pico mínimo (fração do volume máximo); diminua se suas palmas não forem ouvidas
CLAP_RATIO = 6             # pico precisa ser >= 6x o ruído de fundo
CLAP_MAX_MS = 150          # barulho mais longo que isso é fala/música, não palma
CLAP_GAP_MS = (200, 900)   # intervalo (início a início) entre as duas palmas
LOCK_PORT = 8766           # instância única: porta local usada como trava e para "mostrar"
HISTORY_MAX = 20           # mensagens lembradas na sessão

HERE = os.path.dirname(os.path.abspath(__file__))
MODELS = os.path.join(HERE, "models")
PAGE = os.path.join(HERE, "desktop.html")
LOG = os.path.join(HERE, "jarvis.log")
CLAUDE = shutil.which("claude")
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)  # sem isso o claude.cmd pisca um console sob pythonw

GREETING = "Systems online. At your service, sir."
ACK = "Yes, sir?"
OWNER = conf.get()["owner_name"]  # como os prompts chamam o dono (config.json)
ERROR_MSG = "I'm afraid I can't reach my cognitive core, sir."
LOADING = "Loading..."
BLOCK = 1280               # 80 ms a 16 kHz (tamanho exigido pelo openWakeWord)
BLOCK_S = BLOCK / 16000

# ===================== DADOS =====================
AREA = {
    "metas": {"label":"Metas","color":"#fbbf24"},
    "trabalho": {"label":"Carreira","color":"#ff5547"},
    "projetos": {"label":"Projetos","color":"#8b7cff"},
    "financas": {"label":"Finanças","color":"#f7931a"},
    "aprendizado": {"label":"Aprendizado","color":"#2dd4ff"},
    "saude": {"label":"Saúde","color":"#10b981"},
    "relacoes": {"label":"Relações","color":"#ec4899"},
    "meta": {"label":"Você","color":"#8a90a6"},
}

# Notas iniciais (semente): só usadas para criar o cofre do Obsidian na primeira vez (depois ele é a fonte da verdade).
# Vêm do seed.json (seu, fora do git) ou, se não existir, do seed.example.json.
NOTES, REL = conf.load_seed()

# ===================== NOTAS / MEMÓRIA VIVA =====================
SAVE_RE = re.compile(r"\[\[SAVE:([a-z_]+)\|([^|]+)\|([\s\S]+?)\]\]", re.I)


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def norm(s):
    """Minúsculas e sem acentos (para comparar títulos)."""
    return "".join(c for c in unicodedata.normalize("NFD", str(s).lower()) if not unicodedata.combining(c))


def valid_notes(arr):
    return isinstance(arr, list) and all(isinstance(n, dict) and n.get("id") and n.get("title") for n in arr)


def slug(t):
    return re.sub(r"[^a-z0-9]+", "_", norm(t)).strip("_")[:24] + "_" + format(int(time.time() * 1000), "x")


def parse_save(text, notes):
    """Aplica as linhas [[SAVE:area|titulo|texto]] em `notes` (altera a lista). Devolve (texto limpo, id da última nota)."""
    touched = None
    for m in SAVE_RE.finditer(text):
        area = m.group(1).strip().lower()
        area = area if area in AREA else "meta"
        title, body = m.group(2).strip(), m.group(3).strip()
        n = next((x for x in notes if norm(x["title"]) == norm(title)), None)
        if n:
            n["body"], n["area"] = body, area
        else:
            n = {"id": slug(title), "area": area, "title": title, "body": body}
            notes.append(n)
        touched = n["id"]
    return SAVE_RE.sub("", text).strip(), touched


def build_system(notes, ctx=""):
    """Prompt de sistema, refeito a cada chamada (tem a data de hoje, o segundo cérebro inteiro, as notas recentes do cofre e, em ctx, o painel)."""
    brain = ""
    for key, a in AREA.items():
        items = [n for n in notes if (n.get("area") if n.get("area") in AREA else "meta") == key]
        if items:
            brain += "\n" + a["label"] + "\n" + "\n".join(f"- {n['title']}: {n['body']}" for n in items) + "\n"
    recent = vault.recent_context()  # o que o Claude Desktop/Code escreveu no cofre fora do Second Brain
    return (
        f"You are JARVIS, {OWNER}'s personal AI assistant: a refined, formal British butler with dry, subtle wit, "
        "in the style of JARVIS from Iron Man. Always address him as 'sir'.\n"
        "Sir speaks to you in Brazilian Portuguese; understand it perfectly and always reply in British English.\n"
        "Rules: reply in British English, in 1 to 3 short spoken sentences, with no markdown, no emojis and no lists.\n"
        f"Today's date: {datetime.date.today():%A, %d %B %Y}.\n\n"
        f"SECOND BRAIN (notes about {OWNER}; they are written in Portuguese):\n" + brain + "\n"
        + (recent + "\n\n" if recent else "") +
        f"If {OWNER} reveals something new and lasting, END your reply with one line in the EXACT format "
        "[[SAVE:area|title|text]] (area is one of metas, trabalho, projetos, financas, aprendizado, saude, relacoes, meta). "
        "If a note with that title already exists it is updated, otherwise a new one is created. "
        "Include it only when there is really something new. The title and text may be written in Portuguese. "
        "That line is never spoken."
        + ("\n\n" + ctx if ctx else "")
    )


# ===================== ÁUDIO: utilitários =====================
def split_sentences(text):
    """Divide em frases mantendo a pontuação (não corta em '3.5', só em pontuação seguida de espaço)."""
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+", text.strip()) if s.strip()]


FIRST_CHUNK_S = 4.5        # primeiro pedaço: junta a interjeição ("Very good, sir.") com o começo da resposta, para não haver buraco depois dela
SYNTH_FIXED_S = 0.6        # custo fixo de cada síntese do Kokoro neste PC (medido)
SYNTH_RATIO = 0.62         # segundos de síntese por segundo de fala (medido)
SECONDS_PER_CHAR = 0.065   # estimativa inicial; depois vale a medida do pedaço anterior


def clauses(text):
    """Frases cortadas nas vírgulas/ponto e vírgula/dois-pontos (a pontuação fica com o pedaço)."""
    out = []
    for sent in split_sentences(text):
        out += [c.strip() for c in re.split(r"(?<=[,;:])\s+", sent) if c.strip()]
    return out


def next_chunk(pieces, budget_s, spc):
    """Tira de `pieces` o próximo texto a sintetizar: pelo menos um trecho, e mais enquanto a fala estimada couber em `budget_s`."""
    chunk = pieces.pop(0)
    while pieces and (len(chunk) + 1 + len(pieces[0])) * spc <= budget_s:
        chunk += " " + pieces.pop(0)
    return chunk


def fx(a, sr):
    """Eco curtinho que dá o timbre de 'sistema'."""
    out = a.copy()
    for ms, g in [(11, .22), (23, .12), (90, .07)]:
        d = int(sr * ms / 1000)
        out[d:] += g * a[:-d]
    peak = np.abs(out).max()
    return out / peak * .9 if peak else out


def synth(kokoro, text):
    a, _ = kokoro.create(text, voice=VOICE, speed=VOICE_SPEED, lang="en-gb")
    return fx(a, 24000) if VOICE_FX else a


def to16k(a, sr):
    """float -> int16 a 16 kHz."""
    from math import gcd
    from scipy.signal import resample_poly
    g = gcd(16000, sr)
    return (np.clip(resample_poly(a, 16000 // g, sr // g), -1, 1) * 32767).astype(np.int16)


def wake_score(oww, block):
    return max(oww.predict(block).values(), default=0.0)


def transcribe(model, pcm):
    segs, _ = model.transcribe(pcm.astype(np.float32) / 32768, beam_size=1, vad_filter=True, language=COMMAND_LANGUAGE)
    return "".join(s.text for s in segs).strip()


def drain(q):
    while True:
        try:
            q.get_nowait()
        except queue.Empty:
            return


def load_kokoro():
    from kokoro_onnx import Kokoro
    return Kokoro(os.path.join(MODELS, "kokoro-v1.0.onnx"), os.path.join(MODELS, "voices-v1.0.bin"))


def load_whisper():
    from faster_whisper import WhisperModel
    return WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8", download_root=os.path.join(MODELS, "whisper"))


def load_oww():
    from openwakeword.model import Model
    return Model(wakeword_models=["hey_jarvis"], inference_framework="onnx")


class Recorder:
    """Grava um comando: espera a fala, e termina após SILENCE_SECONDS de silêncio (ou MAX_COMMAND_SECONDS de fala)."""

    def __init__(self, wait_seconds):
        self.wait = wait_seconds
        self.floor = 0.005   # ruído de fundo (RMS), adaptativo
        self.pre = []        # últimos blocos antes da fala (não corta o começo da palavra)
        self.chunks = []
        self.speaking = False
        self.t = self.talk = self.silent = 0.0
        self.n = 0

    def feed(self, block):
        """None = continua; "timeout" = ninguém falou; senão o áudio int16 gravado."""
        x = block.astype(np.float32) / 32768
        rms = float(np.sqrt(np.mean(x * x)))
        self.t += BLOCK_S
        self.n += 1
        loud = rms > max(0.012, 3 * self.floor)
        if not self.speaking:
            if loud and self.n > 3:
                self.speaking = True
                self.chunks = self.pre + [block]
            else:
                # ponytail: nos 3 primeiros blocos calibra o piso mesmo com som (limitado a 0.03); depois só no silêncio
                if self.n <= 3 or not loud:
                    self.floor += (0.3 if self.n <= 3 else 0.05) * (min(rms, 0.03) - self.floor)
                self.pre = (self.pre + [block])[-4:]
                if self.t >= self.wait:
                    return "timeout"
            return None
        self.chunks.append(block)
        self.talk += BLOCK_S
        self.silent = 0.0 if loud else self.silent + BLOCK_S
        if self.silent >= SILENCE_SECONDS - 1e-6 or self.talk >= MAX_COMMAND_SECONDS:
            return np.concatenate(self.chunks)
        return None


# ===================== CÉREBRO (Claude Code, plano Pro) =====================
def build_prompt(messages):
    """Transforma o histórico em texto, já que cada chamada ao claude -p é independente."""
    *past, last = messages
    lines = [("Sir: " if m["role"] == "user" else "JARVIS: ") + m["content"] for m in past]
    history = "Conversation so far:\n" + "\n".join(lines) + "\n\n" if lines else ""
    return history + "Sir now says:\n" + last["content"]


def ask_claude(system, messages, model="sonnet", tools=None, deny=()):
    """claude -p sem ferramentas; com `tools`, libera SÓ essas (conectores do claude.ai) e bloqueia `deny`."""
    if not CLAUDE:
        raise RuntimeError("Claude Code não encontrado (npm install -g @anthropic-ai/claude-code)")
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as f:
        f.write(system)
        sys_file = f.name
    # --strict-mcp-config esconde os conectores do claude.ai: só pode ser usado quando não há ferramentas
    access = ["--allowedTools", *tools, "--disallowedTools", *deny] if tools else ["--tools", "", "--strict-mcp-config"]
    try:
        r = subprocess.run(
            [CLAUDE, "-p", "--model", model, "--effort", EFFORT, "--system-prompt-file", sys_file, *access,
             "--setting-sources", "", "--disable-slash-commands", "--no-session-persistence",
             "--output-format", "json" if tools else "text"],
            input=build_prompt(messages).encode("utf-8"),
            capture_output=True, timeout=420 if tools else 120, cwd=tempfile.gettempdir(), creationflags=NO_WINDOW,
        )
    finally:
        os.remove(sys_file)
    out = r.stdout.decode("utf-8", "replace").strip()
    if r.returncode != 0 or not out:
        raise RuntimeError(r.stderr.decode("utf-8", "replace").strip() or out or f"código {r.returncode}")
    if tools:  # sem chamada de ferramenta, um JSON "vazio" seria mentira: vira erro
        data = json.loads(out)
        if data.get("is_error") or data.get("num_turns", 0) < 2:
            raise RuntimeError("conectores não usados (confira em claude.ai → Configurações → Conectores)")
        out = str(data.get("result", "")).strip()
    return out


# ===================== PALMAS =====================
class ClapDetector:
    """Detecta duas palmas seguidas em blocos int16 de 16 kHz (quadros de 10 ms)."""
    FRAME = 160

    def __init__(self):
        self.buf = np.zeros(0, np.int16)
        self.ms = 0        # relógio do áudio (ms)
        self.bg = 0.01     # ruído de fundo (média móvel dos picos)
        self.start = None  # ms em que o barulho atual começou
        self.prev = None   # ms em que a palma anterior começou

    def feed(self, pcm):
        """True quando as duas palmas acontecem dentro de CLAP_GAP_MS."""
        buf = np.concatenate([self.buf, pcm])
        n = len(buf) // self.FRAME
        self.buf = buf[n * self.FRAME:]
        peaks = np.abs(buf[:n * self.FRAME].astype(np.int32)).reshape(n, self.FRAME).max(axis=1) / 32768
        return any([self._frame(p) for p in peaks])  # lista (não gerador): processa todos os quadros

    def _frame(self, peak):
        self.ms += 10
        loud = peak >= CLAP_THRESHOLD
        if not loud:
            self.bg = max(0.003, self.bg + 0.02 * (peak - self.bg))
        if self.start is None:
            if loud and peak >= CLAP_RATIO * self.bg:
                self.start = self.ms
            return False
        if loud:
            return False
        start, self.start = self.start, None  # o barulho acabou: palma de verdade é curta
        if self.ms - start > CLAP_MAX_MS:
            return False
        lo, hi = CLAP_GAP_MS
        if self.prev is not None and lo <= start - self.prev <= hi:
            self.prev = None
            return True
        if self.prev is None or start - self.prev > hi:  # senão é eco da palma anterior: ignora
            self.prev = start
        return False


def redirect_output():
    """Sob pythonw não há stdout/stderr: manda tudo para o jarvis.log (trunca se passar de 1 MB)."""
    big = os.path.exists(LOG) and os.path.getsize(LOG) > 1_000_000
    sys.stdout = sys.stderr = open(LOG, "w" if big else "a", buffering=1, encoding="utf-8")


# ===================== SAÍDA DE ÁUDIO =====================
_warned_output = False


def find_output_device(name, devices, hostapis):
    """Índice da saída cujo nome contém `name` (sem acento/maiúscula), ou None. Prefere o MME (ele reamostra 24 kHz sozinho)."""
    want = norm(name)
    found = []
    for i, d in enumerate(devices):
        if d["max_output_channels"] > 0 and want and want in norm(d["name"]) and "mapper" not in norm(d["name"]):
            found.append((0 if hostapis[d["hostapi"]]["name"] == "MME" else 1, i))
    return min(found)[1] if found else None


def open_output(sd):
    """Abre a saída pedida em OUTPUT_DEVICE; se ela não existir ou falhar (Echo desconectado), usa a padrão do Windows."""
    global _warned_output
    dev = find_output_device(OUTPUT_DEVICE, sd.query_devices(), sd.query_hostapis()) if OUTPUT_DEVICE else None
    if dev is not None:
        try:
            st = sd.OutputStream(samplerate=24000, channels=1, dtype="float32", device=dev)
            st.start()
            _warned_output = False
            return st
        except Exception as e:
            log("! saída", OUTPUT_DEVICE, "falhou, usando a padrão:", str(e)[:80])
    elif OUTPUT_DEVICE and not _warned_output:
        _warned_output = True
        log(f'! saída "{OUTPUT_DEVICE}" não encontrada (pareada e conectada?); usando a padrão')
    st = sd.OutputStream(samplerate=24000, channels=1, dtype="float32")
    st.start()
    return st


# ===================== MOTOR =====================
class Engine:
    def __init__(self, background=False):
        self.window = None
        self.background = background
        self.page_ready = threading.Event()               # a página carregou (só então vale evaluate_js)
        self.tts_ready, self.stt_ready = threading.Event(), threading.Event()
        self.kokoro = self.whisper = self.oww = None
        self.tts_lock = threading.Lock()                  # o Kokoro não é thread-safe
        self.notes_lock = threading.Lock()
        self.notes, self.relations, self.vsig = self.open_vault()
        self.history = []
        self.mic_on = True
        self.visible = not background
        self.state, self.status = "idle", LOADING
        self.mode = "wake"                                # wake | command
        self.rec = None
        self.claps = ClapDetector()
        self.deaf = False                                 # True = descarta o microfone (pensando/falando)
        self.blocks, self.typed = queue.Queue(), queue.Queue()
        self.summon, self.reset, self.listen_evt = threading.Event(), threading.Event(), threading.Event()
        self.quitting = False
        self.hibernating = threading.Event()               # hibernando: tela fechada, microfone desligado, buscas em fundo pausadas
        self.cur_stop = threading.Event()                 # interrompe a fala em andamento
        self.out = None                                   # OutputStream ativo
        self.feeds = feeds.Feeds(self.ask_feeds, lambda: self.notes, on_update=self.push_dashboard,
                                 notify=lambda info: self.ui("connect", info))
        self.refresh_now = threading.Event()              # força o refresher a atualizar já
        self.digest_req = threading.Event()               # botão "Ouvir" da página
        self.offer_pending = False                        # acabei de oferecer o digest (o "sim" vira [[DIGEST]])
        self.hud = {"sys": None, "weather": None, "usage": None}  # cache do HUD (clima, sistema, uso do Claude)
        self.usage_req = threading.Event()                # pede nova leitura do uso já
        self.hud_busy = set()                             # buscas lentas em andamento (evita duplicar)

    # ---------- cofre do Obsidian (Second Brain) ----------
    @staticmethod
    def open_vault():
        """Cria o cofre se preciso e lê as notas. Se falhar (disco, permissão), segue com a semente em memória."""
        try:
            vault.ensure_vault(NOTES, REL, AREA)
            sig = vault.signature()
            return (*vault.load(), sig)
        except Exception as e:
            log("! cofre:", e)
            return copy.deepcopy(NOTES), copy.deepcopy(REL), None

    def reload_vault(self):
        """Relê o cofre (chamar com notes_lock). A assinatura vem ANTES da leitura: edição no meio dispara nova releitura."""
        sig = vault.signature()
        self.notes, self.relations = vault.load()
        self.vsig = sig

    def vault_watcher(self):
        """Thread própria: a cada 3 s vê se alguém editou o cofre por fora (Obsidian, Claude) e atualiza a página."""
        while not self.quitting:
            time.sleep(3)
            if self.vsig is None or self.hibernating.is_set():  # cofre indisponível, ou hibernando (relê ao acordar)
                continue
            try:
                with self.notes_lock:
                    changed = vault.signature() != self.vsig
                    if changed:
                        self.reload_vault()
                if changed:
                    log(f"cofre atualizado por fora: {len(self.notes)} notas, {len(self.relations)} conexões")
                    self.ui("notes", self.notes, None, self.relations)
            except Exception as e:
                log("! cofre:", e)

    # ---------- ponte Python -> JS ----------
    def ui(self, fn, *args):
        """Chama window.ui.<fn>(...) na página; antes de ela carregar, descarta (o estado é reenviado no on_loaded)."""
        if not (self.window and self.page_ready.is_set()):
            return
        js = "window.ui&&ui." + fn + "(" + ", ".join(json.dumps(a, ensure_ascii=False) for a in args) + ")"
        try:
            self.window.evaluate_js(js)
        except Exception as e:
            log("! evaluate_js:", e)

    @staticmethod
    def ask_feeds(system, text, model="sonnet", tools=None, deny=()):
        return ask_claude(system, [{"role": "user", "content": text}], model, tools, deny)

    def push_dashboard(self):
        self.ui("dashboard", self.feeds.dash())

    def show_answer(self, text, error=False):
        """Mostra na tela o que o Jarvis diz. Com SHOW_ANSWER_TEXT desligado só os erros aparecem (ele apenas fala)."""
        if SHOW_ANSWER_TEXT or error:
            self.ui("answer", text)

    def set_state(self, s):
        self.state = s
        self.ui("state", s)

    def set_status(self, text):
        self.status = text
        self.ui("status", text)

    def on_loaded(self):
        self.page_ready.set()
        self.ui("state", self.state)
        self.ui("status", self.status)
        self.ui("mic", self.mic_on)
        self.push_dashboard()
        self.ui("hud", self.hud)

    def on_closing(self):
        """Fechar a janela (X) hiberna em vez de encerrar (cancela o fechamento)."""
        if self.quitting:
            return True
        threading.Thread(target=self.hibernate, daemon=True).start()
        return False

    def show(self):
        self.visible = True
        if self.hibernating.is_set():  # acordou: volta a atualizar tudo na hora (os laços de fundo estavam parados)
            log("saindo da hibernação")
            self.hibernating.clear()
            self.refresh_now.set()
            self.usage_req.set()
        try:
            self.window.show()
            self.window.on_top = True   # traz para a frente (o Windows costuma segurar o foco)
            self.window.on_top = False
        except Exception as e:
            log("! show:", e)
        self.ui("boot")

    def hibernate(self):
        """Hibernar: esconde a tela, desliga o microfone e pausa as buscas em segundo plano. Só volta por Ctrl+Alt+J ou pelo ícone."""
        log("hibernando: tela fechada, microfone desligado")
        self.hibernating.set()
        self.hide()
        return True

    def hide(self):
        self.visible = False
        self.stop_speaking()
        try:
            self.window.hide()
        except Exception as e:
            log("! hide:", e)
        self.reset.set()

    # ---------- chamadas vindas da página (Api) ----------
    def get_state(self):
        return {"config": {"name": "Jarvis", "address": "sir"}, "notes": self.notes, "areas": AREA, "relations": self.relations}

    def save_notes(self, notes):
        if not valid_notes(notes):
            return False
        try:
            with self.notes_lock:
                vault.save_all(notes, self.notes)
                self.reload_vault()
        except Exception as e:
            log("! cofre:", e)
            return False
        # em outra thread: evaluate_js dentro de uma chamada da própria página pode travar
        threading.Thread(target=self.ui, args=("notes", self.notes, None, self.relations), daemon=True).start()
        return True

    def send_text(self, text):
        text = str(text).strip()
        if text:
            self.typed.put(text)
            if self.state == "speaking":
                self.stop_speaking()
        return True

    def set_mic(self, on):
        self.mic_on = bool(on)
        self.ui("mic", self.mic_on)
        if not self.mic_on:
            self.reset.set()
        return True

    def listen_now(self):
        """Orbe clicado: liga o microfone e grava a próxima frase sem palavra de ativação."""
        if self.state in ("thinking", "speaking"):
            return True
        self.mic_on = True
        self.ui("mic", True)
        self.listen_evt.set()
        return True

    # ---------- painel (agenda, e-mails, notícias, carteira, digest) ----------
    def get_dashboard(self):
        return self.feeds.dash()

    def refresh_dashboard(self):
        self.refresh_now.set()
        return True

    def play_digest(self):
        if self.state == "speaking":
            self.stop_speaking()
        self.digest_req.set()
        return True

    def connect_account(self, provider):
        if provider not in ("google", "microsoft"):
            return False
        threading.Thread(target=self.feeds.connect, args=(provider,), daemon=True).start()
        return True

    def open_link(self, url):
        url = str(url)
        if not url.startswith(("http://", "https://")):
            return False
        webbrowser.open(url)
        return True

    # ---------- HUD (sistema a cada 2 s, clima e uso em threads próprias; nunca bloqueia o áudio) ----------
    def get_hud(self):
        return self.hud

    def refresh_usage(self):
        self.usage_req.set()
        return True

    def hud_set(self, key, val):
        self.hud[key] = val
        self.ui("hud", {key: val})

    def hud_job(self, key, fn):
        """Busca lenta numa thread: falha mantém o valor antigo e loga só o tipo do erro."""
        if key in self.hud_busy:
            return
        self.hud_busy.add(key)

        def run():
            try:
                self.hud_set(key, fn())
            except Exception as e:
                log("! hud", key + ":", type(e).__name__, str(e)[:80])
            finally:
                self.hud_busy.discard(key)
        threading.Thread(target=run, daemon=True).start()

    def hud_loop(self):
        prev, t0 = None, time.time()
        next_w, next_u = t0 + 5, None
        while not self.quitting:
            if self.hibernating.is_set():
                time.sleep(HIBERNATE_POLL_S)
                continue
            now = time.time()
            try:
                d, prev = hud.sys_sample(prev)
                self.hud_set("sys", d)
            except Exception as e:
                log("! hud sys:", type(e).__name__)
            if next_u is None and self.stt_ready.is_set():
                next_u = now + 20                         # uso: só depois dos modelos, para não disputar a partida
            if now >= next_w:
                next_w = now + hud.WEATHER_EVERY
                self.hud_job("weather", hud.fetch_weather)
            if next_u is not None and (now >= next_u or self.usage_req.is_set()):
                next_u = now + hud.USAGE_EVERY
                self.usage_req.clear()
                self.hud_job("usage", lambda: hud.probe_usage(CLAUDE, tempfile.gettempdir(), NO_WINDOW))
            self.usage_req.wait(hud.SYS_EVERY)

    def refresher(self):
        """Thread própria (nunca bloqueia o áudio): atualiza agenda/e-mails e notícias/cotações no seu ritmo e sob demanda."""
        self.stt_ready.wait(300)
        last_a = last_n = 0.0
        while not self.quitting:
            if self.hibernating.is_set():
                time.sleep(HIBERNATE_POLL_S)
                continue
            force = self.refresh_now.is_set()
            self.refresh_now.clear()
            now = time.time()
            try:
                if force or now - last_n >= feeds.NEWS_MARKET_MIN * 60:
                    last_n = now
                    self.feeds.refresh_news_market()
                if force or now - last_a >= feeds.AGENDA_EMAIL_MIN * 60:
                    last_a = now
                    self.feeds.refresh_agenda_email()
            except Exception as e:
                log("! refresher:", e)
            self.refresh_now.wait(30)

    def stop_speaking(self):
        self.cur_stop.set()
        out = self.out
        if out:
            try:
                out.abort()
            except Exception:
                pass
        return True

    def quit(self):
        log("desligado pelo botão ⏻")
        self.quitting = True
        self.stop_speaking()
        try:
            self.window.destroy()
        except Exception:
            os._exit(0)

    # ---------- modelos ----------
    def load_models(self):
        """Carrega tudo em segundo plano para a janela abrir logo."""
        try:
            self.kokoro = load_kokoro()
        except Exception as e:
            log("! kokoro:", e)
        self.tts_ready.set()
        try:
            self.oww = load_oww()
        except Exception as e:
            log("! openwakeword (só palmas):", e)
        try:
            self.whisper = load_whisper()
        except Exception as e:
            log("! whisper (só texto digitado):", e)
        self.stt_ready.set()
        log("modelos prontos")
        self.set_status("")

    # ---------- fala ----------
    def speak(self, text):
        """Kokoro frase a frase: sintetiza a próxima enquanto a atual toca."""
        if self.hibernating.is_set():  # hibernou no meio de um comando: a resposta que ainda vinha não é falada
            return
        if not self.tts_ready.wait(120) or not self.kokoro:
            return
        import sounddevice as sd
        stop = self.cur_stop = threading.Event()
        q = queue.Queue()

        def produce():
            pieces, budget, spc = clauses(text), FIRST_CHUNK_S, SECONDS_PER_CHAR
            while pieces and not stop.is_set():
                chunk = next_chunk(pieces, budget, spc)
                try:
                    with self.tts_lock:
                        a = synth(self.kokoro, chunk)
                except Exception as e:
                    log("! tts:", e)
                    continue
                q.put(a)
                spc = len(a) / 24000 / max(1, len(chunk))
                dur = len(a) / 24000                       # o próximo pedaço tem de ficar pronto antes de este acabar de tocar
                budget = max(dur, (dur * 0.95 - SYNTH_FIXED_S) / SYNTH_RATIO)
            q.put(None)

        threading.Thread(target=produce, daemon=True).start()
        halt, lvl = threading.Event(), []                 # halt: encerra as threads de nível ao fim da fala

        def levels(a):
            """Emite ui.level(x) no ritmo da reprodução desta frase (~15/s)."""
            t = time.time()
            for i, x in enumerate(hud.envelope(a, 24000)):
                wait = t + i * hud.LEVEL_FRAME_S - time.time()
                if stop.is_set() or halt.wait(max(0, wait)):
                    return
                self.ui("level", round(x, 2))
        st = None
        try:
            st = open_output(sd)
            self.out = st
            while not stop.is_set():
                try:
                    a = q.get(timeout=0.1)
                except queue.Empty:
                    continue
                if a is None:
                    st.stop()  # espera o fim do que está no buffer
                    break
                lvl.append(threading.Thread(target=levels, args=(a,), daemon=True))
                lvl[-1].start()
                st.write(a.reshape(-1, 1))
        except Exception as e:  # abort() durante o write levanta erro: é a parada manual
            if not stop.is_set():
                log("! áudio:", e)
        finally:
            if st is not None:
                try:
                    st.close()
                except Exception:
                    pass
            self.out = None
            halt.set()
            for t in lvl:
                t.join(1)
            self.ui("level", 0)

    def say(self, text):
        self.deaf = True
        self.set_state("speaking")
        self.speak(text)
        time.sleep(0.3 + (0.8 if OUTPUT_DEVICE else 0))  # deixa o eco da sala morrer (a caixa Bluetooth atrasa ~1 s)
        drain(self.blocks)

    # ---------- transições ----------
    def to_wake(self):
        self.mode, self.rec = "wake", None
        self.claps = ClapDetector()
        if self.oww:
            self.oww.reset()
        drain(self.blocks)
        self.deaf = False
        self.set_state("idle")

    def listen(self, wait):
        if not self.mic_on:
            return self.to_wake()
        drain(self.blocks)
        self.mode, self.rec = "command", Recorder(wait)
        self.deaf = False
        self.set_state("listening")

    def wake(self, reason, greet=False):
        log("despertar:", reason)
        self.deaf = True
        text = GREETING if (greet or not self.visible) else ACK
        if self.feeds.digest_offer_due():  # primeira vez do dia: oferece o Morning Digest
            text, self.offer_pending = feeds.DIGEST_OFFER, True
        if not self.visible:
            self.show()
        self.ui("you", "")
        self.show_answer(text)
        self.say(text)
        self.listen(COMMAND_WAIT_SECONDS)

    def heard_wake(self, block):
        clap = self.claps.feed(block)
        if self.oww and wake_score(self.oww, block) >= WAKE_THRESHOLD:
            return True
        return clap

    def _think(self, text, desk):
        """Pipeline do comando: Claude -> [[SAVE]]. O histórico é um só.
        Devolve (tipo, resposta, id da nota, erro); tipo: "ok" | "digest" | "err". `desk` = veio da mesa (usa a oferta do digest)."""
        self.history.append({"role": "user", "content": text})
        del self.history[:-HISTORY_MAX]
        while self.history and self.history[0]["role"] != "user":
            self.history.pop(0)
        try:
            ctx = self.feeds.context() + "\n" + feeds.DIGEST_RULE
            if desk and self.offer_pending:
                ctx += " You have just offered to read the digest, so a yes from sir (sim, pode, claro...) means " + feeds.DIGEST_TAG + "."
            raw = ask_claude(build_system(self.notes, ctx), self.history)
        except Exception as e:
            log("! claude:", e)
            self.history.pop()
            return "err", ERROR_MSG, None, str(e)
        finally:
            if desk:
                self.offer_pending = False
        if feeds.is_digest_reply(raw):  # [[DIGEST]]: o chamador entrega o digest no lugar da resposta
            self.history.append({"role": "assistant", "content": "(read the morning digest)"})
            return "digest", "", None, ""
        answer, touched = raw, None
        try:
            with self.notes_lock:
                novas = copy.deepcopy(self.notes)
                answer, touched = parse_save(raw, novas)
                if touched:
                    vault.save_all(novas, self.notes)  # só grava as notas que mudaram; as relações ficam
                    self.reload_vault()
        except Exception as e:  # a resposta não pode depender do disco
            log("! cofre:", e)
            touched = None
        answer = answer or "Noted, sir."
        self.history.append({"role": "assistant", "content": answer})
        if touched:
            self.ui("notes", self.notes, touched, self.relations)
        return "ok", answer, touched, ""

    def handle(self, text):
        """Pipeline do comando (voz ou digitado): Claude -> memória -> fala -> follow-up."""
        self.deaf = True
        log("> " + text)
        if self.status != LOADING:
            self.set_status("")
        self.ui("you", text)
        self.ui("answer", "")
        self.set_state("thinking")
        kind, answer, _, err = self._think(text, True)
        if kind == "err":
            self.set_status("Erro do cérebro: " + err[:120])
        elif kind == "digest":
            return self.read_digest()
        log("< " + answer)
        self.show_answer(answer, error=(kind == "err"))
        self.say(answer)
        self.listen(FOLLOW_UP_SECONDS)

    def read_digest(self):
        """Mostra e fala o digest (do cache ou gerado agora) e volta a ouvir."""
        self.deaf = True
        self.set_state("thinking")
        try:
            d = self.feeds.digest()
            text = d["text"]
            self.ui("digest", d)
        except Exception as e:
            log("! digest:", e)
            text = ERROR_MSG
        self.show_answer(text, error=(text == ERROR_MSG))
        self.say(text)
        self.listen(FOLLOW_UP_SECONDS)

    # ---------- laço principal ----------
    def step(self, block):
        if self.reset.is_set():
            self.reset.clear()
            self.to_wake()
        if not self.typed.empty():
            return self.handle(self.typed.get())
        if self.digest_req.is_set():
            self.digest_req.clear()
            return self.read_digest()
        if self.summon.is_set():
            self.summon.clear()
            return self.wake("atalho")
        if self.listen_evt.is_set():
            self.listen_evt.clear()
            return self.listen(COMMAND_WAIT_SECONDS)
        if block is None or not self.mic_on:
            return
        if self.mode == "wake":
            if self.heard_wake(block):
                self.wake("voz/palmas")
            return
        r = self.rec.feed(block)
        if r is None:
            return
        if isinstance(r, str):  # ninguém falou
            return self.to_wake()
        self.deaf = True
        self.set_state("thinking")
        self.stt_ready.wait(120)
        text = transcribe(self.whisper, r) if self.whisper else ""
        if text:
            self.handle(text)
        else:
            self.to_wake()

    def _on_audio(self, indata, frames, t, status):
        if self.mic_on and not self.deaf and not self.quitting:
            self.blocks.put(indata[:, 0].copy())

    def next_block(self):
        try:
            return self.blocks.get(timeout=0.1)
        except queue.Empty:
            return None

    def run(self):
        import sounddevice as sd
        self.page_ready.wait(30)
        if not self.background:
            self.wake("início", greet=True)
        while not self.quitting:
            try:
                with sd.InputStream(samplerate=16000, channels=1, dtype="int16", blocksize=BLOCK, callback=self._on_audio):
                    log("microfone aberto")
                    while not self.quitting and not self.hibernating.is_set():
                        self.step(self.next_block())
                log("microfone fechado")
                drain(self.blocks)
                while self.hibernating.is_set() and not self.quitting:  # só o atalho (Ctrl+Alt+J) acorda: step() trata o summon
                    self.step(None)
                    time.sleep(HIBERNATE_POLL_S / 5)
            except Exception as e:  # microfone ocupado/sumiu: segue só com texto e tenta de novo em 30 s
                log("! microfone:", e)
                self.to_wake()
                end = time.time() + 30
                while time.time() < end and not self.quitting:
                    self.step(None)
                    time.sleep(0.1)

    def start(self):
        """Roda numa thread depois que a janela existe (webview.start(func))."""
        threading.Thread(target=self.load_models, daemon=True).start()
        threading.Thread(target=self.refresher, daemon=True).start()
        threading.Thread(target=self.vault_watcher, daemon=True).start()
        threading.Thread(target=self.hud_loop, daemon=True).start()
        self.run()


class Api:
    """Métodos chamados pela página via window.pywebview.api.* (o prefixo _ esconde o motor da exposição)."""

    def __init__(self, engine):
        self._e = engine

    def get_state(self):
        return self._e.get_state()

    def save_notes(self, notes):
        return self._e.save_notes(notes)

    def send_text(self, text):
        return self._e.send_text(text)

    def set_mic(self, on):
        return self._e.set_mic(on)

    def listen_now(self):
        return self._e.listen_now()

    def stop_speaking(self):
        return self._e.stop_speaking()

    def get_dashboard(self):
        return self._e.get_dashboard()

    def refresh_dashboard(self):
        return self._e.refresh_dashboard()

    def play_digest(self):
        return self._e.play_digest()

    def connect_account(self, provider):
        return self._e.connect_account(provider)

    def get_hud(self):
        return self._e.get_hud()

    def refresh_usage(self):
        return self._e.refresh_usage()

    def open_link(self, url):
        return self._e.open_link(url)

    def hibernate(self):
        threading.Thread(target=self._e.hibernate, daemon=True).start()  # hide() dentro da chamada da página pode travar
        return True

    def quit(self):
        self._e.quit()


# ===================== INSTÂNCIA ÚNICA =====================
def acquire_lock(show_existing):
    """Prende a porta local. Se já tem outra instância, manda ela se mostrar (quando pedido) e devolve None."""
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", LOCK_PORT))
        s.listen(5)
        return s
    except OSError:
        s.close()
        if show_existing:
            try:
                with socket.create_connection(("127.0.0.1", LOCK_PORT), 2) as c:
                    c.sendall(b"show")
            except OSError:
                pass
        return None


def serve_lock(sock, engine):
    while True:
        try:
            conn, _ = sock.accept()
            with conn:
                conn.settimeout(2)
                if conn.recv(16).startswith(b"show"):
                    engine.summon.set()
        except OSError:
            pass


def main():
    background = "--background" in sys.argv
    if background or sys.stdout is None:
        redirect_output()
    lock = acquire_lock(show_existing=not background)
    if not lock:
        return
    import webview
    engine = Engine(background)
    w = webview.create_window("JARVIS", PAGE, js_api=Api(engine), fullscreen=True, hidden=background,
                              background_color="#05060a")
    engine.window = w
    w.events.loaded += engine.on_loaded
    w.events.closing += engine.on_closing
    threading.Thread(target=serve_lock, args=(lock, engine), daemon=True).start()
    for problem in conf.PROBLEMS:
        log("! config:", problem)
    log("Jarvis Desktop iniciado" + (" (segundo plano)" if background else ""))
    webview.start(engine.start, private_mode=False)  # precisa rodar na thread principal
    log("janela encerrada: Jarvis desligado" + ("" if engine.quitting else " (sem o botão ⏻: verifique o WebView2)"))
    os._exit(0)  # encerra as threads de áudio/modelos de uma vez


# ===================== SELFTEST =====================
def selftest():
    global NOTES, REL
    NOTES, REL = conf.load_seed(use_example=True)  # testes só com a semente genérica
    assert len(NOTES) >= 8 and REL
    """Sem microfone, janela ou rede."""
    t0 = time.time()
    kokoro = load_kokoro()

    def say_en(text):
        a, sr = kokoro.create(text, voice=VOICE, speed=VOICE_SPEED, lang="en-gb")
        assert sr == 24000
        return a

    def say_pt(text, lang="pt-br"):
        a, sr = kokoro.create(text, voice="pm_alex", speed=1.0, lang=lang)
        return to16k(a, sr)

    # 1) TTS
    a = synth(kokoro, "Good evening, sir.")
    assert len(a) > 12000 and a.dtype == np.float32 and np.isfinite(a).all(), "Kokoro não gerou áudio"
    assert np.abs(a).max() <= 0.91, "fx deveria normalizar"
    print(f"tts: ok ({len(a) / 24000:.1f} s)")

    # 2) palavra de ativação (inglês e pt-BR)
    oww = load_oww()

    def best(pcm):
        pcm = np.concatenate([np.zeros(8000, np.int16), pcm, np.zeros(16000, np.int16)])
        oww.reset()
        return max(wake_score(oww, pcm[i:i + BLOCK]) for i in range(0, len(pcm) - BLOCK + 1, BLOCK))

    pos, neg = best(to16k(say_en("Hey Jarvis."), 24000)), best(to16k(say_en("Good morning, what time is it?"), 24000))
    print(f"wake EN: 'Hey Jarvis.'={pos:.2f}  negativo={neg:.2f}  (limite {WAKE_THRESHOLD})")
    assert pos >= WAKE_THRESHOLD and neg < WAKE_THRESHOLD, "wake word EN"
    # Só informativo (determinístico, 1 rodada basta): com fonemas pt-br o "Jarvis" sai aportuguesado e o modelo não
    # reconhece; com fonemas en-us (voz de falante PT dizendo "Jarvis" à inglesa) reconhece.
    for lang in ("pt-br", "en-us"):
        for phrase in ["Ei Jarvis.", "Oi Jarvis.", "Jarvis.", "Ei, tudo bem?", "Bom dia, que horas são?"]:
            print(f"wake PT/{lang}: {phrase!r:28} score={best(say_pt(phrase, lang)):.2f}".encode("ascii", "replace").decode())

    # 3) STT em português
    whisper = load_whisper()
    pcm = np.concatenate([say_pt("Jarvis, quanto tempo falta para a entrega do site?"), np.zeros(8000, np.int16)])
    text = transcribe(whisper, pcm)
    print(f"stt: {text!r}")
    assert "entrega" in text.lower(), "Whisper não entendeu o comando em português"

    # 4) VAD / gravador
    rng = np.random.default_rng(0)

    def noise(sec, amp):
        return np.clip(rng.normal(0, amp, int(sec * 16000)) * 32767, -32768, 32767).astype(np.int16)

    def run_rec(sig, wait):
        r = Recorder(wait)
        for i in range(0, len(sig) - BLOCK + 1, BLOCK):
            out = r.feed(sig[i:i + BLOCK])
            if out is not None:
                return r, out
        return r, None

    r, out = run_rec(np.concatenate([noise(.5, .002), noise(1.5, .1), noise(3, .002)]), 10)
    assert isinstance(out, np.ndarray) and abs(r.t - (2.0 + SILENCE_SECONDS)) < 0.2, f"fim por silêncio em {r.t:.2f}s"
    assert len(out) / 16000 > 2.0, "o áudio gravado deveria conter a fala"
    r, out = run_rec(noise(5, .002), 2)
    assert out == "timeout" and abs(r.t - 2) < 0.2, "sem fala deveria dar timeout"
    r, out = run_rec(np.concatenate([noise(.5, .002), noise(20, .1)]), 10)
    assert isinstance(out, np.ndarray) and abs(r.t - (.5 + MAX_COMMAND_SECONDS)) < 0.3, f"limite máximo em {r.t:.2f}s"
    print("vad: ok")

    # 5) memória viva (e cofre do Obsidian, sempre num diretório temporário: nunca o cofre real)
    tdir = tempfile.mkdtemp(prefix="jarvis_selftest_")
    atexit.register(shutil.rmtree, tdir, True)
    vault.VAULT_DIR, vault.NOTES_JSON = os.path.join(tdir, "cofre"), os.path.join(tdir, "notes.json")
    vault.vault_selftest()
    vault.ensure_vault(NOTES, REL, AREA)
    vn, vr = vault.load()
    assert len(vn) == len(NOTES) and {n["id"]: (n["area"], n["title"], n["body"]) for n in vn} == {n["id"]: (n["area"], n["title"], n["body"]) for n in NOTES}, "semente -> cofre -> leitura"
    assert {frozenset(r) for r in vr} == {frozenset(r) for r in REL} and len(vr) == len(REL)
    assert all("## Conexões" in vault._read(n["path"]) for n in vault._load_full()[0])
    notes = copy.deepcopy(NOTES)
    n0 = len(notes)
    clean, tid = parse_save("Noted, sir.\n[[SAVE:projetos|Site Exemplo|Entrega adiada para 24/11]]", notes)
    assert clean == "Noted, sir." and tid == "site_exemplo" and len(notes) == n0
    assert next(n for n in notes if n["id"] == "site_exemplo")["body"] == "Entrega adiada para 24/11"
    clean, tid = parse_save("Very well. [[SAVE:xyz|Café favorito|Gosta de café sem açúcar]]", notes)
    assert clean == "Very well." and len(notes) == n0 + 1 and notes[-1]["area"] == "meta" and notes[-1]["id"] == tid
    assert parse_save("Nothing new.", notes) == ("Nothing new.", None)
    sysmsg = build_system(notes)
    assert "Brazilian Portuguese" in sysmsg and "Café favorito" in sysmsg and "[[SAVE:" in sysmsg and "Carreira" in sysmsg
    assert valid_notes(NOTES) and not valid_notes([{"id": 1}]) and "meta" in AREA
    print("memória: ok")

    # 6) separador de frases
    assert split_sentences("Good evening, sir. It is 3.5 degrees! Shall I proceed?") == \
        ["Good evening, sir.", "It is 3.5 degrees!", "Shall I proceed?"]
    assert split_sentences("  Hello  ") == ["Hello"] and split_sentences("") == []
    print("frases: ok")

    # 7) painel: parse/normalização/triagem/digest (sem rede) e ganchos do motor
    feeds.feeds_selftest()
    assert "LIVE DASHBOARD" in build_system(notes, "LIVE DASHBOARD x") and "LIVE DASHBOARD" not in build_system(notes)
    global ask_claude
    real_ask, spoken, asked = ask_claude, [], []
    tmp = os.path.join(tempfile.gettempdir(), "jarvis_hooks_test.json")
    try:
        eng = Engine()
        eng.feeds = feeds.Feeds(lambda s, t, m="sonnet": "Good morning, sir. Quiet day.", lambda: [], cache_file=tmp)
        eng.say, eng.listen = spoken.append, lambda w: None
        ask_claude = lambda system, msgs, model="sonnet": (asked.append(system), feeds.DIGEST_TAG)[1]  # noqa: E731
        eng.handle("sim, pode ler")
        assert spoken == ["Good morning, sir. Quiet day."] and feeds.DIGEST_TAG in asked[0], (spoken, asked)
        assert eng.get_dashboard()["digest"]["text"].startswith("Good morning") and not eng.open_link("file:///c:/x")
        assert eng.feeds.digest_offer_due() and not eng.feeds.digest_offer_due()
    finally:
        ask_claude = real_ask
        for p in (tmp, tmp + ".tmp"):
            if os.path.exists(p):
                os.remove(p)
    # motor + cofre: SAVE grava no arquivo (relações ficam), editor renomeia (links acompanham), edição externa é percebida
    eng = Engine()
    assert len(eng.notes) == len(NOTES) and len(eng.relations) == len(REL) and eng.get_state()["relations"] == eng.relations
    eng.say, eng.listen = spoken.append, lambda w: None
    ask_claude = lambda system, msgs, model="sonnet": "Noted, sir.\n[[SAVE:projetos|Projeto Alfa|Novo texto]]\n[[SAVE:meta|Café|Sem açúcar]]"  # noqa: E731
    try:
        eng.handle("salve isso")
    finally:
        ask_claude = real_ask
    assert spoken[-1] == "Noted, sir." and len(eng.notes) == len(NOTES) + 1 and len(eng.relations) == len(REL)
    assert next(n for n in eng.notes if n["id"] == "projeto_alfa")["body"] == "Novo texto"
    assert "[[Time Alfa]]" in vault._read(os.path.join(vault.VAULT_DIR, vault.SB, "Projetos", "Projeto Alfa.md"))
    ed = copy.deepcopy(eng.notes)
    next(n for n in ed if n["id"] == "projeto_alfa")["title"] = "Projeto Alfa Plus"
    assert eng.save_notes(ed) and not eng.save_notes([{"id": 1}])
    assert next(n for n in eng.notes if n["id"] == "projeto_alfa")["title"] == "Projeto Alfa Plus" and len(eng.relations) == len(REL)
    assert "[[Projeto Alfa Plus" in vault._read(os.path.join(vault.VAULT_DIR, vault.SB, "Relações", "Time Alfa.md"))
    sig = vault.signature()
    assert sig == eng.vsig
    with open(os.path.join(vault.VAULT_DIR, vault.SB, "Finanças", "Carteira.md"), "a", encoding="utf-8") as f:
        f.write("\nVeja [[IA]]")
    assert vault.signature() != eng.vsig
    eng.reload_vault()
    assert len(eng.relations) == len(REL) + 1 and vault.signature() == eng.vsig
    print("painel: ok")
    hud.hud_selftest()
    apis = [{"name": "MME"}, {"name": "Windows WASAPI"}]
    devs = [{"name": "Microsoft Sound Mapper - Output", "max_output_channels": 2, "hostapi": 0},
            {"name": "Alto-falantes (Realtek High Def", "max_output_channels": 2, "hostapi": 0},
            {"name": "Fones de ouvido (Echo Dot-4XY Stereo)", "max_output_channels": 2, "hostapi": 1},
            {"name": "Fones de ouvido (Echo Dot-4XY Stereo)", "max_output_channels": 2, "hostapi": 0},
            {"name": "Headset (Echo Dot-4XY Hands-Free)", "max_output_channels": 0, "hostapi": 0}]
    assert find_output_device("echo", devs, apis) == 3          # prefere o MME; ignora a entrada Hands-Free
    assert find_output_device("ÉCHO dot", devs, apis) == 3      # sem acento/maiúscula
    assert find_output_device("realtek", devs, apis) == 1 and find_output_device("mapper", devs, apis) is None
    assert find_output_device("jbl", devs, apis) is None and find_output_device("", devs, apis) is None
    print("saída de áudio: ok")

    # hibernar: esconde e marca; acordar (show) limpa a marca e pede atualização imediata
    import types
    global HIBERNATE_POLL_S
    eng.window = None
    assert not eng.hibernating.is_set()
    eng.hibernate()
    assert eng.hibernating.is_set() and not eng.visible
    t_sp = time.time()
    eng.speak("This must not be spoken.")                      # hibernando: volta na hora, sem esperar o Kokoro nem tocar
    assert time.time() - t_sp < 1 and eng.out is None
    # o refresher não busca nada enquanto hiberna e busca assim que acorda
    real_feeds, real_poll, calls = eng.feeds, HIBERNATE_POLL_S, []
    eng.feeds = types.SimpleNamespace(refresh_news_market=lambda: calls.append("n"), refresh_agenda_email=lambda: calls.append("a"))
    HIBERNATE_POLL_S, eng.stt_ready = 0.05, threading.Event()
    eng.stt_ready.set()
    th = threading.Thread(target=eng.refresher, daemon=True)
    th.start()
    time.sleep(0.5)
    assert calls == [], calls                                  # hibernando: nenhuma busca
    eng.show()
    assert not eng.hibernating.is_set() and eng.visible and eng.usage_req.is_set()
    for _ in range(60):
        if len(calls) >= 2:
            break
        time.sleep(0.05)
    assert sorted(calls) == ["a", "n"], calls                  # acordou: busca tudo na hora
    eng.quitting = True
    eng.refresh_now.set()
    th.join(3)
    eng.quitting, eng.feeds, HIBERNATE_POLL_S = False, real_feeds, real_poll
    assert not th.is_alive()
    print("hibernar: ok")
    print(f"({time.time() - t0:.0f} s)")
    print("SELFTEST OK")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
        raise SystemExit(0)
    main()
