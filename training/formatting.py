"""Turn OpenAI-format conversations into (prompt, completion) training pairs.

Each assistant turn becomes its own example whose prompt is rendered exactly
the way llama-server renders it at inference time: the model's own chat
template, tools included, thinking disabled, generation prompt appended. Loss
is applied only to the completion, so the model learns to produce answers and
tool calls rather than to imitate user messages or tool outputs.
"""

import json


def _normalise(messages):
    """Chat templates expect tool-call arguments as dicts, not JSON strings."""
    out = []
    for m in messages:
        m = dict(m)
        if m.get("tool_calls"):
            calls = []
            for c in m["tool_calls"]:
                fn = dict(c["function"])
                if isinstance(fn.get("arguments"), str):
                    try:
                        fn["arguments"] = json.loads(fn["arguments"] or "{}")
                    except json.JSONDecodeError:
                        return None
                calls.append({**c, "function": fn})
            m["tool_calls"] = calls
        if m.get("content") is None:
            m["content"] = ""
        out.append(m)
    return out


def render(tokenizer, messages, tools, add_generation_prompt):
    return tokenizer.apply_chat_template(
        messages,
        tools=tools or None,
        tokenize=False,
        add_generation_prompt=add_generation_prompt,
        enable_thinking=False,
    )


def to_pairs(tokenizer, row, max_turns=3):
    """Return [(prompt_text, completion_text), ...] for the last ``max_turns``
    assistant turns of a conversation (skipping any that fail to render
    consistently)."""
    messages = _normalise(row["messages"])
    if messages is None:
        return []
    tools = row.get("tools")
    idx = [i for i, m in enumerate(messages) if m["role"] == "assistant"][-max_turns:]
    pairs = []
    for i in idx:
        prompt = render(tokenizer, messages[:i], tools, add_generation_prompt=True)
        full = render(tokenizer, messages[: i + 1], tools, add_generation_prompt=False)
        if not full.startswith(prompt):
            continue
        completion = full[len(prompt):]
        # some templates add a trailing newline after <|im_end|>; keep the end token only
        completion = completion.rstrip("\n")
        if completion.strip():
            pairs.append((prompt, completion))
    return pairs


def tokenize_pair(tokenizer, prompt, completion, max_len):
    p_ids = tokenizer(prompt, add_special_tokens=False)["input_ids"]
    c_ids = tokenizer(completion, add_special_tokens=False)["input_ids"]
    if len(c_ids) >= max_len:
        return None
    # drop the oldest part of the prompt if needed (keeps the system prompt
    # out of the cut only when it fits; very long rows are skipped instead)
    budget = max_len - len(c_ids)
    if len(p_ids) > budget:
        return None
    ids = p_ids + c_ids
    labels = [-100] * len(p_ids) + c_ids
    return {"input_ids": ids, "attention_mask": [1] * len(ids), "labels": labels}
