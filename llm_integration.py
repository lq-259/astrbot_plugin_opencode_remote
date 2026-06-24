"""LLM Function Calling 工具集成 - 自然语言控制 OpenCode"""
import asyncio
import os
import sqlite3
from urllib.parse import urlparse

from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.provider import ProviderRequest
from astrbot.api import logger
from .formatters import (
    extract_text_from_parts, format_help_text, format_window_state,
    format_response_with_meta,
)


class LLMIntegration:

    def __init__(self, plugin):
        self.plugin = plugin
        self.client = plugin.client
        self.state_mgr = plugin.state_mgr
        self.path_mgr = plugin.path_mgr
        self.model_mgr = plugin.model_mgr
        self.pending_mgr = plugin.pending_mgr
        self._last_search_results: dict[str, list[dict]] = {}

    # ──── 工具可见性控制 ────

    async def on_llm_request_hook(self, event: AstrMessageEvent, request: ProviderRequest):
        if not self.plugin._can_use(event):
            self._remove_all_tools(request)
            return

        umo = event.unified_msg_origin
        directory = self.state_mgr.get_current_directory(umo)
        current_sid = self.state_mgr.get_current_session(umo)

        if not directory and not current_sid:
            self._remove_all_tools(request, keep_basic=True)
            return

    def _remove_all_tools(self, request: ProviderRequest, keep_basic: bool = False):
        if not hasattr(request, 'func_tool') or not request.func_tool:
            return
        basic = {"opencode_search_sessions", "opencode_list_commands"}
        for name in list(request.func_tool._tools.keys()):
            if keep_basic and name in basic:
                continue
            if name.startswith("opencode_"):
                request.func_tool.remove_tool(name)

    async def _sync_actual_session_meta(self, umo: str) -> dict:
        state = dict(self.state_mgr.get_window_state(umo))
        sid = state.get("current_session", "")
        directory = state.get("directory", "")
        if not sid or not directory:
            return state
        try:
            resp = await self.client.request("GET", f"/session/{sid}/message", directory=directory)
            messages = resp.json()
            if isinstance(messages, list):
                for msg in reversed(messages):
                    info = msg.get("info", {})
                    if info.get("role") == "assistant" and info.get("modelID"):
                        provider_id = info.get("providerID", "")
                        model_id = info.get("modelID", "")
                        variant = info.get("variant", "")
                        agent = info.get("agent", "")
                        state["server_model"] = f"{provider_id}/{model_id}" if provider_id else model_id
                        if variant:
                            state["server_variant"] = variant
                        if agent:
                            state["server_agent"] = agent
                        break
        except Exception:
            pass
        return state

    def _local_sessions(self) -> list[dict]:
        host = urlparse(self.client.server_url).hostname or ""
        if host not in ("127.0.0.1", "localhost", "0.0.0.0", "::1"):
            return []
        db_path = os.path.expanduser("~/.local/share/opencode/opencode.db")
        if not os.path.exists(db_path):
            return []
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=1)
            conn.row_factory = sqlite3.Row
            try:
                rows = conn.execute(
                    """
                    SELECT id, directory, title, parent_id, time_updated, time_archived
                    FROM session
                    ORDER BY time_updated DESC
                    """
                ).fetchall()
            finally:
                conn.close()
        except Exception as e:
            logger.debug(f"读取本地 OpenCode 会话库失败: {e}")
            return []
        return [
            {
                "id": row["id"],
                "directory": row["directory"] or "",
                "title": row["title"] or "无标题",
                "parentID": row["parent_id"],
                "time": {"updated": row["time_updated"], "archived": row["time_archived"]},
            }
            for row in rows
        ]

    async def _search_sessions(self, query: str = "", limit: int = 10) -> list[dict]:
        query_lower = (query or "").lower().strip()
        seen = {}

        # 1. Try global session list first (no directory filter) — works for remote servers
        try:
            for s in await self.client.session_list(None):
                sid = s.get("id")
                if sid:
                    seen[sid] = s
        except Exception:
            pass

        # 2. Search per-directory for local context
        directories = set()
        directory = self.path_mgr.default_workdir
        if directory:
            directories.add(self.path_mgr.normalize_path(directory))
        for state in self.state_mgr._window_states.values():
            if state.get("directory"):
                directories.add(state["directory"])
        for path in self.path_mgr.get_allowed_dirs_with_recent():
            directories.add(path)
        local = self._local_sessions()
        for s in local:
            if s.get("directory"):
                directories.add(s["directory"])
        for d in directories:
            try:
                for s in await self.client.session_list(d):
                    sid = s.get("id")
                    if sid:
                        seen[sid] = s
            except Exception:
                continue

        # 3. Merge local DB fallback
        for s in local:
            sid = s.get("id")
            if sid and sid not in seen:
                seen[sid] = s

        sessions = list(seen.values())
        if query_lower:
            sessions = [
                s for s in sessions
                if s.get("id", "").lower().startswith(query_lower)
                or query_lower in (s.get("title", "") or "").lower()
                or query_lower in (s.get("directory", "") or "").lower()
            ]
        return sessions[: max(1, min(limit, 30))]

    def _format_session_rows(self, sessions: list[dict]) -> str:
        if not sessions:
            return "未找到会话"
        lines = []
        for i, s in enumerate(sessions, 1):
            sid = s.get("id", "")
            title = s.get("title", "无标题") or "无标题"
            directory = s.get("directory", "")
            lines.append(f"{i}. {title} ({sid[:12]})")
            if sid:
                lines.append(f"   ID: {sid}")
            if directory:
                lines.append(f"   {directory}")
        return "\n".join(lines)

    async def _resolve_session_target(self, umo: str, target: str) -> tuple[dict | None, str]:
        if not target:
            sid = self.state_mgr.get_current_session(umo)
            if not sid:
                return None, "未绑定会话"
            return {
                "id": sid,
                "title": self.state_mgr.get_window_state(umo).get("session_title", "无标题"),
                "directory": self.state_mgr.get_current_directory(umo) or "",
            }, ""
        last_results = self._last_search_results.get(umo) or []
        sessions = last_results if target.isdigit() and last_results else await self._search_sessions(target, limit=30)
        if target.isdigit():
            idx = int(target)
            if 1 <= idx <= len(sessions):
                return sessions[idx - 1], ""
        target_lower = target.lower().strip()
        matches = [
            s for s in sessions
            if s.get("id", "").startswith(target)
            or target_lower in (s.get("title", "") or "").lower()
        ]
        if len(matches) == 1:
            return matches[0], ""
        if len(matches) > 1:
            return None, self._format_session_rows(matches[:10])
        return None, f"未找到会话: {target}"

    # ──── 审批机制 ────

    async def _require_approval(self, tool_name: str, args: dict, event: AstrMessageEvent) -> tuple:
        index = self.pending_mgr.add_plugin_confirmation(
            "", tool_name, str(args), ""
        )
        total = self.pending_mgr.count
        args_str = ", ".join(f"{k}={v}" for k, v in args.items())
        msg = (
            f"请求 [{index}] {tool_name}\n"
            f"参数: {args_str}\n\n"
            f"当前共 {total} 个待审批\n"
            f"/oc allow {index}  批准\n"
            f"/oc deny {index}   拒绝\n"
            f"/oc approve       全部批准"
        )
        try:
            await event.send(MessageChain().message(msg))
        except Exception as e:
            logger.warning(f"审批通知发送失败: {e}")
            return (False, "notification_failed")

        try:
            approved = await asyncio.wait_for(
                self.pending_mgr._pending[str(index)]["future"], timeout=60
            )
            return (True, "approved") if approved else (False, "denied")
        except asyncio.TimeoutError:
            self.pending_mgr.deny(index)
            return (False, "timeout")

    # ──── 查询工具 ────

    @filter.llm_tool(name="opencode_get_session_detail")
    async def tool_get_session_detail(self, event: AstrMessageEvent):
        '''获取当前 OpenCode 会话详情，包括完整 ID、目录、实际模型、agent 和思考等级。'''
        umo = event.unified_msg_origin
        state = await self._sync_actual_session_meta(umo)
        sid = state.get("current_session", "")
        directory = state.get("directory", "")
        if not sid:
            yield "未绑定会话"
            return
        lines = [format_window_state(state), f"完整会话 ID: {sid}"]
        if directory:
            try:
                session = await self.client.session_get(sid, directory)
                if session.get("slug"):
                    lines.append(f"slug: {session.get('slug')}")
                if session.get("version"):
                    lines.append(f"OpenCode version: {session.get('version')}")
                summary = session.get("summary") or {}
                if summary:
                    lines.append(
                        f"改动统计: +{summary.get('additions', 0)} -{summary.get('deletions', 0)} files={summary.get('files', 0)}"
                    )
            except Exception as e:
                lines.append(f"获取服务端会话详情失败: {e}")
        yield "\n".join(lines)

    @filter.llm_tool(name="opencode_search_sessions")
    async def tool_search_sessions(self, event: AstrMessageEvent, keyword: str = ""):
        '''搜索 OpenCode 会话，可按标题、ID 前缀或目录关键词查找。

        Args:
            keyword(string): 搜索关键词，可为空
        '''
        sessions = await self._search_sessions(keyword, limit=10)
        self._last_search_results[event.unified_msg_origin] = sessions
        yield self._format_session_rows(sessions)

    @filter.llm_tool(name="opencode_list_workdirs")
    async def tool_list_workdirs(self, event: AstrMessageEvent):
        '''列出默认工作目录、白名单目录和最近使用目录。'''
        current = self.state_mgr.get_current_directory(event.unified_msg_origin) or "未设置"
        lines = [f"当前目录: {current}", f"默认目录: {self.path_mgr.default_workdir}"]
        allowed = self.path_mgr.allowed_workdirs
        if allowed:
            lines.append("允许目录:")
            for path in allowed[:20]:
                lines.append(f"  {self.path_mgr.normalize_path(path)}")
        recent = self.path_mgr.get_recent_paths(20)
        if recent:
            lines.append("最近目录:")
            for path in recent:
                lines.append(f"  {path}")
        yield "\n".join(lines)

    @filter.llm_tool(name="opencode_switch_workdir")
    async def tool_switch_workdir(self, event: AstrMessageEvent, path: str):
        '''切换 OpenCode 工作目录。路径必须通过插件白名单安全检查。

        Args:
            path(string): 目标工作目录
        '''
        norm_path = self.path_mgr.normalize_path(path)
        if not self.path_mgr.is_path_allowed(norm_path):
            yield f"路径不在白名单或不允许: {norm_path}"
            return
        try:
            await self.client.project_current(norm_path)
            await self.client.path_info(norm_path)
        except Exception as e:
            yield f"OpenCode 无法识别该路径: {e}"
            return
        umo = event.unified_msg_origin
        self.state_mgr.set_window_state(umo, directory=norm_path)
        await self.state_mgr.persist_window_state(umo)
        self.path_mgr.add_recent_path(norm_path)
        yield f"已切换目录: {norm_path}"

    @filter.llm_tool(name="opencode_get_recent_messages")
    async def tool_get_recent_messages(self, event: AstrMessageEvent, limit: int = 5):
        '''获取当前会话最近消息摘要。

        Args:
            limit(number): 返回最近多少条消息，默认 5，最大 20
        '''
        umo = event.unified_msg_origin
        sid = self.state_mgr.get_current_session(umo)
        directory = self.state_mgr.get_current_directory(umo)
        if not sid or not directory:
            yield "未绑定会话"
            return
        try:
            resp = await self.client.request("GET", f"/session/{sid}/message", directory=directory)
            messages = resp.json()
        except Exception as e:
            yield f"获取消息失败: {e}"
            return
        n = max(1, min(int(limit or 5), 20))
        lines = []
        for msg in messages[-n:]:
            info = msg.get("info", {})
            role = info.get("role", "?")
            model = info.get("modelID", "")
            variant = info.get("variant", "")
            text = extract_text_from_parts(msg.get("parts", []))
            if len(text) > 500:
                text = text[:497] + "..."
            meta = f" [{model}/{variant}]" if model or variant else ""
            lines.append(f"{role}{meta}: {text or '(无文本)'}")
        yield "\n\n".join(lines) if lines else "暂无消息"

    @filter.llm_tool(name="opencode_get_last_error")
    async def tool_get_last_error(self, event: AstrMessageEvent):
        '''获取当前会话最近一次 OpenCode 或模型供应商错误。'''
        sid = self.state_mgr.get_current_session(event.unified_msg_origin)
        if not sid:
            yield "未绑定会话"
            return
        error = self.state_mgr.get_last_error(sid)
        yield error or "当前会话暂无错误记录"

    @filter.llm_tool(name="opencode_list_models")
    async def tool_list_models(self, event: AstrMessageEvent, keyword: str = ""):
        '''搜索可用模型。

        Args:
            keyword(string): provider 或 model 关键词，可为空
        '''
        keyword_lower = (keyword or "").lower().strip()
        try:
            data = await self.client.providers()
            try:
                cfg = await self.client.config_get(self.state_mgr.get_current_directory(event.unified_msg_origin))
                config_providers = cfg.get("provider", {})
            except Exception:
                config_providers = {}
        except Exception as e:
            yield f"获取模型列表失败: {e}"
            return
        rows = []
        for provider in data.get("all", []) or data.get("connected", []):
            pid = provider.get("id", "")
            pname = provider.get("name", "")
            models = provider.get("models", {})
            cfg_models = config_providers.get(pid, {}).get("models", {})
            if cfg_models:
                models = {k: v for k, v in models.items() if k in cfg_models}
            for mid, meta in models.items():
                if any(mid.startswith(prefix) for prefix in ("text-embedding-", "gpt-image-", "chatgpt-image-")):
                    continue
                full = f"{pid}/{mid}"
                label = meta.get("name", mid) if isinstance(meta, dict) else mid
                haystack = f"{full} {label} {pname}".lower()
                if keyword_lower and keyword_lower not in haystack:
                    continue
                rows.append(f"{full} - {label}")
                if len(rows) >= 30:
                    break
            if len(rows) >= 30:
                break
        yield "\n".join(rows) if rows else "未找到模型"

    @filter.llm_tool(name="opencode_clear_model_override")
    async def tool_clear_model_override(self, event: AstrMessageEvent):
        '''清除本地模型、思考等级和 agent 覆盖，恢复使用 Web 会话显示的配置。'''
        umo = event.unified_msg_origin
        self.state_mgr.set_window_state(umo, model=None, variant=None, agent=None)
        await self.state_mgr.persist_window_state(umo)
        yield "已清除本地 model/variant/agent 覆盖，后续发送将使用 Web 会话配置"

    @filter.llm_tool(name="opencode_set_model")
    async def tool_set_model(self, event: AstrMessageEvent, model: str, variant: str = ""):
        '''设置本地模型和思考等级覆盖。

        Args:
            model(string): 模型名，格式 provider/model
            variant(string): 思考等级，可为空或 none/minimal/low/medium/high/xhigh/max
        '''
        if variant and not self.model_mgr.validate_variant(variant):
            yield f"无效思考等级: {variant}"
            return
        umo = event.unified_msg_origin
        self.state_mgr.set_window_state(umo, model=model, variant=variant or None)
        await self.state_mgr.persist_window_state(umo)
        yield f"已设置模型覆盖: {model}" + (f" / {variant}" if variant else "")

    @filter.llm_tool(name="opencode_set_agent")
    async def tool_set_agent(self, event: AstrMessageEvent, agent: str):
        '''设置本地 agent 覆盖。

        Args:
            agent(string): agent 名称，如 build/general/plan/explore
        '''
        umo = event.unified_msg_origin
        self.state_mgr.set_window_state(umo, agent=agent)
        await self.state_mgr.persist_window_state(umo)
        yield f"已设置 agent 覆盖: {agent}"

    @filter.llm_tool(name="opencode_list_commands")
    async def tool_list_commands(self, event: AstrMessageEvent, topic: str = ""):
        '''列出 OpenCode 可用命令或帮助主题。话题可选: 基础/路径/会话/消息/指令/模型/通知/审批

        Args:
            topic(string): 帮助主题，不填显示全部
        '''
        yield format_help_text(topic)

    # ──── 操作工具 ────

    @filter.llm_tool(name="opencode_send_message")
    async def tool_send_message(self, event: AstrMessageEvent, message: str, target: str = ""):
        '''向 OpenCode 会话发送任务。target 为空时发送到当前会话。

        Args:
            message(string): 任务内容
            target(string): 可选，会话序号、ID 前缀或标题关键词
        '''
        umo = event.unified_msg_origin
        directory = self.state_mgr.get_current_directory(umo)
        sid = self.state_mgr.get_current_session(umo)
        if target:
            last_results = self._last_search_results.get(umo) or []
            sessions = last_results if target.isdigit() and last_results else await self._search_sessions(target, limit=30)
            chosen = None
            if target.isdigit():
                idx = int(target)
                if 1 <= idx <= len(sessions):
                    chosen = sessions[idx - 1]
            if not chosen:
                target_lower = target.lower().strip()
                matches = [
                    s for s in sessions
                    if s.get("id", "").startswith(target)
                    or target_lower in (s.get("title", "") or "").lower()
                ]
                if len(matches) == 1:
                    chosen = matches[0]
                elif len(matches) > 1:
                    yield self._format_session_rows(matches[:10])
                    return
            if not chosen:
                yield f"未找到会话: {target}"
                return
            sid = chosen["id"]
            directory = chosen.get("directory") or directory
        if not sid:
            if directory:
                try:
                    sess = await self.client.session_create(directory=directory)
                    sid = sess.get("id", "")
                    self.state_mgr.set_window_state(umo, current_session=sid)
                    self.state_mgr.set_session_owner(sid, umo)
                    await self.state_mgr.persist_window_state(umo)
                    await self.state_mgr.persist_session_owners()
                except Exception:
                    pass
        if not sid:
            yield "未绑定会话"
            return
        local_model = self.state_mgr.get_current_model(umo)
        local_variant = self.state_mgr.get_current_variant(umo)
        model_body = self.model_mgr.build_model_body(local_model) if local_model else None
        variant = local_variant or None
        agent = self.state_mgr.get_current_agent(umo) or None
        try:
            result = await self.client.session_prompt(
                sid, message, directory=directory,
                model=model_body, agent=agent, variant=variant
            )
            yield format_response_with_meta(
                extract_text_from_parts(result.get("parts", [])) or "执行完成",
                self.state_mgr.get_window_state(umo),
            )
        except Exception as e:
            yield f"请求失败: {e}"

    @filter.llm_tool(name="opencode_switch_session")
    async def tool_switch_session(self, event: AstrMessageEvent, target: str):
        '''切换到指定会话。

        Args:
            target(string): 会话序号（如 1）或 ID 前缀（如 abc123）
        '''
        umo = event.unified_msg_origin
        last_results = self._last_search_results.get(umo) or []
        sessions = last_results if target.isdigit() and last_results else await self._search_sessions(target, limit=30)
        chosen = None
        if target.isdigit():
            idx = int(target)
            if 1 <= idx <= len(sessions):
                chosen = sessions[idx - 1]
        if not chosen:
            target_lower = target.lower().strip()
            matches = [
                s for s in sessions
                if s.get("id", "").startswith(target)
                or target_lower in (s.get("title", "") or "").lower()
            ]
            if len(matches) == 1:
                chosen = matches[0]
            elif len(matches) > 1:
                yield self._format_session_rows(matches[:10])
                return
        if not chosen:
            yield f"未找到会话: {target}"
            return
        sid = chosen["id"]
        self.state_mgr.set_window_state(
            umo,
            current_session=sid,
            directory=chosen.get("directory") or self.state_mgr.get_current_directory(umo),
            session_title=chosen.get("title", "") or "无标题",
        )
        self.state_mgr.set_session_owner(sid, umo)
        await self.state_mgr.persist_window_state(umo)
        await self.state_mgr.persist_session_owners()
        yield f"已切换到: {chosen.get('title', '无标题')} ({sid[:12]})"

    @filter.llm_tool(name="opencode_rename_session")
    async def tool_rename_session(self, event: AstrMessageEvent, title: str):
        '''重命名当前 OpenCode 会话。

        Args:
            title(string): 新会话标题
        '''
        umo = event.unified_msg_origin
        sid = self.state_mgr.get_current_session(umo)
        directory = self.state_mgr.get_current_directory(umo)
        if not sid or not directory:
            yield "未绑定会话"
            return
        try:
            await self.client.session_update(sid, title, directory)
            self.state_mgr.set_window_state(umo, session_title=title)
            await self.state_mgr.persist_window_state(umo)
            yield f"已重命名会话: {title} ({sid[:12]})"
        except Exception as e:
            yield f"重命名失败: {e}"

    @filter.llm_tool(name="opencode_archive_session")
    async def tool_archive_session(self, event: AstrMessageEvent, target: str = ""):
        '''归档指定 OpenCode 会话。不传 target 时归档当前会话。

        Args:
            target(string): 可选，会话序号、ID 前缀或标题关键词
        '''
        umo = event.unified_msg_origin
        chosen, error = await self._resolve_session_target(umo, target)
        if not chosen:
            yield error
            return
        sid = chosen["id"]
        state = self.state_mgr.get_window_state(umo)
        archived = list(state.get("archived_sessions", []) or [])
        if sid in archived:
            yield f"该会话已归档: {chosen.get('title', '无标题')} ({sid[:12]})"
            return
        archived.append(sid)
        self.state_mgr.set_window_state(umo, archived_sessions=archived)
        await self.state_mgr.persist_window_state(umo)
        yield f"已归档会话: {chosen.get('title', '无标题')} ({sid[:12]})"

    @filter.llm_tool(name="opencode_unarchive_session")
    async def tool_unarchive_session(self, event: AstrMessageEvent, target: str = ""):
        '''取消归档指定 OpenCode 会话。不传 target 时取消归档当前会话。

        Args:
            target(string): 可选，会话序号、ID 前缀或标题关键词
        '''
        umo = event.unified_msg_origin
        chosen, error = await self._resolve_session_target(umo, target)
        if not chosen:
            yield error
            return
        sid = chosen["id"]
        state = self.state_mgr.get_window_state(umo)
        archived = list(state.get("archived_sessions", []) or [])
        if sid not in archived:
            yield f"该会话未归档: {chosen.get('title', '无标题')} ({sid[:12]})"
            return
        archived.remove(sid)
        self.state_mgr.set_window_state(umo, archived_sessions=archived)
        await self.state_mgr.persist_window_state(umo)
        yield f"已取消归档: {chosen.get('title', '无标题')} ({sid[:12]})"

    @filter.llm_tool(name="opencode_delete_session")
    async def tool_delete_session(self, event: AstrMessageEvent, target: str):
        '''删除指定 OpenCode 会话。需要人工审批。

        Args:
            target(string): 会话序号或 ID 前缀
        '''
        approved, reason = await self._require_approval(
            "opencode_delete_session", {"target": target}, event
        )
        if not approved:
            yield f"操作被{'超时' if reason == 'timeout' else '拒绝'}"
            return
        umo = event.unified_msg_origin
        last_results = self._last_search_results.get(umo) or []
        sessions = last_results if target.isdigit() and last_results else await self._search_sessions(target, limit=30)
        chosen = None
        if target.isdigit():
            idx = int(target)
            if 1 <= idx <= len(sessions):
                chosen = sessions[idx - 1]
        if not chosen:
            target_lower = target.lower().strip()
            matches = [
                s for s in sessions
                if s.get("id", "").startswith(target)
                or target_lower in (s.get("title", "") or "").lower()
            ]
            if len(matches) == 1:
                chosen = matches[0]
            elif len(matches) > 1:
                yield self._format_session_rows(matches[:10])
                return
        if not chosen:
            yield f"未找到会话: {target}"
            return
        sid = chosen["id"]
        directory = chosen.get("directory") or self.state_mgr.get_current_directory(umo)
        try:
            await self.client.session_delete(sid, directory)
            if sid == self.state_mgr.get_current_session(umo):
                self.state_mgr.set_window_state(umo, current_session=None)
                await self.state_mgr.persist_window_state(umo)
            self.state_mgr.remove_session_owner(sid)
            await self.state_mgr.persist_session_owners()
            yield f"已删除会话: {chosen.get('title', '无标题')} ({sid[:12]})"
        except Exception as e:
            yield f"删除失败: {e}"

    @filter.llm_tool(name="opencode_run_command")
    async def tool_run_command(self, event: AstrMessageEvent, command: str, arguments: str = ""):
        '''执行 OpenCode 内置 command。

        Args:
            command(string): 命令名，不带斜杠
            arguments(string): 命令参数，可为空
        '''
        umo = event.unified_msg_origin
        sid = self.state_mgr.get_current_session(umo)
        directory = self.state_mgr.get_current_directory(umo)
        if not sid or not directory:
            yield "未绑定会话"
            return
        try:
            result = await self.client.session_command(sid, command.lstrip("/"), arguments, directory=directory)
            yield extract_text_from_parts(result.get("parts", [])) or f"命令 /{command.lstrip('/')} 已执行"
        except Exception as e:
            yield f"执行命令失败: {e}"

    @filter.llm_tool(name="opencode_create_session")
    async def tool_create_session(self, event: AstrMessageEvent, title: str = ""):
        '''创建新的 OpenCode 会话。

        Args:
            title(string): 会话标题
        '''
        umo = event.unified_msg_origin
        directory = self.state_mgr.get_current_directory(umo)
        if not directory:
            directory = self.path_mgr.default_workdir
        try:
            sess = await self.client.session_create(title=title or None, directory=directory)
            sid = sess.get("id", "")
            self.state_mgr.set_window_state(umo, current_session=sid)
            self.state_mgr.set_session_owner(sid, umo)
            await self.state_mgr.persist_window_state(umo)
            await self.state_mgr.persist_session_owners()
            yield f"已创建会话: {title or '未命名'} ({sid[:12]})"
        except Exception as e:
            yield f"创建失败: {e}"

    @filter.llm_tool(name="opencode_stop")
    async def tool_stop(self, event: AstrMessageEvent):
        '''停止当前 OpenCode 会话的任务。'''
        umo = event.unified_msg_origin
        sid = self.state_mgr.get_current_session(umo)
        directory = self.state_mgr.get_current_directory(umo)
        if not sid or not directory:
            yield "未绑定会话"
            return
        try:
            await self.client.session_abort(sid, directory)
            yield f"已停止会话 ({sid[:12]})"
        except Exception as e:
            yield f"停止失败: {e}"
    async def tool_stop(self, event: AstrMessageEvent):
        '''停止当前 OpenCode 会话的任务。'''
        umo = event.unified_msg_origin
        sid = self.state_mgr.get_current_session(umo)
        directory = self.state_mgr.get_current_directory(umo)
        if not sid or not directory:
            yield "未绑定会话"
            return
        try:
            await self.client.session_abort(sid, directory)
            yield f"已停止会话 ({sid[:12]})"
        except Exception as e:
            yield f"停止失败: {e}"

    @filter.llm_tool(name="opencode_read_file")
    async def tool_read_file(self, event: AstrMessageEvent, file_path: str):
        '''读取 OpenCode 工作目录中的文件内容。

        Args:
            file_path(string): 文件相对路径或绝对路径
        '''
        umo = event.unified_msg_origin
        directory = self.state_mgr.get_current_directory(umo)
        if not directory:
            yield "未设置工作路径"
            return
        try:
            result = await self.client.file_content(file_path, directory)
            content = result.get("content", "")
            if not content:
                yield f"文件 `{file_path}` 内容为空"
                return
            if len(content) > 4000:
                content = content[:3997] + "..."
            yield f"`{file_path}`:\n\n```\n{content}\n```"
        except Exception as e:
            yield f"读取失败: {e}"

    @filter.llm_tool(name="opencode_write_file")
    async def tool_write_file(self, event: AstrMessageEvent, file_path: str, content: str):
        '''向 OpenCode 工作目录写入文件内容（覆盖写入）。

        Args:
            file_path(string): 文件路径
            content(string): 文件内容
        '''
        umo = event.unified_msg_origin
        directory = self.state_mgr.get_current_directory(umo)
        if not directory:
            yield "未设置工作路径"
            return
        try:
            await self.client.file_write(file_path, content, directory)
            yield f"已写入 `{file_path}` ({len(content)} 字节)"
        except Exception as e:
            yield f"写入失败: {e}"

    @filter.llm_tool(name="opencode_list_files")
    async def tool_list_files(self, event: AstrMessageEvent, path: str = ""):
        '''列出 OpenCode 工作目录中的文件和子目录。

        Args:
            path(string): 可选，子目录路径
        '''
        umo = event.unified_msg_origin
        directory = self.state_mgr.get_current_directory(umo)
        if not directory:
            yield "未设置工作路径"
            return
        try:
            files = await self.client.file_list(path or None, directory)
            if not files:
                yield "目录为空"
                return
            lines = [f"`{path or directory}` 下的文件:"]
            for f in files[:30]:
                name = f.get("name", "?")
                ftype = f.get("type", "?")
                size = f.get("size", "")
                meta = f" ({size} 字节)" if size else ""
                lines.append(f"  {'D' if ftype == 'directory' else 'F'} {name}{meta}")
            yield "\n".join(lines)
        except Exception as e:
            yield f"列出文件失败: {e}"
