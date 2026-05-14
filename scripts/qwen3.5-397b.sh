#!/bin/bash

# Qwen3.5-397B-A17B (MoE) — vLLM GGUF endpoint
# Model: https://huggingface.co/unsloth/Qwen3.5-397B-A17B-GGUF
# Quantisation: Q3_K_S (~164 GB); requires 2× 141 GB GPUs

PROJECT_ID='rds-core-sih4hpc-rw'
NGPUS=2

runai inference submit vllm-qwen35-397b \
  -p "${PROJECT_ID}" \
  --image vllm/vllm-openai:cu129-nightly-1acd67a795ebccdf9b9db7697ae9082058301657 \
  --image-pull-policy IfNotPresent \
  -c \
  --gpu-devices-request "$NGPUS" \
  --existing-pvc claimname="pvc-${PROJECT_ID}",path="/scratch/pvc-${PROJECT_ID}" \
  -e HF_HOME="/scratch/pvc-${PROJECT_ID}/huggingface" \
  -e HF_TOKEN_PATH="/scratch/pvc-${PROJECT_ID}/fred_scratch/.hf-token" \
  -e VLLM_WORKER_MULTIPROC_METHOD=spawn \
  --serving-port container=8000,protocol=http \
  --large-shm \
  --initialization-timeout-seconds 1800 \
  -- vllm serve unsloth/Qwen3.5-397B-A17B-GGUF \
    --language-model-only \
    --tensor-parallel-size "$NGPUS" \
    --enable-expert-parallel \
    --reasoning-parser qwen3 \
    --tokenizer Qwen/Qwen3.5-397B-A17B \
    --gpu-memory-utilization 0.95 \
    --enable-prefix-caching \
    --max-model-len 32768
