"""AstrBot OpenCode Remote Controller 插件入口"""
import asyncio
import json
from typing import Optional

import httpx

from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.star import Context, Star, register
from astrbot.core.message.components import At
from astrbot.core.platform.message_type import MessageType
from astrbot.api import AstrBotConfig, logger
from astrbot.api.message_components import Plain

from astrbot.core.utils.session_waiter import session_waiter, SessionController
from .opencode_client import OpenCodeClient
from .state_manager import StateManager
from .path_manager import PathManager, ModelManager
from .pending_manager import PendingManager
from .command_handlers import CommandHandlers
from .formatters import extract_text_from_parts, format_response_with_meta
from .sse_listener import SSEListener
from .notification_manager import NotificationManager
from .llm_integration import LLMIntegration
from .router import MessageRouter
from .task_queue import TaskQueue


@register(
    "astrbot_plugin_opencode_remote",
    "gitsang",
    "通过 AstrBot 远程控制 OpenCode Server/Web/TUI，支持会话、模型、工作路径、指令和状态推送。",
    "1.0.0",
)
class OpenCodeRemotePlugin(Star):

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config

        basic_cfg = config.get("basic_config", {})
        server_url = basic_cfg.get("server_url", "http://127.0.0.1:4096")
        username = basic_cfg.get("username", "opencode")
        password = basic_cfg.get("password", "")
        timeout = basic_cfg.get("timeout", 300)

        self.client = OpenCodeClient(
            server_url=server_url,
            username=username,
            password=password,
            timeout=timeout,
        )

        self.state_mgr = StateManager(self)
        self.path_mgr = PathManager(config)
        self.model_mgr = ModelManager(config)
        self.pending_mgr = PendingManager()
        self.state_mgr.pending_mgr = self.pending_mgr

        # Runtime stats
        self.stats = {
            "tasks_sent": 0,
            "tasks_completed": 0,
            "tasks_failed": 0,
            "total_response_time": 0.0,
            "sse_reconnects": 0,
        }

        self.cmd_handlers = CommandHandlers(self)

        self.notification_mgr = NotificationManager(self.context, self.state_mgr)
        self.sse_listener = SSEListener(
            self.client,
            self.notification_mgr.push_notification,
            self.state_mgr,
            plugin=self,
        )

        self.llm_integration = LLMIntegration(self)

        self._quick_prefix = config.get("notification_config", {}).get(
            "quick_prefix", ">"
        )
        self.router = MessageRouter(config)
        self.task_queue = TaskQueue(self)

    # ──── 生命周期 ────

    async def initialize(self):
        await self.state_mgr.load_all()
        self._fix_llm_tool_origin()
        self.llm_integration.sync_tool_activation()

        try:
            health = await self.client.health()
            logger.info(
                f"OpenCode Remote 已连接: "
                f"version={health.get('version', 'unknown')}"
            )
        except Exception as e:
            logger.warning(f"OpenCode Server 连接失败: {e}。"
                           f"请在配置中检查 server_url 和认证信息。")

        notify_cfg = self.config.get("notification_config", {})
        self.sse_listener.start(
            output_level=notify_cfg.get("output_level", "simple"),
            summary_msg_count=notify_cfg.get("summary_msg_count", 5),
            max_reconnect_attempts=notify_cfg.get("max_reconnect_attempts", 10),
            reconnect_backoff_base=notify_cfg.get("reconnect_backoff_base", 2),
        )
        logger.info(f"SSE 监听已启动，推送级别: {self.sse_listener.output_level}")

    def _fix_llm_tool_origin(self):
        """Make dashboard show this plugin as origin and bind helper-class tool handlers."""
        try:
            tool_mgr = self.context.get_llm_tool_manager()
            module_path = self.__class__.__module__
            handler_map = {
                "opencode_get_session_detail": "tool_get_session_detail",
                "opencode_search_sessions": "tool_search_sessions",
                "opencode_list_workdirs": "tool_list_workdirs",
                "opencode_switch_workdir": "tool_switch_workdir",
                "opencode_get_recent_messages": "tool_get_recent_messages",
                "opencode_get_last_error": "tool_get_last_error",
                "opencode_list_models": "tool_list_models",
                "opencode_clear_model_override": "tool_clear_model_override",
                "opencode_set_model": "tool_set_model",
                "opencode_set_agent": "tool_set_agent",
                "opencode_list_commands": "tool_list_commands",
                "opencode_send_message": "tool_send_message",
                "opencode_switch_session": "tool_switch_session",
                "opencode_rename_session": "tool_rename_session",
                "opencode_archive_session": "tool_archive_session",
                "opencode_unarchive_session": "tool_unarchive_session",
                "opencode_delete_session": "tool_delete_session",
                "opencode_run_command": "tool_run_command",
                "opencode_read_file": "tool_read_file",
                "opencode_write_file": "tool_write_file",
                "opencode_list_files": "tool_list_files",
                "opencode_create_session": "tool_create_session",
                "opencode_stop": "tool_stop",
                "opencode_schedule_task": "tool_schedule_task",
                "opencode_list_scheduled_tasks": "tool_list_scheduled_tasks",
                "opencode_cancel_scheduled_task": "tool_cancel_scheduled_task",
            }
            for tool in getattr(tool_mgr, "func_list", []):
                name = getattr(tool, "name", "")
                if name.startswith("opencode_"):
                    tool.handler_module_path = module_path
                    method_name = handler_map.get(name)
                    if method_name:
                        tool.handler = getattr(self.llm_integration, method_name)
        except Exception as e:
            logger.debug(f"修正 OpenCode LLM 工具来源失败: {e}")

    async def terminate(self):
        await self.sse_listener.stop()
        await self.client.close()
        logger.info("OpenCode Remote Plugin 已终止")

    # ──── 公共任务发送 ────

    async def send_task_to_opencode(self, text: str, umo: str, session_id: str = None) -> str:
        """向 OpenCode 发送任务，返回格式化后的结果文本。"""
        import time as _time
        start_time = _time.monotonic()
        directory = self.state_mgr.get_current_directory(umo)
        if not directory:
            directory = self.path_mgr.default_workdir
            if directory:
                self.state_mgr.set_window_state(umo, directory=directory)
                await self.state_mgr.persist_window_state(umo)

        if not directory:
            return "未设置工作路径"

        sid = session_id or self.state_mgr.get_current_session(umo)
        if not sid:
            from . import session_ops
            try:
                sid = await session_ops.ensure_session(self.client, self.state_mgr, umo, directory)
            except Exception as e:
                return f"获取会话失败: {e}"

        if not sid:
            return "未绑定会话"

        # 任务队列：同一目录同时只能有一个 active 任务
        if self.task_queue.is_active(directory):
            task_id = await self.task_queue.enqueue(umo, directory, text)
            return f"当前目录有任务正在执行，已加入队列（任务 ID: {task_id}）"

        await self.task_queue.set_active(directory, umo, text, sid)

        local_model = self.state_mgr.get_current_model(umo)
        local_variant = self.state_mgr.get_current_variant(umo)
        local_agent = self.state_mgr.get_current_agent(umo)
        model_body = self.model_mgr.build_model_body(local_model) if local_model else None
        variant = local_variant or None
        agent = local_agent or None

        try:
            self.stats["tasks_sent"] += 1
            result = await self.client.session_prompt(
                sid, text, directory=directory,
                model=model_body, agent=agent, variant=variant
            )
            response_text = extract_text_from_parts(result.get("parts", []))
            self.stats["tasks_completed"] += 1
            self.stats["total_response_time"] += _time.monotonic() - start_time
            return format_response_with_meta(response_text or "(无响应)", self.state_mgr.get_window_state(umo))
        except (httpx.HTTPError, json.JSONDecodeError, Exception) as e:
            self.stats["tasks_failed"] += 1
            return f"请求失败: {e}"

    # ──── LLM 意图分类调用 ────

    async def _llm_intent_call(self, system_prompt: str, prompt: str):
        """调用 AstrBot 的 LLM 进行意图分类。"""
        try:
            # 使用当前默认 provider
            provider = self.context.get_using_provider()
            if not provider:
                logger.debug("LLM 意图分类: 没有可用的 provider")
                return None
            resp = await provider.text_chat(
                system_prompt=system_prompt,
                prompt=prompt,
                func_tool=None,
            )
            return resp
        except Exception as e:
            logger.debug(f"LLM 意图分类调用失败: {e}")
            return None

    # ──── 权限 ────

    def _is_admin(self, event: AstrMessageEvent) -> bool:
        astrbot_config = self.context.get_config(event.unified_msg_origin)
        admin_ids = [str(x) for x in astrbot_config.get("admins_id", [])]
        return str(event.get_sender_id()) in admin_ids

    def _can_use(self, event: AstrMessageEvent) -> bool:
        only_admin = self.config.get("basic_config", {}).get("only_admin", True)
        return not only_admin or self._is_admin(event)

    # ──── LLM 工具钩子 ────

    @filter.on_llm_request()
    async def on_llm_request_hook(self, event: AstrMessageEvent, request):
        """LLM 工具可见性控制钩子"""
        await self.llm_integration.on_llm_request_hook(event, request)

    # ──── /oc 主命令 ────

    @filter.command("oc")
    async def oc_handler(self, event: AstrMessageEvent):
        self.notification_mgr._event_cache[event.unified_msg_origin] = event
        if not self._can_use(event):
            yield event.plain_result("权限不足，仅管理员可用。")
            event.stop_event()
            return

        message_str = event.message_str.strip()
        parts = message_str.split(None, 1)
        remainder = parts[1].strip() if len(parts) > 1 else ""

        async for result in self.cmd_handlers.route(event, remainder):
            yield result

        event.stop_event()

    # ──── 快捷前缀 ────

    @filter.event_message_type(filter.EventMessageType.ALL, priority=1)
    async def delete_confirmation_handler(self, event: AstrMessageEvent):
        self.notification_mgr._event_cache[event.unified_msg_origin] = event
        result = await self.cmd_handlers.handle_delete_confirmation(event)
        if result is None:
            return
        yield event.plain_result(result)
        event.stop_event()

    @filter.event_message_type(filter.EventMessageType.ALL, priority=10)
    async def quick_prefix_handler(self, event: AstrMessageEvent):
        self.notification_mgr._event_cache[event.unified_msg_origin] = event
        if not self._can_use(event):
            return

        raw = event.message_str
        if not raw or not raw.startswith(self._quick_prefix):
            return

        rest = raw[len(self._quick_prefix):]
        if not rest:
            return

        umo = event.unified_msg_origin
        directory = self.state_mgr.get_current_directory(umo)
        if not directory:
            directory = self.path_mgr.default_workdir
            if directory:
                self.state_mgr.set_window_state(umo, directory=directory)
                await self.state_mgr.persist_window_state(umo)

        parts = rest.split(None, 1)
        target_sid = None
        text = None

        if parts[0].isdigit():
            idx = int(parts[0])
            if len(parts) < 2:
                return
            text = parts[1]
            if directory:
                try:
                    sessions = await self.client.session_list(directory)
                    if 1 <= idx <= len(sessions):
                        target_sid = sessions[idx - 1]["id"]
                except httpx.HTTPError:
                    yield event.plain_result(f"获取会话列表失败")
                    event.stop_event()
                    return
            if not target_sid:
                yield event.plain_result(f"无效序号 {idx}")
                event.stop_event()
                return
        else:
            text = rest.lstrip()
            if not text:
                return
            if directory:
                from . import session_ops
                try:
                    target_sid = await session_ops.ensure_session(
                        self.client, self.state_mgr, umo, directory
                    )
                except (httpx.HTTPError, json.JSONDecodeError) as e:
                    yield event.plain_result(f"获取会话失败: {e}")
                    event.stop_event()
                    return
            if not target_sid:
                yield event.plain_result("未绑定会话，请先 /oc switch")
                event.stop_event()
                return

        if not text:
            return

        result_text = await self.send_task_to_opencode(text, umo, session_id=target_sid)
        yield event.plain_result(result_text)
        event.stop_event()

    # ──── 自动路由 ────

    @filter.event_message_type(filter.EventMessageType.ALL, priority=20)
    async def auto_route_handler(self, event: AstrMessageEvent):
        self.notification_mgr._event_cache[event.unified_msg_origin] = event
        raw = event.message_str.strip()

        # Skip command patterns handled elsewhere
        if raw.startswith("/oc") or raw.startswith(self._quick_prefix):
            return

        if not self._can_use(event):
            return

        if not self.router.enable_auto_route or self.router.mode == "off":
            return

        # Group mention detection
        is_group = not event.is_private_chat()
        is_mentioned = False
        if is_group and self.router.ignore_group_no_mention:
            self_id = event.get_self_id()
            for comp in getattr(event.message_obj, "message", []):
                if isinstance(comp, At):
                    if str(comp.qq) == str(self_id) or str(comp.qq) == "all":
                        is_mentioned = True
                        break

        decision = self.router.classify(raw, is_group=is_group, is_mentioned=is_mentioned)

        # LLM 意图分类补充判断
        if self.router.enable_llm_intent:
            # 场景1：规则评分在 confirm 和 auto 之间，用 LLM 二次判断
            if decision.action in ("confirm", "opencode") and self.router.confirm_threshold <= decision.confidence < self.router.auto_threshold:
                llm_decision = await self.router.classify_with_llm(raw, self._llm_intent_call)
                if llm_decision:
                    decision = RouteDecision(
                        action=llm_decision.action,
                        reason=f"{decision.reason}；{llm_decision.reason}",
                        confidence=llm_decision.confidence,
                        rewritten_task=llm_decision.rewritten_task,
                    )
            # 场景2：规则评分很低（< confirm_threshold），但用户可能用自然语言表达意图
            elif decision.action == "chat" and decision.confidence < self.router.confirm_threshold:
                llm_decision = await self.router.classify_with_llm(raw, self._llm_intent_call)
                if llm_decision and llm_decision.action in ("confirm", "opencode"):
                    decision = RouteDecision(
                        action="confirm",
                        reason=f"LLM 意图分类：{llm_decision.reason}",
                        confidence=llm_decision.confidence,
                        rewritten_task=llm_decision.rewritten_task,
                    )

        if decision.action == "chat":
            return

        # From here we intercept the message
        event.stop_event()

        if decision.action == "opencode":
            result = await self.send_task_to_opencode(
                decision.rewritten_task, event.unified_msg_origin
            )
            yield event.plain_result(f"[OpenCode] {result}")
            return

        # confirm mode
        task_preview = decision.rewritten_task[:80] + "..." if len(decision.rewritten_task) > 80 else decision.rewritten_task
        yield event.plain_result(
            f"🛠 这看起来是代码任务（置信度 {decision.confidence:.0%}）：{decision.reason}\n"
            f"任务预览：{task_preview}\n\n"
            f"回复「确认」交给 OpenCode 执行，其他内容取消。"
        )

        approved = False
        choice_event = asyncio.Event()

        @session_waiter(timeout=self.router.confirm_timeout)
        async def wait_confirm(c, e: AstrMessageEvent):
            nonlocal approved
            if e.message_str.strip() in ("确认", "是", "yes", "y"):
                approved = True
            choice_event.set()
            c.stop()

        try:
            await wait_confirm(event)
            await choice_event.wait()
        except TimeoutError:
            yield event.plain_result("⏱ 确认超时，已取消")
            return

        if approved:
            yield event.plain_result("🚀 已提交 OpenCode，请稍候...")
            result = await self.send_task_to_opencode(
                decision.rewritten_task, event.unified_msg_origin
            )
            yield event.plain_result(f"[OpenCode] {result}")
        else:
            yield event.plain_result("❌ 已取消")
