"""人工触发下载官方 XML 快照；仅访问官方资料，无用户描述或会话参数。"""

import argparse
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

import httpx

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--directory", type=Path, default=Path("data/sources/medlineplus"))
args = parser.parse_args()
with httpx.Client(timeout=120, follow_redirects=True) as client:
    page = client.get("https://medlineplus.gov/xml.html")
    page.raise_for_status()
    filename = re.search(r"mplus_topics_\d{4}-\d{2}-\d{2}\.xml", page.text).group()
    url = "https://medlineplus.gov/xml/" + filename
    response = client.get(url)
    response.raise_for_status()
digest = hashlib.sha256(response.content).hexdigest()
directory = args.directory / digest
directory.mkdir(parents=True, exist_ok=True)
(directory / "topics.xml").write_bytes(response.content)
(directory / "download.json").write_text(
    json.dumps(
        {
            "url": url,
            "downloaded_at": datetime.now(timezone.utc).isoformat(),
            "sha256": digest,
        },
        indent=2,
    ),
    encoding="utf-8",
)
print(directory / "topics.xml")
