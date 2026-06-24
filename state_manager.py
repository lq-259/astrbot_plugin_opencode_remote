"""状态管理器：窗口-目录-会话状态持久化"""
import os
from typing import Optional


class StateManager:
    """管理窗口状态和用户偏好，支持 KV 持久化"""

    def __init__(self, kv_helper):
        self.kv = kv_helper
        self._window_states: dict[str, dict] = {}
        self._user_states: dict[str, dict] = {}
        self._session_owners: dict[str, str] = {}
        self._known_users: list[str] = []
        self._last_errors: dict[str, str] = {}

    def get_window_state(self, umo: str) -> dict:
        return self._window_states.get(umo, {})

    def set_window_state(self, umo: str, **kwargs):
        state = dict(self._window_states.get(umo, {}))
        state.update(kwargs)
        self._window_states[umo] = state

    def get_user_state(self, sender_id: str) -> dict:
        return self._user_states.get(sender_id, {})

    def set_user_state(self, sender_id: str, **kwargs):
        state = dict(self._user_states.get(sender_id, {}))
        state.update(kwargs)
        self._user_states[sender_id] = state

    def get_current_directory(self, umo: str) -> Optional[str]:
        return self._window_states.get(umo, {}).get("directory")

    def get_current_session(self, umo: str) -> Optional[str]:
        return self._window_states.get(umo, {}).get("current_session")

    def get_current_model(self, umo: str) -> Optional[str]:
        return self._window_states.get(umo, {}).get("model")

    def get_current_variant(self, umo: str) -> Optional[str]:
        return self._window_states.get(umo, {}).get("variant")

    def get_current_agent(self, umo: str) -> Optional[str]:
        return self._window_states.get(umo, {}).get("agent")

    def set_session_owner(self, session_id: str, umo: str):
        self._session_owners[session_id] = umo

    def get_session_owner(self, session_id: str) -> Optional[str]:
        return self._session_owners.get(session_id)

    def remove_session_owner(self, session_id: str):
        self._session_owners.pop(session_id, None)

    def set_last_error(self, session_id: str, error: str):
        if session_id and error:
            self._last_errors[session_id] = error

    def get_last_error(self, session_id: str) -> Optional[str]:
        return self._last_errors.get(session_id)

    def find_window_by_session(self, session_id: str) -> Optional[str]:
        for umo, state in self._window_states.items():
            if state.get("current_session") == session_id:
                return umo
        return self._session_owners.get(session_id)

    async def persist_window_state(self, umo: str):
        state = self._window_states.get(umo)
        if state:
            await self.kv.put_kv_data(f"window_state_{umo}", state)
        else:
            await self.kv.put_kv_data(f"window_state_{umo}", None)

    async def persist_session_owners(self):
        await self.kv.put_kv_data("session_owners", self._session_owners)

    async def persist_user_state(self, sender_id: str):
        state = self._user_states.get(sender_id)
        if state:
            await self.kv.put_kv_data(f"user_state_{sender_id}", state)

    async def load_all(self):
        known_users = await self.kv.get_kv_data("known_users", [])
        self._known_users = [str(u) for u in known_users]

        for uid in self._known_users:
            state = await self.kv.get_kv_data(f"user_state_{uid}", None)
            if state:
                self._user_states[uid] = state

        owners = await self.kv.get_kv_data("session_owners", {})
        if isinstance(owners, dict):
            self._session_owners = owners

        loaded_umos = set()
        for sid in self._session_owners:
            umo = self._session_owners[sid]
            if umo and umo not in loaded_umos:
                loaded_umos.add(umo)
                state = await self.kv.get_kv_data(f"window_state_{umo}", None)
                if state:
                    self._window_states[umo] = state

        for uid in self._known_users:
            state = self._user_states.get(uid, {})
            primary_umo = state.get("primary_umo")
            if primary_umo and primary_umo not in loaded_umos:
                loaded_umos.add(primary_umo)
                ws = await self.kv.get_kv_data(f"window_state_{primary_umo}", None)
                if ws:
                    self._window_states[primary_umo] = ws

        await self._load_indexless_states()

    async def _load_indexless_states(self):
        try:
            from astrbot.core import sp
            plugin_id = getattr(self.kv, "plugin_id", None)
            if not plugin_id:
                return
            prefs = await sp.range_get_async("plugin", plugin_id, None)
        except Exception:
            return

        for pref in prefs:
            key = getattr(pref, "key", "")
            value = getattr(pref, "value", {}) or {}
            data = value.get("val") if isinstance(value, dict) else None
            if not data:
                continue
            if key.startswith("window_state_") and isinstance(data, dict):
                umo = key[len("window_state_"):]
                if umo:
                    self._window_states[umo] = data
            elif key.startswith("user_state_") and isinstance(data, dict):
                uid = key[len("user_state_"):]
                if uid:
                    self._user_states[uid] = data
                    if uid not in self._known_users:
                        self._known_users.append(uid)

    async def register_user(self, sender_id: str):
        uid = str(sender_id)
        if uid not in self._known_users:
            self._known_users.append(uid)
            await self.kv.put_kv_data("known_users", self._known_users)
