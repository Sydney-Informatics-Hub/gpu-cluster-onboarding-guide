# Serving LLMs with vLLM on the GPU Cluster

[vLLM](https://docs.vllm.ai) is an open-source library for fast LLM inference. It exposes an OpenAI-compatible REST API, so any tool that works with the OpenAI SDK can be pointed at a vLLM endpoint instead.

This notebook walks through submitting a vLLM inference workload on the SIH GPU cluster using the Run:AI CLI, using [Qwen3.5-397B-A17B](https://huggingface.co/unsloth/Qwen3.5-397B-A17B-GGUF) (a 397B Mixture-of-Experts model, 228 GB quantised) as an example.

## Prerequisites

- Run:AI CLI configured and logged in (`runai login`)
- A project PVC with sufficient storage (~250 GB free) for model weights (228 GB for Q4_K_S shards)
- Model weights pre-downloaded to the PVC (see [Pre-downloading model weights] below)

## Pre-downloading model weights

Model weights (228 GB) must be downloaded to the PVC before submitting the inference workload. Do this once from a [JupyterLab terminal workload](jupyter_tutorial.html) with your PVC mounted.

This ensures that model weights do not need to be downloaded during the inference workload initialisation.

**1. Set environment variables**

```bash
export HF_TOKEN_PATH="/scratch/rds-core-sih4hpc-rw/<path_to_hf_token>"
export HF_HUB_DISABLE_XET=1
export HF_HOME="/scratch/rds-core-sih4hpc-rw/huggingface"
```

- `HF_TOKEN_PATH` — path to your HuggingFace token file on the PVC; used by `hf download` for authenticated requests
- `HF_HUB_DISABLE_XET=1` — disables the XET transfer protocol which can cause downloads to hang ([huggingface/hf_transfer#30](https://github.com/huggingface/hf_transfer/issues/30#issuecomment-2878604131))
- `HF_HOME` — sets the cache root; weights are saved here and the vLLM container reads from the same location via its own mount path (`/scratch/pvc-rds-core-sih4hpc-rw/huggingface`)

**2. Dry-run to confirm files and size**

```bash
hf download unsloth/Qwen3.5-397B-A17B-GGUF \
  --include "config.json" \
  --include "Q4_K_S/*" \
  --dry-run
```

Expected output — 7 files, 6 to download totalling 228 GB (`config.json` is skipped if already cached from a previous download):

```bash
FILE                                            SIZE
----------------------------------------------- -----
Q4_K_S/Qwen3.5-397B-A17B-Q4_K_S-00001-of-00006 10.9M
Q4_K_S/Qwen3.5-397B-A17B-Q4_K_S-00002-of-00006 49.6G
Q4_K_S/Qwen3.5-397B-A17B-Q4_K_S-00003-of-00006 49.0G
Q4_K_S/Qwen3.5-397B-A17B-Q4_K_S-00004-of-00006 49.0G
Q4_K_S/Qwen3.5-397B-A17B-Q4_K_S-00005-of-00006 49.0G
Q4_K_S/Qwen3.5-397B-A17B-Q4_K_S-00006-of-00006 31.4G
config.json                                      (cached)
```

**3. Download model weights and config**

```bash
hf download unsloth/Qwen3.5-397B-A17B-GGUF \
  --include "config.json" \
  --include "Q4_K_S/*"
```

No `--local-dir` needed — `HF_HOME` is set so `huggingface_hub` caches files there automatically. Files persist on the PVC across jobs.

**4. Download the tokenizer**

The GGUF repo lacks the `<think>`/`</think>` special tokens needed by the reasoning parser. Download the tokenizer from the base model:

```bash
hf download Qwen/Qwen3.5-397B-A17B \
  --include "tokenizer.json" \
  --include "tokenizer_config.json" \
  --include "vocab.json" \
  --include "merges.txt" \
  --include "chat_template.jinja"
```

Each file needs its own `--include` flag — passing multiple patterns after a single `--include` causes the extras to be treated as positional file arguments, which aborts the download if any are missing.

## Submitting the Run.ai inference workload

The full submission script is at `scripts/qwen3.5-397b.sh`:

```{.bash}
{{< include ../scripts/qwen3.5-397b.sh >}}
```

## Parameter reference

### `runai inference submit` flags

| Flag | Value | Purpose |
|------|-------|---------|
| `vllm-qwen35-397b` | *(job name)* | Unique name for the inference job within the project |
| `-p` | `${PROJECT_ID}` | Run:AI project to bill and schedule the job under |
| `--image` | `vllm/vllm-openai:cu129-nightly-...` | Docker image targeting CUDA 12.9; pin to a specific tag for reproducibility |
| `--image-pull-policy` | `IfNotPresent` | Reuses a locally cached image rather than re-pulling on every submission |
| `-c` | *(flag)* | Connects stdout/stderr to your terminal so you can watch startup logs directly |
| `--gpu-devices-request` | `2` | Number of physical GPUs to allocate; must match `--tensor-parallel-size` |
| `--existing-pvc` | `claimname=pvc-${PROJECT_ID},path=/scratch/pvc-${PROJECT_ID}` | Mounts a pre-existing PVC; keeps model weights cached across job restarts |
| `--large-shm` | *(flag)* | Allocates a larger `/dev/shm`; required for tensor parallelism |
| `--serving-port` | `container=8000,protocol=http` | Exposes port 8000 as an HTTP endpoint via the Run:AI service layer |
| `--initialization-timeout-seconds` | `1800` | Time allowed for the container to become ready; too low causes `CrashLoopBackOff` during model load |

### Environment variables

| Variable | Purpose |
|----------|---------|
| `HF_HOME` | HuggingFace cache directory; must match where weights were pre-downloaded on the PVC. vLLM finds the cached GGUF shards here and will not re-download them. |
| `HF_TOKEN` | HuggingFace API token passed into the container; read from `HF_TOKEN_PATH` at submit time. Required to avoid unauthenticated rate limits when vLLM resolves model metadata from the Hub. |
| `VLLM_WORKER_MULTIPROC_METHOD` | Set to `spawn` to avoid CUDA context inheritance errors in multi-GPU worker processes |

### `vllm serve` flags

| Flag | Value | Purpose |
|------|-------|---------|
| *(model)* | `unsloth/Qwen3.5-397B-A17B-GGUF:Q4_K_S` | HuggingFace GGUF repo; `:Q4_K_S` selects the Q4_K_S subdirectory (228 GB, ~114 GiB/GPU with TP=2) |
| `--language-model-only` | *(flag)* | Forces text-generation mode; skips vision encoder initialisation which requires files not present in the GGUF repo |
| `--tensor-parallel-size` | `2` | Shards weights across 2 GPUs (~114 GiB each); must match `--gpu-devices-request` |
| `--enable-expert-parallel` | *(flag)* | Distributes MoE experts across GPUs; this model has 512 experts (10 routed + 1 shared per token) |
| `--reasoning-parser` | `qwen3` | Parses Qwen3 chain-of-thought `<think>` blocks and exposes them separately in the API response |
| `--tokenizer` | `Qwen/Qwen3.5-397B-A17B` | Points to the base model tokenizer, which includes the `<think>`/`</think>` special tokens required by the reasoning parser |
| `--enable-prefix-caching` | *(flag)* | Reuses KV cache entries for shared prompt prefixes; reduces latency on repeated system prompts |
| `--enforce-eager` | *(flag)* | Disables CUDA graph compilation; reduces startup from 60–90 min to ~25 min at a small throughput cost. Omitted from the default script — add it if startup time matters more than peak throughput |
| `--max-model-len` | `32768` | Maximum sequence length; the model supports 262,144 tokens but full context would exhaust VRAM on 2 GPUs |

## Submitting and monitoring

```bash
# Submit
bash scripts/qwen3.5-397b.sh

# Check status
runai inference describe vllm-qwen35-397b

# Stream logs
runai inference logs vllm-qwen35-397b -f

# Delete
runai inference delete vllm-qwen35-397b -p rds-core-sih4hpc-rw
```

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| `CrashLoopBackOff` during init | `--initialization-timeout-seconds` too low | Increase to `1800` or above |
| `CUDA out of memory` during model load | Q5_K_S weights (~136 GiB/GPU with TP=2) leave insufficient room for vLLM's `FusedMoE.create_weights` to allocate a per-layer BF16 staging tensor (~4 GiB/GPU for 256 experts). `--cpu-offload-gb` does not help — offloading only moves weights after they are loaded, but the OOM occurs during the initial allocation. Increasing to 4 GPUs also does not help: the staging tensor shrinks (4 GiB → 1 GiB) but per-GPU base load *increases* by ~1.6 GiB (NCCL buffers), so the net result is worse | Use Q4_K_S quantisation (~112 GiB/GPU); re-download from the same GGUF repo with `--include "Q4_K_S/*"` and update the `vllm serve` model argument to `unsloth/Qwen3.5-397B-A17B-GGUF:Q4_K_S` |
| `Can't load image processor … preprocessor_config.json` | vLLM attempting multimodal init; `preprocessor_config.json` not present in the GGUF repo | Add `--task generate` to skip vision encoder initialisation |
| Multimodal (image inputs) not supported | The GGUF repo omits `preprocessor_config.json` and the vision encoder weights (`mmproj-*.gguf`). vLLM loads the image processor from the GGUF snapshot path — symlinking these files in from the base model repo is unreliable because symlinks created in JupyterLab (`/scratch/rds-core-sih4hpc-rw/…`) resolve to a different path inside inference containers (`/scratch/pvc-rds-core-sih4hpc-rw/…`). Use `--task generate` for text-only serving, or switch to the native HF model format (see [Serving LLMs with vLLM (native HF)](vllm_inference_native.html)) |
| `Qwen3ReasoningParser could not locate think tokens` | External tokenizer missing special tokens | Point `--tokenizer` to `Qwen/Qwen3.5-397B-A17B` (base model), not the GGUF repo |
| `unrecognized arguments: --gguf-file` | Flag not supported in this vLLM version | Remove `--gguf-file`; pass the model as `repo:revision` instead |
| Startup takes 60–90 min | CUDA graph compilation across 51 batch sizes | Add `--enforce-eager` to skip graph capture |
| `hf download` hangs partway through | XET transfer protocol stalling | Set `HF_HUB_DISABLE_XET=1` before downloading (see [hf_transfer#30](https://github.com/huggingface/hf_transfer/issues/30#issuecomment-2878604131)) |
| vLLM cannot find cached weights even though download succeeded | PVC mount path differs between workloads: JupyterLab mounts at `/scratch/rds-core-sih4hpc-rw`, inference containers at `/scratch/pvc-rds-core-sih4hpc-rw` | Ensure `HF_HOME` in both contexts appends the same relative path (e.g. `/huggingface`) to their respective mount roots |
