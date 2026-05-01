#!/bin/bash
# Spin up a JupyterLab workspace for editing files on the PVC.
# Connect via the Run.ai UI Connect button once the workload is Running.
# Delete when done:
#   runai workspace delete vim-editor -p rds-core-sih4hpc-rw

runai workspace submit vim-editor \
  -p rds-core-sih4hpc-rw \
  -i sydneyinformaticshub/dgx-interactive-jupyterlab:latest \
  --existing-pvc claimname=pvc-rds-core-sih4hpc-rw,path=/scratch/pvc-rds-core-sih4hpc-rw \
  --external-url container=8888 \
  -c \
  -- jupyter lab --ip=0.0.0.0 --port=8888 --no-browser --allow-root --NotebookApp.token=''
