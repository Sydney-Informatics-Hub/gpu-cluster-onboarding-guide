#!/bin/bash
# Submit the Triton vLLM inference workload to Run.ai
# Corresponds to Steps 6-7 of notebooks/triton_tutorial.md

runai inference submit triton-vllm1 \
  -p rds-core-sih4hpc-rw \
  -i nvcr.io/nvidia/tritonserver:25.06-vllm-python-py3 \
  --image-pull-policy IfNotPresent \
  -c \
  --gpu-portion-request 1 \
  --existing-pvc claimname=pvc-rds-core-sih4hpc-rw,path=/scratch/pvc-rds-core-sih4hpc-rw \
  -e HF_HOME=/scratch/pvc-rds-core-sih4hpc-rw/huggingface \
  -e HUGGING_FACE_HUB_TOKEN=hf_xxxxxxx \
  --serving-port container=9000,protocol=http \
  --min-replicas 1 \
  --max-replicas 1 \
  --initialization-timeout-seconds 600 \
  -- bash -c "cd /opt/tritonserver/python/openai && python3 openai_frontend/main.py --model-repository=/scratch/pvc-rds-core-sih4hpc-rw/model_repository --tokenizer Qwen/Qwen3-4B"

