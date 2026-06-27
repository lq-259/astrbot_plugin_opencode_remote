"""每项目任务队列：避免同一目录同时执行多个 OpenCode 任务"""
import asyncio
import time
import uuid
from typing import Optional

from astrbot.api import logger


class TaskQueue:
    """管理每个工作目录的任务队列"""

    def __init__(self, plugin):
        self.plugin = plugin
        self._lock = asyncio.Lock()
        # directory -> {"session_id": str, "umo": str, "text": str, "started_at": float}
        self._active: dict[str, dict] = {}
        # directory -> list[{"id": str, "umo": str, "text": str, "created_at": float}]
        self._queues: dict[str, list] = {}

    def is_active(self, directory: str) -> bool:
        return directory in self._active

    def get_active(self, directory: str) -> Optional[dict]:
        return self._active.get(directory)

    async def set_active(self, directory: str, umo: str, task_text: str, session_id: str):
        async with self._lock:
            self._active[directory] = {
                "session_id": session_id,
                "umo": umo,
                "text": task_text,
                "started_at": time.monotonic(),
            }

    async def clear_active(self, directory: str):
        async with self._lock:
            self._active.pop(directory, None)

    async def enqueue(self, umo: str, directory: str, task_text: str) -> str:
        async with self._lock:
            task_id = str(uuid.uuid4())[:8]
            self._queues.setdefault(directory, []).append({
                "id": task_id,
                "umo": umo,
                "text": task_text,
                "created_at": time.monotonic(),
            })
            return task_id

    async def dequeue(self, directory: str) -> Optional[dict]:
        async with self._lock:
            queue = self._queues.get(directory)
            if not queue:
                return None
            return queue.pop(0)

    async def cancel(self, directory: str, task_id: str) -> bool:
        async with self._lock:
            queue = self._queues.get(directory)
            if not queue:
                return False
            for i, task in enumerate(queue):
                if task["id"] == task_id:
                    queue.pop(i)
                    return True
            return False

    async def clear(self, directory: str) -> int:
        async with self._lock:
            queue = self._queues.get(directory)
            if not queue:
                return 0
            count = len(queue)
            self._queues[directory] = []
            return count

    async def get_queue(self, directory: str) -> list:
        async with self._lock:
            return list(self._queues.get(directory, []))

    async def on_session_idle(self, session_id: str, directory: str):
        """SSE 收到 session.idle 时调用，检查是否是当前 active 任务"""
        async with self._lock:
            active = self._active.get(directory)
            if not active:
                return
            if active.get("session_id") != session_id:
                return
            # Clear active but keep lock while checking queue
            self._active.pop(directory, None)
            queue = self._queues.get(directory)
            next_task = queue.pop(0) if queue else None

        if next_task:
            logger.info(
                "TaskQueue: directory=%s 任务完成，自动执行队列中的下一个任务 %s",
                directory, next_task["id"]
            )
            # Notify user
            try:
                from .formatters import format_response_with_meta
                await self.plugin.notification_mgr.notify(
                    f"任务队列：开始执行下一个任务 [{next_task['id']}]: {next_task['text'][:60]}",
                    directory,
                    session_id,
                )
            except Exception as e:
                logger.warning("TaskQueue notify failed: %s", e)
            # Start next task
            await self.plugin.send_task_to_opencode(
                next_task["text"], next_task["umo"], session_id=session_id
            )
