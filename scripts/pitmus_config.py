# Single source of truth for the dataset/output version.
#
# Change DATASET_VERSION here and everything downstream moves together:
#   - scripts/gen_dataset.py         writes into PITMuS_dataset_fresh_generation-<VERSION>/
#   - blackbox_checks/evaluate_reconstruction.ipynb  reads that folder and stamps
#     <VERSION> into every eval*/Evaluation-* output filename.

DATASET_VERSION = "v1"


def dataset_dirname():
    """Folder (under a test-project) that holds the generated dataset CSVs."""
    return f"PITMuS_dataset_fresh_generation-{DATASET_VERSION}"
