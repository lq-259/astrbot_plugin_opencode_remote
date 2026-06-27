"""命令处理器 - 所有 /oc 子命令路由和实现"""
import asyncio
import os
import re
import sqlite3
import subprocess
import time
from typing import Optional
from urllib.parse import urlparse

import httpx

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.core.utils.session_waiter import session_waiter, SessionController

from .opencode_client import OpenCodeClient
from .state_manager import StateManager
from .path_manager import PathManager, ModelManager
from .pending_manager import PendingManager
from . import session_ops
from .formatters import (
    format_health,
    format_path_info,
    format_session_list,
    format_session_status,
    format_messages,
    format_config_summary,
    format_help_text,
    format_unknown_command,
    format_window_state,
    extract_text_from_parts,
)
from .constants import HELP_TOPIC_LIST, DEFAULT_DESTRUCTIVE_KEYWORDS


class CommandHandlers:

    def __init__(self, plugin):
        self.plugin = plugin
        self.client: OpenCodeClient = plugin.client
        self.state_mgr: StateManager = plugin.state_mgr
        self.path_mgr: PathManager = plugin.path_mgr
        self.model_mgr: ModelManager = plugin.model_mgr
        self.pending_mgr: PendingManager = plugin.pending_mgr
        self.config = plugin.config
        self._delete_confirmations: dict[str, dict] = {}

    # ──── 工具方法 ────

    def _is_admin(self, event: AstrMessageEvent) -> bool:
        astrbot_config = self.plugin.context.get_config(event.unified_msg_origin)
        admin_ids = [str(x) for x in astrbot_config.get("admins_id", [])]
        return str(event.get_sender_id()) in admin_ids

    def _get_umo(self, event: AstrMessageEvent) -> str:
        return event.unified_msg_origin

    def _get_sender(self, event: AstrMessageEvent) -> str:
        return str(event.get_sender_id())

    def _get_directory(self, event: AstrMessageEvent) -> str:
        umo = self._get_umo(event)
        directory = self.state_mgr.get_current_directory(umo)
        if not directory:
            directory = self.path_mgr.default_workdir
        return directory

    async def _ensure_directory(self, event: AstrMessageEvent) -> Optional[str]:
        umo = self._get_umo(event)
        directory = self.state_mgr.get_current_directory(umo)
        if not directory:
            directory = self.path_mgr.default_workdir
            if directory:
                self.state_mgr.set_window_state(umo, directory=directory)
                await self.state_mgr.persist_window_state(umo)
            else:
                return None
        return directory

    async def _ensure_session(self, event: AstrMessageEvent) -> Optional[str]:
        umo = self._get_umo(event)
        directory = self.state_mgr.get_current_directory(umo)
        if not directory:
            directory = self.path_mgr.default_workdir
            if directory:
                self.state_mgr.set_window_state(umo, directory=directory)
                await self.state_mgr.persist_window_state(umo)
            else:
                return None
        sid = await session_ops.ensure_session(
            self.client, self.state_mgr, umo, directory
        )
        if not sid:
            return None
        return sid

    def _is_destructive(self, text: str) -> bool:
        cfg = self.config.get("security_config", {})
        keywords = cfg.get("destructive_keywords", DEFAULT_DESTRUCTIVE_KEYWORDS)
        text_lower = text.lower()
        for kw in keywords:
            if re.search(kw, text_lower):
                return True
        return False

    async def handle_delete_confirmation(self, event: AstrMessageEvent) -> str | None:
        umo = self._get_umo(event)
        pending = self._delete_confirmations.get(umo)
        if not pending:
            return None
        if pending.get("sender_id") != self._get_sender(event):
            return None

        now = time.monotonic()
        if now > pending.get("expires_at", 0):
            self._delete_confirmations.pop(umo, None)
            return "删除确认已超时"

        self._delete_confirmations.pop(umo, None)
        sid = pending.get("sid", "")
        if event.message_str.strip() != "确认":
            return "已取消"

        try:
            await self.client.session_delete(sid)
            self.state_mgr.set_window_state(umo, current_session=None)
            await self.state_mgr.persist_window_state(umo)
            self.state_mgr.remove_session_owner(sid)
            await self.state_mgr.persist_session_owners()
            return f"已删除会话 [{sid[:12]}]"
        except httpx.HTTPError as e:
            return f"删除失败: {e}"

    async def _confirm_action(
        self, event: AstrMessageEvent, message: str, timeout: int = 30
    ):
        yield event.plain_result(message)

        approved = False
        user_choice = asyncio.Event()

        @session_waiter(timeout=timeout)
        async def confirm(c: SessionController, e: AstrMessageEvent):
            nonlocal approved
            if e.message_str.strip() == "确认":
                approved = True
                user_choice.set()
                c.stop()
            else:
                user_choice.set()
                c.stop()

        try:
            await confirm(event)
            await user_choice.wait()
        except TimeoutError:
            yield event.plain_result("超时取消")
            yield False
            return

        if not approved:
            yield event.plain_result("已取消")
        yield approved

    # ──── 命令路由 ────

    ROUTES = {
        "help": ("基础", True),
        "health": ("基础", False),
        "status": ("基础", False),
        "pwd": ("路径", False),
        "config": ("基础", False),
        "cd": ("路径", True),
        "dirs": ("路径", False),
        "list": ("会话", True),
        "new": ("会话", True),
        "switch": ("会话", True),
        "rename": ("会话", True),
        "delete": ("会话", True),
        "archive": ("会话", True),
        "unarchive": ("会话", True),
        "share": ("会话", False),
        "unshare": ("会话", False),
        "summary": ("会话", True),
        "messages": ("会话", True),
        "ask": ("消息", True),
        "work": ("消息", True),
        "to": ("消息", True),
        "stop": ("消息", False),
        "commands": ("指令", False),
        "cmd": ("指令", True),
        "shell": ("指令", True),
        "models": ("模型", True),
        "model": ("模型", True),
        "variant": ("模型", True),
        "agent": ("会话", True),
        "bind": ("通知", True),
        "output": ("通知", True),
        "pending": ("审批", False),
        "allow": ("审批", True),
        "deny": ("审批", True),
        "approve": ("审批", False),
        "read": ("文件", True),
        "write": ("文件", True),
        "files": ("文件", True),
        "diff": ("会话", False),
        "commit": ("会话", True),
        "project": ("路径", True),
        "queue": ("会话", False),
    }

    async def route(self, event: AstrMessageEvent, remainder: str):
        if not remainder:
            async for result in self.cmd_help(event, ""):
                yield result
            return

        parts = remainder.split(None, 1)
        subcommand = parts[0].lower()
        argument = parts[1] if len(parts) > 1 else ""

        if subcommand in ("ls",):
            subcommand = "list"
        if subcommand in ("s", "session"):
            subcommand = "status"
        if subcommand in ("msg",):
            subcommand = "messages"
        if subcommand in ("new",):
            full_cmd = remainder.strip()
            cmd_parts = full_cmd.split(None, 1)
            subcommand = "new"
            argument = cmd_parts[1] if len(cmd_parts) > 1 else ""

        route = self.ROUTES.get(subcommand)
        if not route:
            yield event.plain_result(format_unknown_command(subcommand))
            return

        _, takes_arg = route
        if takes_arg:
            async for result in self._dispatch(subcommand, event, argument):
                yield result
        else:
            async for result in self._dispatch(subcommand, event):
                yield result

    async def _dispatch(self, cmd: str, event: AstrMessageEvent, arg: str = ""):
        method_name = f"cmd_{cmd}"
        method = getattr(self, method_name, None)
        if not method:
            yield event.plain_result(f"命令 {cmd} 未实现")
            return

        import inspect
        sig = inspect.signature(method)
        params = list(sig.parameters.keys())
        if len(params) >= 2:
            async for result in method(event, arg):
                yield result
        else:
            async for result in method(event):
                yield result

    # ──── 基础命令 ────

    async def cmd_help(self, event: AstrMessageEvent, topic: str = ""):
        yield event.plain_result(format_help_text(topic))

    async def cmd_health(self, event: AstrMessageEvent):
        try:
            health_data = await self.client.health()
            yield event.plain_result(format_health(health_data))
        except httpx.HTTPError as e:
            yield event.plain_result(f"OpenCode Server 不可用: {e}")

    async def cmd_status(self, event: AstrMessageEvent):
        umo = self._get_umo(event)
        state = self.state_mgr.get_window_state(umo)
        directory = self._get_directory(event)
        sid = state.get("current_session", "")
        # Get actual model/variant from the session's latest assistant message
        if sid and directory:
            try:
                resp = await self.client.request(
                    "GET", f"/session/{sid}/message", directory=directory
                )
                messages = resp.json()
                if isinstance(messages, list):
                    for msg in reversed(messages):
                        info = msg.get("info", {})
                        if info.get("role") == "assistant" and info.get("modelID"):
                            provider_id = info.get("providerID", "")
                            model_id = info.get("modelID", "")
                            variant = info.get("variant", "")
                            state["server_model"] = f"{provider_id}/{model_id}" if provider_id else model_id
                            if variant:
                                state["server_variant"] = variant
                            break
            except Exception:
                pass
        yield event.plain_result(format_window_state(state))

    async def cmd_pwd(self, event: AstrMessageEvent):
        directory = self._get_directory(event)
        if not directory:
            yield event.plain_result("未设置工作路径")
            return
        try:
            path_data = await self.client.path_info(directory)
            yield event.plain_result(format_path_info(path_data, directory))
        except httpx.HTTPError as e:
            yield event.plain_result(f"获取路径信息失败: {e}")

    async def cmd_config(self, event: AstrMessageEvent):
        yield event.plain_result(format_config_summary(self.config))

    # ──── 路径命令 ────

    async def cmd_cd(self, event: AstrMessageEvent, path: str = ""):
        if not path:
            yield event.plain_result("用法: /oc cd <路径>")
            return
        norm_path = self.path_mgr.normalize_path(path)
        if not self.path_mgr.is_path_allowed(norm_path):
            yield event.plain_result(f"路径不在白名单或不允许: {norm_path}")
            return
        try:
            await self.client.project_current(norm_path)
            await self.client.path_info(norm_path)
        except httpx.HTTPError as e:
            yield event.plain_result(f"OpenCode 无法识别该路径: {e}")
            return
        umo = self._get_umo(event)
        self.state_mgr.set_window_state(umo, directory=norm_path)
        await self.state_mgr.persist_window_state(umo)
        self.path_mgr.add_recent_path(norm_path)
        state = self.state_mgr.get_window_state(umo)
        yield event.plain_result(
            f"已切换到: {norm_path}\n\n" + format_window_state(state)
        )

    async def cmd_dirs(self, event: AstrMessageEvent):
        dirs = self.path_mgr.get_allowed_dirs_with_recent()
        if not dirs:
            yield event.plain_result("未配置允许的工作路径。请在配置中添加 allowed_workdirs。")
            return
        lines = ["可用工作路径:"]
        current = self._get_directory(event)
        for i, d in enumerate(dirs, 1):
            mark = " <--" if d == current else ""
            lines.append(f"  {i}. {d}{mark}")
        yield event.plain_result("\n".join(lines))

    # ──── 会话命令 ────

    async def cmd_list(self, event: AstrMessageEvent, scope: str = ""):
        if scope == "all":
            sessions = await self._list_all_sessions()
        else:
            directory = self._get_directory(event)
            if not directory:
                yield event.plain_result("未设置工作路径")
                return
            try:
                sessions = await self.client.session_list(directory)
            except httpx.HTTPError as e:
                yield event.plain_result(f"获取会话列表失败: {e}")
                return
        current_sid = self.state_mgr.get_current_session(self._get_umo(event))
        raw_count = len(sessions)
        local_archived = set()
        for state in self.state_mgr._window_states.values():
            local_archived.update(state.get("archived_sessions", []) or [])
        sessions = [
            s for s in sessions
            if not s.get("time", {}).get("archived") and s.get("id") not in local_archived
        ]
        archived_count = raw_count - len(sessions)
        child_count = sum(1 for s in sessions if s.get("parentID"))
        sessions = [s for s in sessions if not s.get("parentID")]
        if scope == "all":
            from collections import Counter
            dir_counts = Counter(s.get("directory", "?") for s in sessions)
            header = [f"共 {raw_count} 个会话（已归档 {archived_count}，子会话 {child_count}），"
                     f"显示 {len(sessions)} 个，分布在 {len(dir_counts)} 个目录:"]
            for d, c in dir_counts.most_common():
                header.append(f"  {c:3d}  {d}")
            header.append("")
            header.append(format_session_list(sessions, current_sid))
            yield event.plain_result("\n".join(header))
        else:
            if archived_count or child_count:
                parts = []
                if archived_count:
                    parts.append(f"已过滤 {archived_count} 个已归档会话")
                if child_count:
                    parts.append(f"已过滤 {child_count} 个子会话")
                yield event.plain_result(
                    "，".join(parts) + "\n\n"
                    + format_session_list(sessions, current_sid)
                )
            else:
                yield event.plain_result(format_session_list(sessions, current_sid))

    async def _list_all_sessions(self) -> list:
        seen = {}
        try:
            for s in await self.client.session_list(None):
                sid = s.get("id")
                if sid and sid not in seen:
                    seen[sid] = s
        except httpx.HTTPError:
            pass

        dirs = set()
        default = self.path_mgr.normalize_path(self.path_mgr.default_workdir)
        if default:
            dirs.add(default)
        for ws in self.state_mgr._window_states.values():
            d = ws.get("directory")
            if d:
                dirs.add(d)
        for d in self.path_mgr.allowed_workdirs:
            norm = self.path_mgr.normalize_path(d)
            if norm:
                dirs.add(norm)
        for d in self.path_mgr.get_recent_paths():
            dirs.add(d)
        for s in seen.values():
            d = s.get("directory")
            if d:
                dirs.add(d)

        local_sessions = self._local_opencode_sessions()
        for s in local_sessions:
            d = s.get("directory")
            if d:
                dirs.add(d)

        for d in dirs:
            try:
                for s in await self.client.session_list(d):
                    sid = s.get("id")
                    if sid and sid not in seen:
                        seen[sid] = s
            except httpx.HTTPError:
                continue

        for s in local_sessions:
            sid = s.get("id")
            if sid and sid not in seen:
                seen[sid] = s

        return list(seen.values())

    def _local_opencode_sessions(self) -> list[dict]:
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
                    SELECT id, directory, title, parent_id,
                           time_created, time_updated, time_archived
                    FROM session
                    ORDER BY time_updated DESC
                    """
                ).fetchall()
            finally:
                conn.close()
        except Exception as e:
            logger.debug("读取本地 OpenCode 会话库失败: %s", e)
            return []

        sessions = []
        for row in rows:
            time_info = {
                "created": row["time_created"],
                "updated": row["time_updated"],
            }
            if row["time_archived"]:
                time_info["archived"] = row["time_archived"]
            sessions.append(
                {
                    "id": row["id"],
                    "directory": row["directory"],
                    "title": row["title"] or "无标题",
                    "parentID": row["parent_id"],
                    "time": time_info,
                }
            )
        return sessions

    async def cmd_new(self, event: AstrMessageEvent, title: str = ""):
        directory = self._get_directory(event)
        if not directory:
            yield event.plain_result("未设置工作路径")
            return
        try:
            result = await self.client.session_create(title=title or None, directory=directory)
            sid = result.get("id", "")
            umo = self._get_umo(event)
            self.state_mgr.set_window_state(umo, current_session=sid, session_title=title or "未命名")
            await self.state_mgr.persist_window_state(umo)
            self.state_mgr.set_session_owner(sid, umo)
            await self.state_mgr.persist_session_owners()
            yield event.plain_result(
                f"已创建会话: {title or '未命名'}\nID: {sid}"
            )
        except httpx.HTTPError as e:
            yield event.plain_result(f"创建会话失败: {e}")

    async def cmd_switch(self, event: AstrMessageEvent, target: str = ""):
        if not target:
            yield event.plain_result("用法: /oc switch <序号|ID前缀>")
            return
        directory = self._get_directory(event)
        if not directory:
            yield event.plain_result("未设置工作路径")
            return

        async def _find(sessions):
            if target.isdigit():
                idx = int(target)
                if 1 <= idx <= len(sessions):
                    return sessions[idx - 1]
            matches = [s for s in sessions if s.get("id", "").startswith(target)]
            if len(matches) == 1:
                return matches[0]
            if len(matches) > 1:
                return matches
            return None

        try:
            sessions = await self.client.session_list(directory)
        except httpx.HTTPError as e:
            yield event.plain_result(f"获取会话列表失败: {e}")
            return

        chosen = await _find(sessions)
        if isinstance(chosen, list):
            ids = [s["id"][:12] for s in chosen]
            yield event.plain_result(f"匹配到 {len(chosen)} 个: {', '.join(ids)}")
            return
        if chosen is None:
            # Try global session list (no directory filter)
            try:
                all_sessions = await self.client.session_list(None)
                chosen = await _find(all_sessions)
            except httpx.HTTPError:
                pass
        if chosen is None and target.startswith("ses_"):
            # Try direct session_get without directory
            try:
                sess = await self.client.session_get(target, directory)
                if sess and sess.get("id"):
                    chosen = sess
            except Exception:
                pass
            if chosen is None:
                try:
                    sess = await self.client.session_get(target, None)
                    if sess and sess.get("id"):
                        chosen = sess
                except Exception:
                    pass
        if chosen is None:
            # Fallback: search local SQLite DB by ID prefix
            db_path = os.path.expanduser("~/.local/share/opencode/opencode.db")
            if os.path.exists(db_path):
                try:
                    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=1)
                    conn.row_factory = sqlite3.Row
                    try:
                        rows = conn.execute(
                            "SELECT id, title, directory FROM session WHERE id LIKE ? ORDER BY time_updated DESC LIMIT 5",
                            (f"{target}%",)
                        ).fetchall()
                    finally:
                        conn.close()
                    if len(rows) == 1:
                        row = rows[0]
                        chosen = {"id": row["id"], "title": row["title"] or "", "directory": row["directory"] or ""}
                    elif len(rows) > 1:
                        ids = [r["id"][:12] for r in rows]
                        yield event.plain_result(f"本地匹配到 {len(rows)} 个: {', '.join(ids)}")
                        return
                except Exception as e:
                    logger.debug("SQLite fallback failed: %s", e)
        if isinstance(chosen, list):
            ids = [s["id"][:12] for s in chosen]
            yield event.plain_result(f"全局匹配到 {len(chosen)} 个: {', '.join(ids)}")
            return
        if chosen is None:
            yield event.plain_result(f"未找到: {target}")
            return

        sid = chosen["id"]
        umo = self._get_umo(event)
        sess_dir = chosen.get("directory", "")
        sess_title = chosen.get("title", "") or "无标题"
        if sess_dir:
            self.state_mgr.set_window_state(umo, current_session=sid, directory=sess_dir, session_title=sess_title)
        else:
            self.state_mgr.set_window_state(umo, current_session=sid, session_title=sess_title)
        await self.state_mgr.persist_window_state(umo)
        self.state_mgr.set_session_owner(sid, umo)
        await self.state_mgr.persist_session_owners()
        yield event.plain_result(f"已切换到: {sess_title} ({sid[:12]})")

    async def cmd_rename(self, event: AstrMessageEvent, target: str = ""):
        directory = self._get_directory(event)
        if not directory:
            yield event.plain_result("未设置工作路径")
            return
        sid = self.state_mgr.get_current_session(self._get_umo(event))
        if target and not target.isdigit() and not target.startswith("ses_"):
            new_name = target
        elif not sid:
            yield event.plain_result("未绑定会话，请先 /oc switch")
            return

        if not target or (target.isdigit() or target.startswith("ses_")):
            yield event.plain_result(f"请输入 [{sid[:12]}] 的新名称 (输入 q 退出):")

            cancelled = False

            @session_waiter(timeout=60, record_history_chains=False)
            async def name_waiter(c: SessionController, e: AstrMessageEvent):
                nonlocal cancelled
                name = e.message_str.strip()
                if not name:
                    c.keep(timeout=60, reset_timeout=True)
                    return
                if name == "q":
                    cancelled = True
                    c.stop()
                    return
                try:
                    await self.client.session_update(sid, name, directory)
                    await e.send(e.plain_result(f"已重命名: {name}"))
                except httpx.HTTPError as err:
                    await e.send(e.plain_result(f"重命名失败: {err}"))
                c.stop()

            try:
                await name_waiter(event)
            except TimeoutError:
                yield event.plain_result("超时取消")
                return
            if cancelled:
                yield event.plain_result("已取消")
            return

        try:
            await self.client.session_update(sid, new_name, directory)
            yield event.plain_result(f"已重命名: {new_name}")
        except httpx.HTTPError as e:
            yield event.plain_result(f"重命名失败: {e}")

    async def cmd_delete(self, event: AstrMessageEvent, target: str = ""):
        directory = self._get_directory(event)
        if not directory:
            yield event.plain_result("未设置工作路径")
            return
        sid = self.state_mgr.get_current_session(self._get_umo(event))
        if target:
            try:
                sessions = await self.client.session_list(directory)
                sessions = [s for s in sessions if not s.get("time", {}).get("archived")]
                sessions = [s for s in sessions if not s.get("parentID")]
                if target.isdigit() and 1 <= int(target) <= len(sessions):
                    sid = sessions[int(target) - 1]["id"]
                else:
                    matches = [s for s in sessions if s.get("id", "").startswith(target)]
                    if not matches:
                        matches = [
                            s for s in await self._list_all_sessions()
                            if s.get("id", "").startswith(target)
                        ]
                    if len(matches) == 1:
                        sid = matches[0]["id"]
                    elif len(matches) > 1:
                        ids = [s.get("id", "")[:12] for s in matches]
                        yield event.plain_result(f"匹配到 {len(matches)} 个: {', '.join(ids)}")
                        return
                    else:
                        yield event.plain_result(f"未找到会话: {target}")
                        return
            except httpx.HTTPError as e:
                yield event.plain_result(f"获取会话列表失败: {e}")
                return
        if not sid:
            yield event.plain_result("未绑定会话")
            return

        cfg = self.config.get("security_config", {})
        if cfg.get("confirm_delete", True):
            timeout = cfg.get("confirm_timeout", 30)
            self._delete_confirmations[self._get_umo(event)] = {
                "sid": sid,
                "sender_id": self._get_sender(event),
                "expires_at": time.monotonic() + timeout,
            }
            yield event.plain_result(f"即将删除会话 [{sid[:12]}]\n输入 确认 继续 (其他任意内容取消):")
            return

        try:
            await self.client.session_delete(sid, directory)
            umo = self._get_umo(event)
            self.state_mgr.set_window_state(umo, current_session=None)
            await self.state_mgr.persist_window_state(umo)
            self.state_mgr.remove_session_owner(sid)
            await self.state_mgr.persist_session_owners()
            yield event.plain_result(f"已删除会话 [{sid[:12]}]")
        except httpx.HTTPError as e:
            yield event.plain_result(f"删除失败: {e}")

    async def cmd_archive(self, event: AstrMessageEvent, target: str = ""):
        sid = await self._get_session_for_cmd(event, target)
        if not sid:
            yield event.plain_result("未绑定会话")
            return
        directory = self._get_directory(event)
        archived = self.state_mgr.get_window_state(self._get_umo(event)).get(
            "archived_sessions", []
        )
        if sid in archived:
            yield event.plain_result("该会话已归档")
            return
        archived.append(sid)
        self.state_mgr.set_window_state(
            self._get_umo(event), archived_sessions=archived
        )
        await self.state_mgr.persist_window_state(self._get_umo(event))
        yield event.plain_result(f"已归档会话 [{sid[:12]}]")

    async def cmd_unarchive(self, event: AstrMessageEvent, target: str = ""):
        sid = await self._get_session_for_cmd(event, target)
        if not sid:
            yield event.plain_result("未绑定会话")
            return
        umo = self._get_umo(event)
        archived = self.state_mgr.get_window_state(umo).get("archived_sessions", [])
        if sid not in archived:
            yield event.plain_result("该会话未归档")
            return
        archived.remove(sid)
        self.state_mgr.set_window_state(umo, archived_sessions=archived)
        await self.state_mgr.persist_window_state(umo)
        yield event.plain_result(f"已取消归档 [{sid[:12]}]")

    async def cmd_share(self, event: AstrMessageEvent):
        sid = self.state_mgr.get_current_session(self._get_umo(event))
        directory = self._get_directory(event)
        if not sid or not directory:
            yield event.plain_result("未绑定会话")
            return
        try:
            result = await self.client.session_share(sid, directory)
            url = result.get("share", {}).get("url", "N/A")
            yield event.plain_result(f"已分享: {url}")
        except httpx.HTTPError as e:
            yield event.plain_result(f"分享失败: {e}")

    async def cmd_unshare(self, event: AstrMessageEvent):
        sid = self.state_mgr.get_current_session(self._get_umo(event))
        directory = self._get_directory(event)
        if not sid or not directory:
            yield event.plain_result("未绑定会话")
            return
        try:
            await self.client.session_unshare(sid, directory)
            yield event.plain_result("已取消分享")
        except httpx.HTTPError as e:
            yield event.plain_result(f"取消分享失败: {e}")

    async def cmd_summary(self, event: AstrMessageEvent, model: str = ""):
        sid = await self._ensure_session(event)
        if not sid:
            yield event.plain_result("未绑定会话")
            return
        umo = self._get_umo(event)
        model_str = model or self.state_mgr.get_current_model(umo) or self.model_mgr.default_model
        if not model_str:
            yield event.plain_result("请指定模型: /oc summary <provider/model>")
            return
        if "/" in model_str:
            provider_id, model_id = model_str.split("/", 1)
        else:
            provider_id = "openai"
            model_id = model_str
        directory = self._get_directory(event)
        try:
            result = await self.client.session_summarize(sid, provider_id, model_id, directory)
            text = extract_text_from_parts(result.get("parts", []))
            yield event.plain_result(text or "摘要已生成")
        except httpx.HTTPError as e:
            yield event.plain_result(f"生成摘要失败: {e}")

    async def cmd_messages(self, event: AstrMessageEvent, rounds: str = ""):
        sid = self.state_mgr.get_current_session(self._get_umo(event))
        directory = self._get_directory(event)
        if not sid or not directory:
            yield event.plain_result("未绑定会话")
            return
        rounds_int = max(int(rounds), 1) if rounds.isdigit() else 1
        limit = max(rounds_int * 2, 20)
        try:
            msgs = await self.client.session_messages(sid, directory, limit=limit)
            yield event.plain_result(format_messages(msgs, rounds=rounds_int))
        except httpx.HTTPError as e:
            yield event.plain_result(f"获取消息失败: {e}")

    # ──── 文件命令 ────

    async def cmd_read(self, event: AstrMessageEvent, file_path: str = ""):
        if not file_path:
            yield event.plain_result("用法: /oc read <路径>")
            return
        directory = self._get_directory(event)
        if not directory:
            yield event.plain_result("未设置工作路径")
            return
        try:
            result = await self.client.file_content(file_path, directory)
            content = result.get("content", "")
            if content:
                if len(content) > 4000:
                    content = content[:3997] + "..."
                yield event.plain_result(f"`{file_path}`:\n\n```\n{content}\n```")
            else:
                yield event.plain_result(f"读取 `{file_path}` 内容为空")
        except httpx.HTTPError as e:
            yield event.plain_result(f"读取失败: {e}")

    async def cmd_write(self, event: AstrMessageEvent, args: str = ""):
        parts = args.split(None, 1)
        if len(parts) < 2:
            yield event.plain_result("用法: /oc write <路径> <内容>")
            return
        file_path = parts[0]
        content = parts[1]
        directory = self._get_directory(event)
        if not directory:
            yield event.plain_result("未设置工作路径")
            return
        try:
            await self.client.file_write(file_path, content, directory)
            yield event.plain_result(f"已写入 `{file_path}` ({len(content)} 字节)")
        except httpx.HTTPError as e:
            yield event.plain_result(f"写入失败: {e}")

    async def cmd_files(self, event: AstrMessageEvent, path: str = ""):
        directory = self._get_directory(event)
        if not directory:
            yield event.plain_result("未设置工作路径")
            return
        try:
            files = await self.client.file_list(path or None, directory)
            if not files:
                yield event.plain_result("目录为空")
                return
            lines = [f"`{path or directory}` 下的文件:"]
            for f in files[:30]:
                name = f.get("name", "?")
                ftype = f.get("type", "?")
                size = f.get("size", "")
                meta = f" ({size} 字节)" if size else ""
                lines.append(f"  {'D' if ftype == 'directory' else 'F'} {name}{meta}")
            yield event.plain_result("\n".join(lines))
        except httpx.HTTPError as e:
            yield event.plain_result(f"列出文件失败: {e}")

    # ──── 消息命令 ────

    async def cmd_ask(self, event: AstrMessageEvent, text: str = ""):
        if not text:
            yield event.plain_result("用法: /oc ask <任务内容>")
            return
        sid = await self._ensure_session(event)
        if not sid:
            yield event.plain_result("未绑定会话，请先 /oc switch")
            return
        directory = self._get_directory(event)
        umo = self._get_umo(event)
        local_model = self.state_mgr.get_current_model(umo)
        local_variant = self.state_mgr.get_current_variant(umo)
        model_body = self.model_mgr.build_model_body(local_model) if local_model else None
        variant = local_variant or None
        agent = self.state_mgr.get_current_agent(umo) or None

        if self._is_destructive(text):
            cfg = self.config.get("security_config", {})
            async for result in self._confirm_action(
                event,
                f"敏感操作: {text[:100]}\n回复 确认 继续",
                cfg.get("confirm_timeout", 30),
            ):
                if isinstance(result, bool):
                    if not result:
                        return
                else:
                    yield result

        result = await session_ops.send_message(
            self.client, sid, text, directory, model_body, agent, variant
        )
        yield event.plain_result(
            format_response_with_meta(result, self.state_mgr.get_window_state(umo))
        )

    async def cmd_work(self, event: AstrMessageEvent, text: str = ""):
        if not text:
            yield event.plain_result("用法: /oc work <任务描述>")
            return
        prompt = (
            f"你是 OpenCode，负责在当前仓库完成代码任务。\n\n"
            f"用户请求：{text}\n\n"
            f"执行要求：\n"
            f"1. 先理解问题和相关代码。\n"
            f"2. 只做必要修改，避免过度改动。\n"
            f"3. 修改后运行最小必要验证。\n"
            f"4. 不要推送远程分支，不要提交 git，除非用户明确要求。\n"
            f"5. 最后用中文总结：根因、修改内容、验证结果、后续建议。\n"
        )
        result = await self.plugin.send_task_to_opencode(prompt, self._get_umo(event))
        yield event.plain_result(result)

    async def cmd_to(self, event: AstrMessageEvent, args: str = ""):
        parts = args.split(None, 1)
        if len(parts) < 2 or not parts[0].isdigit():
            yield event.plain_result("用法: /oc to <序号> <内容>")
            return
        idx = int(parts[0])
        text = parts[1]
        directory = self._get_directory(event)
        try:
            sessions = await self.client.session_list(directory)
        except httpx.HTTPError as e:
            yield event.plain_result(f"获取会话列表失败: {e}")
            return
        if idx < 1 or idx > len(sessions):
            yield event.plain_result(f"无效序号，共 {len(sessions)} 个会话")
            return
        sid = sessions[idx - 1]["id"]
        umo = self._get_umo(event)
        local_model = self.state_mgr.get_current_model(umo)
        local_variant = self.state_mgr.get_current_variant(umo)
        model_body = self.model_mgr.build_model_body(local_model) if local_model else None
        variant = local_variant or None
        agent = self.state_mgr.get_current_agent(umo) or None
        result = await session_ops.send_message(
            self.client, sid, text, directory, model_body, agent, variant
        )
        yield event.plain_result(
            format_response_with_meta(result, self.state_mgr.get_window_state(umo))
        )

    async def cmd_stop(self, event: AstrMessageEvent):
        sid = self.state_mgr.get_current_session(self._get_umo(event))
        directory = self._get_directory(event)
        if not sid or not directory:
            yield event.plain_result("未绑定会话")
            return
        result = await session_ops.abort_session(self.client, sid, directory)
        yield event.plain_result(result)

    # ──── 指令命令 ────

    async def cmd_commands(self, event: AstrMessageEvent):
        try:
            cmds = await self.client.commands()
            if not cmds:
                yield event.plain_result("无可用命令")
                return
            lines = ["OpenCode 内置命令:"]
            for cmd in cmds[:30]:
                name = cmd.get("name", "N/A")
                desc = cmd.get("description", "")[:50]
                lines.append(f"  /{name} - {desc}")
            yield event.plain_result("\n".join(lines))
        except httpx.HTTPError as e:
            yield event.plain_result(f"获取命令列表失败: {e}")

    async def cmd_cmd(self, event: AstrMessageEvent, args: str = ""):
        if not args:
            yield event.plain_result("用法: /oc cmd <命令> [参数]")
            return
        cmd_parts = args.split(None, 1)
        cmd_name = cmd_parts[0].lstrip("/")
        cmd_args = cmd_parts[1] if len(cmd_parts) > 1 else ""
        sid = await self._ensure_session(event)
        if not sid:
            yield event.plain_result("未绑定会话")
            return
        directory = self._get_directory(event)
        try:
            result = await self.client.session_command(
                sid, cmd_name, cmd_args, directory=directory
            )
            parts_data = result.get("parts", [])
            text = extract_text_from_parts(parts_data)
            yield event.plain_result(text or f"命令 /{cmd_name} 已执行")
        except httpx.HTTPError as e:
            yield event.plain_result(f"执行命令失败: {e}")

    async def cmd_shell(self, event: AstrMessageEvent, cmd: str = ""):
        if not cmd:
            yield event.plain_result("用法: /oc shell <命令>")
            return
        cfg = self.config.get("security_config", {})
        if cfg.get("confirm_shell", True):
            async for result in self._confirm_action(
                event,
                f"即将执行 Shell: {cmd}\n回复 确认 继续",
                cfg.get("confirm_timeout", 30),
            ):
                if isinstance(result, bool):
                    if not result:
                        return
                else:
                    yield result
        sid = await self._ensure_session(event)
        if not sid:
            yield event.plain_result("未绑定会话")
            return
        directory = self._get_directory(event)
        try:
            result = await self.client.session_shell(sid, cmd, directory)
            parts_data = result.get("parts", [])
            text = extract_text_from_parts(parts_data)
            yield event.plain_result(text or f"Shell 已执行: {cmd}")
        except httpx.HTTPError as e:
            yield event.plain_result(f"执行 Shell 失败: {e}")

    # ──── 模型命令 ────

    async def cmd_models(self, event: AstrMessageEvent, provider: str = ""):
        try:
            data = await self.client.providers()
            providers_list = data.get("all", []) or data.get("connected", [])
            try:
                config_data = await self.client.config_get()
                config_providers = config_data.get("provider", {})
            except Exception:
                config_providers = {}

            opencode_builtins = [
                {"id": "opencode", "name": "OpenCode (Built-in)",
                 "models": {
                     "openai/gpt-5.4-mini": {"name": "GPT-5.4 Mini"},
                     "deepseek/deepseek-v4-flash": {"name": "DeepSeek V4 Flash"},
                     "anthropic/claude-sonnet-5": {"name": "Claude Sonnet 5"},
                 }}
            ]
            merged = providers_list + opencode_builtins

            if provider:
                provider_lower = provider.lower()
                for p in merged:
                    pid = p.get("id", "")
                    pname = p.get("name", "")
                    if pid.lower() == provider_lower or pname.lower() == provider_lower:
                        models = p.get("models", {})
                        cfg_models = config_providers.get(pid, {}).get("models", {})
                        if cfg_models and pid != "opencode":
                            models = {k: v for k, v in models.items() if k in cfg_models}
                        lines = [f"Provider: {pname or pid}"]
                        chat_models = {k: m for k, m in models.items()
                                       if not any(k.startswith(p) for p in
                                                  ("text-embedding-", "gpt-image-", "chatgpt-image-"))}
                        for k, m in chat_models.items():
                            lines.append(f"  {pid}/{k} - {m.get('name', k)}")
                        if cfg_models and pid != "opencode":
                            lines.append(f"  ... 实际配置 {len(cfg_models)} 个，显示 {len(chat_models)} 个对话模型")
                        elif len(chat_models) < len(models):
                            lines.append(f"  ... 已过滤 {len(models) - len(chat_models)} 个非对话模型")
                        yield event.plain_result("\n".join(lines))
                        return
                yield event.plain_result(
                    f"未找到 provider: {provider}\n请使用 id 或名称搜索，如 /oc models deepseek"
                )
                return

            connected = data.get("connected", [])
            if connected:
                lines = []
                for conn_id in connected:
                    name = conn_id
                    for p in providers_list:
                        if p.get("id") == conn_id:
                            name = p.get("name", conn_id)
                            break
                    lines.append(f"  {conn_id} - {name}")
                lines.append("")
                lines.append("  opencode - OpenCode (Built-in)")
                yield event.plain_result("\n".join(lines))
            else:
                yield event.plain_result(
                    "暂无已连接的 provider\n内置模型: opencode (OpenCode Built-in)\n请使用 /oc models opencode 查看"
                )
        except httpx.HTTPError as e:
            yield event.plain_result(f"获取模型列表失败: {e}")

    async def cmd_model(self, event: AstrMessageEvent, model_str: str = ""):
        if not model_str:
            current = self.state_mgr.get_current_model(self._get_umo(event))
            yield event.plain_result(f"当前模型: {current or '未设置'}")
            return
        provider_id, model_id = self.model_mgr.parse_model(model_str)

        try:
            data = await self.client.providers()
            all_providers = data.get("all", []) or data.get("connected", [])
            found = False
            for p in all_providers:
                pid = p.get("id", "")
                if provider_id and pid == provider_id:
                    if model_id in p.get("models", {}):
                        found = True
                        break
                elif not provider_id:
                    if model_id in p.get("models", {}):
                        provider_id = pid
                        found = True
                        break
            if not found:
                yield event.plain_result(
                    f"未找到模型: {model_str}\n请使用 /oc models 查看可用模型"
                )
                return
        except Exception:
            pass

        umo = self._get_umo(event)
        self.state_mgr.set_window_state(umo, model=model_str)
        await self.state_mgr.persist_window_state(umo)
        yield event.plain_result(f"已设置模型: {model_str}")

    async def cmd_variant(self, event: AstrMessageEvent, variant: str = ""):
        if not variant:
            current = self.state_mgr.get_current_variant(self._get_umo(event))
            yield event.plain_result(f"当前思考等级: {current or '未设置'}")
            return
        if not self.model_mgr.validate_variant(variant):
            from .constants import MODEL_VARIANTS
            yield event.plain_result(
                f"无效的思考等级: {variant}\n"
                f"可用: {', '.join(v for v in MODEL_VARIANTS if v)}"
            )
            return
        umo = self._get_umo(event)
        self.state_mgr.set_window_state(umo, variant=variant)
        await self.state_mgr.persist_window_state(umo)
        yield event.plain_result(f"已设置思考等级: {variant}")

    async def cmd_agent(self, event: AstrMessageEvent, agent: str = ""):
        umo = self._get_umo(event)
        if not agent:
            current = self.state_mgr.get_current_agent(umo)
            yield event.plain_result(f"当前 Agent: {current or '未设置 (默认 build)'}")
            return
        agent = agent.lower()
        if agent not in ("build", "plan"):
            yield event.plain_result("无效 agent，可用: build, plan")
            return
        self.state_mgr.set_window_state(umo, agent=agent)
        await self.state_mgr.persist_window_state(umo)
        yield event.plain_result(f"已设置 Agent: {agent}")

    # ──── 通知命令 ────

    async def cmd_bind(self, event: AstrMessageEvent, arg: str = ""):
        umo = self._get_umo(event)
        sender_id = self._get_sender(event)
        if arg == "status":
            state = self.state_mgr.get_user_state(sender_id)
            primary = state.get("primary_umo", "未设置")
            window = self.state_mgr.get_window_state(umo)
            lines = [
                f"默认通知窗口: {primary}",
                f"当前窗口会话: {window.get('current_session', '未绑定') or '未绑定'}",
                f"当前窗口目录: {window.get('directory', '未设置') or '未设置'}",
            ]
            yield event.plain_result("\n".join(lines))
        elif arg == "reset":
            self.state_mgr.set_user_state(sender_id, primary_umo=None)
            await self.state_mgr.persist_user_state(sender_id)
            yield event.plain_result("已清除默认通知窗口")
        else:
            await self.state_mgr.register_user(sender_id)
            self.state_mgr.set_user_state(sender_id, primary_umo=umo)
            await self.state_mgr.persist_user_state(sender_id)
            yield event.plain_result(f"已设置当前窗口为默认通知窗口")

    async def cmd_output(self, event: AstrMessageEvent, level: str = ""):
        from .constants import OUTPUT_LEVELS, OUTPUT_LEVEL_DESC
        cfg = self.config.get("notification_config", {})
        current = cfg.get("output_level", "simple")
        if not level:
            lines = [f"当前推送级别: {current}"]
            for lvl in OUTPUT_LEVELS:
                mark = " <--" if lvl == current else ""
                lines.append(f"  {lvl}{mark} - {OUTPUT_LEVEL_DESC.get(lvl, '')}")
            yield event.plain_result("\n".join(lines))
            return
        if level not in OUTPUT_LEVELS:
            yield event.plain_result(f"无效级别: {level}\n可用: {', '.join(OUTPUT_LEVELS)}")
            return
        self.config["notification_config"]["output_level"] = level
        self.config.save_config()
        self.plugin.sse_listener.output_level = level
        yield event.plain_result(f"推送级别已切换为: {level}")

    # ──── 审批命令 ────

    async def cmd_pending(self, event: AstrMessageEvent):
        items = self.pending_mgr.get_all_visible()
        if not items:
            yield event.plain_result("没有待审批的请求")
            return
        lines = ["待审批列表:"]
        for item in items:
            lines.append(
                f"  [{item['index']}] {item['action']}: {item['detail'][:80]}"
            )
        lines.append(f"\n共 {len(items)} 项")
        yield event.plain_result("\n".join(lines))

    async def cmd_allow(self, event: AstrMessageEvent, index_str: str = ""):
        if not index_str or not index_str.isdigit():
            yield event.plain_result("用法: /oc allow <序号>")
            return
        idx = int(index_str)
        item = self.pending_mgr.get(idx)
        if item and item.get("type") == "opencode_permission":
            try:
                logger.info("权限响应: sid=%s pid=%s dir=%s",
                            item.get("session_id", "")[:12],
                            item.get("permission_id", ""),
                            item.get("directory", ""))
                await self.client.permission_respond(
                    item.get("session_id", ""),
                    item.get("permission_id", ""),
                    "allow",
                    directory=item.get("directory", "") or None,
                )
                self.pending_mgr.remove(idx)
                yield event.plain_result(f"已批准 [{idx}]")
            except httpx.HTTPError as e:
                yield event.plain_result(f"批准失败 [{idx}]: {e}")
            return
        if self.pending_mgr.approve(idx):
            yield event.plain_result(f"已批准 [{idx}]")
        else:
            yield event.plain_result(f"未找到请求 [{idx}]")

    async def cmd_deny(self, event: AstrMessageEvent, index_str: str = ""):
        if not index_str or not index_str.isdigit():
            yield event.plain_result("用法: /oc deny <序号>")
            return
        idx = int(index_str)
        item = self.pending_mgr.get(idx)
        if item and item.get("type") == "opencode_permission":
            try:
                await self.client.permission_respond(
                    item.get("session_id", ""),
                    item.get("permission_id", ""),
                    "deny",
                    directory=item.get("directory", "") or None,
                )
                self.pending_mgr.remove(idx)
                yield event.plain_result(f"已拒绝 [{idx}]")
            except httpx.HTTPError as e:
                yield event.plain_result(f"拒绝失败 [{idx}]: {e}")
            return
        if self.pending_mgr.deny(idx):
            yield event.plain_result(f"已拒绝 [{idx}]")
        else:
            yield event.plain_result(f"未找到请求 [{idx}]")

    async def cmd_approve(self, event: AstrMessageEvent):
        count = self.pending_mgr.approve_all()
        yield event.plain_result(f"已批准全部 {count} 项请求")

    async def cmd_diff(self, event: AstrMessageEvent, target: str = ""):
        """查看会话变更 diff"""
        sid = await self._get_session_for_cmd(event, target)
        if not sid:
            yield event.plain_result("未绑定会话，请先 /oc switch")
            return
        directory = self._get_directory(event)
        try:
            diff = await self.client.session_git_diff(sid, directory)
            if not diff or not diff.strip():
                yield event.plain_result("当前会话没有变更")
                return
            # Truncate if too long
            if len(diff) > 3000:
                diff = diff[:3000] + "\n... (内容过长，已截断)"
            yield event.plain_result(f"会话变更 diff ({sid[:12]}):\n```diff\n{diff}\n```")
        except httpx.HTTPError as e:
            yield event.plain_result(f"获取 diff 失败: {e}")

    async def cmd_commit(self, event: AstrMessageEvent, message: str = ""):
        """封装 git add + git commit"""
        if not message:
            yield event.plain_result("用法: /oc commit <提交信息>")
            return
        directory = self._get_directory(event)
        if not directory:
            yield event.plain_result("未绑定工作目录")
            return
        # Run git commands in the workdir
        try:
            proc = await asyncio.create_subprocess_exec(
                "git", "-C", directory, "add", "-A",
                stdout=subprocess.PIPE, stderr=subprocess.PIPE
            )
            _, err = await proc.communicate()
            if proc.returncode != 0:
                yield event.plain_result(f"git add 失败: {err.decode()[:200]}")
                return

            proc2 = await asyncio.create_subprocess_exec(
                "git", "-C", directory, "commit", "-m", message,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE
            )
            out, err2 = await proc2.communicate()
            output = (out + err2).decode().strip()
            if proc2.returncode != 0:
                yield event.plain_result(f"git commit 失败: {output[:500]}")
                return
            yield event.plain_result(f"已提交:\n{output[:500]}")
        except Exception as e:
            yield event.plain_result(f"提交失败: {e}")

    async def cmd_project(self, event: AstrMessageEvent, args: str = ""):
        """项目别名管理：add/remove/list/use"""
        parts = args.split(None, 1)
        if not parts:
            yield event.plain_result("用法: /oc project add|remove|list|use <别名> [路径]")
            return

        sub = parts[0].lower()
        aliases = self.config.get("project_aliases", {})

        if sub == "list":
            if not aliases:
                yield event.plain_result("没有配置项目别名")
                return
            lines = ["项目别名:"]
            for alias, path in aliases.items():
                lines.append(f"  {alias} -> {path}")
            yield event.plain_result("\n".join(lines))
            return

        if sub == "add":
            rest = parts[1] if len(parts) > 1 else ""
            aparts = rest.split(None, 1)
            if len(aparts) < 2:
                yield event.plain_result("用法: /oc project add <别名> <路径>")
                return
            alias, path = aparts[0], aparts[1]
            from astrbot.core.star.config import update_config
            aliases[alias] = path
            update_config("astrbot_plugin_opencode_remote", "project_aliases", aliases)
            yield event.plain_result(f"已添加别名: {alias} -> {path}")
            return

        if sub == "remove":
            rest = parts[1] if len(parts) > 1 else ""
            if not rest:
                yield event.plain_result("用法: /oc project remove <别名>")
                return
            alias = rest.strip()
            if alias not in aliases:
                yield event.plain_result(f"别名不存在: {alias}")
                return
            from astrbot.core.star.config import update_config
            del aliases[alias]
            update_config("astrbot_plugin_opencode_remote", "project_aliases", aliases)
            yield event.plain_result(f"已删除别名: {alias}")
            return

        if sub == "use":
            rest = parts[1] if len(parts) > 1 else ""
            if not rest:
                yield event.plain_result("用法: /oc project use <别名>")
                return
            alias = rest.strip()
            if alias not in aliases:
                yield event.plain_result(f"别名不存在: {alias}")
                return
            path = aliases[alias]
            if not os.path.isdir(path):
                yield event.plain_result(f"路径不存在: {path}")
                return
            if self.path_mgr.check_path_safety and not self.path_mgr.is_path_allowed(path):
                yield event.plain_result(f"路径不在白名单中: {path}")
                return
            umo = self._get_umo(event)
            self.state_mgr.set_window_state(umo, directory=path)
            await self.state_mgr.persist_window_state(umo)
            yield event.plain_result(f"已切换工作路径: {path} (别名: {alias})")
            return

        yield event.plain_result("未知子命令。用法: /oc project add|remove|list|use")

    async def cmd_queue(self, event: AstrMessageEvent, args: str = ""):
        """任务队列管理：查看、取消、清空"""
        directory = self._get_directory(event)
        task_queue = getattr(self.plugin, "task_queue", None)
        if not task_queue:
            yield event.plain_result("任务队列未启用")
            return

        parts = args.split(None, 1)
        sub = parts[0].lower() if parts else ""

        if sub in ("", "list"):
            active = task_queue.get_active(directory)
            queue = await task_queue.get_queue(directory)
            lines = []
            if active:
                lines.append(f"当前执行任务: {active['text'][:60]}...")
            else:
                lines.append("当前没有执行中的任务")
            if queue:
                lines.append(f"队列任务 ({len(queue)}):")
                for task in queue:
                    lines.append(f"  [{task['id']}] {task['text'][:60]}...")
            else:
                lines.append("队列为空")
            yield event.plain_result("\n".join(lines))
            return

        if sub == "clear":
            count = await task_queue.clear(directory)
            yield event.plain_result(f"已清空 {count} 个队列任务")
            return

        if sub == "cancel":
            task_id = parts[1].strip() if len(parts) > 1 else ""
            if not task_id:
                yield event.plain_result("用法: /oc queue cancel <任务ID>")
                return
            ok = await task_queue.cancel(directory, task_id)
            yield event.plain_result("已取消任务" if ok else "任务不存在或不在队列中")
            return

        yield event.plain_result("未知子命令。用法: /oc queue [list]|cancel <ID>|clear")

    # ──── 辅助方法 ────

    async def _get_session_for_cmd(self, event: AstrMessageEvent, target: str) -> Optional[str]:
        if target and target.startswith("ses_"):
            return target
        if target and target.isdigit():
            try:
                sessions = await self.client.session_list(self._get_directory(event))
                archived = self.state_mgr.get_window_state(self._get_umo(event)).get("archived_sessions", [])
                sessions = [s for s in sessions if s.get("id") not in archived]
                idx = int(target)
                if 1 <= idx <= len(sessions):
                    return sessions[idx - 1]["id"]
            except Exception:
                pass
        return self.state_mgr.get_current_session(self._get_umo(event))
