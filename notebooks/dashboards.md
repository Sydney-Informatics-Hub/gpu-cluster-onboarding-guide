# Run:ai Dashboards
## Overview
This dashboard view provides holistic infrastructure information useful for both researchers and system administrators in managing and planning the resources.

### Indicators
This section presents high-level statistics of the GPU computing resources

![The "Indicators" panel under "Overview"](../fig/dashboards_overview_indicators.png)

### Cluster Load
Real-time monitoring of the cluster status in terms of GPU and CPU utilisation.

![Monitoring system-wide workload](../fig/dashboards_overview_clusterload.png)

### Queueing
Inspecting queueing jobs. Possible reasons why your jobs are queueing include:

- The number of GPUs requested to be allocated to the job has exceeded the remaining GPU quota in the project.
- The GPU cluster is currently at full capacity and therefore has no available resources to schedule the job.
- The job is waiting for other jobs to finish before it can be scheduled.

![Queueing jobs](../fig/dashboards_overview_queueing.png)

### Idle GPUs
Displaying the number of idle GPUs currently allocated to running workloads.

![Idle GPUs](../fig/dashboards_overview_idleGPUs.png)

### Running Workloads
Summary of the list of running workloads.

![Running workloads](../fig/dashboards_overview_workloads.png)

## Analytics
This dashboard provides more detailed breakdowns of the Apollo GPU cluster running status. Key
statistics that are reported at separate levels:

- Cluster
- Project
- Workloads
- Nodes

![System analytics](../fig/dashboards_analytics.png)