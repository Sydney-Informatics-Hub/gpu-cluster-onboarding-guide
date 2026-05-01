#!/bin/bash
# Submit an Open WebUI workspace that connects to the Triton inference workload.
# Corresponds to notebooks/triton_tutorial.md
#
# Prerequisites: the triton-vllm1 inference workload must be running first.
# Submit it with: bash triton-inference-endpoint.sh

# Internal cluster address of the Triton inference server.
# Format: <workload-name>.runai-<project-id>.svc.cluster.local
TRITON_URL="http://triton-vllm1.runai-rds-core-sih4hpc-rw.svc.cluster.local"

runai workspace submit openwebui \
  -p rds-core-sih4hpc-rw \
  -i ghcr.io/open-webui/open-webui:main \
  --image-pull-policy IfNotPresent \
  --existing-pvc claimname=pvc-rds-core-sih4hpc-rw,path=/scratch/pvc-rds-core-sih4hpc-rw \
  -e OPENAI_API_BASE_URL="${TRITON_URL}/v1" \
  -e OPENAI_API_KEY=dummy \
  -e WEBUI_AUTH=false \
  -e HF_HOME=/scratch/pvc-rds-core-sih4hpc-rw/huggingface \
  -e DEFAULT_MAX_TOKENS=4096 \
  --external-url container=8080
