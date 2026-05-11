#!/bin/bash

# Downloads Qwen3.5-397B-A17B Q5_K_S weights to the PVC.
# Run from a JupyterLab terminal workload with the PVC mounted.
# See: notebooks/vllm_inference.html#pre-downloading-model-weights

PROJECT_ID='rds-core-sih4hpc-rw'

export HF_TOKEN_PATH="/scratch/${PROJECT_ID}/<path_to_hf_token>"
export HF_HUB_DISABLE_XET=1
export HF_HOME="/scratch/${PROJECT_ID}/huggingface"

hf download unsloth/Qwen3.5-397B-A17B-GGUF --include "Q5_K_S/*"
