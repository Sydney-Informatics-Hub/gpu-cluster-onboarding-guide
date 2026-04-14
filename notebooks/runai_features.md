# Main Run:ai Features

## Workloads

A **workload** in Run:ai represents a unit of compute work submitted to the cluster, such as a training job, interactive session, or inference service. Workloads are scheduled and managed by the Run:ai scheduler, which optimises GPU allocation across projects and users based on defined quotas and priorities. From the Workloads page, users can submit new workloads, monitor their status, and manage existing ones.

Examples of creating workloads are included in the [Tutorials section](jupyter_tutorial.md).

## Environments
In Run:AI, an environment consists of a set of configurations that define the software setup needed to run your AI workloads. An environment typically includes:

- Base Docker image (e.g., `pytorch/pytorch`, `tensorflow/tensorflow:2.20.0-jupyter`)
- Tools (such as Jupyter, RStudio, etc.)
- Custom runtime settings to run scripts or setup commands (e.g., installing extra packages, configuring the base URL, etc.)

The SIH GPU platform has provided several basic environments for users to get started with:
![Pre-defined environments](../fig/environment_predefined.png)

:::{.callout-important}
The SIH team is actively configuring and testing new environments on the cluster. Please follow related tutorials for the applications you intend to run and make sure correct environments are selected when creating workloads. **Your workload will very likely fail to start if a wrong environment is loaded.**
:::

## Compute resources
**Compute resources** in Run:ai define the hardware specifications allocated to a workload, including the number of GPUs, CPUs, and memory. Rather than configuring these settings each time a workload is submitted, compute resources can be saved as named presets and reused across workloads and templates. This simplifies job submission and ensures consistent resource allocation. When creating a workload, users select a compute resource preset that best fits their task — for example, a single GPU with moderate memory for interactive development, or multiple GPUs for large-scale distributed training.

We provide a list of predefined Compute Resources such as "one-gpu", "two-gpu-16-cpu-memory-boost", "data-transfer", *etc*:
![Pre-defined compute resources](../fig/compute_resources.png)

You may also further adjust the requested resources during workload configuration to better suit your purposes.


## Data sources
**Data sources** in Run:ai provide a way to connect storage to your workloads, making datasets, model weights, and output directories accessible inside the container at runtime.

Persistent Volume Claims (PVCs) are currently the only supported data source type. By default, 1 TB of PVC is provisioned when the Run:ai project is created and can be reused across multiple workloads, avoiding the need to reconfigure storage paths each time. When submitting a workload, users attach the PVC at a specified path within the container.
![Project PVC under "Data sources"](../fig/data_sources.png)

:::{.callout-important}
**PVCs are NOT backed up and should only be used as a scratch space for storing temporary data.** Please regularly transfer your data back to your RDS when working on the cluster.
:::

:::{.callout-note}
Please contact the SIH team if you do not see a PVC data source on your account.
:::