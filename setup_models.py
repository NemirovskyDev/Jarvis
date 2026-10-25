"""Baixa os modelos de voz do Jarvis (cerca de 480 MB no total). Rode uma vez depois de instalar as dependências.

    python setup_models.py             baixa o que faltar
    python setup_models.py --check     só mostra o que está pronto e o que falta (não baixa nada)
    python setup_models.py --selftest  testa o download atômico contra um servidor local (sem internet)

O que é baixado:
  models/kokoro-v1.0.onnx + voices-v1.0.bin   voz do Jarvis (Kokoro, GitHub: thewh1teagle/kokoro-onnx)
  models/whisper/                             reconhecimento da fala em português (faster-whisper "base", via Hugging Face)
  palavra de ativação "Hey Jarvis"            modelo do openWakeWord (vai para a pasta do próprio pacote)
"""
import functools
import hashlib
import http.server
import os
import sys
import tempfile
import threading
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
MODELS = os.path.join(HERE, "models")
KOKORO_BASE = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/"
# (nome, tamanho mínimo em bytes, SHA-256). O hash trava a versão que foi testada: se o arquivo na internet mudar ou for adulterado, o download é recusado.
KOKORO_FILES = [
    ("kokoro-v1.0.onnx", 300_000_000, "7d5df8ecf7d4b1878015a32686053fd0eebe2bc377234608764cc0ef3636a6c5"),
    ("voices-v1.0.bin", 20_000_000, "bca610b8308e8d99f32e6fe4197e7ec01679264efed0cac9140fe9c29f1fbf7d"),
]
WHISPER_MODEL = "base"  # igual ao WHISPER_MODEL do jarvis_desktop.py
WAKE_MODEL_FILE = "hey_jarvis_v0.1.onnx"


def have_file(path, min_size):
    return os.path.isfile(path) and os.path.getsize(path) >= min_size


def download(url, dest, min_size, sha256=None, opener=urllib.request.urlopen):
    """Baixa para dest + '.part' e só então renomeia: uma queda no meio ou um arquivo diferente do esperado (tamanho/SHA-256) nunca ficam com o nome final."""
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    part = dest + ".part"
    try:
        with opener(url, timeout=60) as r, open(part, "wb") as f:
            total = int(r.headers.get("Content-Length") or 0)
            got, shown, digest = 0, -1, hashlib.sha256()
            while True:
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                f.write(chunk)
                digest.update(chunk)
                got += len(chunk)
                if total and got * 100 // total // 10 != shown:  # um aviso a cada 10%
                    shown = got * 100 // total // 10
                    print(f"  {os.path.basename(dest)}: {got * 100 // total}%", flush=True)
        if total and got != total:
            raise OSError(f"download incompleto ({got} de {total} bytes)")
        if got < min_size:
            raise OSError(f"arquivo menor que o esperado ({got} bytes; mínimo {min_size})")
        if sha256 and digest.hexdigest() != sha256:
            raise OSError("SHA-256 diferente do esperado: arquivo recusado (o conteúdo na internet mudou ou foi adulterado)")
        os.replace(part, dest)
    finally:
        if os.path.exists(part):
            os.remove(part)


def wake_path():
    import openwakeword
    return os.path.join(os.path.dirname(openwakeword.__file__), "resources", "models", WAKE_MODEL_FILE)


def whisper_ready():
    root = os.path.join(MODELS, "whisper")
    return os.path.isdir(root) and any(f.endswith(".bin") for _, _, fs in os.walk(root) for f in fs)


def status():
    """[(descrição, pronto?)]"""
    out = [(name, have_file(os.path.join(MODELS, name), size)) for name, size, _ in KOKORO_FILES]
    out.append(("whisper (" + WHISPER_MODEL + ")", whisper_ready()))
    try:
        out.append(("openWakeWord (Hey Jarvis)", os.path.exists(wake_path())))
    except ImportError:
        out.append(("openWakeWord (pacote não instalado: pip install -r requirements.txt)", False))
    return out


def main(argv):
    if "--selftest" in argv:
        return selftest()
    rows = status()
    for name, ok in rows:
        print(("[ok]     " if ok else "[falta]  ") + name)
    if "--check" in argv:
        return 0 if all(ok for _, ok in rows) else 1
    todo = dict(rows)
    for name, size, digest in KOKORO_FILES:
        if not todo[name]:
            print(f"Baixando {name}...")
            download(KOKORO_BASE + name, os.path.join(MODELS, name), size, digest)
    try:
        if not todo["whisper (" + WHISPER_MODEL + ")"]:
            print("Baixando o modelo de reconhecimento de fala (Whisper)...")
            from faster_whisper import WhisperModel
            WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8", download_root=os.path.join(MODELS, "whisper"))
        if not todo.get("openWakeWord (Hey Jarvis)"):
            print('Baixando o modelo da palavra de ativação "Hey Jarvis"...')
            import openwakeword.utils as u
            u.download_models(["hey_jarvis"])
    except ImportError as e:
        print(f"Falta instalar uma dependência ({e.name}). Rode primeiro: pip install -r requirements.txt")
        return 1
    ok = all(ok for _, ok in status())
    print("Tudo pronto." if ok else "Algo ficou faltando: rode de novo com --check.")
    return 0 if ok else 1


def selftest():
    """Servidor HTTP local: arquivo completo é renomeado; arquivo curto demais e queda no meio não deixam rastro."""
    with tempfile.TemporaryDirectory() as t:
        src, dst = os.path.join(t, "src"), os.path.join(t, "dst")
        os.makedirs(src)
        open(os.path.join(src, "ok.bin"), "wb").write(os.urandom(3_000_000))
        open(os.path.join(src, "small.bin"), "wb").write(b"x" * 100)
        class Quiet(http.server.SimpleHTTPRequestHandler):
            def log_message(self, *a):  # sem barulho no teste
                pass
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=src))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{srv.server_address[1]}/"
        try:
            download(base + "ok.bin", os.path.join(dst, "ok.bin"), 1_000_000)
            assert os.path.getsize(os.path.join(dst, "ok.bin")) == 3_000_000 and not os.path.exists(os.path.join(dst, "ok.bin.part"))
            good = hashlib.sha256(open(os.path.join(src, "ok.bin"), "rb").read()).hexdigest()
            download(base + "ok.bin", os.path.join(dst, "ok2.bin"), 1_000_000, sha256=good)  # hash certo: aceito
            assert os.path.exists(os.path.join(dst, "ok2.bin"))
            try:
                download(base + "ok.bin", os.path.join(dst, "bad.bin"), 1_000_000, sha256="0" * 64)  # hash errado: recusado
                raise AssertionError("hash errado deveria falhar")
            except OSError as e:
                assert "SHA-256" in str(e)
            assert not os.path.exists(os.path.join(dst, "bad.bin")) and not os.path.exists(os.path.join(dst, "bad.bin.part"))
            for name, why in (("small.bin", "pequeno"), ("nao_existe.bin", "404")):
                try:
                    download(base + name, os.path.join(dst, name), 1_000_000)
                    raise AssertionError("deveria falhar: " + why)
                except OSError:
                    pass  # urllib.error.HTTPError também é OSError
                assert not os.path.exists(os.path.join(dst, name)) and not os.path.exists(os.path.join(dst, name + ".part")), why

            class Cut:  # resposta que anuncia 2 MB mas entrega só 1 MB
                headers = {"Content-Length": "2000000"}
                def __init__(self): self.sent = False
                def __enter__(self): return self
                def __exit__(self, *a): return False
                def read(self, n):
                    if self.sent:
                        return b""
                    self.sent = True
                    return b"y" * 1_000_000
            try:
                download("x://cut", os.path.join(dst, "cut.bin"), 10, opener=lambda url, timeout: Cut())
                raise AssertionError("queda no meio deveria falhar")
            except OSError:
                pass
            assert not os.path.exists(os.path.join(dst, "cut.bin")) and not os.path.exists(os.path.join(dst, "cut.bin.part"))
        finally:
            srv.shutdown()
    print("setup_models: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
