"""One-shot Kaggle pipeline: install -> build data -> fine-tune -> export GGUF.

Run from a Kaggle notebook cell (see KAGGLE.md):

    !python /kaggle/input/<dataset>/forge-kit/kaggle/run_all.py --preset qwen3-8b

Everything it produces lands in /kaggle/working/ so it shows up in the
notebook's Output tab:
    forge-<preset>-*.gguf        the model to run locally
    forge-<preset>/lora/         the LoRA adapter (small backup)
    forge-<preset>/checkpoints/  only kept if training stopped on the time limit
    run_all.log                  full log
"""

import argparse
import glob
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

KIT = Path(__file__).resolve().parents[1]          # read-only copy under /kaggle/input
WORK = Path(os.environ.get("FORGE_WORK", "/kaggle/working"))
CODE = WORK / "forge-kit"                          # writable copy we run from
TMP = Path(os.environ.get("FORGE_TMP", "/tmp/forge-export"))                    # big intermediate files (not in Output)
LOG = WORK / "run_all.log"

# leave time for export inside Kaggle's 12 h session limit
DEFAULT_HOURS = {"qwen3-8b": 10.0, "qwen3-14b": 9.0, "qwen3-14b-open": 10.0, "qwen3-0.6b": 1.0}
# share of the public data used by default, sized so one epoch fits a single
# 12 h session on a T4 (8B trains at roughly 330 tokens/s; 14B at about half that)
DEFAULT_SCALE = {"qwen3-8b": 0.6, "qwen3-14b": 0.3, "qwen3-14b-open": 0.3, "qwen3-0.6b": 1.0}


def log(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def run(cmd, check=True, env=None):
    log("$ " + " ".join(str(c) for c in cmd))
    proc = subprocess.Popen([str(c) for c in cmd], cwd=CODE, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                            errors="replace", env=env, bufsize=1)
    with open(LOG, "a", encoding="utf-8") as f:
        for line in proc.stdout:
            print(line, end="", flush=True)
            f.write(line)
    proc.wait()
    if check and proc.returncode != 0:
        raise SystemExit(f"command failed with exit code {proc.returncode}")
    return proc.returncode


def find_resume_checkpoint(preset):
    """Latest checkpoint from a previous run attached as notebook input."""
    pattern = f"/kaggle/input/**/forge-{preset}/checkpoints/checkpoint-*"
    found = [p for p in glob.glob(pattern, recursive=True) if os.path.isdir(p)]
    if not found:
        return None
    return max(found, key=lambda p: int(p.rsplit("-", 1)[-1]))


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--preset", default="qwen3-8b", choices=["qwen3-8b", "qwen3-14b", "qwen3-14b-open", "qwen3-0.6b"])
    p.add_argument("--quant", default="q4_k_m", help="q4_k_m | iq4_xs | q3_k_m | q5_k_m | q8_0")
    p.add_argument("--max-hours", type=float, help="training time budget (default depends on preset)")
    p.add_argument("--max-steps", type=int, default=-1, help="for a quick test run, e.g. 20")
    p.add_argument("--scale", type=float, help="fraction of the public data mix to use "
                                              "(default depends on preset)")
    p.add_argument("--resume", choices=["auto", "no"], default="auto",
                   help="auto: continue from a checkpoint found in attached inputs")
    p.add_argument("--skip-train", action="store_true", help="only export an existing adapter")
    p.add_argument("--abliterate", action="store_true",
                   help="also write forge-uncensored-*.gguf: the same model with its refusal direction removed")
    p.add_argument("--no-export", action="store_true", help="skip the normal GGUF export (with --abliterate: only make the uncensored one)")
    p.add_argument("--adapter", help="with --skip-train: folder holding adapter_config.json (default: search /kaggle/input)")
    a = p.parse_args()

    WORK.mkdir(parents=True, exist_ok=True)
    out = WORK / f"forge-{a.preset}"
    hours = a.max_hours or DEFAULT_HOURS[a.preset]
    scale = a.scale if a.scale is not None else DEFAULT_SCALE[a.preset]
    log(f"preset={a.preset} quant={a.quant} max_hours={hours} scale={scale} kit={KIT}")

    # 1. writable copy of the code (Kaggle inputs are read-only)
    if CODE.exists():
        shutil.rmtree(CODE)
    shutil.copytree(KIT, CODE, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))

    # 2. dependencies
    if os.environ.get("FORGE_SKIP_INSTALL"):
        log("skipping pip installs (FORGE_SKIP_INSTALL set)")
    else:
        try:
            import unsloth  # noqa: F401
            log("unsloth already installed")
        except ImportError:
            run([sys.executable, "-m", "pip", "install", "-q", "unsloth"])
        run([sys.executable, "-m", "pip", "install", "-q", "ddgs", "httpx"], check=False)

    # one GPU: Unsloth trains on a single device, and a 4-bit model under
    # DataParallel on both T4s fails
    env = {**os.environ, "CUDA_VISIBLE_DEVICES": "0", "PYTHONUNBUFFERED": "1",
           "TOKENIZERS_PARALLELISM": "false",
           # fewer out-of-memory errors from fragmentation on the 15 GB T4
           "PYTORCH_CUDA_ALLOC_CONF": "expandable_segments:True"}
    run(["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv"], check=False)

    if not a.skip_train:
        # 3. data
        n_agentic = sum(1 for _ in open(CODE / "data/out/agentic.jsonl", encoding="utf-8")) \
            if (CODE / "data/out/agentic.jsonl").exists() else 0
        log(f"agentic conversations in kit: {n_agentic}")
        run([sys.executable, "data/build_dataset.py", "--scale", scale], env=env)

        # 4. train
        cmd = [sys.executable, "training/finetune.py", "--preset", a.preset, "--out", out,
               "--max-hours", hours, "--max-steps", a.max_steps]
        ckpt = find_resume_checkpoint(a.preset) if a.resume == "auto" else None
        if ckpt:
            log(f"resuming from {ckpt}")
            cmd += ["--resume-from", ckpt]
        run(cmd, env=env)

    lora = out / "lora"
    if a.skip_train:
        found = [a.adapter] if a.adapter else glob.glob(f"/kaggle/input/**/forge-{a.preset}/lora", recursive=True)
        if found:
            lora = Path(found[0])
    if not (lora / "adapter_config.json").exists():
        raise SystemExit(f"no adapter at {lora}; training did not finish")

    # 5. export: merged GGUF first, adapter-only GGUF as a fallback
    TMP.mkdir(parents=True, exist_ok=True)
    if not a.no_export:
        code = run([sys.executable, "training/export.py", lora, "--quant", a.quant,
                    "--out", TMP / "gguf", "--copy-to", WORK], check=False, env=env)
        if code != 0:
            log("merged export failed; exporting the adapter as GGUF instead")
            run([sys.executable, "training/export.py", lora, "--method", "lora",
                 "--out", WORK], check=False, env=env)

    # 5b. optional: the same model with refusals removed from its weights
    if a.abliterate:
        base = {"qwen3-8b": "Qwen/Qwen3-8B", "qwen3-14b": "Qwen/Qwen3-14B", "qwen3-14b-open": "huihui-ai/Huihui-Qwen3-14B-abliterated-v2", "qwen3-0.6b": "Qwen/Qwen3-0.6B"}[a.preset]
        run([sys.executable, "-m", "pip", "install", "-q", "peft", "datasets"], check=False)
        code = run([sys.executable, CODE / "kaggle/abliterate.py", "--base", base, "--adapter", lora,
                    "--out", TMP / "open", "--quant", a.quant], check=False, env={**env, "CUDA_VISIBLE_DEVICES": "0,1"})
        if code == 0:
            for f in (TMP / "open").glob("forge-uncensored-*.gguf"):
                shutil.copy(f, WORK / f.name)
            shutil.copy(TMP / "open/report.json", WORK / "abliteration_report.json")
        else:
            log("abliteration failed; the normal model above is still fine (see the log)")

    # 6. tidy the Output tab (20 GB limit); keep checkpoints only for resuming
    status = out / "status.json"
    finished = status.exists() and json.loads(status.read_text())["completed"]
    if not finished:
        log("training stopped on the time limit: checkpoints kept so the next run can resume")
    elif (out / "checkpoints").exists():
        shutil.rmtree(out / "checkpoints")
    shutil.rmtree(CODE, ignore_errors=True)
    shutil.rmtree(TMP, ignore_errors=True)

    log("done. files in /kaggle/working:")
    for f in sorted(WORK.rglob("*")):
        if f.is_file() and f.stat().st_size > 1_000_000:
            log(f"  {f.relative_to(WORK)}  {f.stat().st_size / 1e9:.2f} GB")


if __name__ == "__main__":
    main()
