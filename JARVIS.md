# jarvis: your local model as a terminal command

## One-time setup

```powershell
cd C:\Users\himan\local-assistant
pip install -r requirements.txt          # rich + prompt_toolkit are the only new ones
python scripts\install_jarvis.py         # creates the `jarvis` command
```

Open a **new** terminal. That is all. `python scripts\install_jarvis.py --uninstall` removes it.

## Every day

```powershell
jarvis                                   # interactive session in the current folder
jarvis "fix the failing test in app.py"  # start with a task
jarvis -p "summarise notes.txt"          # run one task, print the answer, exit
jarvis --yes                             # autonomous: tools run without asking
jarvis --check                           # is the model server ready?
```

You do not have to start anything first. `jarvis` starts Ollama if it is not running, registers `forge` from the
Modelfile on first use, and loads the model into memory in the background.

The tools work in the folder where you typed `jarvis` (change it with `/ws <folder>`).

## What it does

You describe a task; the model picks the tools (run Python, run PowerShell, read/write/list files, search the web,
fetch a page, call a web API), looks at the results and keeps going until it is done. By default it asks before anything that changes
the machine (`run_python`, `run_shell`, `write_file`): answer `y`, `a` (always for this tool this session) or `n`.
Reading and searching never ask. `/auto` switches to fully autonomous.

When an answer contains Python code it runs the code and checks the output before replying (`--no-verify` turns that
off). A reply is stopped after 15 minutes (`--turn-timeout`). Ctrl+C stops the current task and keeps the session.

## Commands inside jarvis

`/help` `/auto` `/mode ask|auto` `/reset` `/clear` `/ws <folder>` `/model <name>` `/stats` `/tools` `/save <file>`
`/remote` `/local` `/backend` (see REMOTE.md) `/exit`. Alt+Enter adds a new line; Up/Down browse history.

| Command | What it does |
|---|---|
| `/context` | how full the model's 8k-token window is, split into system prompt, tool definitions, your messages, replies and tool output |
| `/compact [focus]` | the model summarises the conversation and the summary replaces it. It also happens by itself at 85% full, so long sessions no longer lose their start silently |
| `/usage` (or `/cost`) | tokens read and generated this session, today and in total (kept in `~/.forge/usage.json`) |
| `/undo`, `/retry` | forget the last exchange; or ask the last request again |
| `/skills`, `/skill <name>` | list the how-to notes; force one into your next request |
| `/good`, `/bad`, `/learn on\|off` | keep or forget the last task as an example to copy; turn task memory on or off |
| `/key`, `/key NAME value`, `/key -NAME` | list, save or delete API keys |

## Files: PDF, Excel, Word, slides, charts

Ask in plain words ("make an Excel of my expenses with a total row and a chart", "turn this into a PDF invoice",
"5 slides about X"). The request's words pick a **skill**: a short recipe from `skills/` that is known to run
(reportlab, openpyxl/pandas, python-docx, python-pptx, matplotlib). It is added under your request only when needed,
once per conversation, and the recipe ends by reading the file back, so the model reports what is really in it.
Add your own skills as `.md` files in `~/.forge/skills/` (same header as the built-in ones; yours win on a name clash).

## Live data and web APIs

The `http_request` tool calls any web API (GET runs without asking; POST/PUT/PATCH/DELETE ask first). The `apis` skill
lists free, keyless sources that were checked to work: weather and air quality (Open-Meteo), exchange rates
(Frankfurter), crypto (CoinGecko), stocks and Nifty/Sensex (Yahoo), Indian mutual-fund NAVs (mfapi.in), Wikipedia,
GitHub, Hacker News, holidays, earthquakes, books, arXiv. For an API that needs a key: `/key OPENWEATHER_KEY abc123`.
The model writes `{{OPENWEATHER_KEY}}` and jarvis fills it in, so the key never enters the conversation and is masked
in the output.

## It learns from your tasks (no training)

When a task that used tools ends well, jarvis saves the steps that worked (`~/.forge/experience.jsonl`). When a similar
request comes later, the closest saved example goes with it, so the model copies a working pattern instead of
guessing. `/bad` removes the last one; `/good` keeps one the checks skipped. `python scripts\export_experience.py`
turns them into training rows that `data/build_dataset.py` adds to the next Kaggle run, without the notes, so the next
model does these tasks on its own.

## Troubleshooting

- `'jarvis' is not recognized`: open a new terminal; if it still fails run `python scripts\install_jarvis.py` again.
- `Ollama has no model called 'forge'`: run `ollama create forge -f Modelfile` in the project folder.
- Slow first answer: the model is loading (5 GB). Later answers are quick. Typical speed is 7 to 9 tokens per second.
- To use the cloud GPU as well, see REMOTE.md and start with `jarvis --llm-mode auto`.

## Defaults you can change

- `/default auto` (inside jarvis) makes it always open in autonomous mode; `/default ask` goes back. Saved in `~/.forge/config.json`.
  `jarvis --ask` asks anyway for one session.
- Refusals are off by default: the model answers and warns instead. `jarvis --allow-refusals` brings the old behaviour back.
- Websites drawn by JavaScript (most modern sites) are read through a headless browser when Playwright is installed:
  `pip install playwright` then `python -m playwright install chromium-headless-shell`. Without it only the title and description are read.

## Speed

The model keeps 22 of its layers on your 4 GB GPU (`PARAMETER num_gpu 22` in the Modelfile). Ollama should run with
flash attention and an 8-bit KV cache, with one model loaded at a time. jarvis sets these when it starts Ollama itself, and
they are saved as user environment variables (`OLLAMA_FLASH_ATTENTION=1`, `OLLAMA_KV_CACHE_TYPE=q8_0`,
`OLLAMA_MAX_LOADED_MODELS=1`) for the tray app. Measured on 2026-10-13 with uncached 1.2k-token prompts:

| Setup | Generation | Prompt reading |
|---|---|---|
| plain Ollama, 22 layers | 9.5 tok/s | ~580 tok/s |
| flash attention + q8_0 KV, 22 layers | 10.7 tok/s | ~590 tok/s |
| flash attention + q8_0 KV, 24 layers | 12.6 tok/s | ~136 tok/s (too little VRAM left for prompt batches) |

22 wins overall because tool results and skill notes are new text that must be read every step. If you see CUDA
out-of-memory errors, lower the number and run `ollama create forge -f Modelfile`.

## Voice (it speaks, it does not listen)

Type `/speak` and jarvis reads each answer aloud. Code blocks are skipped ("the code is on your screen"), links and
markdown symbols are dropped, and speech starts after the first sentence is ready. A new question or Ctrl+C stops it.

- `/speak` toggles it (`/speak on`, `/speak off`, `/speak test`); `/nospeak` turns it off; `/mute` stops talking right now but keeps speak mode on; it is remembered for next time. `jarvis --speak` starts with it on.
- `/voice` lists the voices, `/voice 3` picks number 3 and plays a sample, `/voice rate faster|slower|normal|+10%` changes the speed.
- Two engines, switch with `/voice engine edge` or `/voice engine windows`:
  - **edge**: natural neural voices (US, UK, India, Australia; any name from `edge-tts --list-voices` works, e.g. `/voice en-IN-NeerjaNeural`).
    Needs the `edge-tts` package (`/speak` installs it for you the first time) and internet, and the text of each answer is sent to Microsoft's service to make the audio.
  - **windows**: the voices built into Windows. Fully offline, more robotic.
