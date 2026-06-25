# How to run AlphaFold3 on the SIH GPU Cluster

[AlphaFold3](https://github.com/google-deepmind/alphafold3) predicts the 3D structure of proteins, nucleic acids, and small molecules. You describe your molecules in a simple text file and AlphaFold3 returns predicted 3D structures with confidence scores.

In this guide, AlphaFold3 will be run as two separate jobs:

1. **Alignment job (CPU only):** searches large sequence databases to find proteins related to yours. No GPU required.
2. **Structure prediction job (GPU):** uses those alignment results to generate 3D structure predictions.

Splitting the work this way means the GPU is only reserved for the short prediction step, keeping your compute costs low.

:::{.callout-note}
**What SIH provides (no action needed):**

- A ready-to-use AlphaFold3 container image

**What you need to set up once before your first prediction:**

- AlphaFold3 model weights (`af3.bin`) from Google DeepMind - [Step 1](#step-1-request-model-weights)
- Sequence databases downloaded to your PVC (~394 GB) - [Step 2](#step-2-download-sequence-databases)
:::

## Prerequisite

- An active Run:AI project and PVC (see [Accessing the SIH GPU Cluster](access.html))
- A running [JupyterLab workload](jupyter_tutorial.html) - you will use the JupyterLab terminal to run the setup commands in Steps 1 and 2

## Step 1: Request model weights

AlphaFold3's model weights are provided by Google DeepMind under a non-commercial licence. You must request access individually. Approval typically takes around 3 business days at the fastest.

1. Follow the request instructions on the [AlphaFold3 GitHub page](https://github.com/google-deepmind/alphafold3). You will fill in a short form and receive an email when access is approved.
2. Once you receive the approval email, open a terminal in JupyterLab (**File → New → Terminal**) and run the following commands. Replace `<PROJECT_ID>` with your Run:AI project name and `<DOWNLOAD_URL>` with the link in the approval email:

```bash
# Install the tool needed to decompress the weights file
apt install -y -q zstd

# Create a folder for the weights on your PVC
mkdir -p /scratch/pvc-<PROJECT_ID>/alphafold3/params
cd /scratch/pvc-<PROJECT_ID>/alphafold3/params

# Download the weights (this may take a few minutes)
wget -O af3.bin.zst <DOWNLOAD_URL>

# Decompress the file (requires ~2 GB of free space temporarily)
zstd -d af3.bin.zst

# Protect the file so only you can read it (required by the licence)
chmod 400 af3.bin

# Remove the compressed archive to recover space
rm af3.bin.zst
```

The decompressed `af3.bin` file is approximately 1.1 GB.

## Step 2: Download sequence databases

:::{.callout-note}
Shared databases available to all cluster users are coming soon. Once live, this step will no longer be needed.
:::

The alignment job (Step 4) searches several large sequence databases. These must be downloaded once to your PVC. The full set is approximately 394 GB; allow several hours for the download to complete. The script is resumable if interrupted.

In your JupyterLab terminal, run:

```bash
# Download the AlphaFold3 setup scripts
git clone https://github.com/google-deepmind/alphafold3.git

# Download all sequence databases to your PVC (takes several hours)
bash alphafold3/fetch_databases.sh /scratch/pvc-<PROJECT_ID>/alphafold3/db
```

:::{.callout-important}
Leave the terminal running until the download completes. Closing the JupyterLab session will interrupt it. You can work through Step 3 in a separate terminal tab while the download runs.
:::

* Add that you can close the browser, but the terminal jobs needs to persist.

## Step 3: Create your input file

AlphaFold3 reads a JSON file - a structured text file that describes the molecules you want to model. Create this file in JupyterLab before submitting your jobs.

1. In the JupyterLab file browser (left panel), navigate to `/scratch/pvc-<PROJECT_ID>/alphafold3/`
2. Select **File → New → Text File** to create a new empty file
3. Right-click the new file and rename it to `oxytocin.json`
4. Paste the following content into the file and save:

```json
{
  "name": "oxytocin",
  "sequences": [
    {
      "protein": {
        "id": "A",
        "sequence": "CYIQNCPLG"
      }
    }
  ],
  "bondedAtomPairs": [
    [["A", 1, "SG"], ["A", 6, "SG"]]
  ],
  "modelSeeds": [1],
  "dialect": "alphafold3",
  "version": 2
}
```

This example describes [oxytocin](https://www.uniprot.org/uniprotkb/P01178), a 9-residue protein with a disulfide bond between cysteines at positions 1 and 6. For your own proteins, replace the `sequence` value with your amino-acid sequence in single-letter code and remove `bondedAtomPairs` unless you are specifying bonds explicitly. The full input format is described in the [official documentation](https://github.com/google-deepmind/alphafold3/blob/main/docs/input.md).

:::{.callout-note collapse="true"}
## JSON field reference

| Field | Description |
|---|---|
| `name` | Label for the prediction that is used in output file names |
| `sequences[].protein.id` | Chain identifier in the output structure (`"A"`, `"B"`, …) |
| `sequences[].protein.sequence` | Amino-acid sequence in single-letter code. One continuous string, no spaces or line breaks |
| `bondedAtomPairs` | Explicit bonds to enforce. Each entry is `[chain_id, residue_number, atom_name]`. Use `"SG"` on cysteine (`C`) for disulfide bonds; residue numbers are 1-based. Omit this field if you have no explicit bonds. |
| `modelSeeds` | Each number produces one predicted structure — add more seeds for greater structural diversity |
| `dialect` / `version` | Always `"alphafold3"` and `2` |
:::

## Step 4: Run the alignment job

The alignment job searches the sequence databases and produces pre-computed features used in Step 5. It runs on CPU only and does not need a GPU.

1. In the Run:AI web interface, navigate to **Workload Manager** → **Workloads** and click **NEW WORKLOAD**
2. Select **Training** from the workload type dropdown

<!-- TODO: screenshot fig/af3_new_training_workload.png — Run:AI new workload dialog with Training selected -->

3. Configure the workload:
    - **Cluster**: set automatically - no change needed
    - **Project**: select your project
    - **Templates**: select *Start from scratch*
    - **Name**: give the job a unique name, e.g. `oxytocin-align`

4. Under **Environment**, enter the image URL:
    ```
    sydneyinformaticshub/alphafold3:v3.0.2-26060624
    ```
5. Under **Command**, enter (replacing `<PROJECT_ID>` throughout):
    ```
    python run_alphafold.py --json_path=/scratch/pvc-<PROJECT_ID>/alphafold3/oxytocin.json --model_dir=/scratch/pvc-<PROJECT_ID>/alphafold3/params --db_dir=/scratch/pvc-<PROJECT_ID>/alphafold3/db --output_dir=/scratch/pvc-<PROJECT_ID>/alphafold3/output --run_inference=false
    ```

* I think PROJECT_ID can be replaced as a variable?

6. Under **Compute resources**, set: GPU fraction — none (0), CPU cores — 8, Memory — 64 GB

* Convert to table

7. Under **Data & sources**, attach your PVC with mount path `/scratch/pvc-<PROJECT_ID>`
* Note that this is an important step to access the databases, weights, and inputs

8. Click **Create** to submit

<!-- TODO: screenshot fig/af3_alignment_running.png — Run:AI Workloads page showing alignment job in Running state -->

Wait for the job status to show **Completed** before continuing to Step 5. This typically takes around 7 minutes.

* Need an output, what does success look like?

## Step 5: Run the structure prediction job

Once the alignment job has completed, submit the GPU step. This reads the pre-computed features written by Step 4.

1. Navigate to **Workloads** → **NEW WORKLOAD** → **Training**
2. Configure the workload:
    - **Project**: same project as Step 4
    - **Name**: a unique name, e.g. `oxytocin-inference`
    - **Environment** image: `sydneyinformaticshub/alphafold3:26060604`
3. Under **Command**, enter (replacing `<PROJECT_ID>` throughout):
    ```
    python run_alphafold.py --json_path=/scratch/pvc-<PROJECT_ID>/alphafold3/oxytocin.json --model_dir=/scratch/pvc-<PROJECT_ID>/alphafold3/params --output_dir=/scratch/pvc-<PROJECT_ID>/alphafold3/output --run_data_pipeline=false --num_diffusion_samples=5
    ```
4. Under **Compute resources**, set the GPU fraction to 0.5 (half an H200, ~70 GB VRAM), CPU cores to 8, and memory to 64 GB
5. Under **Data & sources**, attach your PVC with mount path `/scratch/pvc-<PROJECT_ID>`
6. Click **Create** to submit

<!-- TODO: screenshot fig/af3_inference_complete.png — Run:AI Workloads page showing inference job Completed -->

## Retrieve your results

Once the inference job shows **Completed**, your output files are in:

```
/scratch/pvc-<PROJECT_ID>/alphafold3/output/oxytocin/
```

This folder contains predicted structure files (`.cif` format) and JSON confidence score files for each predicted model. Structure files can be viewed in [Mol\*](https://molstar.org/viewer/) or [PyMOL](https://pymol.org/).

:::{.callout-note}
A 9-residue peptide (oxytocin) takes approximately 8 minutes total: ~7 minutes for alignment and ~1 minute for structure prediction. Longer proteins take proportionally more time for the alignment step.
:::

## Troubleshooting

| Symptom | Likely cause | Fix |
|---------|-------------|-----|
| Job stays in `Pending` | No suitable node is available (e.g. no free GPU for the inference job) | Wait for a free slot; check the workload queue in the Run:AI web interface |
| Job stuck in `ContainerCreating` for 5–10 min | The container image is being downloaded to that node for the first time (~15 GB) | No action needed; check the workload **Events** tab for a `Pulled` message |
| `FileNotFoundError: af3.bin` | The weights file is missing or in the wrong location | Confirm `af3.bin` is directly inside `alphafold3/params/`, not inside a subfolder |
| `FileNotFoundError` for a database file | A database file is missing or has an unexpected name | Verify all files from `fetch_databases.sh` are in `alphafold3/db/` with their original names |
| XLA compilation takes 20+ minutes on first run | The GPU kernel is compiled on first use for your protein's length | Expected on the first run only; subsequent runs reuse the compiled result stored in `output_dir` |

## Coming soon

- **Shared databases** — sequence databases will be available on a shared cluster path for all users, removing the need for [Step 2](#step-2-download-sequence-databases)
- **PyMol** to visualise output data on the cluster https://hub.docker.com/r/pegi3s/pymol
