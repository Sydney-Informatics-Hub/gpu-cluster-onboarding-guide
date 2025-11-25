# The Run:AI Command Line Interface (CLI)

The Run:AI [Command Line Interface](https://run-ai-docs.nvidia.com/self-hosted/2.22/reference/cli) (CLI) is a tool that allows researchers to manage and run workloads directly from the terminal. It provides commands to submit, monitor, and control jobs on the SIH GPU cluster, as well as to manage projects, resources, and configurations. Using the CLI, users can interact with Run:AI’s platform without needing to access the graphical interface.

## Setting up the Run:AI CLI

To set up the CLI in your terminal:

1. Log into the Run:AI web interface and select the 'Researcher Command Line Interface' from the drop-down menu under the '?' icon in the top right.

![](../fig/researcher_command_line_interface.png)

2. Select your preferred operating system and copy the command indicated in the box using the icon on the right - then paste this command into a terminal session on your local machine.

![](../fig/install_cli.png)

3. Follow the prompts in your terminal to set up the CLI. Once complete you should now be able to start the CLI in a terminal using `runai login` at the command line.

Once the Run:AI CLI is set up - you can start a workflow by running a saved docker image of your choice. SIH have provided base docker images with a pre-installed set of common dependencies for GPU (`sydneyinformaticshub/dgx-interactive-gpu`) and CPU (`sydneyinformaticshub/dgx-interactive-cpu`) workflows on [dockerhub](https://hub.docker.com/u/sydneyinformaticshub), including basic packages for interactive use (e.g. ipython).


## How to Run a Terminal Environment at the Command Line

You can start a workload from a terminal session on your own laptop as long as you are connected to the University VPN. You can run this interactively which provides a simple terminal environment running inside the SIH GPU cluster.

### Example running a GPU workflow using the CLI

1. Login to the Run:AI CLI at the command line:

```bash
runai login
```

you will be prompted for your password and Okta credentials in a browser window during this step.

2. Set your project (replace `<my project>` with the name of your project):

```bash
runai project set <my_project>
```

3. To run an `sydneyinformaticshub/dgx-interactive-terminal` container with an interactive terminal session including mounting your projects existing PVC in /scratch inside the container you can use the following command (be sure to replace everything in brackets `<...>` with values specific to your requirements):

```bash
runai workspace submit <workspace-name> --image sydneyinformaticshub/dgx-interactive-terminal --gpu-devices-request 1 --cpu-core-request 1.0 --run-as-user --existing-pvc claimname=<pvc-name>,path=/scratch/<my_project> --attach
```

Here is a brief rundown of the arguments of the command above:

- `runai workspace submit <workspace-name>` will run a new workspace and give it the name specified in `<workspace-name>`

- `--image sydneyinformaticshub/dgx-interactive-terminal` will run the base Docker image located at `sydneyinformaticshub/dgx-interactive-terminal`, you can replace this image with your own, perhaps built yourself with extra package installs and using this image as a base

- `--gpu-devices-request 1 --cpu-core-request 1.0` requests 1 GPU and 1 CPU for the workflow. There are multiple options for selecting GPU and CPU RAM and devices, see [here](https://run-ai-docs.nvidia.com/self-hosted/2.22/reference/cli/runai/runai_workspace_submit) or use `runai workspace submit --help` for a full list of options

- `--run-as-user` will run the workflow using your user id and group ids inherited from DashR for your project. These will be the user and groups for the account you logged into Run:AI with in step 1 above. You should normally use this option otherwise user and group ids may not be set up correctly inside your workspace

- `--existing-pvc claimname=<pvc-name>,path=/scratch` will mount an existing PVC associated with your project into the running workload. Replace <pvc-name> with the name of your PVC. This will mount the PVC into `/scratch` inside the running container - you can change this mount point to whatever you prefer inside the running workload. You can also omit this flag entirely if you do not intend to use a PVC in your workspace.

- `--attach` will run the container and attach to it, which in this case will provide an interactive shell session inside it.

## Run a workflow in the background and connect to it using the CLI

Using the CLI it is also possible to set up a workflow that persists in the background. You can then connect to it whenever you like or as many times as you like - this is a useful option if you want to reserve resources that you can keep available as you require or you want to share resources interactively amongst multiple users.

To set this up follow the steps 1 and 2 above, then change the command in step 3 to:

```bash
runai workspace submit <workspace-name> --image sydneyinformaticshub/dgx-interactive-terminal --gpu-devices-request 1 --cpu-core-request 1.0 --run-as-user --existing-pvc claimname=<pvc-name>,path=/scratch --command -- bash -c 'trap : TERM INT; sleep infinity & wait'
```

This will run the workload and keep it persisting in the background. You can then connect to this container whenever you like using:

```bash
runai workspace bash <workspace-name>
```

you can use this method to connect to any running workspace on the cluster as well as connect multiple terminals inside the running workspace.

Make sure you terminate the background workspace when you are finished. You can do this using:

```bash
runai workspace suspend <workspace-name>
```

to suspend the workspace so you can restart it again later or

```bash
runai workspace delete <workspace-name>
```

to delete the workspace entirely. Please note that when deleting the workspace you will lose all data inside it not saved to a mounted PVC.
