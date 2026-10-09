# Using free remote APIs with Forge

Forge's weights stay on your laptop. When the local model struggles, `chat.py` can send that turn
to a hosted model over an OpenAI-compatible API. The tools (Python, shell, files) always run locally.

## Option A (recommended): run your own Forge weights on a rented GPU (Modal)

`remote/modal_forge.py` serves your `forge-8b-Q4_K_M.gguf` with llama.cpp on a cloud T4 (16 GB, all layers on the GPU,
32k context) behind an OpenAI-compatible URL. chat.py calls it like any other provider, so you never open a website.

Cost: Modal's free Starter plan includes about $30 of credit per month; a T4 is about $0.59/hour, so roughly 50 GPU hours.
You are billed per second, only while the container is up (it stops after 5 idle minutes). Check modal.com/pricing for current terms.

```powershell
pip install modal
modal setup                                    # browser login, once
modal volume create forge-weights
modal volume put forge-weights models\forge-8b-Q4_K_M.gguf forge.gguf     # 5 GB upload, once
modal secret create forge-api-key FORGE_API_KEY=<any-long-random-string>
modal deploy remote/modal_forge.py             # prints https://<workspace>--forge-serve.modal.run
```

Then put the same random string in `.env` as `FORGE_API_KEY=...`, copy `providers.example.json` to `providers.json`,
and set the first entry's `base_url` to `<printed url>/v1`. Run `python chat.py --mode auto`.

- The first request after idle takes about 30 to 90 seconds (container start plus loading 5 GB); later ones are fast.
- Your prompts go to Modal's servers, not to a model vendor, and the weights stay in your private Modal volume.
- Same model, so style and behaviour match your laptop; you just get a bigger context and more speed.

## Option B: other providers' hosted models (they run *their* models, not your weights)

### Setup (once, about 5 minutes per provider)

1. Make a free account and an API key at one or more of: Groq, OpenRouter, Cerebras, Google AI Studio (Gemini).
2. `copy providers.example.json providers.json` and check the `model` names against each provider's
   current free-model list. Free lists change often.
3. Put the keys in a `.env` file next to `chat.py` (both files are gitignored):
   ```
   GROQ_API_KEY=...
   OPENROUTER_API_KEY=...
   ```
   Providers without a key are skipped.

## Use

```powershell
python chat.py --url http://127.0.0.1:11434/v1 --model forge --mode auto --stats
```

| Mode | Behaviour |
|---|---|
| `local` (default) | Nothing leaves your machine. |
| `auto` | Local first. Goes remote when the chat no longer fits the 8k local window, when the last tool calls keep failing, when local returns an empty reply, or when the local server is down. |
| `remote` | Remote APIs first, local as the last resort. |

In chat: `/remote` forces remote for the next turns, `/local` releases it, `/backend` shows call counts and cool-downs.
A provider that returns 429 (rate limit) is skipped for the time it asks for, and the next provider is tried.
The benchmark accepts the same flags: `python -m bench.run --run forge-hybrid --mode auto`.

## Things to know

- In `auto` and `remote` mode the prompt, file contents and tool output you send are uploaded to that provider.
  Free tiers may use them for training. Keep secrets on `--mode local`.
- Free tiers have request and token caps. This is generous for personal use but not unlimited.
- Hosted models run under each provider's own usage policy. Your local Forge has no such filter.
- The remote model is a different (usually bigger) model than Forge, so answers will differ in style and quality.
