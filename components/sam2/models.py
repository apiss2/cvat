# SPDX-License-Identifier: MIT
"""SAM2 model selection and build-only checkpoint inputs for cvatctl."""
from contextlib import contextmanager
from pathlib import Path
import shutil
import tempfile

DEFAULT_MODEL = "sam2.1_hiera_small"
MODELS = {
    "sam2.1_hiera_tiny": "configs/sam2.1/sam2.1_hiera_t.yaml",
    "sam2.1_hiera_small": "configs/sam2.1/sam2.1_hiera_s.yaml",
    "sam2.1_hiera_base_plus": "configs/sam2.1/sam2.1_hiera_b+.yaml",
    "sam2.1_hiera_large": "configs/sam2.1/sam2.1_hiera_l.yaml",
}
DOWNLOAD = '''      - kind: RUN
        value: mkdir -p /opt/nuclio/checkpoints && curl --fail --location --retry
          3 https://dl.fbaipublicfiles.com/segment_anything_2/092824/sam2.1_hiera_small.pt
          -o /opt/nuclio/checkpoints/sam2.1_hiera_small.pt && python -m pip freeze > /opt/nuclio/build-requirements.txt
'''


def environment(values: dict[str, str]) -> dict[str, str]:
    name = values.get("SAM2_MODEL") or DEFAULT_MODEL
    if name not in MODELS:
        raise ValueError("SAM2_MODEL must be one of: " + ", ".join(MODELS))
    return {"SAM2_MODEL": name, "SAM2_CONFIG": MODELS[name],
            "SAM2_CHECKPOINT": f"/opt/nuclio/checkpoints/{name}.pt"}


def checkpoint_source(values: dict[str, str], root: Path, *, verify_file=True) -> Path | None:
    raw = values.get("SAM2_CHECKPOINT_HOST", "")
    if not raw:
        return None  # Use the official checkpoint for SAM2_MODEL.
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise ValueError("SAM2_CHECKPOINT_HOST must be an absolute path outside the checkout")
    path = path.resolve()
    if path == root.resolve() or root.resolve() in path.parents:
        raise ValueError("Keep source checkpoint weights outside the checkout")
    if verify_file and (not path.is_file() or not path.stat().st_size):
        raise ValueError("SAM2_CHECKPOINT_HOST must name a nonempty checkpoint file")
    return path


def render(source: str, values: dict[str, str]) -> str:
    name = environment(values)["SAM2_MODEL"]
    if source.count(DOWNLOAD) != 1:
        raise ValueError("SAM2 download recipe changed; review the model renderer")
    if values.get("SAM2_CHECKPOINT_HOST"):
        source = source.replace(DOWNLOAD, "      - kind: RUN\n"
                                "        value: python -m pip freeze > /opt/nuclio/build-requirements.txt\n")
    return source.replace(DEFAULT_MODEL, name)


@contextmanager
def build_source(source: Path, values: dict[str, str], state: Path, *, no_build=False):
    """One private copy for both functions; weights never enter the checkout."""
    if no_build or not values.get("SAM2_CHECKPOINT_HOST"):
        yield source
        return
    with tempfile.TemporaryDirectory(dir=state, prefix="sam2-source-") as directory:
        destination = Path(directory)
        for path in source.iterdir():
            if path.is_file() and path.suffix in (".py", ".yaml", ".txt", ".json"):
                shutil.copyfile(path, destination / path.name)
        target = destination / "checkpoints" / Path(environment(values)["SAM2_CHECKPOINT"]).name
        target.parent.mkdir()
        shutil.copyfile(values["SAM2_CHECKPOINT_HOST"], target)
        target.chmod(0o444)
        yield destination
