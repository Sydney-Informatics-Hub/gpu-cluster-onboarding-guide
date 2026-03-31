---
title: "Tutorial: Running a Triton Inference Server"
description: "A step-by-step introduction to launching a Triton Inference Server on the SIH GPU cluster, connecting to it from a Jupyter notebook, and downloading your first model."
categories: [triton, inference, run-ai]
---

## What you will learn

By the end of this tutorial you will have:

- Created a [Triton Inference Server](https://docs.nvidia.com/deeplearning/triton-inference-server/user-guide/docs/index.html) environment in [Run.ai](https://run-ai-docs.nvidia.com/self-hosted/2.22/workloads-in-nvidia-run-ai/using-inference/quick-starts/inference-quickstart)
- Launched a Jupyter notebook on the same cluster
- Connected to the server and confirmed it is live
- Listed the models the server is serving
- Downloaded a model from [HuggingFace](https://huggingface.co) to the cluster's shared storage

This tutorial does not assume any knowledge of Kubernetes or MLOps infrastructure. Where cluster-specific concepts appear, we explain them briefly inline.

**Prerequisites:**

- Access to an active Run.ai project
- A persistent volume claim (PVC) mounted at `/scratch/YOUR_PROJECT_ID/` — a shared network drive attached to your project where data and models are stored

---

## Step 1: Create a Triton server environment

An *environment* in Run.ai packages the container image and configuration needed to run a workload. We will create one specifically for Triton.

Navigate to **Environments** in the Run.ai UI and click **New Environment**. Fill in the following fields:

| Field | Value |
|---|---|
| Environment name | `triton-example-server` |
| Image URL | `runai.jfrog.io/demo/example-triton-server` |
| Workload architecture | Standard |
| Workload type | Inference |
| Endpoint | HTTP — port `8000` |
| Security | From the image |

Click **Create**.

![PLACEHOLDER: Screenshot of the Run.ai New Environment form with the fields above filled in.](images/placeholder.png){fig-alt="Run.ai New Environment form showing the triton-example-server configuration." width="80%"}

## Step 2: Launch a Triton inference workload

With the environment saved, go to **Workloads** and create a new **Inference** workload using the `triton-example-server` environment you just created. Attach your project PVC as the data source.

<!-- TODO: verify — confirm the exact workload creation steps for inference workloads in v2.22 match the UI described in the Jupyter tutorial -->

Wait until the workload status shows **Running** before continuing.

## Step 3: Launch a Jupyter notebook

We will interact with the Triton server from a Jupyter notebook running on the same cluster. Follow the [Jupyter Lab tutorial](jupyter_tutorial.md) to start a notebook workload if you do not already have one running.

Once the notebook is running, open a new notebook or terminal.

## Step 4: Install the Triton HTTP client

In a notebook cell, install the Triton client library:

```bash
%pip install tritonclient[http]
```

## Step 5: Connect to the server

The Triton server is reachable from within the cluster using its internal DNS name. This follows the pattern `<workload-name>.<namespace>.svc.cluster.local` — Kubernetes's built-in service discovery that lets workloads find each other without needing external network access.

In a new notebook cell, run:

```python
import tritonclient.http as httpclient

TRITON_URL = "example-triton-server.runai-rds-core-sih4hpc-rw.svc.cluster.local"
# Replace with the internal DNS name shown in the Run.ai workload details for your server.

client = httpclient.InferenceServerClient(url=TRITON_URL, verbose=True)

print("Server:", client.is_server_live())
print("Ready:", client.is_server_ready())
print("Models:", [m['name'] for m in client.get_model_repository_index()])
```

A successful response looks like this:

```
Server: True
Ready: True
Models: ['densenet_onnx', 'inception_graphdef', 'simple', 'simple_dyna_sequence',
         'simple_identity', 'simple_int8', 'simple_sequence', 'simple_string']
```

<!-- TODO: verify — document where users find their workload's internal DNS name in the Run.ai UI -->

![PLACEHOLDER: Screenshot of a Jupyter notebook cell showing the above output with Server: True, Ready: True, and the model list.](images/placeholder.png){fig-alt="Jupyter notebook output confirming the Triton server is live and listing available models." width="80%"}

## Step 6: Create a model repository directory

Models served by Triton must live in a *model repository* — a directory with a specific layout on your PVC. We create it now so it is ready for downloads.

In a notebook cell or terminal, run:

```bash
mkdir -p /scratch/YOUR_PROJECT_ID/model_repository
cd /scratch/YOUR_PROJECT_ID/model_repository
```

## Step 7: Authenticate with HuggingFace

Many models require a HuggingFace account to download. First install the [HuggingFace CLI](https://huggingface.co/docs/huggingface_hub/en/guides/cli):

```bash
pip install -U huggingface_hub
```

Next, create a read-scoped access token:

1. Go to [https://huggingface.co/settings/tokens](https://huggingface.co/settings/tokens)
2. Click **New token**
3. Set the scope to **Read** and give it a name such as `triton-download`
4. Copy the token and save it to a file: in a notebook terminal run `echo "<hf_your_token_here>" > ~/.hf-token`

![PLACEHOLDER: Screenshot of the HuggingFace token creation page showing the Read scope selected and the name field filled in.](images/placeholder.png){fig-alt="HuggingFace token creation page with Read scope and triton-download name." width="80%"}

Then authenticate:

```bash
hf auth login --token $(cat ~/.hf-token)
```

You should see:

```
Token is valid (permission: read).
Login successful.
```

## Step 8: Download a model to the PVC

We will download `openai/gpt-oss-120b` as our example. Before downloading, run a **dry run** to check the file sizes without transferring any data:

```bash
hf download openai/gpt-oss-120b \
  --include "original/*" \
  --local-dir /scratch/YOUR_PROJECT_ID/model_repository/gpt-oss-120b/ \
  --dry-run
```

The output will list every file and the total size — in this case approximately **65.2 GB**. Confirm you have sufficient space on your PVC before proceeding.

Once you are satisfied, run the same command without `--dry-run` to start the download:

```bash
hf download openai/gpt-oss-120b \
  --include "original/*" \
  --local-dir /scratch/YOUR_PROJECT_ID/model_repository/gpt-oss-120b/
```

![PLACEHOLDER: Screenshot of the terminal showing the dry-run output with file sizes listed and the total of 65.2G.](images/placeholder.png){fig-alt="Terminal output of hf download --dry-run listing 10 files totalling 65.2 GB." width="80%"}