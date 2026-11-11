# Appendix: What's Inside Each SIH Docker Image

This page lists the key libraries, language runtimes, and tools pre-installed in each `sydneyinformaticshub` image referenced in this guide, so you can check compatibility before building on top of one (see [Building and using a custom Docker image](build_docker_image.html)) or launching a workload directly.

::: {.callout-tip}
**Want the exact, up-to-the-minute package list?** Versions here are a snapshot and images are rebuilt from time to time, so a package may have moved on since this page was last updated. Pull the image and ask it directly:

```bash
# Full Python package list
docker run --rm sydneyinformaticshub/dgx-interactive-jupyterlab:latest pip list

# Specific framework versions
docker run --rm sydneyinformaticshub/dgx-interactive-jupyterlab:latest \
  python3 -c "import torch, tensorflow as tf; print(torch.__version__, tf.__version__)"

# OS + CUDA version
docker run --rm sydneyinformaticshub/dgx-interactive-jupyterlab:latest bash -c "cat /etc/os-release; nvcc --version"
```
:::

## [`sydneyinformaticshub/dgx-interactive-jupyterlab`](https://hub.docker.com/r/sydneyinformaticshub/dgx-interactive-jupyterlab)

Used by the [Jupyter Lab](jupyter_tutorial.html) tutorial's `jupyter-notebook` environment, and as the base image for custom builds in [Building and using a custom Docker image](build_docker_image.html).

Built on top of [`cschranz/gpu-jupyter:v1.9_cuda-12.6_ubuntu-24.04`](https://github.com/iot-salzburg/gpu-jupyter/blob/master/extra/README-versions.md#cschranzgpu-jupyterv19_cuda-126_ubuntu-2404), which itself bundles the [Jupyter Docker Stacks](https://jupyter-docker-stacks.readthedocs.io/) data science stack (pandas, scikit-learn, matplotlib, seaborn, scipy, sympy, etc. — see the upstream project for the full pinned list).

| Component | Version |
|---|---|
| OS | Ubuntu 24.04.2 LTS (Noble Numbat) |
| CUDA | 12.6 (V12.6.85) |
| Python | 3.12.9 |
| R | 4.3.3 |
| Julia | 1.11.3 |
| JupyterLab | 4.3.5 |
| Jupyter Notebook | 7.3.2 |
| IPython | 8.32.0 |
| NumPy | 2.0.2 |
| PyTorch | 2.6.0 (+cu126) |
| TensorFlow | 2.18.0 |
| Ollama | latest at build time (unpinned) |

**SIH additions on top of the base image:** `rsync`, `ssh`, `htop`, `openjdk-21-jre`, `curl`, `nano`, `gosu`, `build-essential`, `pkg-config`, `libnss-wrapper`, `less`, `iputils-ping`, `git-lfs`.

## [`sydneyinformaticshub/dgx-interactive-terminal`](https://hub.docker.com/r/sydneyinformaticshub/dgx-interactive-terminal)

Used by the [terminal workload via the CLI](CLI.html). A lighter, non-Jupyter image intended for command-line/scripting sessions.

Built on top of [`nvidia/cuda:13.0.1-cudnn-devel-ubuntu24.04`](https://hub.docker.com/r/nvidia/cuda).

| Component | Version |
|---|---|
| OS | Ubuntu 24.04 |
| CUDA | 13.0.1 |
| cuDNN | included (`-cudnn-devel` variant — full toolkit, not just runtime) |
| Python | 3.12 (Ubuntu 24.04 default), via `python3-venv`/`python3-pip` |
| IPython | latest at build time (unpinned), installed into a dedicated venv at `/opt/dgxenv` |

**SIH additions on top of the base image:** `rsync`, `ssh`, `htop`, `openjdk-21-jre`, `curl`, `nano`, `gosu`, `build-essential`, `pkg-config`, `libnss-wrapper`, `less`.

::: {.callout-note}
No Jupyter, PyTorch, or TensorFlow are pre-installed here — this is a minimal base for terminal/CLI work. Install what you need into `/opt/dgxenv` or build your own image from this one.
:::

## [`sydneyinformaticshub/alphafold3`](https://hub.docker.com/r/sydneyinformaticshub/alphafold3)

Used by the [How to run AlphaFold3](alphafold3_guide.html) guide. Unlike the images above, this is **not** built from an SIH Dockerfile — it is built from the official [AlphaFold3 repository](https://github.com/google-deepmind/alphafold3), which maintains its own [`Dockerfile`](https://github.com/google-deepmind/alphafold3/blob/main/docker/Dockerfile) and pinned [`requirements.txt`](https://github.com/google-deepmind/alphafold3/blob/main/requirements.txt) (JAX with CUDA support, plus the sequence-search tools AlphaFold3 depends on). Check those files directly for the exact dependency versions matching the image tag in use (e.g. `v3.0.2-20260604`).
