# Serving LLMs with vLLM on the GPU Cluster

[vLLM](https://docs.vllm.ai) is an open-source library for fast LLM inference. It exposes an OpenAI-compatible REST API, so any tool that works with the OpenAI SDK can be pointed at a vLLM endpoint instead.

This notebook walks through submitting a vLLM inference workload on the SIH GPU cluster using the Run:AI CLI, using [Qwen3.5-397B-A17B](https://huggingface.co/unsloth/Qwen3.5-397B-A17B-GGUF) (a 397B Mixture-of-Experts model, Q3_K_S quantised to 164 GB) as an example.

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
  --include "Q3_K_S/*" \
  --dry-run
```

Expected output — 6 files, 5 to download totalling 164.3 GB (`config.json` is skipped if already cached from a previous download):

```bash
FILE                                            SIZE
----------------------------------------------- -----
Q3_K_S/Qwen3.5-397B-A17B-Q3_K_S-00001-of-00005 10.9M
Q3_K_S/Qwen3.5-397B-A17B-Q3_K_S-00002-of-00005 49.6G
Q3_K_S/Qwen3.5-397B-A17B-Q3_K_S-00003-of-00005 49.7G
Q3_K_S/Qwen3.5-397B-A17B-Q3_K_S-00004-of-00005 49.7G
Q3_K_S/Qwen3.5-397B-A17B-Q3_K_S-00005-of-00005 15.2G
config.json                                      (cached)
```

**Why Q3_K_S and not Q4_K_S or Q5_K_S?** vLLM's in-memory footprint for GGUF models is ~26% larger than the file size on disk, because it re-encodes some tensors to GPU-native formats during loading. On 2× H200 141 GiB GPUs, Q4_K_S (228 GB file) loads to ~136 GiB/GPU — leaving only ~1.7 GiB free, which is insufficient for vLLM's `FusedMoE.create_weights` 4 GiB staging tensor. Q3_K_S (164 GB file) loads to ~100 GiB/GPU, leaving ~38 GiB of headroom.

**3. Download model weights and config**

```bash
hf download unsloth/Qwen3.5-397B-A17B-GGUF \
  --include "config.json" \
  --include "Q3_K_S/*"
```

No `--local-dir` needed — `HF_HOME` is set so `huggingface_hub` caches files there automatically. Files persist on the PVC across jobs.

If other quantisation variants (e.g. `Q4_K_S/`, `Q5_K_S/`) are already in the snapshot directory, delete them so vLLM auto-detects only Q3_K_S:

```bash
SNAP="${HF_HOME}/hub/models--unsloth--Qwen3.5-397B-A17B-GGUF/snapshots"
# List snapshot hash(es)
ls $SNAP/
# Remove unwanted quant directories (symlinks only — actual blobs are unaffected)
rm -rf $SNAP/<hash>/Q4_K_S $SNAP/<hash>/Q5_K_S
```

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
| `HF_TOKEN_PATH` | Path to the HuggingFace token file **inside the container** (i.e. the PVC mount path). `huggingface_hub` reads the token from this file at runtime — no local file access or pre-export needed. |
| `VLLM_WORKER_MULTIPROC_METHOD` | Set to `spawn` to avoid CUDA context inheritance errors in multi-GPU worker processes |

### `vllm serve` flags

| Flag | Value | Purpose |
|------|-------|---------|
| *(model)* | `unsloth/Qwen3.5-397B-A17B-GGUF` | HuggingFace GGUF repo; vLLM resolves the architecture from `config.json` in the HF cache and auto-detects the Q3_K_S shards (164 GB file, ~100 GiB/GPU loaded with TP=2). Ensure only one quantisation variant is present in the snapshot — see [Pre-downloading model weights] for cleanup instructions |
| `--language-model-only` | *(flag)* | Forces text-generation mode; skips vision encoder initialisation which requires files not present in the GGUF repo |
| `--tensor-parallel-size` | `2` | Shards weights across 2 GPUs (~100 GiB each with Q3_K_S); must match `--gpu-devices-request` |
| `--enable-expert-parallel` | *(flag)* | Distributes MoE experts across GPUs; this model has 512 experts (10 routed + 1 shared per token) |
| `--reasoning-parser` | `qwen3` | Parses Qwen3 chain-of-thought `<think>` blocks and exposes them separately in the API response |
| `--tokenizer` | `Qwen/Qwen3.5-397B-A17B` | Points to the base model tokenizer, which includes the `<think>`/`</think>` special tokens required by the reasoning parser |
| `--gpu-memory-utilization` | `0.95` | Fraction of GPU memory reserved for model weights + KV cache. With Q3_K_S occupying ~72% of VRAM, `0.95` reserves sufficient room for the KV cache while leaving headroom for the FusedMoE staging tensor during model load |
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
| `CUDA out of memory` during model load | vLLM converts K-quant GGUF formats (Q3_K_S, Q4_K_S, Q5_K_S) to an internal Q4 representation on GPU at load time. All three quantisations produce an identical in-memory footprint of ~136 GiB/GPU (~272 GiB across TP=2), regardless of the GGUF file size on disk (164 GB / 228 GB / 273 GB respectively). On 2× H200 141 GiB GPUs, this leaves only ~1.7 GiB free — insufficient for `FusedMoE.create_weights` which requires a ~4 GiB BF16 staging tensor during model `__init__`. `--cpu-offload-gb` does not help (offloading occurs after loading, OOM occurs during `__init__`). `--enforce-eager`, `--max-num-seqs`, and `--gpu-memory-utilization` only affect post-load KV cache profiling and cannot reduce the weight-loading footprint. Increasing to 4 GPUs also does not help due to higher NCCL buffer overhead. **This model cannot be served on 2× H200 141 GiB with vLLM's current GGUF implementation.** | Use the native HuggingFace model format with GPTQ or AWQ quantisation instead (see [Serving LLMs with vLLM (native HF)](vllm_inference_native.html)), or wait for a vLLM release that supports lower-precision GPU kernels for K-quant GGUF formats. |
| `Can't load image processor … preprocessor_config.json` | vLLM attempting multimodal init; `preprocessor_config.json` not present in the GGUF repo | Add `--task generate` to skip vision encoder initialisation |
| Multimodal (image inputs) not supported | The GGUF repo omits `preprocessor_config.json` and the vision encoder weights (`mmproj-*.gguf`). vLLM loads the image processor from the GGUF snapshot path — symlinking these files in from the base model repo is unreliable because symlinks created in JupyterLab (`/scratch/rds-core-sih4hpc-rw/…`) resolve to a different path inside inference containers (`/scratch/pvc-rds-core-sih4hpc-rw/…`). Use `--task generate` for text-only serving, or switch to the native HF model format (see [Serving LLMs with vLLM (native HF)](vllm_inference_native.html)) |
| `Qwen3ReasoningParser could not locate think tokens` | External tokenizer missing special tokens | Point `--tokenizer` to `Qwen/Qwen3.5-397B-A17B` (base model), not the GGUF repo |
| `RuntimeError: Unknown gguf model_type: qwen3_5_moe` | The `repo:revision` syntax (e.g. `:Q4_K_S`) is interpreted as a HuggingFace git revision, not a subdirectory. That revision has no `config.json`, so vLLM falls back to reading the GGUF file header which contains `qwen3_5_moe` — a type not yet registered in this vLLM build | Remove the revision suffix; use `unsloth/Qwen3.5-397B-A17B-GGUF` only. vLLM resolves architecture from `config.json` on the main branch and auto-detects the correct GGUF shards in the cache. If multiple quantisation levels are cached, delete the unused shards to avoid ambiguity |
| `unrecognized arguments: --gguf-file` | Flag not supported in this vLLM version | Remove `--gguf-file`; pass the model as the base repo ID without a revision suffix |
| Startup takes 60–90 min | CUDA graph compilation across 51 batch sizes | Add `--enforce-eager` to skip graph capture |
| `hf download` hangs partway through | XET transfer protocol stalling | Set `HF_HUB_DISABLE_XET=1` before downloading (see [hf_transfer#30](https://github.com/huggingface/hf_transfer/issues/30#issuecomment-2878604131)) |
| vLLM cannot find cached weights even though download succeeded | PVC mount path differs between workloads: JupyterLab mounts at `/scratch/rds-core-sih4hpc-rw`, inference containers at `/scratch/pvc-rds-core-sih4hpc-rw` | Ensure `HF_HOME` in both contexts appends the same relative path (e.g. `/huggingface`) to their respective mount roots |
