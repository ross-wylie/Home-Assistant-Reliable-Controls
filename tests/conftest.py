"""Make the add-on modules importable when tests run from the repo root."""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "reliable_controls"))
