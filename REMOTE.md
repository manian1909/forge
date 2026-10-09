# Using free remote APIs with Forge

Forge's weights stay on your laptop. When the local model struggles, `chat.py` can send that turn
to a hosted model over an OpenAI-compatible API. The tools (Python, shell, files) always run locally.

## Setup (once, about 5 minutes per provider)

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
