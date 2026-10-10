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
fetch a page), looks at the results and keeps going until it is done. By default it asks before anything that changes
the machine (`run_python`, `run_shell`, `write_file`): answer `y`, `a` (always for this tool this session) or `n`.
Reading and searching never ask. `/auto` switches to fully autonomous.

When an answer contains Python code it runs the code and checks the output before replying (`--no-verify` turns that
off). A reply is stopped after 15 minutes (`--turn-timeout`). Ctrl+C stops the current task and keeps the session.

## Commands inside jarvis

`/help` `/auto` `/mode ask|auto` `/reset` `/clear` `/ws <folder>` `/model <name>` `/stats` `/tools` `/save <file>`
`/remote` `/local` `/backend` (see REMOTE.md) `/exit`. Alt+Enter adds a new line; Up/Down browse history.

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

The model keeps 22 of its layers on your 4 GB GPU (`PARAMETER num_gpu 22` in the Modelfile). Measured on the RTX 3050 laptop:
Ollama's automatic split gave about 10 tok/s, 22 layers about 15 tok/s with roughly 400 MB of VRAM to spare, 26 layers about 16 tok/s
with only 200 MB spare. If you ever see CUDA out-of-memory errors, lower the number and run `ollama create forge -f Modelfile`.

## Voice (it speaks, it does not listen)

Type `/speak` and jarvis reads each answer aloud. Code blocks are skipped ("the code is on your screen"), links and
markdown symbols are dropped, and speech starts after the first sentence is ready. A new question or Ctrl+C stops it.

- `/speak` toggles it (`/speak on`, `/speak off`, `/speak test`); it is remembered for next time. `jarvis --speak` starts with it on.
- `/voice` lists the voices, `/voice 3` picks number 3 and plays a sample, `/voice rate faster|slower|normal|+10%` changes the speed.
- Two engines, switch with `/voice engine edge` or `/voice engine windows`:
  - **edge**: natural neural voices (US, UK, India, Australia; any name from `edge-tts --list-voices` works, e.g. `/voice en-IN-NeerjaNeural`).
    Needs `pip install edge-tts` and internet, and the text of each answer is sent to Microsoft's service to make the audio.
  - **windows**: the voices built into Windows. Fully offline, more robotic.
