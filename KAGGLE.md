# Training Forge on Kaggle

`forge-kit.zip` holds everything the training run needs. You upload it once as a
Kaggle Dataset, then a single notebook cell installs the libraries, builds the
training data, fine-tunes the model, exports the `.gguf`, and cleans up.

## 0. Before you start (one time)

- Create a Kaggle account and **verify your phone number** (Settings → Phone
  verification). Without it, GPU and Internet are greyed out.
- The free tier gives you about 30 GPU hours per week, and a single session can
  run for up to 12 hours.

## 1. Build the zip (on your PC)

```powershell
cd C:\Users\himan\local-assistant
python scripts\make_kaggle_kit.py
```

This creates `dist\forge-kit.zip` (a few MB) and prints how many tool-use
conversations it contains. Rebuild the zip whenever `data\out\agentic.jsonl`
grows.

## 2. Upload it as a Dataset

1. Go to kaggle.com → **Datasets** → **+ New Dataset**.
2. Drag `forge-kit.zip` into the upload box. Kaggle unzips it automatically.
3. Title: `forge-kit`. Visibility: **Private**. Click **Create**.

To update it later, open the dataset → **New Version** → upload the new zip.

## 3. Create the notebook

1. Go to kaggle.com → **Code** → **+ New Notebook**.
2. In the right-hand panel (**Session options**):
   - **Accelerator** → `GPU T4 x2`
   - **Internet** → `On`
   - **Persistence** → `Files only` (optional)
3. Click **+ Add Input** → **Datasets** → **Your Datasets** → `forge-kit` → **+**.
4. Delete the sample cell.

## 4. (Recommended) 10-minute test run

Paste this into a cell and press **Shift+Enter**. It runs the whole pipeline on a
tiny 0.6B model with very little data, so you know everything works before
committing 10 hours.

```python
import glob
script = glob.glob("/kaggle/input/**/forge-kit/kaggle/run_all.py", recursive=True)[0]
!python {script} --preset qwen3-0.6b --max-steps 20 --scale 0.05 --resume no
```

The last lines should list a file like `forge-qwen3-0.6b-...Q4_K_M.gguf`. If you
see that, delete the test output with `!rm -rf /kaggle/working/*`.

## 5. The real run

Replace the cell contents with:

```python
import glob
script = glob.glob("/kaggle/input/**/forge-kit/kaggle/run_all.py", recursive=True)[0]
!python {script} --preset qwen3-8b
```

To run it in the background so you can close the browser, click **Save Version**
(top right) → **Save & Run All (Commit)** → **Save**. The run continues on
Kaggle's servers for up to 12 hours.

| Option | What it does |
|---|---|
| `--preset qwen3-8b` | 8B model. About 8 h of training with the default data mix (`--scale 0.6`). Recommended. |
| `--preset qwen3-14b` | 14B model. Uses a smaller data mix (`--scale 0.3`) and may still need a second session (see step 7). |
| `--quant q4_k_m` | Default quantization: 8B → 5.0 GB, 14B → 9 GB. |
| `--quant iq4_xs` / `q3_k_m` | Smaller file, so more of it fits on your 4 GB GPU and it runs faster, at slightly lower quality. |
| `--max-hours 10` | Training time budget. The script saves and exports before Kaggle's 12 h limit. |

## 6. Watch it and download the model

- **Progress**: open the notebook → **Versions** (clock icon) → the running
  version → **Logs**. Every 10 steps you'll see lines like
  `{'loss': 0.71, ... 'epoch': 0.25}`.
- **Download**: when the version shows *Successful*, open it → **Output** tab
  → click `forge-qwen3-8b-...Q4_K_M.gguf` → **Download**. The `run_all.log`
  file has the full log.

## 7. If training stopped on the time limit

If the log says `training stopped on the time limit: checkpoints kept so the
next run can resume`, you still get a usable `.gguf` from the partly trained
model. To finish training:

1. Open the notebook → **Edit**.
2. **+ Add Input** → **Notebook Output Files** → pick this same notebook's
   previous version.
3. Run the same cell again (Save & Run All). It finds the checkpoint
   automatically and continues where it left off.

## 8. Run it on your laptop

```powershell
cd C:\Users\himan\local-assistant
mkdir models                                  # put the downloaded .gguf in here
powershell -ExecutionPolicy Bypass -File scripts\setup_llamacpp.ps1   # once
powershell -ExecutionPolicy Bypass -File scripts\serve.ps1
```

When the server prints `server is listening on http://127.0.0.1:8080`, open a
second terminal:

```powershell
cd C:\Users\himan\local-assistant
python chat.py --stats
```

### If the merged export failed

If the Output tab has `forge-lora.gguf` instead of a full model (which can happen
with 14B when Kaggle runs out of disk), download the official base model
(`Qwen3-8B-Q4_K_M.gguf` from huggingface.co/Qwen/Qwen3-8B-GGUF) into `models\`
and run:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\serve.ps1 -Model models\Qwen3-8B-Q4_K_M.gguf -Lora models\forge-lora.gguf
```

## Troubleshooting

| Problem | Fix |
|---|---|
| `IndexError: list index out of range` in the cell | The dataset isn't attached. Do step 3.3 again. |
| `pip` / download errors | Internet is off, or the phone number isn't verified. |
| `CUDA out of memory` with 14B | Add `--max-hours 9` and make sure the accelerator is a T4/P100 (16 GB), not "None". |
| Very slow (hours per 100 steps) | The accelerator is set to CPU. Switch it to GPU T4 x2. |
