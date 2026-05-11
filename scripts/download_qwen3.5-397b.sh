#!/bin/bash

# Downloads Qwen3.5-397B-A17B Q5_K_S weights and tokenizer to the PVC.
# Run from a JupyterLab terminal workload with the PVC mounted.
# See: notebooks/vllm_inference.html#pre-downloading-model-weights

PROJECT_ID='rds-core-sih4hpc-rw'

export HF_TOKEN_PATH="/scratch/${PROJECT_ID}/<path_to_hf_token>"
export HF_HUB_DISABLE_XET=1
export HF_HOME="/scratch/${PROJECT_ID}/huggingface"

hf download unsloth/Qwen3.5-397B-A17B-GGUF \
  --include "config.json" \
  --include "mmproj-BF16.gguf" \
  --include "Q5_K_S/*"

# Tokenizer + image processor from the base model — the GGUF repo lacks the <think>/<\think>
# special tokens needed by the reasoning parser, and preprocessor_config.json needed by the
# vision encoder. vLLM loads the image processor from the --tokenizer path for GGUF models.
hf download Qwen/Qwen3.5-397B-A17B \
  --include "tokenizer.json" \
  --include "tokenizer_config.json" \
  --include "vocab.json" \
  --include "merges.txt" \
  --include "chat_template.jinja" \
  --include "preprocessor_config.json" \
  --include "video_preprocessor_config.json"
