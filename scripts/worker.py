"""独立启动 Huey 消费者；与 HTTP 后端分开运行，处理持久化队列中的任务。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from huey.consumer import Consumer
from app.database import initialize
from app.tasks import queue

initialize()
# 两个线程消费队列，实际模型工作仍由 run_analysis 中既有的文件锁串行执行。
Consumer(queue, workers=2, worker_type="thread", periodic=False).run()
