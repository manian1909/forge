"""Remove the model's refusal behaviour from its weights ("abliteration"), then export a Q4_K_M GGUF.

A model refuses along one direction in its hidden state. We find that direction (mean activation on requests it
refuses minus mean activation on ordinary requests), pick the layer where cutting it out works best, and then
project it out of every weight matrix that writes into the residual stream. No training involved; the result is
a normal model with the same architecture. Based on "Refusal in LLMs is mediated by a single direction" (2024).

    python kaggle/abliterate.py --base Qwen/Qwen3-8B --adapter /path/to/forge-qwen3-8b/lora --out /tmp/forge-open

Writes <out>/hf (the edited model), <out>/report.json (refusal rates before/after, sample replies) and, unless
--no-gguf is given, <out>/forge-uncensored-<quant>.gguf.
"""

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import torch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "data"))

from refusal_prompts import ADULT_WRITING, EDGY, is_refusal_text  # noqa: E402


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# ------------------------------------------------------------------ math (testable without a model)
def unit(v):
    return v / v.norm().clamp_min(1e-8)


def project_out_rows(weight, r):
    """W has the residual stream on dim 0 (o_proj, down_proj): remove r from every column's output."""
    r = r.to(weight.dtype).to(weight.device)
    return weight - torch.outer(r, r @ weight)


def project_out_embed(weight, r):
    """Embedding table [vocab, d]: remove r from every token vector."""
    r = r.to(weight.dtype).to(weight.device)
    return weight - torch.outer(weight @ r, r)


def refusal_direction(harmful_acts, harmless_acts):
    """Both [n, layers, d]; returns the unit difference of means per layer: [layers, d]."""
    diff = harmful_acts.float().mean(0) - harmless_acts.float().mean(0)
    return diff / diff.norm(dim=-1, keepdim=True).clamp_min(1e-8)


# ------------------------------------------------------------------ model helpers
def chat_prompts(tok, prompts):
    out = []
    for p in prompts:
        msgs = [{"role": "user", "content": p}]
        try:
            text = tok.apply_chat_template(msgs, tokenize=False, add_generation_prompt=True, enable_thinking=False)
        except TypeError:
            text = tok.apply_chat_template(msgs, tokenize=False, add_generation_prompt=True)
        out.append(text)
    return out


@torch.no_grad()
def last_token_states(model, tok, prompts, batch=8):
    """Hidden state of the final prompt token at every layer: [n, layers + 1, d] (float32, CPU)."""
    texts = chat_prompts(tok, prompts)
    rows = []
    for i in range(0, len(texts), batch):
        enc = tok(texts[i:i + batch], return_tensors="pt", padding=True).to(model.device)
        hs = model(**enc, output_hidden_states=True).hidden_states
        rows.append(torch.stack([h[:, -1, :].float().cpu() for h in hs], dim=1))
    return torch.cat(rows)


def layers_of(model):
    return model.model.layers


class Ablation:
    """Temporarily remove direction r from the residual stream with forward hooks (no weight edit)."""

    def __init__(self, model, r):
        self.model, self.r, self.handles = model, r, []

    def _strip(self, x):
        r = self.r.to(x.dtype).to(x.device)
        return x - (x @ r).unsqueeze(-1) * r

    def __enter__(self):
        def hook(_m, _i, out):
            if isinstance(out, tuple):
                return (self._strip(out[0]),) + out[1:]
            return self._strip(out)

        for layer in layers_of(self.model):
            self.handles.append(layer.register_forward_hook(hook))
        self.handles.append(self.model.model.embed_tokens.register_forward_hook(lambda _m, _i, out: self._strip(out)))
        return self

    def __exit__(self, *exc):
        for h in self.handles:
            h.remove()


@torch.no_grad()
def reply_to(model, tok, prompts, max_new_tokens=40, batch=8):
    texts = chat_prompts(tok, prompts)
    outs = []
    for i in range(0, len(texts), batch):
        enc = tok(texts[i:i + batch], return_tensors="pt", padding=True).to(model.device)
        gen = model.generate(**enc, max_new_tokens=max_new_tokens, do_sample=False, pad_token_id=tok.pad_token_id)
        outs += tok.batch_decode(gen[:, enc["input_ids"].shape[1]:], skip_special_tokens=True)
    return outs


def refusal_rate(model, tok, prompts, r=None):
    if r is None:
        replies = reply_to(model, tok, prompts)
    else:
        with Ablation(model, r):
            replies = reply_to(model, tok, prompts)
    return sum(is_refusal_text(x) for x in replies) / max(1, len(prompts)), replies


def load_prompt_sets(n_train, n_test, n_harmless_train=None):
    from datasets import load_dataset

    harmful = load_dataset("mlabonne/harmful_behaviors")
    harmless = load_dataset("mlabonne/harmless_alpaca")
    return (list(harmful["train"]["text"])[:n_train], list(harmful["test"]["text"])[:n_test],
            list(harmless["train"]["text"])[:n_harmless_train or n_train], list(harmless["test"]["text"])[:n_test])


# ------------------------------------------------------------------ main
def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--base", default="Qwen/Qwen3-8B")
    p.add_argument("--adapter", help="LoRA adapter folder to merge first (your Forge fine-tune)")
    p.add_argument("--out", default="/tmp/forge-open")
    p.add_argument("--n-train", type=int, default=128, help="prompts per side used to find the direction")
    p.add_argument("--n-test", type=int, default=48, help="held-out harmful prompts used for the report")
    p.add_argument("--quant", default="q4_k_m")
    p.add_argument("--no-gguf", action="store_true")
    a = p.parse_args()

    from transformers import AutoModelForCausalLM, AutoTokenizer

    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    tok = AutoTokenizer.from_pretrained(a.base)
    tok.padding_side = "left"
    if tok.pad_token is None:
        tok.pad_token = tok.eos_token
    log(f"loading {a.base} in fp16 across the available GPUs")
    model = AutoModelForCausalLM.from_pretrained(a.base, torch_dtype=torch.float16, device_map="auto")
    if a.adapter:
        from peft import PeftModel

        log(f"merging adapter {a.adapter}")
        model = PeftModel.from_pretrained(model, a.adapter).merge_and_unload()
    model.eval()

    harmful_tr, harmful_te, harmless_tr, harmless_te = load_prompt_sets(a.n_train, a.n_test, a.n_train + 250)
    try:
        from persona_prompts import EVAL_ADULT, adult_prompts      # kit copy of data/persona_prompts.py
    except ImportError:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "data"))
        from persona_prompts import EVAL_ADULT, adult_prompts
    adult_tr = [t["prompt"] for t in adult_prompts(240)]            # adult requests are part of what we want it to stop declining
    harmful_tr = harmful_tr + ADULT_WRITING[:6] + adult_tr
    harmless_tr = harmless_tr[: len(harmful_tr)]                    # keep the two sides balanced
    eval_sets = {"harmful_heldout": harmful_te, "adult_writing": ADULT_WRITING[6:] + EVAL_ADULT, "edgy": EDGY, "harmless": harmless_te[:16]}

    log("collecting activations")
    h_acts = last_token_states(model, tok, harmful_tr)
    n_acts = last_token_states(model, tok, harmless_tr)
    dirs = refusal_direction(h_acts, n_acts)            # [layers + 1, d]
    n_layers = len(layers_of(model))

    before = {k: refusal_rate(model, tok, v)[0] for k, v in eval_sets.items()}
    log("refusal rates before editing: " + ", ".join(f"{k} {v:.0%}" for k, v in before.items()))

    # try several layers; keep the direction that removes the most refusals while ordinary requests stay normal
    probe = harmful_te[:24] + ADULT_WRITING[6:] + EDGY[:6]
    best = None
    for layer in range(int(n_layers * 0.3), int(n_layers * 0.75), max(1, n_layers // 12)):
        rate, _ = refusal_rate(model, tok, probe, unit(dirs[layer]))
        sane = refusal_rate(model, tok, harmless_te[:8], unit(dirs[layer]))[0] == 0
        log(f"  layer {layer}: refusals {rate:.0%} (ordinary requests fine: {sane})")
        if sane and (best is None or rate < best[1]):
            best = (layer, rate)
    if best is None:
        raise SystemExit("no layer gave a usable direction; try more --n-train prompts")
    layer, _ = best
    r = unit(dirs[layer]).to(model.device if hasattr(model, "device") else "cpu")
    log(f"using the direction from layer {layer}")

    # bake it into the weights
    with torch.no_grad():
        model.model.embed_tokens.weight.data = project_out_embed(model.model.embed_tokens.weight.data, r)
        for blk in layers_of(model):
            blk.self_attn.o_proj.weight.data = project_out_rows(blk.self_attn.o_proj.weight.data, r)
            blk.mlp.down_proj.weight.data = project_out_rows(blk.mlp.down_proj.weight.data, r)

    samples = {}
    after = {}
    for k, v in eval_sets.items():
        rate, replies = refusal_rate(model, tok, v)
        after[k] = rate
        samples[k] = [{"prompt": q, "reply": x} for q, x in list(zip(v, replies))[:4]]
    log("refusal rates after editing: " + ", ".join(f"{k} {v:.0%}" for k, v in after.items()))
    (out / "report.json").write_text(json.dumps({"layer": layer, "before": before, "after": after, "samples": samples}, indent=2), encoding="utf-8")

    hf = out / "hf"
    model.save_pretrained(hf, safe_serialization=True)
    copy_base_tokenizer(a.base, hf)
    log(f"saved edited model to {hf}")
    del model
    torch.cuda.empty_cache()
    if not a.no_gguf:
        to_gguf(hf, out, a.quant)


def copy_base_tokenizer(base, hf):
    """Use the base model's own tokenizer files. Saving the tokenizer with a new transformers writes
    `extra_special_tokens` as a list, which llama.cpp's converter then fails to load."""
    from huggingface_hub import hf_hub_download

    for name in ("tokenizer.json", "tokenizer_config.json", "vocab.json", "merges.txt", "generation_config.json"):
        try:
            hf_hub_download(base, name, local_dir=hf)
        except Exception as e:  # noqa: BLE001 - optional files may not exist
            log(f"  {name}: {type(e).__name__}")
    cfg = Path(hf) / "tokenizer_config.json"
    if cfg.exists():  # older transformers cannot read this field as a list
        data = json.loads(cfg.read_text(encoding="utf-8"))
        if isinstance(data.get("extra_special_tokens"), list):
            data["extra_special_tokens"] = {}
            cfg.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def find_quantize(root):
    """Unsloth's prebuilt llama.cpp has llama-quantize; otherwise build only that tool."""
    import shutil

    for cand in (shutil.which("llama-quantize"), "/root/.unsloth/llama.cpp/llama-quantize",
                 "/root/.unsloth/llama.cpp/build/bin/llama-quantize", str(root / "build/bin/llama-quantize")):
        if cand and Path(cand).exists():
            return Path(cand)
    return None


def to_gguf(hf, out, quant):
    """HF folder -> f16 GGUF -> quantised GGUF using llama.cpp (cloned, and llama-quantize built, on first use)."""
    root = Path(os.environ.get("LLAMA_CPP_DIR", out / "llama.cpp"))
    if not root.exists():
        subprocess.run(["git", "clone", "--depth", "1", "https://github.com/ggml-org/llama.cpp", str(root)], check=True)
    subprocess.run([sys.executable, "-m", "pip", "install", "-q", "-r", str(root / "requirements/requirements-convert_hf_to_gguf.txt")], check=False)
    f16 = out / "forge-uncensored-f16.gguf"
    subprocess.run([sys.executable, str(root / "convert_hf_to_gguf.py"), str(hf), "--outfile", str(f16), "--outtype", "f16"], check=True)
    target = out / f"forge-uncensored-{quant.upper()}.gguf"
    quantize = find_quantize(root) or root / "build/bin/llama-quantize"
    if not quantize.exists():
        try:
            subprocess.run(["cmake", "-S", str(root), "-B", str(root / "build"), "-DGGML_CUDA=OFF", "-DLLAMA_CURL=OFF"], check=True)
            subprocess.run(["cmake", "--build", str(root / "build"), "--target", "llama-quantize", "-j", "4"], check=True)
        except (subprocess.CalledProcessError, FileNotFoundError):
            log("could not build llama-quantize; writing a q8_0 file instead (bigger, same quality)")
            q8 = out / "forge-uncensored-Q8_0.gguf"
            subprocess.run([sys.executable, str(root / "convert_hf_to_gguf.py"), str(hf), "--outfile", str(q8), "--outtype", "q8_0"], check=True)
            f16.unlink(missing_ok=True)
            return
    subprocess.run([str(quantize), str(f16), str(target), quant.upper()], check=True)
    f16.unlink(missing_ok=True)
    log(f"wrote {target} ({target.stat().st_size / 1e9:.2f} GB)")


if __name__ == "__main__":
    main()
