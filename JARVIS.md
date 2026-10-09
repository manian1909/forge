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
