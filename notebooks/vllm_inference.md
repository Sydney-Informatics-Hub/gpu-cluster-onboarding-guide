# Serving LLMs with vLLM on the GPU Cluster

[vLLM](https://docs.vllm.ai) is an open-source library for fast LLM inference. It exposes an OpenAI-compatible REST API, so any tool that works with the OpenAI SDK can be pointed at a vLLM endpoint instead.

This notebook walks through submitting a vLLM inference workload on the SIH GPU cluster using the Run:AI CLI, using [Qwen3.5-397B-A17B](https://huggingface.co/unsloth/Qwen3.5-397B-A17B-GGUF) (a 397B Mixture-of-Experts model, ~273 GB quantised) as an example.

## Prerequisites

- Run:AI CLI configured and logged in (`runai login`)
- A project PVC with sufficient storage (~300 GB free) for model weights
- Model weights pre-downloaded to the PVC (see [Pre-downloading model weights] below)

## Pre-downloading model weights

Model weights (~276 GB) must be downloaded to the PVC before submitting the inference workload. Do this once from a [JupyterLab terminal workload](jupyter_tutorial.html) with your PVC mounted.

This ensures that model weights do not need to be downloaded during the inference workload initialisation.

**1. Set environment variables**

```bash
export HF_TOKEN_PATH="/scratch/rds-core-sih4hpc-rw/<path_to_hf_token>"
export HF_HUB_DISABLE_XET=1
export HF_HOME="/scratch/rds-core-sih4hpc-rw/huggingface"
```

- `HF_TOKEN_PATH` — points `huggingface_hub` to your token file on the PVC; no interactive login needed
- `HF_HUB_DISABLE_XET=1` — disables the XET transfer protocol which can cause downloads to hang ([huggingface/hf_transfer#30](https://github.com/huggingface/hf_transfer/issues/30#issuecomment-2878604131))
- `HF_HOME` — sets the cache root; weights are saved here and the vLLM container reads from the same location via its own mount path (`/scratch/pvc-rds-core-sih4hpc-rw/huggingface`)

**2. Dry-run to confirm files and size**

```bash
hf download unsloth/Qwen3.5-397B-A17B-GGUF \
  --include "config.json" \
  --include "mmproj-BF16.gguf" \
  --include "Q5_K_S/*" \
  --dry-run
```

Expected output — 9 files totalling ~277.5 GB:

```
FILE                                            SIZE
----------------------------------------------- -----
config.json                                     3.7K
mmproj-BF16.gguf                                922M
Q5_K_S/Qwen3.5-397B-A17B-Q5_K_S-...            10.9M
Q5_K_S/Qwen3.5-397B-A17B-Q5_K_S-...            49.5G
Q5_K_S/Qwen3.5-397B-A17B-Q5_K_S-...            48.8G
Q5_K_S/Qwen3.5-397B-A17B-Q5_K_S-...            48.7G
Q5_K_S/Qwen3.5-397B-A17B-Q5_K_S-...            48.8G
Q5_K_S/Qwen3.5-397B-A17B-Q5_K_S-...            48.8G
Q5_K_S/Qwen3.5-397B-A17B-Q5_K_S-...            31.9G
```

`mmproj-BF16.gguf` is the vision encoder (multimodal projector). Only the BF16 variant is downloaded — the GGUF repo also contains F16 and F32 variants, but vLLM auto-selects the alphabetically first `mmproj-*.gguf` file in the cache; downloading only one removes ambiguity.

**3. Download**

```bash
hf download unsloth/Qwen3.5-397B-A17B-GGUF \
  --include "config.json" \
  --include "mmproj-BF16.gguf" \
  --include "Q5_K_S/*"
```

No `--local-dir` needed — `HF_HOME` is set so `huggingface_hub` caches files there automatically. Files persist on the PVC across jobs.

**4. Download the tokenizer and image processor**

The GGUF repo lacks the `<think>`/`</think>` special tokens needed by the reasoning parser, and the `preprocessor_config.json` needed by the vision encoder. Both come from the base model. vLLM loads the image processor from the `--tokenizer` path for GGUF models, so downloading these files to the same cache is sufficient — no manual path manipulation required.

```bash
hf download Qwen/Qwen3.5-397B-A17B \
  --include "tokenizer.json" \
  --include "tokenizer_config.json" \
  --include "vocab.json" \
  --include "merges.txt" \
  --include "chat_template.jinja" \
  --include "preprocessor_config.json" \
  --include "video_preprocessor_config.json"
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
| `--large-shm` | *(flag)* | Allocates a larger `/dev/shm`; required for tensor parallelism and `--mm-processor-cache-type shm` |
| `--serving-port` | `container=8000,protocol=http` | Exposes port 8000 as an HTTP endpoint via the Run:AI service layer |
| `--initialization-timeout-seconds` | `1800` | Time allowed for the container to become ready; too low causes `CrashLoopBackOff` during model load |

### Environment variables

| Variable | Purpose |
|----------|---------|
| `HF_HOME` | HuggingFace cache directory; must match where weights were pre-downloaded on the PVC. vLLM finds the cached GGUF shards here and will not re-download them. |
| `VLLM_WORKER_MULTIPROC_METHOD` | Set to `spawn` to avoid CUDA context inheritance errors in multi-GPU worker processes |

### `vllm serve` flags

Derived from the [vLLM Qwen3.5 multimodal recipe](https://docs.vllm.ai/projects/recipes/en/latest/Qwen/Qwen3.5.html#multimodal).

| Flag | Value | Purpose |
|------|-------|---------|
| *(model)* | `unsloth/Qwen3.5-397B-A17B-GGUF` | HuggingFace GGUF repo; vLLM resolves the default branch and loads the Q5_K_S shards from the HF cache |
| `--tensor-parallel-size` | `2` | Shards weights across 2 GPUs (~137 GB each); must match `--gpu-devices-request` |
| `--enable-expert-parallel` | *(flag)* | Distributes MoE experts across GPUs; this model has 512 experts (10 routed + 1 shared per token) |
| `--mm-encoder-tp-mode` | `data` | Runs a full vision encoder copy per tensor-parallel rank rather than sharding it |
| `--mm-processor-cache-type` | `shm` | Stores the multimodal processor cache in `/dev/shm`; requires `--large-shm` |
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
| `Can't load image processor … preprocessor_config.json` | `preprocessor_config.json` not downloaded; vLLM loads the image processor from the `--tokenizer` path for GGUF models | Re-run step 4 of the download to include `preprocessor_config.json` and `video_preprocessor_config.json` |
| `Qwen3ReasoningParser could not locate think tokens` | External tokenizer missing special tokens | Point `--tokenizer` to `Qwen/Qwen3.5-397B-A17B` (base model), not the GGUF repo |
| `unrecognized arguments: --gguf-file` | Flag not supported in this vLLM version | Remove `--gguf-file`; pass the model as `repo:revision` instead |
| Startup takes 60–90 min | CUDA graph compilation across 51 batch sizes | Add `--enforce-eager` to skip graph capture |
| `hf download` hangs partway through | XET transfer protocol stalling | Set `HF_HUB_DISABLE_XET=1` before downloading (see [hf_transfer#30](https://github.com/huggingface/hf_transfer/issues/30#issuecomment-2878604131)) |
| vLLM cannot find cached weights even though download succeeded | PVC mount path differs between workloads: JupyterLab mounts at `/scratch/rds-core-sih4hpc-rw`, inference containers at `/scratch/pvc-rds-core-sih4hpc-rw` | Ensure `HF_HOME` in both contexts appends the same relative path (e.g. `/huggingface`) to their respective mount roots |
