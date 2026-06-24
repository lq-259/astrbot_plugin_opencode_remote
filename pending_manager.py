"""待审批确认队列管理"""
import asyncio
from typing import Optional


class PendingManager:
    """管理权限请求和插件侧高风险确认的队列"""

    def __init__(self):
        self._pending: dict[str, dict] = {}
        self._free_indices: set[int] = set()
        self._max_index: int = 0

    def allocate_index(self) -> int:
        if self._free_indices:
            idx = min(self._free_indices)
            self._free_indices.remove(idx)
            return idx
        self._max_index += 1
        return self._max_index

    def free_index(self, index: int):
        if index > 0:
            self._free_indices.add(index)

    def add_plugin_confirmation(self, session_id: str, action: str,
                                 detail: str, directory: str) -> int:
        index = self.allocate_index()
        fut = asyncio.Future()
        self._pending[str(index)] = {
            "type": "plugin_confirm",
            "index": index,
            "session_id": session_id,
            "directory": directory,
            "action": action,
            "detail": detail,
            "future": fut,
        }
        return index

    def add_opencode_permission(
        self,
        session_id: str,
        permission_id: str,
        title: str,
        detail: str,
        directory: str,
    ) -> int:
        for item in self._pending.values():
            if (
                item.get("type") == "opencode_permission"
                and item.get("permission_id") == permission_id
            ):
                return item["index"]
        index = self.allocate_index()
        self._pending[str(index)] = {
            "type": "opencode_permission",
            "index": index,
            "session_id": session_id,
            "permission_id": permission_id,
            "directory": directory,
            "action": title,
            "detail": detail,
        }
        return index

    def get(self, index: int) -> Optional[dict]:
        return self._pending.get(str(index))

    def remove(self, index: int):
        self._pending.pop(str(index), None)
        self.free_index(index)

    def remove_opencode_permission(self, permission_id: str):
        for key, item in list(self._pending.items()):
            if (
                item.get("type") == "opencode_permission"
                and item.get("permission_id") == permission_id
            ):
                self._pending.pop(key, None)
                self.free_index(item.get("index", 0))

    def get_all_visible(self) -> list[dict]:
        items = []
        for key, item in self._pending.items():
            entry = {
                "index": item["index"],
                "session_id": item.get("session_id", ""),
                "directory": item.get("directory", ""),
                "action": item.get("action", ""),
                "detail": item.get("detail", ""),
                "type": item.get("type", "plugin_confirm"),
            }
            items.append(entry)
        items.sort(key=lambda x: x["index"])
        return items

    def approve(self, index: int) -> bool:
        key = str(index)
        entry = self._pending.get(key)
        if not entry:
            return False
        fut = entry.get("future")
        if fut and not fut.done():
            fut.set_result(True)
        self._pending.pop(key, None)
        self.free_index(index)
        return True

    def deny(self, index: int) -> bool:
        key = str(index)
        entry = self._pending.get(key)
        if not entry:
            return False
        fut = entry.get("future")
        if fut and not fut.done():
            fut.set_result(False)
        self._pending.pop(key, None)
        self.free_index(index)
        return True

    def approve_all(self) -> int:
        count = 0
        for key in list(self._pending.keys()):
            entry = self._pending.get(key)
            if entry:
                fut = entry.get("future")
                if fut and not fut.done():
                    fut.set_result(True)
                self.free_index(entry.get("index", 0))
                count += 1
        self._pending.clear()
        return count

    @property
    def count(self) -> int:
        return len(self._pending)
