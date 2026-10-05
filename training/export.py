"""Turn a trained LoRA adapter into a GGUF file for llama.cpp.

    # merged model (best runtime speed), needs Unsloth: run on Kaggle
    python training/export.py outputs/forge-qwen3-8b/lora --quant q4_k_m --out /kaggle/tmp/gguf

    # adapter only (small, ~100-300 MB): use with an official base GGUF + --lora
    python training/export.py outputs/forge-qwen3-8b/lora --method lora --out exports

Quantization picks for a 4 GB GPU + 16 GB RAM (smaller = more layers fit on the
GPU = faster, at some quality cost):
    8B:  q4_k_m 5.0 GB (default) | iq4_xs 4.6 GB | q3_k_m 4.1 GB
    14B: q4_k_m 9.0 GB           | q3_k_m 7.3 GB
"""

import argparse
import glob
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


def export_merged(lora_dir, out_dir, quant, max_len):
    from unsloth import FastLanguageModel

    model, tokenizer = FastLanguageModel.from_pretrained(
        model_name=str(lora_dir), max_seq_length=max_len, load_in_4bit=True,
    )
    model.save_pretrained_gguf(str(out_dir), tokenizer, quantization_method=quant)
    found = sorted(glob.glob(str(out_dir) + "*/**/*.gguf", recursive=True)) + \
        sorted(glob.glob(str(out_dir) + "*.gguf"))
    return [f for f in dict.fromkeys(found)]


def export_lora(lora_dir, out_dir):
    """Convert the PEFT adapter with llama.cpp's converter (pure Python)."""
    llama_cpp = Path(os.environ.get("LLAMA_CPP_DIR", "llama.cpp"))
    if not (llama_cpp / "convert_lora_to_gguf.py").exists():
        # the full repo is large; only the converter scripts and gguf-py are needed
        subprocess.run(["git", "clone", "--depth", "1", "--filter=blob:none", "--sparse",
                        "https://github.com/ggml-org/llama.cpp", str(llama_cpp)], check=True)
        subprocess.run(["git", "-C", str(llama_cpp), "sparse-checkout", "set", "--no-cone",
                        "/convert_*.py", "/conversion/", "/gguf-py/"], check=True)
    subprocess.run([sys.executable, "-m", "pip", "install", "-q", "gguf", "sentencepiece"], check=True)
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / "forge-lora.gguf"
    subprocess.run([sys.executable, str(llama_cpp / "convert_lora_to_gguf.py"), str(lora_dir),
                    "--outfile", str(target), "--outtype", "f16"], check=True)
    return [str(target)]


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("lora_dir")
    p.add_argument("--method", choices=["merged", "lora"], default="merged")
    p.add_argument("--quant", default="q4_k_m")
    p.add_argument("--out", default="exports/gguf")
    p.add_argument("--copy-to", help="copy the final .gguf files here (e.g. /kaggle/working)")
    a = p.parse_args()

    lora_dir = Path(a.lora_dir)
    meta_path = lora_dir / "forge_preset.json"
    meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
    out_dir = Path(a.out)

    if a.method == "merged":
        files = export_merged(lora_dir, out_dir, a.quant, meta.get("max_len", 4096))
    else:
        files = export_lora(lora_dir, out_dir)

    if not files:
        sys.exit(f"no .gguf produced under {out_dir}; check the log above")
    for f in files:
        size = os.path.getsize(f) / 1e9
        print(f"{f}  ({size:.2f} GB)")
        if a.copy_to:
            dest = Path(a.copy_to) / f"forge-{meta.get('preset', 'model')}-{Path(f).name}"
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(f, dest)
            print(f"  copied to {dest}")


if __name__ == "__main__":
    main()
