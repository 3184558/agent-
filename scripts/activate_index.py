"""显式切回指定的向量索引发布版本。"""

import argparse
import json
import sys
from contextlib import closing
from pathlib import Path

from filelock import FileLock
from qdrant_client import QdrantClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from app.config import settings
from app.knowledge import FAMILIES, fingerprint

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--family", choices=tuple(FAMILIES), required=True)
parser.add_argument("--version", required=True)
args = parser.parse_args()
directory = settings.data_dir / "indexes"
with FileLock(settings.data_dir / "ollama.lock"):
    manifest = json.loads(
        (directory / "versions" / f"{args.version}.json").read_text(encoding="utf-8")
    )
    config = {
        k: v
        for k, v in manifest.items()
        if k
        not in (
            "index_version",
            "collection",
            "chunks",
            "documents",
            "embedding_dimensions",
            "lexical_hash",
            "lexical_file",
        )
    }
    if (
        manifest["family"] != args.family
        or manifest["index_version"] != args.version
        or fingerprint(config) != args.version
    ):
        raise ValueError("发布版本不匹配")
    with closing(QdrantClient(path=str(settings.data_dir / "vectors"))) as vectors:
        if (
            vectors.count(manifest["collection"], exact=True).count
            != manifest["chunks"]
        ):
            raise ValueError("向量分块数量不一致")
    temporary = directory / f"{args.family}.pending.json"
    temporary.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    temporary.replace(directory / f"{args.family}.json")
print(args.version)
