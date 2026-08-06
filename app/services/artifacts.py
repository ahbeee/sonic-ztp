import hashlib
import re
import uuid
from pathlib import Path
from typing import BinaryIO, Tuple


SAFE_SUFFIX = re.compile(r"^\.[A-Za-z0-9._-]{1,15}$")


def store_stream(source: BinaryIO, original_name: str, target_dir: Path, max_bytes: int) -> Tuple[str, int, str]:
    target_dir.mkdir(parents=True, exist_ok=True)
    suffix = Path(original_name).suffix
    if not SAFE_SUFFIX.match(suffix):
        suffix = ".bin"
    stored_name = "{}{}".format(uuid.uuid4().hex, suffix.lower())
    target = target_dir / stored_name
    digest = hashlib.sha256()
    size = 0
    try:
        with target.open("xb") as output:
            while True:
                chunk = source.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > max_bytes:
                    raise ValueError("Artifact exceeds maximum upload size")
                digest.update(chunk)
                output.write(chunk)
    except Exception:
        target.unlink(missing_ok=True)
        raise
    return stored_name, size, digest.hexdigest()

