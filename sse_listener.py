"""后台 SSE 事件监听 + 防抖 + 重连 + 60s 合并操作推送"""
import json
import asyncio
import time
from typing import Callable, Awaitable

from astrbot.api import logger


class SSEListener:
    """后台 SSE 监听 OpenCode /global/event，60s 窗口合并操作推送"""

    MERGE_WINDOW = 60.0
    THINKING_COOLDOWN = 90.0

    def __init__(
        self,
        client,
        notify_callback: Callable[[str, str, str], Awaitable[None]],
        state_mgr,
    ):
        self.client = client
        self.notify_callback = notify_callback
        self.state_mgr = state_mgr
        self.output_level: str = "simple"
        self._summary_msg_count: int = 5
        self._max_reconnect: int = 10
        self._task: asyncio.Task | None = None
        self._hibernated: bool = False
        self.conn_fail_count: int = 0
        self.conn_error: str | None = None

        self._completion_notified: dict[str, float] = {}
        self._idle_notified: dict[str, float] = {}
        self._thinking_sent: dict[str, bool] = {}
        self._thinking_cooldown: dict[str, float] = {}
        self._child_sessions: set[str] = set()
        self._reasoning_active: dict[str, bool] = {}
        self._last_user_msg: dict[str, float] = {}
        self._last_flush_time: dict[str, float] = {}
        self._last_assistant_text: dict[str, str] = {}
        self._session_titles: dict[str, str] = {}
        self._session_directories: dict[str, str] = {}
        self._question_notified: set = set()
        self._completion_timers: dict[str, asyncio.Task] = {}

        self._session_buffers: dict[str, dict] = {}
        self._flush_tasks: dict[str, asyncio.Task] = {}

    def start(
        self,
        output_level: str = "simple",
        summary_msg_count: int = 5,
        max_reconnect_attempts: int = 10,
    ):
        self.output_level = output_level
        self._summary_msg_count = summary_msg_count
        self._max_reconnect = max_reconnect_attempts

        if self._task and not self._task.done():
            logger.info("SSE 监听已在运行，跳过重复启动")
            return
        self._task = asyncio.create_task(self._listen_loop())
        logger.info("SSE listener v3 started, output_level=%s", output_level)

    async def stop(self):
        for task_dict in (self._flush_tasks,):
            for t in list(task_dict.values()):
                if t and not t.done():
                    t.cancel()
                    try:
                        await t
                    except asyncio.CancelledError:
                        pass
            task_dict.clear()
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None

    def wake_up(self):
        if self._hibernated:
            self._hibernated = False
            self.conn_fail_count = 0
            self.conn_error = None
            if self._task is None or self._task.done():
                self._task = asyncio.create_task(self._listen_loop())
                logger.info("SSE 监听器已唤醒，重新开始连接")

    async def _push(self, text: str, directory: str, session_id: str):
        logger.info("SSE _push called: sid=%s dir=%s text_head=%s",
                    session_id[:12] if session_id else "-",
                    directory[:30] if directory else "-",
                    text[:80].replace("\n", "\\n"))
        try:
            await self.notify_callback(text, directory, session_id)
        except Exception as e:
            logger.warning(f"推送通知失败: {e}")

    async def _listen_loop(self):
        backoff = 1
        max_backoff = 60

        while True:
            resp = None
            try:
                resp = await self.client.subscribe_global_events()
                got_data = False

                async for line in resp.aiter_lines():
                    got_data = True
                    self.conn_error = None
                    was_hibernated = self._hibernated
                    self._hibernated = False
                    if self.conn_fail_count > 0:
                        logger.info("SSE 连接已恢复（此前失败 %d 次）", self.conn_fail_count)
                        if was_hibernated:
                            await self._push("SSE 连接已恢复", "", "")
                    backoff = 1
                    self.conn_fail_count = 0

                    line = line.rstrip("\r\n")
                    if not line or not line.startswith("data: "):
                        continue
                    try:
                        data = json.loads(line[6:])
                    except json.JSONDecodeError:
                        continue
                    await self._handle_event(data)

            except asyncio.CancelledError:
                return
            except Exception as e:
                self.conn_fail_count += 1
                self.conn_error = f"{type(e).__name__}: {e}"
                logger.warning("SSE 断线: %s, %ds 后重连", self.conn_error, backoff)
            finally:
                if resp is not None:
                    try:
                        await resp.aclose()
                    except Exception:
                        pass
                    sse_client = resp.extensions.get("sse_client")
                    if sse_client is not None:
                        try:
                            await sse_client.aclose()
                        except Exception:
                            pass

            if self._max_reconnect > 0 and self.conn_fail_count >= self._max_reconnect:
                self._hibernated = True
                logger.warning("SSE 已连续失败 %d 次，进入休眠，5 分钟后自动重试", self.conn_fail_count)
                await self._push(
                    f"SSE 已连续失败 {self.conn_fail_count} 次，进入休眠，5 分钟后自动重试",
                    "", ""
                )
                await asyncio.sleep(300)
                self._hibernated = False
                self.conn_fail_count = 0
                backoff = 1
                continue

            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, max_backoff)

    def _extract_session_id(self, props: dict) -> str:
        sid = props.get("sessionID", "")
        if isinstance(sid, dict):
            sid = sid.get("id", "")
        return sid

    def _extract_tool_detail(self, tool_name: str, props: dict) -> str:
        from .constants import TOOL_DETAIL_EXTRACTORS
        candidates = TOOL_DETAIL_EXTRACTORS.get(tool_name, [])
        for key in candidates:
            val = props.get(key)
            if val and isinstance(val, str) and val.strip():
                return val
        return ""

    async def _handle_event(self, data: dict):
        directory = data.get("directory", "")
        payload = data.get("payload") or data
        event_type = payload.get("type", "")
        props = payload.get("properties", {})
        session_id = self._extract_session_id(props)

        if directory and session_id:
            self._session_directories[session_id] = directory

        if event_type not in ("server.connected",):
            logger.info("SSE event: %s sid=%s", event_type, session_id[:12] if session_id else "-")

        if session_id and session_id in self._child_sessions:
            if event_type in ("session.idle", "session.status", "session.error", "session.created", "session.deleted", "permission.asked", "question.asked", "message.part.updated"):
                pass
            else:
                return

        if event_type in ("session.created", "session.updated"):
            info = props.get("info", {})
            sid = info.get("id", "")
            title = info.get("title", "")
            if sid and title:
                self._session_titles[sid] = title
            if sid and info.get("parentID"):
                self._child_sessions.add(sid)
                if event_type == "session.created":
                    full_parent_id = info.get("parentID", "")
                    parent_id_short = full_parent_id[:12]
                    parent_title = self._session_titles.get(full_parent_id, "") or self._session_title(full_parent_id, directory)
                    msg = f"{parent_title or '无标题'} ({parent_id_short})\n创建子会话: {title or '无标题'} ({sid[:12]})"
                    await self._push(msg, directory, full_parent_id)
            # Note: don't sync model from session events to local state
            # The model is per-message, not per-session. Let the server decide.

        if event_type in (
            "session.created", "session.deleted", "session.error",
            "session.idle", "permission.updated", "permission.asked", "permission.replied", "question.asked",
        ):
            if event_type == "session.idle" and session_id:
                if session_id in self._child_sessions:
                    logger.info("SSE idle child session sid=%s clearing buffer", session_id[:12])
                    self._clear_buffer(session_id)
                    self._cancel_completion_timer(session_id)
                    return
                buf = self._session_buffers.get(session_id)
                effective_dir = directory or (buf.get("directory", "") if buf else "") or self._session_directories.get(session_id, "")
                pushed = await self._flush_buffer(session_id, effective_dir, done=True, append_done=True)
                self._thinking_sent[session_id] = False
                self._cancel_completion_timer(session_id)
                if not pushed:
                    await self._on_event(event_type, props, effective_dir)
            elif event_type in ("permission.updated", "permission.asked"):
                effective_dir = directory or self._session_directories.get(session_id, "")
                await self._on_event(event_type, props, effective_dir)
            elif event_type == "question.asked":
                return
            else:
                await self._on_event(event_type, props, directory)
            return

        if event_type == "session.status":
            status = props.get("status", {})
            if isinstance(status, dict):
                if status.get("type") == "idle":
                    if session_id:
                        if session_id in self._child_sessions:
                            logger.info("SSE idle child session sid=%s (status)", session_id[:12])
                            self._clear_buffer(session_id)
                            self._cancel_completion_timer(session_id)
                            return
                        buf = self._session_buffers.get(session_id)
                        effective_dir = directory or (buf.get("directory", "") if buf else "") or self._session_directories.get(session_id, "")
                        pushed = await self._flush_buffer(session_id, effective_dir, done=True, append_done=True)
                        self._thinking_sent[session_id] = False
                        self._cancel_completion_timer(session_id)
                        if not pushed:
                            await self._on_event("session.idle", {"sessionID": session_id}, effective_dir)
                else:
                    logger.info("SSE session.status type=%s (non-idle)", status.get("type", "?"))
            else:
                logger.info("SSE session.status raw=%s", str(status)[:60])
            return

        if not session_id:
            return

        if self.output_level == "silence" and event_type != "message.part.updated":
            return

        if event_type == "message.updated":
            role = props.get("info", {}).get("role", "")
            now = time.monotonic()
            logger.info("SSE msg.updated role=%s sid=%s", role, session_id[:12])
            if role == "user":
                last_flush = self._last_flush_time.get(session_id, 0)
                if now - last_flush < 30.0:
                    logger.info("SSE user msg debounced sid=%s (%.1fs since last flush)", session_id[:12], now - last_flush)
                    return
                self._last_flush_time[session_id] = now
                self._cancel_completion_timer(session_id)
                parts = props.get("parts", [])
                has_text = any(
                    p.get("type") == "text" and p.get("text", "").strip()
                    for p in parts
                ) if parts else False
                if has_text:
                    await self._flush_buffer(session_id, directory, done=True, append_done=True)
                    self._thinking_sent.pop(session_id, None)
                    self._thinking_cooldown.pop(session_id, None)
                    self._reasoning_active.pop(session_id, None)
                    self._idle_notified.pop(session_id, None)
                    self._last_assistant_text.pop(session_id, None)
                    self._cancel_completion_timer(session_id)
                return
            if role == "assistant":
                self._reasoning_active[session_id] = False
                parts = props.get("parts", [])
                text_parts = [p.get("text", "") for p in parts if p.get("type") == "text"]
                if not text_parts:
                    info = props.get("info", {})
                    it = info.get("text", "") or info.get("content", "")
                    if it and it.strip():
                        text_parts = [it]
                if not text_parts:
                    pt = props.get("text", "") or props.get("content", "")
                    if pt and pt.strip():
                        text_parts = [pt]
                if text_parts:
                    self._last_assistant_text[session_id] = "\n".join(text_parts)
                for part in parts:
                    if part.get("type") == "tool_use":
                        tool_name = part.get("name", "")
                        tool_input = part.get("input", {}) or {}
                        detail = self._extract_tool_detail(tool_name, tool_input)
                        logger.info("SSE tool_use part: %s %s", tool_name, detail[:50])
                        await self._add_to_buffer(
                            session_id, directory, tool_name, detail, tool_input
                        )
                return

        if event_type == "message.part.delta":
            part = props.get("part", {})
            field = props.get("field", "") or part.get("field", "")
            text = props.get("delta", "") or props.get("text", "") or props.get("content", "") or part.get("text", "")
            if not text:
                text = part.get("content", "") or part.get("value", "") or part.get("delta", "")
            is_reasoning = (field == "reasoning") or self._reasoning_active.get(session_id, False)
            if field == "reasoning":
                self._reasoning_active[session_id] = True
            elif field == "text":
                self._reasoning_active[session_id] = False
            if text and not is_reasoning:
                prev = self._last_assistant_text.get(session_id, "")
                self._last_assistant_text[session_id] = prev + text
            if self.output_level in ("simple", "detail") and not self._thinking_sent.get(session_id, False):
                self._thinking_sent[session_id] = True
                logger.info("SSE delta -> thinking sent sid=%s field=%s", session_id[:12], field)
                await self._send_thinking(session_id, directory)
                self._start_buffer_timer(session_id, directory)
            return

        if event_type == "message.part.updated":
            part = props.get("part", {})
            part_type = part.get("type", "")
            is_child = session_id in self._child_sessions
            if part_type == "reasoning":
                if is_child:
                    return
                self._reasoning_active[session_id] = True
                self._last_assistant_text.pop(session_id, None)
                if self.output_level in ("simple", "detail") and not self._thinking_sent.get(session_id, False):
                    self._thinking_sent[session_id] = True
                    logger.info("SSE reasoning -> thinking sent sid=%s", session_id[:12])
                    await self._send_thinking(session_id, directory)
                    self._start_buffer_timer(session_id, directory)
                return
            if part_type == "tool":
                tool_name = part.get("tool", "")
                state = part.get("state", {})
                status = state.get("status", "")
                if tool_name in ("question", "Question"):
                    if status == "pending":
                        self._question_notified.discard(session_id)
                    elif status in ("running", "completed"):
                        if session_id not in self._question_notified:
                            self._question_notified.add(session_id)
                            tool_input = state.get("input", {}) or {}
                            await self._push_question(tool_input, session_id, directory)
                elif self.output_level == "silence":
                    return
                elif is_child:
                    return
                elif status == "completed":
                    self._reasoning_active[session_id] = False
                    tool_input = state.get("input", {}) or {}
                    detail = self._extract_tool_detail(tool_name, tool_input)
                    logger.info("SSE part.updated tool=%s status=%s detail=%s", tool_name, status, detail[:50])
                    await self._add_to_buffer(
                        session_id, directory, tool_name, detail, tool_input
                    )
                else:
                    logger.info("SSE part.updated tool=%s UNHANDLED status=%s", tool_name, status)
                return
            if is_child:
                return
            if part_type != "reasoning":
                self._reasoning_active[session_id] = False
            logger.info("SSE part.updated type=%s (not tool/reasoning)", part_type)
            return

        if event_type == "tool.execute.before":
            tool_name = props.get("tool", "") or props.get("name", "")
            detail = self._extract_tool_detail(tool_name, props)
            await self._add_to_buffer(session_id, directory, tool_name, detail, props)
            return

        if event_type == "file.edited":
            filename = props.get("file", "?")
            await self._add_to_buffer(session_id, directory, "edit", filename, props)
            return

        if event_type == "todo.updated":
            await self._add_to_buffer(session_id, directory, "todowrite", "", props)
            return

        if event_type == "command.executed":
            cmd = props.get("command", props.get("cmd", "?"))
            await self._add_to_buffer(session_id, directory, "command", cmd, props)
            return

    async def _on_event(self, event_type: str, props: dict, directory: str):
        from .constants import EVENT_TYPES_CHINESE
        from .formatters import format_event_notification

        session_id = self._extract_session_id(props)

        now = time.monotonic()
        if event_type == "session.idle" and session_id:
            last = self._idle_notified.get(session_id, 0)
            if now - last < 10:
                return
            self._idle_notified[session_id] = now

        label = EVENT_TYPES_CHINESE.get(event_type, event_type)
        if event_type in ("permission.updated", "permission.asked"):
            await self._push_permission_request(props, directory, session_id)
            return
        if event_type == "permission.replied":
            permission = props.get("permission", props)
            if not isinstance(permission, dict):
                permission = {}
            permission_id = permission.get("id") or props.get("permissionID") or props.get("id")
            pending_mgr = getattr(self.state_mgr, "pending_mgr", None)
            if pending_mgr and permission_id:
                pending_mgr.remove_opencode_permission(permission_id)
        detail = format_event_notification(event_type, props)
        if event_type == "session.error" and session_id and self.state_mgr:
            self.state_mgr.set_last_error(session_id, detail)
        content = f"[{label}] {detail}"
        if event_type == "session.idle":
            content = "任务已完成"
        elif event_type == "permission.replied":
            content = "权限已响应"
        if session_id:
            title = self._session_title(session_id, directory)
            short_id = session_id[:12] if len(session_id) > 12 else session_id
            text = f"{title or '无标题'} ({short_id})\n{content}"
        else:
            text = content
        await self._push(text, directory, session_id)

    async def _push_permission_request(self, props: dict, directory: str, session_id: str):
        logger.info("SSE permission raw: %s", str(props)[:500])

        from .formatters import format_permission_description

        permission = props.get("permission", props)
        if isinstance(permission, str):
            permission_id = props.get("permissionID", "") or props.get("id", "")
            title = permission
            detail = props.get("description", "") or props.get("detail", "") or str(props)
        elif isinstance(permission, dict):
            permission_id = (
                permission.get("id")
                or props.get("permissionID")
                or props.get("id")
                or ""
            )
            title, detail = format_permission_description(permission, props)
        else:
            permission_id = props.get("permissionID", "") or props.get("id", "")
            title = "权限请求"
            detail = str(props)[:200]
        index = None
        pending_mgr = getattr(self.state_mgr, "pending_mgr", None)
        if pending_mgr and permission_id:
            index = pending_mgr.add_opencode_permission(
                session_id, permission_id, title, detail, directory
            )

        short_id = session_id[:12] if session_id else "-"
        session_title = self._session_title(session_id, directory) if session_id else "无标题"
        lines = [f"{session_title or '无标题'} ({short_id})", "[权限请求]", f"{title}"]
        if detail:
            lines.append(f"{detail[:500]}")
        if index:
            lines.append("")
            lines.append(f"使用 /oc allow {index} 批准 或 /oc deny {index} 拒绝")
        else:
            lines.append("请在 Web 中处理该权限请求")
        await self._push("\n".join(lines), directory, session_id)

    def _session_title(self, session_id: str, directory: str) -> str:
        if session_id in self._session_titles:
            return self._session_titles[session_id]
        for umo, state in getattr(self.state_mgr, "_window_states", {}).items():
            if state.get("directory") == directory:
                t = state.get("session_title", "")
                if t:
                    return t
                break
        return ""

    async def _send_thinking(self, session_id: str, directory: str):
        now = time.monotonic()
        last = self._thinking_cooldown.get(session_id, 0)
        if now - last < 120:
            return
        self._thinking_cooldown[session_id] = now
        title = self._session_title(session_id, directory)
        short_id = session_id[:12] if len(session_id) > 12 else session_id
        lines = [f"{title or '无标题'} ({short_id})"]
        lines.append("思考中...")
        await self._push("\n".join(lines), directory, session_id)

    async def _push_question(self, tool_input: dict, session_id: str, directory: str):
        questions = tool_input.get("questions", [])
        if not questions:
            return

        title = self._session_title(session_id, directory)
        short_id = session_id[:12] if len(session_id) > 12 else session_id
        lines = [f"{title or '无标题'} ({short_id})"]

        for q in questions:
            header = q.get("header", "")
            question = q.get("question", "")
            options = q.get("options", [])

            lines.append("")
            lines.append(f"OpenCode 提问: {header}" if header else "OpenCode 提问:")
            lines.append(question)

            if options:
                lines.append("")
                lines.append("选项:")
                for i, opt in enumerate(options, 1):
                    label = opt.get("label", "")
                    desc = opt.get("description", "")
                    if desc:
                        lines.append(f"  {i}. {label} - {desc}")
                    else:
                        lines.append(f"  {i}. {label}")

            if q.get("multiple"):
                lines.append("(可多选)")

        lines.append("")
        lines.append("请在 OpenCode Web UI 中回答此问题")

        effective_dir = directory or self._session_directories.get(session_id, "")
        await self._push("\n".join(lines), effective_dir, session_id)

    def _cancel_completion_timer(self, session_id: str):
        old_task = self._completion_timers.pop(session_id, None)
        if old_task and not old_task.done():
            old_task.cancel()
            logger.info("SSE completion timer cancelled sid=%s", session_id[:12])

    def _clear_buffer(self, session_id: str):
        old_task = self._flush_tasks.pop(session_id, None)
        if old_task and not old_task.done():
            old_task.cancel()
        self._session_buffers.pop(session_id, None)
        self._last_assistant_text.pop(session_id, None)
        self._thinking_sent.pop(session_id, None)
        self._thinking_cooldown.pop(session_id, None)
        self._reasoning_active.pop(session_id, None)
        self._idle_notified.pop(session_id, None)
        self._question_notified.discard(session_id)

    def _start_buffer_timer(self, session_id: str, directory: str):
        old_task = self._flush_tasks.pop(session_id, None)
        if old_task and not old_task.done():
            old_task.cancel()
        if session_id not in self._session_buffers:
            self._session_buffers[session_id] = {
                "directory": directory,
                "start_time": time.monotonic(),
                "ops": [],
            }
        self._flush_tasks[session_id] = asyncio.create_task(
            self._delayed_flush(session_id, directory)
        )

    async def _delayed_flush(self, session_id: str, directory: str):
        try:
            await asyncio.sleep(self.MERGE_WINDOW)
            await self._flush_buffer(session_id, directory, done=False)
        except asyncio.CancelledError:
            pass

    async def _fetch_summary_text(self, session_id: str, directory: str) -> str:
        try:
            from .formatters import extract_text_from_parts

            resp = await self.client.request(
                "GET", f"/session/{session_id}/message", directory=directory
            )
            messages = resp.json()
            if not isinstance(messages, list):
                return ""
            texts = []
            for msg in messages:
                info = msg.get("info", {})
                if info.get("role") != "assistant":
                    continue
                text = extract_text_from_parts(msg.get("parts", [])).strip()
                if text:
                    texts.append(text)
            texts = texts[-max(1, self._summary_msg_count):]
            return "\n\n".join(texts)
        except Exception as e:
            logger.warning("summary 模式拉取最近消息失败 sid=%s: %s", session_id[:12], e)
            return ""

    async def _flush_buffer(self, session_id: str, directory: str, done: bool, append_done: bool = False):
        flush_task = self._flush_tasks.pop(session_id, None)
        if flush_task and not flush_task.done():
            flush_task.cancel()
        buf = self._session_buffers.get(session_id)
        final_text = self._last_assistant_text.pop(session_id, None) if done else None
        effective_dir = directory or (buf.get("directory", "") if buf else "") or self._session_directories.get(session_id, "")

        ops_list = buf["ops"] if buf and buf.get("ops") else []
        if done and self.output_level == "summary" and (final_text or ops_list):
            summary_text = await self._fetch_summary_text(session_id, effective_dir)
            if summary_text:
                final_text = summary_text
        if done and self.output_level in ("simple", "summary") and not final_text:
            self._session_buffers.pop(session_id, None)
            logger.info("SSE flush skipped sid=%s level=%s done no final_text", session_id[:12], self.output_level)
            return False
        if not done and self.output_level != "detail":
            if buf:
                buf["ops"] = []
            return False
        if not ops_list and not final_text:
            if done:
                self._session_buffers.pop(session_id, None)
            logger.info("SSE flush skipped sid=%s done=%s no ops/final_text", session_id[:12], done)
            return False

        from .formatters import format_consolidated_notification
        title = self._session_title(session_id, directory)
        logger.info("SSE flush buffer sid=%s ops=%d done=%s", session_id[:12], len(ops_list), done)
        text = format_consolidated_notification(
            title, session_id, ops_list, done, final_text=final_text
        )

        if text:
            if done:
                self._thinking_sent.pop(session_id, None)
                self._session_buffers.pop(session_id, None)
                if append_done:
                    self._idle_notified[session_id] = time.monotonic()
                    if self.output_level == "silence":
                        done_text = f"{title or '无标题'} ({session_id[:12] if len(session_id) > 12 else session_id})\n任务已完成"
                        logger.info("SSE push silence done: %s", done_text[:80].replace("\n", "\\n"))
                        await self._push(done_text, effective_dir, session_id)
                        return True
                    ops_text = ""
                    if self.output_level == "detail":
                        ops_text = format_consolidated_notification(
                            title, session_id, ops_list, done, final_text=None
                        )
                    if ops_text:
                        logger.info("SSE push consolidated ops: %s", ops_text[:80].replace("\n", "\\n"))
                        await self._push(ops_text, effective_dir, session_id)
                    if final_text and final_text.strip():
                        done_text = f"{title or '无标题'} ({session_id[:12] if len(session_id) > 12 else session_id})\n\n{final_text.strip()}\n\n任务已完成"
                        logger.info("SSE push conclusion: %s", done_text[:80].replace("\n", "\\n"))
                        await self._push(done_text, effective_dir, session_id)
                    elif not ops_text:
                        done_text = f"{title or '无标题'} ({session_id[:12] if len(session_id) > 12 else session_id})\n任务已完成"
                        logger.info("SSE push done only: %s", done_text[:80].replace("\n", "\\n"))
                        await self._push(done_text, effective_dir, session_id)
                    return True
                else:
                    logger.info("SSE push consolidated: %s", text[:100].replace("\n", "\\n"))
                    await self._push(text, effective_dir, session_id)
                    return True
            effective_dir = directory or (buf.get("directory", "") if buf else "") or self._session_directories.get(session_id, "")
            logger.info("SSE push consolidated: %s", text[:100].replace("\n", "\\n"))
            await self._push(text, effective_dir, session_id)
            if buf:
                buf["ops"] = []
            return True
        elif done:
            self._session_buffers.pop(session_id, None)
        return False

    async def _add_to_buffer(
        self, session_id: str, directory: str, tool_name: str,
        detail: str, props: dict,
    ):
        if self.output_level != "detail":
            return
        if session_id not in self._session_buffers:
            self._session_buffers[session_id] = {
                "directory": directory,
                "start_time": time.monotonic(),
                "ops": [],
            }

        from .constants import TOOL_NAMES_CHINESE
        label = TOOL_NAMES_CHINESE.get(tool_name, tool_name)

        if tool_name in ("edit", "Edit"):
            if detail:
                op = {"type": "edit", "detail": detail}
            else:
                op = {"type": "edit", "detail": ""}
        elif tool_name in ("write", "Write"):
            op = {"type": "write", "detail": detail} if detail else {"type": "write", "detail": ""}
        elif tool_name in ("read", "Read"):
            op = {"type": "read", "detail": detail} if detail else {"type": "read", "detail": ""}
        elif tool_name in ("bash", "Bash"):
            op = {"type": "shell", "detail": detail} if detail else {"type": "shell", "detail": ""}
        elif tool_name in ("task", "Task"):
            desc = detail or props.get("description", props.get("subagent_type", ""))
            op = {"type": "subsession", "detail": desc} if desc else {"type": "subsession", "detail": ""}
        elif tool_name in ("todowrite", "TodoWrite"):
            todos = props.get("todos", [])
            if todos:
                task_lines = []
                for t in todos:
                    content = t.get("content", "")
                    status = t.get("status", "")
                    priority = t.get("priority", "")
                    status_map = {"pending": "⏳", "in_progress": "🔄", "completed": "✅", "cancelled": "❌"}
                    icon = status_map.get(status, "")
                    prio = f" [{priority}]" if priority else ""
                    task_lines.append(f"    {icon} {content}{prio}")
                op = {"type": "todo", "detail": f"任务列表更新 ({len(todos)} 项)\n" + "\n".join(task_lines)}
            else:
                op = {"type": "todo", "detail": "任务列表更新"}
        elif tool_name in ("glob", "Glob", "grep", "Grep"):
            op = {"type": "search", "detail": detail} if detail else {"type": "search", "detail": ""}
        elif tool_name == "command":
            op = {"type": "command", "detail": detail} if detail else {"type": "command", "detail": ""}
        else:
            op = {"type": tool_name, "detail": detail} if detail else {"type": tool_name, "detail": ""}

        buf = self._session_buffers[session_id]
        if op not in buf["ops"]:
            buf["ops"].append(op)
        if session_id not in self._flush_tasks or self._flush_tasks[session_id].done():
            self._flush_tasks[session_id] = asyncio.create_task(
                self._delayed_flush(session_id, directory)
            )
