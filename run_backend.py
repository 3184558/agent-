"""在 PyCharm 中直接运行此文件，启动 FastAPI 后端。"""

import sys
from pathlib import Path

import uvicorn

# 将 backend 加入导入路径，使直接运行此文件时也能找到 app 包。
sys.path.insert(0, str(Path(__file__).resolve().parent / "backend"))

# 仅监听本机；该入口不启动 worker 或 Vue 开发服务器，应分别运行。
uvicorn.run("app.main:app", host="127.0.0.1", port=8000, access_log=False)
