"""通知推送和去重管理"""
import asyncio
import time
from astrbot.api.event import MessageChain
from astrbot.api import logger


class NotificationManager:

    def __init__(self, context, state_mgr):
        self.context = context
        self.state_mgr = state_mgr
        self._recent: dict[tuple[str, str, str], float] = {}
        self._event_cache: dict[str, any] = {}

    @staticmethod
    def notification_body_key(text: str) -> str:
        lines = text.splitlines()
        if len(lines) >= 2 and lines[0].strip() and lines[1].strip().startswith("["):
            lines = lines[1:]
        return "\n".join(line.rstrip() for line in lines).strip() or text.strip()

    @staticmethod
    def is_request_notification(text: str) -> bool:
        return (
            "权限请求" in text
            or "OpenCode 提问" in text
            or "待审批" in text
            or "/oc allow" in text
            or "/oc deny" in text
        )

    @staticmethod
    def split_message(text: str, max_len: int = 4200) -> list[str]:
        chunks = []
        current = ""
        for line in text.split("\n"):
            if current and len(current) + 1 + len(line) > max_len:
                chunks.append(current)
                current = line
            else:
                current = current + "\n" + line if current else line
        if current:
            chunks.append(current)
        return chunks

    def _should_skip(self, umo: str, session_id: str, text: str) -> bool:
        if self.is_request_notification(text):
            return False
        now = time.monotonic()
        expire_before = now - 30
        for key, ts in list(self._recent.items()):
            if ts < expire_before:
                self._recent.pop(key, None)

        body_key = self.notification_body_key(text)
        cache_key = (umo, session_id or "", body_key)
        last = self._recent.get(cache_key, 0)
        if now - last <= 2.5:
            logger.info("通知去重: umo=%s text=%s", umo[:30], text[:60].replace("\n", "\\n"))
            return True
        self._recent[cache_key] = now
        return False

    def _select_targets(self, directory: str, session_id: str) -> list[str]:
        targets = []
        seen = set()

        def add(umo: str | None):
            if umo and umo not in seen:
                seen.add(umo)
                targets.append(umo)

        if session_id:
            umo = self.state_mgr.get_session_owner(session_id)
            add(umo)
            add(self.state_mgr.find_window_by_session(session_id))

        for umo, state in self.state_mgr._window_states.items():
            if state.get("directory") == directory:
                add(umo)

        known = getattr(self.state_mgr, "_known_users", [])
        for uid in known:
            user_state = self.state_mgr._user_states.get(uid, {})
            primary = user_state.get("primary_umo")
            if primary:
                win = self.state_mgr._window_states.get(primary, {})
                if win.get("directory") == directory:
                    add(primary)

        for uid in known:
            primary = self.state_mgr._user_states.get(uid, {}).get("primary_umo")
            if primary:
                add(primary)

        return targets

    async def push_notification(self, text: str, directory: str, session_id: str):
        targets = self._select_targets(directory, session_id)
        if not targets:
            logger.warning("推送通知无目标: session=%s dir=%s", session_id[:12] if session_id else "-", directory[:30] if directory else "-")
            return

        for umo in targets[:1]:
            if self._should_skip(umo, session_id, text):
                logger.info("通知被跳过: umo=%s len=%d", umo[:30], len(text))
                continue
            chunks = self.split_message(text) if len(text) > 4200 else [text]
            logger.info("通知准备发送: umo=%s len=%d chunks=%d", umo[:30], len(text), len(chunks))
            for chunk in chunks:
                chain = MessageChain().message(chunk)
                asyncio.create_task(self._do_send(umo, chain, len(chunk)))

    async def _do_send(self, umo: str, chain: MessageChain, length: int):
        try:
            await asyncio.wait_for(
                self.context.send_message(umo, chain),
                timeout=10,
            )
            logger.info(
                "推送通知成功: umo=%s len=%d",
                umo[:20], length,
            )
        except Exception as e:
            cached_event = self._event_cache.get(umo)
            if cached_event:
                try:
                    await asyncio.wait_for(cached_event.send(chain), timeout=10)
                    logger.info("推送通知通过事件缓存成功: umo=%s len=%d", umo[:20], length)
                    return
                except Exception as e2:
                    logger.warning("事件缓存推送失败: umo=%s len=%d err=%s", umo[:20], length, e2)
            logger.warning("推送通知失败: umo=%s len=%d err=%s", umo[:20], length, e)
