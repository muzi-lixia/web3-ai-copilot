"""终态写前日志；保存于 Agent 持久卷，数据库中断时仍可恢复补写。"""

import asyncio
import hashlib
import json
import os
import tempfile
from pathlib import Path


class TerminalOutbox:
    """目录权限 0700、记录权限 0600；文件只保存业务终态，不保存访问凭证。"""

    def __init__(self, directory: str):
        self.directory = Path(directory)

    def _path(self, turn_id):
        return self.directory / (hashlib.sha256(turn_id.encode()).hexdigest() + ".json")

    def _save(self, record):
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.directory, 0o700)
        fd, temporary = tempfile.mkstemp(dir=self.directory, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(record, stream, ensure_ascii=False, allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self._path(record["turn_id"]))
            self._sync_directory()
        finally:
            Path(temporary).unlink(missing_ok=True)

    def _sync_directory(self):
        fd = os.open(self.directory, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    async def save(self, turn_id, user, values, trace_id):
        await asyncio.to_thread(self._save, {
            "turn_id": turn_id, "user": user, "values": values, "trace_id": trace_id,
        })

    async def clear_temporary(self):
        """仅在取得运行锁、启动生成前清理强杀遗留的未提交临时文件。"""
        def clean():
            for path in self.directory.glob("*.tmp"):
                path.unlink(missing_ok=True)
            if self.directory.exists():
                self._sync_directory()
        await asyncio.to_thread(clean)

    def _load(self):
        records = []
        for path in sorted(self.directory.glob("*.json")):
            # 损坏时启动失败，保留原记录供人工恢复，不能静默丢失终态。
            record = json.loads(path.read_text())
            if self._path(record["turn_id"]) != path or record["values"]["state"] not in {
                "completed", "failed", "cancelled"
            }:
                raise ValueError("终态补写记录不合法")
            records.append(record)
        return records

    async def load(self):
        return await asyncio.to_thread(self._load)

    def _remove(self, turn_id):
        self._path(turn_id).unlink(missing_ok=True)
        if self.directory.exists():
            self._sync_directory()

    async def remove(self, turn_id):
        await asyncio.to_thread(self._remove, turn_id)
