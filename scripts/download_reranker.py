"""显式下载本次试验固定 revision；正常分析只读本地模型。"""

import json
import sys
from pathlib import Path

from huggingface_hub import snapshot_download

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from app.config import settings

name = "cross-encoder/mmarco-mMiniLMv2-L12-H384-v1"
revision = "1427fd652930e4ba29e8149678df786c240d8825"
directory = snapshot_download(
    name,
    revision=revision,
    local_dir=settings.cross_encoder_path,
    allow_patterns=["*.json", "*.safetensors", "*.txt", "*.model"],
)
metadata = {
    "model": name,
    "revision": revision,
    "path": str(Path(directory).resolve()),
    "license": "apache-2.0",
    "source": "https://huggingface.co/" + name,
}
(Path(directory) / "revision.json").write_text(
    json.dumps(metadata, indent=2), encoding="utf-8"
)
print(json.dumps(metadata, indent=2))
