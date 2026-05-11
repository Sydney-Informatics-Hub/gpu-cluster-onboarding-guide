#!/bin/bash

# Downloads Qwen3.5-397B-A17B Q5_K_S weights to the PVC.
# Run from a JupyterLab terminal workload with the PVC mounted.
# See: notebooks/vllm_inference.html#pre-downloading-model-weights

PROJECT_ID='rds-core-sih4hpc-rw'

export HF_HOME="/scratch/pvc-${PROJECT_ID}/huggingface"

pip install -q hf_transfer
export HF_HUB_ENABLE_HF_TRANSFER=1

hf download unsloth/Qwen3.5-397B-A17B-GGUF \
  --include "Q5_K_S/*" \
  --local-dir "${HF_HOME}"
