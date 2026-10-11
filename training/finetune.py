"""QLoRA fine-tune of Qwen3 (8B or 14B) on a single 16 GB GPU (Kaggle T4/P100).

    python training/finetune.py --preset qwen3-8b
    python training/finetune.py --preset qwen3-14b --max-hours 10.5

Uses Unsloth (faster, less memory) and falls back to plain transformers+peft
when Unsloth is not installed. Writes the LoRA adapter to <out>/lora; run
training/export.py afterwards to produce the GGUF file used locally.
"""

import argparse
import json
import math
import os
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Unsloth patches transformers/peft when it is imported, so it has to be imported
# before them; otherwise its memory savings are only partly applied
if "--no-unsloth" not in sys.argv:
    try:
        import unsloth  # noqa: F401, E402
    except ImportError:
        pass

from training.formatting import to_pairs, tokenize_pair  # noqa: E402

PRESETS = {
    # 4-bit pre-quantised checkpoints load fast and fit a 16 GB card
    "qwen3-8b": dict(model="unsloth/Qwen3-8B-unsloth-bnb-4bit", hf_model="Qwen/Qwen3-8B",
                     max_len=4096, batch=2, accum=8, rank=32),
    "qwen3-14b": dict(model="unsloth/Qwen3-14B-unsloth-bnb-4bit", hf_model="Qwen/Qwen3-14B",
                      max_len=2048, batch=1, accum=16, rank=16),
    # a 14B whose refusals were removed before training (community abliteration of Qwen3-14B), loaded in 4-bit on the fly
    "qwen3-14b-open": dict(model="huihui-ai/Huihui-Qwen3-14B-abliterated-v2", hf_model="huihui-ai/Huihui-Qwen3-14B-abliterated-v2",
                           max_len=2048, batch=1, accum=16, rank=16),
    # tiny preset for smoke-testing the pipeline on any machine
    "qwen3-0.6b": dict(model="unsloth/Qwen3-0.6B-unsloth-bnb-4bit", hf_model="Qwen/Qwen3-0.6B",
                       max_len=1024, batch=2, accum=2, rank=8),
}
TARGET_MODULES = ["q_proj", "k_proj", "v_proj", "o_proj", "gate_proj", "up_proj", "down_proj"]


def parse_args():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--preset", choices=PRESETS, default="qwen3-8b")
    p.add_argument("--train", default="data/out/train.jsonl")
    p.add_argument("--val", default="data/out/val.jsonl")
    p.add_argument("--out", default="outputs/forge-qwen3-8b")
    p.add_argument("--epochs", type=float, default=1.0)
    p.add_argument("--lr", type=float, default=1e-4)
    p.add_argument("--max-len", type=int, help="override the preset's sequence length")
    p.add_argument("--max-steps", type=int, default=-1, help="stop early (for testing)")
    p.add_argument("--max-hours", type=float, default=11.0, help="save and stop before Kaggle's 12h limit")
    p.add_argument("--resume-from", help="checkpoint directory to resume from")
    p.add_argument("--no-unsloth", action="store_true")
    p.add_argument("--eval", action="store_true",
                   help="evaluate on the val split during training (needs extra GPU memory)")
    p.add_argument("--seed", type=int, default=3407)
    return p.parse_args()


def load_model(cfg, max_len, use_unsloth):
    if use_unsloth:
        try:
            from unsloth import FastLanguageModel
        except ImportError:
            print("unsloth not installed, falling back to transformers + peft")
            use_unsloth = False
    if use_unsloth:
        model, tokenizer = FastLanguageModel.from_pretrained(
            model_name=cfg["model"], max_seq_length=max_len, load_in_4bit=True, dtype=None,
        )
        model = FastLanguageModel.get_peft_model(
            model, r=cfg["rank"], lora_alpha=cfg["rank"], lora_dropout=0.0, bias="none",
            target_modules=TARGET_MODULES, use_gradient_checkpointing="unsloth", random_state=3407,
        )
        return model, tokenizer

    import torch
    from peft import LoraConfig, get_peft_model, prepare_model_for_kbit_training
    from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig

    tokenizer = AutoTokenizer.from_pretrained(cfg["hf_model"])
    quant = BitsAndBytesConfig(
        load_in_4bit=True, bnb_4bit_quant_type="nf4", bnb_4bit_use_double_quant=True,
        bnb_4bit_compute_dtype=torch.float16,
    )
    model = AutoModelForCausalLM.from_pretrained(cfg["hf_model"], quantization_config=quant,
                                                 device_map="auto", dtype=torch.float16)
    model = prepare_model_for_kbit_training(model, use_gradient_checkpointing=True)
    model = get_peft_model(model, LoraConfig(
        r=cfg["rank"], lora_alpha=cfg["rank"], lora_dropout=0.0, bias="none",
        target_modules=TARGET_MODULES, task_type="CAUSAL_LM",
    ))
    return model, tokenizer


def make_training_args(warmup, group_by_length, **kw):
    """Build TrainingArguments across transformers 4.x and 5.x, whose names differ."""
    import inspect

    from transformers import TrainingArguments

    params = inspect.signature(TrainingArguments.__init__).parameters
    if "warmup_ratio" in params:
        kw["warmup_ratio"] = warmup
    else:
        kw["warmup_steps"] = warmup  # v5: a float < 1 means a ratio
    if group_by_length:
        if "group_by_length" in params:
            kw["group_by_length"] = True
        else:
            kw["train_sampling_strategy"] = "group_by_length"
    return TrainingArguments(**kw)


def build_split(tokenizer, path, max_len, seed):
    rows = [json.loads(line) for line in open(path, encoding="utf-8") if line.strip()]
    examples, skipped = [], 0
    for row in rows:
        for prompt, completion in to_pairs(tokenizer, row):
            item = tokenize_pair(tokenizer, prompt, completion, max_len)
            if item is None:
                skipped += 1
            else:
                examples.append(item)
    random.Random(seed).shuffle(examples)
    n_tok = sum(len(e["input_ids"]) for e in examples)
    print(f"{path}: {len(rows)} conversations -> {len(examples)} examples, "
          f"{n_tok / 1e6:.2f}M tokens ({skipped} too long, skipped)")
    return examples


def main():
    args = parse_args()
    cfg = PRESETS[args.preset]
    max_len = args.max_len or cfg["max_len"]
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    import torch
    from datasets import Dataset
    from transformers import DataCollatorForSeq2Seq, Trainer, TrainerCallback

    model, tokenizer = load_model(cfg, max_len, use_unsloth=not args.no_unsloth)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token

    train = Dataset.from_list(build_split(tokenizer, args.train, max_len, args.seed))
    # evaluation materialises full-vocabulary logits (~4 GB at 4k tokens), which
    # does not fit next to an 8B model on a 16 GB T4, so it is opt-in
    val = None
    if args.eval and Path(args.val).exists():
        val = Dataset.from_list(build_split(tokenizer, args.val, max_len, args.seed))

    steps_per_epoch = math.ceil(len(train) / (cfg["batch"] * cfg["accum"]))
    print(f"preset={args.preset} max_len={max_len} batch={cfg['batch']}x{cfg['accum']} "
          f"-> {steps_per_epoch} optimizer steps per epoch")

    start = time.time()
    stopped = {"early": False}

    class TimeLimit(TrainerCallback):
        """Save and stop cleanly before the notebook session is killed."""

        def on_step_end(self, targs, state, control, **kw):
            if (time.time() - start) / 3600 > args.max_hours:
                print(f"time limit of {args.max_hours}h reached at step {state.global_step}; saving")
                control.should_save = True
                control.should_training_stop = True
                stopped["early"] = True
            return control

    bf16 = torch.cuda.is_available() and torch.cuda.is_bf16_supported()
    targs = make_training_args(
        output_dir=str(out / "checkpoints"),
        per_device_train_batch_size=cfg["batch"],
        per_device_eval_batch_size=1,
        prediction_loss_only=True,
        gradient_accumulation_steps=cfg["accum"],
        num_train_epochs=args.epochs,
        max_steps=args.max_steps,
        learning_rate=args.lr,
        lr_scheduler_type="cosine",
        warmup=0.03,
        weight_decay=0.0,
        optim="adamw_8bit",
        bf16=bf16,
        fp16=not bf16,
        logging_steps=10,
        eval_strategy="steps" if val is not None else "no",
        eval_steps=100,
        save_strategy="steps",
        save_steps=50,  # a crash or session timeout loses at most 50 steps
        save_total_limit=2,
        group_by_length=True,  # batches of similar length waste less compute on padding
        report_to="none",
        remove_unused_columns=False,
        seed=args.seed,
        dataloader_num_workers=2,
    )
    trainer = Trainer(
        model=model,
        args=targs,
        train_dataset=train,
        eval_dataset=val,
        data_collator=DataCollatorForSeq2Seq(tokenizer, padding=True, label_pad_token_id=-100),
        callbacks=[TimeLimit()],
    )
    trainer.train(resume_from_checkpoint=args.resume_from)

    lora_dir = out / "lora"
    model.save_pretrained(str(lora_dir))
    tokenizer.save_pretrained(str(lora_dir))
    with open(lora_dir / "forge_preset.json", "w") as f:
        json.dump({"preset": args.preset, **cfg, "max_len": max_len}, f, indent=2)
    hist = [h for h in trainer.state.log_history if "loss" in h or "eval_loss" in h]
    with open(out / "train_log.json", "w") as f:
        json.dump(hist, f, indent=1)
    with open(out / "status.json", "w") as f:
        json.dump({"completed": not stopped["early"], "steps": trainer.state.global_step,
                   "hours": (time.time() - start) / 3600}, f)
    print(f"done in {(time.time() - start) / 3600:.2f}h; adapter saved to {lora_dir}")


if __name__ == "__main__":
    main()
