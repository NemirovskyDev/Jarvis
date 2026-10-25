# Jarvis

Assistente de voz pessoal para Windows, no estilo do JARVIS do Homem de Ferro. Você chama por voz, ele responde falando e mostra um painel azul com clima, agenda, e-mails, cotações, uso do plano do Claude e o seu "Second Brain" (notas ligadas em grafo).

- **Chamar:** diga "Hey Jarvis", bata duas palmas, clique na esfera ou digite.
- **Entende português, responde em inglês britânico**, com a voz Kokoro (`bm_george`), como um mordomo formal.
- **Roda no seu PC.** A fala e o reconhecimento de voz são locais. O raciocínio vem do **Claude Code** (`claude -p`), usando a sua conta do Claude, sem chave de API.
- **Second Brain em Markdown:** as notas ficam num cofre do [Obsidian](https://obsidian.md) e o Jarvis lê todas antes de responder. O que você disser de duradouro ("comecei a treinar três vezes por semana") vira nota.

## O que você precisa

| Item | Detalhe |
|---|---|
| Sistema | Windows 10 ou 11 (desenvolvido e testado no Windows 10). Linux e macOS não foram testados. |
| Python | 3.13 (a versão testada; outras versões recentes provavelmente funcionam, mas não foram testadas) |
| Claude Code | instalado e logado: `npm install -g @anthropic-ai/claude-code`, depois `claude auth login`. Precisa de uma conta Claude (o Jarvis gasta um pouco do limite do seu plano) |
| Microfone e caixa de som | qualquer um; há opção para escolher a saída (ex.: uma caixa Bluetooth) |
| Espaço em disco | cerca de 480 MB para os modelos de voz |

## Instalação

```bash
git clone https://github.com/NemirovskyDev/Jarvis.git
cd Jarvis
pip install -r requirements.txt
python setup_models.py        # baixa os modelos de voz (uma vez só)
python jarvis_desktop.py      # abre o Jarvis em tela cheia
```

`python setup_models.py --check` mostra o que já está baixado. Os arquivos da voz têm o SHA-256 conferido no download; se o conteúdo na internet mudar, o arquivo é recusado. Para abrir sem a janela do console, use `pythonw jarvis_desktop.py`.

### Atalho na Área de Trabalho (opcional)

No PowerShell, dentro da pasta do projeto. Cria o ícone **Jarvis** e a tecla **Ctrl+Alt+J**:

```powershell
$ws = New-Object -ComObject WScript.Shell
$l = $ws.CreateShortcut("$([Environment]::GetFolderPath('Desktop'))\Jarvis.lnk")
$l.TargetPath = (Get-Command pythonw).Source
$l.Arguments = '"' + "$PWD\jarvis_desktop.py" + '"'
$l.WorkingDirectory = "$PWD"
$l.Hotkey = "CTRL+ALT+J"
$l.Save()
```

## Como usar

1. Ao abrir, o Jarvis cumprimenta e já escuta. Fale em português.
2. Depois de cada resposta ele continua ouvindo por alguns segundos; passado isso, volte a chamar por "Hey Jarvis" ou duas palmas.
3. Botões no canto superior: **⏾** hiberna (fecha a tela, desliga o microfone e pausa as buscas em segundo plano; volta por Ctrl+Alt+J ou pelo ícone) e **⏻** desliga por completo. O **X** da janela também hiberna.
4. Para o "Hey Jarvis" funcionar, pronuncie "Jarvis" à moda inglesa. Se não pegar, use as palmas.

Na primeira vez do dia ele oferece o **Morning Digest**, um resumo falado de agenda, e-mails de ação, notícias, cotações e dos seus prazos.

## Configuração pessoal

Seus dados ficam em arquivos que o git ignora. Copie os exemplos e edite:

```bash
copy config.example.json config.json
copy seed.example.json seed.json
```

| Arquivo | Para quê |
|---|---|
| `config.json` | seu nome (`owner_name`), cidade do clima (`location`), ativos acompanhados (`tickers`, símbolos do Yahoo Finance), tópicos de notícia (`news_topics`) e `forwarded_domain` (e-mails ao Gmail cujo destinatário contém esse domínio aparecem como uma segunda conta, útil para e-mail da faculdade encaminhado) |
| `seed.json` | notas iniciais do Second Brain (id, área, título, texto) e as ligações entre elas. Só são usadas para criar o cofre na primeira vez |

Sem esses arquivos o Jarvis usa padrões genéricos. Um valor inválido nunca derruba o Jarvis: ele usa o padrão e registra o aviso no `jarvis.log`.

Ajustes de comportamento ficam no começo de `jarvis_desktop.py`: `VOICE`, `VOICE_SPEED`, `VOICE_FX`, `OUTPUT_DEVICE` (nome da caixa de som, ex.: `"Echo"`), `SHOW_ANSWER_TEXT`, `WAKE_THRESHOLD`, `WHISPER_MODEL`, `COMMAND_LANGUAGE`, `EFFORT` (esforço do Claude) e os limites das palmas (`CLAP_*`).

## Second Brain no Obsidian

- O cofre fica em `Documentos\Jarvis Brain` (mude `VAULT_DIR` em `jarvis_vault.py`). Ele é criado na primeira execução com as notas do `seed.json`.
- Cada nota é um arquivo `Second Brain/<Área>/<Título>.md`. Os `[[links]]` entre notas viram as linhas do grafo.
- Abra a pasta como cofre no Obsidian. Edições feitas lá aparecem no Jarvis em cerca de 3 segundos, e o contrário também.
- Notas apagadas vão para `.trash/` do cofre. O Jarvis nunca escreve em `.obsidian/`.

## Painel (opcional)

- **Clima** (Open-Meteo), **cotações** (Yahoo Finance) e **notícias** (Google Notícias) funcionam sem conta nem chave.
- **Gmail e Google Agenda:** o Jarvis lê pelos conectores do Claude. Conecte **Gmail** e **Google Calendar** em claude.ai → Configurações → Conectores. Ele só recebe permissão para as ferramentas de **leitura**; enviar, apagar e editar ficam bloqueados.
- **Uso do plano do Claude:** lido do próprio Claude Code, a cada 10 minutos.

## Privacidade: o que sai do seu PC

| Para onde | O quê |
|---|---|
| Claude (via Claude Code) | o que você pede, as suas notas do Second Brain, o contexto do painel (agenda, e-mails de ação, manchetes, cotações) e as notas recentes do cofre |
| Open-Meteo, Yahoo Finance, Google Notícias | coordenadas da cidade, símbolos dos ativos e termos de busca |
| Hugging Face e GitHub | só durante o `setup_models.py`, para baixar os modelos |

Áudio do microfone, transcrições e o cofre ficam no PC. Não guarde senhas nem dados sensíveis nas notas, porque elas vão para o Claude em cada pergunta.

## Testes

```bash
python jarvis_desktop.py --selftest   # completo (usa os modelos; cerca de 1 minuto)
python jarvis_config.py               # configuração
python jarvis_vault.py                # cofre do Obsidian
python jarvis_feeds.py                # agenda, e-mails, notícias, cotações e digest
python setup_models.py --selftest     # download dos modelos
```

Os testes usam uma semente genérica e uma pasta temporária no lugar do cofre do Obsidian: o seu cofre real não é tocado.

## Se algo não funcionar

| Sintoma | Causa provável |
|---|---|
| Ele diz que não alcança o "núcleo cognitivo" | o Claude Code perdeu o login: rode `claude auth login` e confira com `claude auth status` |
| O Jarvis não ouve | confira a permissão de microfone do Windows; o `jarvis.log` mostra "microfone aberto" |
| Fala pelo som do notebook em vez da caixa | a caixa não está pareada/conectada: ele volta para a saída padrão e avisa no log |
| Painel mostra "não configurado" ou "erro" no Google | conecte os conectores Gmail e Google Calendar no claude.ai |
| `setup_models.py` reclama de pacote | rode `pip install -r requirements.txt` antes |

## Estrutura

```
jarvis_desktop.py   motor: janela, áudio, palavra de ativação, fala, Claude, hibernação
desktop.html        interface (esfera holográfica, grafo, painel)
jarvis_feeds.py     agenda, e-mails, notícias, cotações e Morning Digest
jarvis_hud.py       clima, sistema, uso do plano e nível de voz
jarvis_vault.py     Second Brain em arquivos Markdown (Obsidian)
jarvis_config.py    leitura do config.json e do seed.json
setup_models.py     download dos modelos
```

## Licença

Ainda não definida.
