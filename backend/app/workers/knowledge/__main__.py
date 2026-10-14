"""在 backend 目录运行 python -m app.workers.knowledge。"""

import argparse
import asyncio

from app.workers.knowledge.config import WorkerSettings
from app.workers.knowledge.main import main


def cli():
    parser = argparse.ArgumentParser(description="独立知识库文档 worker")
    parser.add_argument("--env-file", default=".env", help="配置文件，默认使用 backend/.env")
    args = parser.parse_args()
    asyncio.run(main(WorkerSettings(_env_file=args.env_file)))


if __name__ == "__main__":
    cli()
