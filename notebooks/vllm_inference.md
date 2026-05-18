# Serving LLMs with vLLM on the GPU Cluster

[vLLM](https://docs.vllm.ai) is an open-source library for fast LLM inference. It exposes an OpenAI-compatible REST API, so any tool that works with the OpenAI SDK can be pointed at a vLLM endpoint instead.

This notebook walks through submitting a vLLM inference workload on the SIH GPU cluster using the Run:AI CLI, using [Qwen3.5-122B-A10B-FP8](https://huggingface.co/Qwen/Qwen3.5-122B-A10B-FP8) (a 122B Mixture-of-Experts model, officially FP8-quantised to ~127 GB) as an example.

## Prerequisites

- Run:AI CLI configured and logged in (`runai login`)
- A project PVC with sufficient storage (~130 GB free) for model weights
- Model weights pre-downloaded to the PVC (see [Pre-downloading model weights] below)

## Pre-downloading model weights

Model weights (~127 GB) must be downloaded to the PVC before submitting the inference workload. Do this once from a [JupyterLab terminal workload](jupyter_tutorial.html) with your PVC mounted.

**1. Set environment variables**

```bash
export HF_TOKEN_PATH="/scratch/rds-core-sih4hpc-rw/<path_to_hf_token>"
export HF_HUB_DISABLE_XET=1
export HF_HOME="/scratch/rds-core-sih4hpc-rw/huggingface"
```

- `HF_TOKEN_PATH` — path to your HuggingFace token file on the PVC; used by `hf download` for authenticated requests
- `HF_HUB_DISABLE_XET=1` — disables the XET transfer protocol which can cause downloads to hang ([huggingface/hf_transfer#30](https://github.com/huggingface/hf_transfer/issues/30#issuecomment-2878604131))
- `HF_HOME` — sets the cache root; weights are saved here and the vLLM container reads from the same location via its own mount path (`/scratch/pvc-rds-core-sih4hpc-rw/huggingface`)

**2. Dry-run to confirm size**

```bash
hf download Qwen/Qwen3.5-122B-A10B-FP8 --dry-run
```

This will list all files in the repo (model shards, tokenizer, config) and the total download size (~127 GB).

**3. Download model weights**

```bash
hf download Qwen/Qwen3.5-122B-A10B-FP8
```

No `--include` flags needed — the entire repo (weights, tokenizer, config) is required and downloaded together. Files persist on the PVC across jobs.

No separate tokenizer download is needed; the tokenizer is bundled in the same HuggingFace repo.

## Submitting the Run.ai inference workload

The full submission script is at `scripts/qwen3.5-122b.sh`:

```{.bash}
{{< include ../scripts/qwen3.5-122b.sh >}}
```

## Parameter reference

### `runai inference submit` flags

| Flag | Value | Purpose |
|------|-------|---------|
| `vllm-qwen35-122b` | *(job name)* | Unique name for the inference job within the project |
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
| `HF_HOME` | HuggingFace cache directory; must match where weights were pre-downloaded on the PVC. vLLM finds the cached weights here and will not re-download them. |
| `HF_TOKEN_PATH` | Path to the HuggingFace token file **inside the container** (i.e. the PVC mount path). `huggingface_hub` reads the token from this file at runtime — no local file access or pre-export needed. |
| `VLLM_WORKER_MULTIPROC_METHOD` | Set to `spawn` to avoid CUDA context inheritance errors in multi-GPU worker processes |

### `vllm serve` flags

| Flag | Value | Purpose |
|------|-------|---------|
| *(model)* | `Qwen/Qwen3.5-122B-A10B-FP8` | HuggingFace model repo; vLLM loads the FP8-quantised weights (~127 GB) directly in their native format — no conversion step. FP8 weights occupy ~59 GiB/GPU with TP=2, leaving ~68 GiB per GPU for KV cache |
| `--tensor-parallel-size` | `2` | Shards weights across 2 GPUs (~59 GiB each); must match `--gpu-devices-request` |
| `--enable-expert-parallel` | *(flag)* | Distributes MoE experts across GPUs; this model has 128 experts (8 routed per token) |
| `--reasoning-parser` | `qwen3` | Parses Qwen3/3.5 chain-of-thought `<think>` blocks and exposes them separately in the API response |
| `--gpu-memory-utilization` | `0.90` | Fraction of GPU memory reserved for model weights + KV cache. FP8 weights occupy ~42% of VRAM; `0.90` gives ~68 GiB per GPU for KV cache |
| `--enable-prefix-caching` | *(flag)* | Reuses KV cache entries for shared prompt prefixes; reduces latency on repeated system prompts |
| `--max-model-len` | `32768` | Maximum sequence length; the model supports 131,072 tokens but longer contexts require more KV cache budget |

## Submitting and monitoring

```bash
# Submit
bash scripts/qwen3.5-122b.sh

# Check status
runai inference describe vllm-qwen35-122b

# Stream logs
runai inference logs vllm-qwen35-122b -f

# Delete
runai inference delete vllm-qwen35-122b -p rds-core-sih4hpc-rw
```

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| `CrashLoopBackOff` during init | `--initialization-timeout-seconds` too low | Increase to `1800` or above |
| `CUDA out of memory` during model load with GGUF | vLLM converts K-quant GGUF formats (Q3_K_S, Q4_K_S, Q5_K_S) to an internal Q4 representation on GPU at load time. All K-quant variants produce an identical in-memory footprint of ~136 GiB/GPU (~272 GiB across TP=2) on this vLLM build, regardless of the GGUF file size on disk. On 2× H200 141 GiB GPUs, this leaves only ~1.7 GiB free — insufficient for `FusedMoE.create_weights` which requires a ~4 GiB BF16 staging tensor during model `__init__`. `--cpu-offload-gb`, `--enforce-eager`, `--max-num-seqs`, and `--gpu-memory-utilization` all affect post-load KV cache profiling and cannot reduce the weight-loading footprint. This was observed with `Qwen3.5-397B-A17B-GGUF` across Q3_K_S, Q4_K_S, and Q5_K_S variants. | Switch to a native HF format model with GPU-native quantisation (FP8, GPTQ, AWQ). The `Qwen3.5-122B-A10B-FP8` model described in this notebook avoids this issue entirely. |
| `Can't load image processor … preprocessor_config.json` | vLLM attempting multimodal init on a GGUF model; `preprocessor_config.json` not present in the GGUF repo | Add `--language-model-only` to skip vision encoder initialisation |
| `Qwen3ReasoningParser could not locate think tokens` | Tokenizer missing `<think>`/`</think>` special tokens (GGUF-specific issue — GGUF repos often omit the full tokenizer) | For GGUF models, add `--tokenizer <base-model-repo>` pointing to the non-GGUF base model. Not needed for native HF models like `Qwen3.5-122B-A10B-FP8`. |
| `RuntimeError: Unknown gguf model_type: qwen3_5_moe` | The `repo:revision` syntax (e.g. `:Q4_K_S`) is interpreted as a HuggingFace git revision, not a subdirectory. That revision has no `config.json`, so vLLM falls back to reading the GGUF file header | Remove the revision suffix; use the base repo ID only (e.g. `unsloth/Qwen3.5-397B-A17B-GGUF`). vLLM resolves architecture from `config.json` on the main branch and auto-detects GGUF shards in the cache. If multiple quantisation levels are cached, delete the unwanted shards to avoid ambiguity |
| `unrecognized arguments: --gguf-file` | Flag not supported in this vLLM version | Remove `--gguf-file`; pass the model as the base repo ID without a revision suffix |
| Startup takes 60–90 min | CUDA graph compilation across 51 batch sizes | Add `--enforce-eager` to skip graph capture |
| `hf download` hangs partway through | XET transfer protocol stalling | Set `HF_HUB_DISABLE_XET=1` before downloading (see [hf_transfer#30](https://github.com/huggingface/hf_transfer/issues/30#issuecomment-2878604131)) |
| vLLM cannot find cached weights even though download succeeded | PVC mount path differs between workloads: JupyterLab mounts at `/scratch/rds-core-sih4hpc-rw`, inference containers at `/scratch/pvc-rds-core-sih4hpc-rw` | Ensure `HF_HOME` in both contexts appends the same relative path (e.g. `/huggingface`) to their respective mount roots |
