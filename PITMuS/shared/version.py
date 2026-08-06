"""Single source of truth for the dataset output folder name.

`PITMuS/gen_dataset.py` writes its CSVs into `<test-project>/PITMuS_dataset/`;
the evaluation notebook reads from the same folder. Change it here and both
sides move together.
"""


def dataset_dirname(version=None):
    """Folder (under a test-project) holding the generated dataset CSVs."""
    return "PITMuS_dataset"
