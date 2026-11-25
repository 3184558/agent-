"""显式导入已下载的官方 XML；不联网下载用户资料，不抓外链。"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from app.database import initialize
from app.medlineplus import import_snapshot

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("xml", type=Path)
parser.add_argument(
    "--selection", type=Path, default=Path("evaluation/medlineplus_pilot.json")
)
args = parser.parse_args()
initialize()
release = import_snapshot(
    args.xml,
    json.loads(args.selection.read_text(encoding="utf-8")),
    json.loads(args.xml.with_name("download.json").read_text(encoding="utf-8")),
)
print(json.dumps(release, ensure_ascii=False, indent=2))
