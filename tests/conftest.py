import os
from pathlib import Path


TEST_ROOT = Path(__file__).resolve().parent / ".runtime"
TEST_ROOT.mkdir(exist_ok=True)
os.environ["ZTP_BASE_DIR"] = str(Path(__file__).resolve().parents[1])
os.environ["ZTP_DATA_DIR"] = str(TEST_ROOT / "data")
os.environ["ZTP_ARTIFACT_DIR"] = str(TEST_ROOT / "artifacts")
os.environ["ZTP_DATABASE_URL"] = "sqlite:///{}".format(TEST_ROOT / "test.db")
os.environ["ZTP_KEA_BINARY"] = str(TEST_ROOT / "missing-kea")

