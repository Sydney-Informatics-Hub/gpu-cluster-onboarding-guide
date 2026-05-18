#!/bin/bash

# Qwen3.5-122B-A10B — vLLM FP8 endpoint
# Model: https://huggingface.co/Qwen/Qwen3.5-122B-A10B-FP8
# Format: FP8 (official Qwen quantisation, ~127 GB); requires 2× 141 GiB GPUs

PROJECT_ID="YOUR_PROJECT"
HF_TOKEN_PATH="YOUR_PATH"
NGPUS=2

runai inference submit vllm-qwen35-122b \
  -p "${PROJECT_ID}" \
  --image vllm/vllm-openai:cu129-nightly-1acd67a795ebccdf9b9db7697ae9082058301657 \
  --image-pull-policy IfNotPresent \
  -c \
  --gpu-devices-request "$NGPUS" \
  --existing-pvc claimname="pvc-${PROJECT_ID}",path="/scratch/pvc-${PROJECT_ID}" \
  -e HF_HOME="/scratch/pvc-${PROJECT_ID}/huggingface" \
  -e HF_TOKEN_PATH="${HF_TOKEN_PATH}" \
  -e VLLM_WORKER_MULTIPROC_METHOD=spawn \
  --serving-port container=8000,protocol=http \
  --large-shm \
  --initialization-timeout-seconds 1800 \
  --min-replicas 0 \
  --max-replicas 1 \
  --scale-to-zero-retention-seconds 1800 \
  -- vllm serve Qwen/Qwen3.5-122B-A10B-FP8 \
    --tensor-parallel-size "$NGPUS" \
    --enable-expert-parallel \
    --reasoning-parser qwen3 \
    --gpu-memory-utilization 0.90 \
    --enable-prefix-caching \
    --max-model-len 131072
