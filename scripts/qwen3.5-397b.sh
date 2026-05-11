#!/bin/bash

# Qwen3.5-397B-A17B (MoE) — vLLM GGUF endpoint
# Model: https://huggingface.co/unsloth/Qwen3.5-397B-A17B-GGUF
# Quantisation: Q5_K_S (~273 GB); requires 2× 141 GB GPUs

PROJECT_ID='rds-core-sih4hpc-rw'

runai inference submit vllm-qwen35-397b \
  -p "${PROJECT_ID}" \
  --image vllm/vllm-openai:cu129-nightly-1acd67a795ebccdf9b9db7697ae9082058301657 \
  --image-pull-policy IfNotPresent \
  -c \
  --gpu-devices-request 2 \
  --existing-pvc claimname="pvc-${PROJECT_ID}",path="/scratch/pvc-${PROJECT_ID}" \
  -e HF_HOME="/scratch/pvc-${PROJECT_ID}/huggingface" \
  -e HF_HUB_OFFLINE=1 \
  -e VLLM_WORKER_MULTIPROC_METHOD=spawn \
  --serving-port container=8000,protocol=http \
  --large-shm \
  --initialization-timeout-seconds 1800 \
  -- vllm serve unsloth/Qwen3.5-397B-A17B-GGUF:Q5_K_S \
    --tensor-parallel-size 2 \
    --enable-expert-parallel \
    --mm-encoder-tp-mode data \
    --mm-processor-cache-type shm \
    --reasoning-parser qwen3 \
    --tokenizer Qwen/Qwen3.5-397B-A17B \
    --enable-prefix-caching \
    --max-model-len 32768
