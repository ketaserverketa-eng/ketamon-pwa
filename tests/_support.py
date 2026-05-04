import shutil
import tempfile
from pathlib import Path


TEST_TMP_ROOT = Path(__file__).resolve().parent / "_tmp"
TEST_TMP_ROOT.mkdir(parents=True, exist_ok=True)


def make_temp_workspace(prefix: str) -> Path:
    TEST_TMP_ROOT.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=f"{prefix}-", dir=TEST_TMP_ROOT))


def cleanup_temp_workspace(path: str | Path) -> None:
    shutil.rmtree(Path(path), ignore_errors=True)
