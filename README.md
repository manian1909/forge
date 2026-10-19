# Forge: a coding and shell assistant I fine-tuned and run on a 4 GB laptop GPU

Forge is **Qwen3-8B fine-tuned with QLoRA** on chat, code, function-calling and
self-generated tool-use data, exported to a 5 GB `Q4_K_M` GGUF. Around it sits
**Jarvis**, a terminal agent that gives the model hands: it runs Python and
PowerShell, reads and writes files (including Word, Excel and PDF), searches
the web, calls APIs, and can be driven by voice. All of it runs offline on an
i5-12450H laptop with an RTX 3050 (4 GB VRAM).

```
you> which 3 processes are using the most memory?
  > run_shell
    Get-Process | Sort-Object WorkingSet -Descending | Select-Object -First 3 Name, ...
forge> Chrome (1.9 GB), Code (1.1 GB) and Teams (640 MB) are using the most memory.
```

## Numbers

| | |
|---|---|
| Base model | Qwen3-8B (Apache-2.0), 4-bit QLoRA, rank 32, context 4096 for training |
| Training data | 9,439 examples / 8.67M tokens: UltraChat, Magicoder, Hermes function-calling, 510 verified tool-use runs, identity rows |
| Training run | 590 steps on one free Kaggle T4, about 7.4 h, loss 1.40 to about 0.7 |
| Export | merged, `Q4_K_M` GGUF, 5.03 GB |
| Speed on the laptop | about 9 tok/s generation, 22 of 36 layers on the 4 GB GPU, 8k context |
| Agent | 15 tools, 364 automated tests, 10 task skills (PDF, Excel, Word, slides, charts, APIs, lectures...) |
| Code | about 14k lines of Python, 99 commits |

### Benchmarks

I wrote a crash-safe benchmark harness (`bench/`) that runs real tasks with real
tools in a scratch folder and grades the *outcome* (unit tests pass, the file has the
right content, the number is right), not the wording.

| Run | Tasks | Pass rate |
|---|---|---|
| Forge, first measurement | 76 | 71% |
| Forge after prompt and tool hardening | 76 | 78% |
| Forge with code checks and loop guards | 76 | **88%** (67/76) |
| Stock `qwen3:8b`, same harness | 76 | 88% (67/76) |
| **Forge, held-out set (never tuned on)** | 34 | **88%** (30/34) |

Held-out breakdown: easy 9/9, medium 16/17, hard 5/8. Strong on math and logic,
Python tool use, PowerShell, knowledge questions and robustness (it refuses to
follow instructions hidden in a file it reads, and says so when a folder is missing instead of
inventing files). Weak spots, listed honestly: exact-count instructions ("exactly 7 words"),
hard multi-step coding, and appending to files without overwriting.

On the 76-task set Forge matches the stock model on score and is faster:
about 7 tok/s against 5 tok/s and 54 minutes of compute against 77, because the
fine-tune answers more directly and uses fewer tokens. The point of the project was owning the whole
pipeline (data, training, export, agent, evaluation), and I report the parity plainly instead of
claiming a win the numbers don't show.

Full reports (open the HTML files in a browser):

- [Held-out benchmark](docs/benchmarks/heldout_forge.html) ([markdown](docs/benchmarks/heldout_forge.md))
- [Main benchmark, Forge](docs/benchmarks/main76_forge.html) and [stock Qwen3-8B](docs/benchmarks/main76_qwen3-8b-stock.html)
- [Jarvis system report](docs/reports/Jarvis_Report_local.html): how one request flows through the agent, with diagrams (works offline; [lighter version](docs/reports/Jarvis_Report.html) loads diagrams from a CDN)
- [Forge project report (PDF)](docs/reports/Forge_Report.pdf)

## What it can do

- **Code**: writes functions, runs them, reads the traceback and fixes them; checks code in its own answer before replying.
- **Shell and files**: PowerShell, Python, read/write/list files, `.docx` read and write, PDF reading with figures described by a small local vision model.
- **Office work**: skills for Excel (formulas, charts), Word, slides and PDFs, matched automatically to the request.
- **Web**: search, fetch pages, call REST APIs with keys kept out of the transcript.
- **Memory**: remembers tool turns that worked and recalls the closest one; `/compact` summarises long chats; `/undo`, `/retry`.
- **Voice**: say "Jarvis" to wake it; quick commands (volume, next song, open things) skip the model entirely.
- **Standby worker**: background jobs that pause when a game starts or RAM runs low, unload the model, and resume from saved state.
- **Safe by default**: commands run as a low-integrity Windows process that can only write inside one folder; risky tools ask first.

## Get the weights

GitHub does not host files this large, so the GGUF lives on Hugging Face:

```powershell
pip install huggingface_hub
python scripts\download_weights.py          # saves modelsorge-8b-Q4_K_M.gguf (5 GB)
ollama create forge -f Modelfile
jarvis --check
```

## How it's built

```
 seed_tasks.json ──► teacher model expands them into ~600 varied tasks
                         │
                         ▼
 generate_agentic.py: teacher solves each task with the REAL tools in a sandbox
                      (same system prompt and tool schemas as at runtime),
                      then quality filters keep only clean, successful runs
                         │
 build_dataset.py ◄──────┘  + UltraChat (chat) + Magicoder (code)
        │                   + Hermes function-calling + identity examples
        ▼
 finetune.py (Kaggle T4): 4-bit QLoRA via Unsloth, loss only on assistant turns,
        │                 every turn rendered exactly like llama-server renders it
        ▼
 export.py: merge + quantize to GGUF (Q4_K_M, 5 GB) ──► llama.cpp on the laptop
```

| Part | File |
|---|---|
| Tools (python, shell, files, web) | `assistant/tools.py` |
| Agent loop (tool calls → results → answer) | `assistant/agent.py` |
| Streaming client for any OpenAI-compatible server | `assistant/llm.py` |
| Terminal chat UI | `chat.py` |
| Teacher-driven tool-use data | `data/generate_agentic.py` |
| Training mix | `data/build_dataset.py` |
| QLoRA training (8B / 14B presets) | `training/finetune.py` |
| GGUF export | `training/export.py` |
| Kaggle notebook | `kaggle/forge_finetune.ipynb` |
| Local server setup | `scripts/setup_llamacpp.ps1`, `scripts/serve.ps1` |

## Speed on a 4 GB GPU

An 8B model doesn't fit in 4 GB of VRAM, so llama.cpp places as many layers as
fit on the GPU and runs the rest on the CPU. `scripts/serve.ps1` turns on these
settings:

- **Q4_K_M quantization**: 16-bit weights become about 4.8 bits per weight
  (16 GB down to 5 GB).
- **8-bit KV cache and flash attention**: about half the memory per token of
  context, which leaves more VRAM for model layers.
- **Thinking disabled** (`--reasoning-budget 0`), and the training data teaches
  short answers, so fewer tokens are generated per reply.
- **Prompt cache reuse**: the system prompt and tool schemas are only processed
  once per chat.
- **Optional speculative decoding** (`-Draft models\Qwen3-0.6B-Q8_0.gguf`): a
  tiny draft model proposes tokens and the 8B model verifies them in one pass.
  This mostly helps on code.

For more speed, export with `--quant iq4_xs` or `q3_k_m` so more layers fit on
the GPU. The 14B model works too, but at Q4 it's 9 GB, so most of it runs on the
CPU and it is noticeably slower.


## Usage

### 1. Generate the tool-use data (local, needs Ollama + `qwen3:8b`)

```bash
pip install -r requirements.txt
ollama pull qwen3:8b
python data/generate_agentic.py expand --per-category 100
python data/generate_agentic.py run          # resumable; ~30 s per task
git add data/out/agentic.jsonl && git commit -m "Add generated tool-use data" && git push
```

### 2. Train on Kaggle

Import `kaggle/forge_finetune.ipynb` into Kaggle and set **GPU T4**,
**Internet on**. Choose `PRESET = "qwen3-8b"` or `"qwen3-14b"` and run all
cells. When it finishes, download the `.gguf` from the Output panel.

### 3. Run locally

Put the downloaded `.gguf` in `models\` as `forge-8b-Q4_K_M.gguf`. The
quickest way to run it is with Ollama as the engine (the weights are still
your own file):

```powershell
ollama create forge -f Modelfile                                   # once
python chat.py --url http://127.0.0.1:11434/v1 --model forge --stats
```

Or with llama.cpp's server, which lets you tune GPU offload and the KV cache:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\setup_llamacpp.ps1   # once
# put the downloaded .gguf in models\
powershell -ExecutionPolicy Bypass -File scripts\serve.ps1
python chat.py --stats        # in a second terminal
```

Before running Python, shell or file-writing tools, the chat asks `run …? [y/N]`.
Use `/auto` (or `--auto`) to skip the prompt, `/reset` to clear the conversation
and `/exit` to quit.

## Data sources

| Dataset | Use | License |
|---|---|---|
| [HuggingFaceH4/ultrachat_200k](https://huggingface.co/datasets/HuggingFaceH4/ultrachat_200k) | general chat | MIT |
| [ise-uiuc/Magicoder-OSS-Instruct-75K](https://huggingface.co/datasets/ise-uiuc/Magicoder-OSS-Instruct-75K) | coding | MIT |
| [NousResearch/hermes-function-calling-v1](https://huggingface.co/datasets/NousResearch/hermes-function-calling-v1) | function calling | Apache-2.0 |
| Qwen3-8B teacher via Ollama | tool-use trajectories with real execution | Apache-2.0 (model) |

The base models, Qwen3-8B and Qwen3-14B, are Apache-2.0 licensed.
