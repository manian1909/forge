# Forge: a local assistant fine-tuned for coding and the shell

Forge is my own fine-tune of **Qwen3-8B** (or **Qwen3-14B**). It chats, writes and
debugs code, and acts on the machine through tools: it can run Python and
PowerShell, read and write files, search the web and fetch pages. It runs fully
offline on a laptop with a 4 GB GPU, through llama.cpp.

```
you> which 3 processes are using the most memory?
  > run_shell
    Get-Process | Sort-Object WorkingSet -Descending | Select-Object -First 3 Name, ...
forge> Chrome (1.9 GB), Code (1.1 GB) and Teams (640 MB) are using the most memory.
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
