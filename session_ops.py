"""会话操作封装"""
from typing import Optional

from astrbot.api import logger

from .opencode_client import OpenCodeClient
from .formatters import extract_text_from_parts, format_session_list


async def ensure_session(
    client: OpenCodeClient,
    state_mgr,
    umo: str,
    directory: str,
) -> Optional[str]:
    current_sid = state_mgr.get_current_session(umo)
    if current_sid:
        try:
            await client.session_get(current_sid, directory)
            return current_sid
        except Exception:
            state_mgr.set_window_state(umo, current_session=None)
            await state_mgr.persist_window_state(umo)

    try:
        session = await client.session_create(directory=directory)
        sid = session.get("id", "")
        if sid:
            title = session.get("title", "")
            state_mgr.set_window_state(umo, current_session=sid, session_title=title)
            state_mgr.set_session_owner(sid, umo)
            await state_mgr.persist_window_state(umo)
            await state_mgr.persist_session_owners()
            return sid
    except Exception as e:
        logger.error(f"自动创建会话失败: {e}")
    return None


async def list_sessions(
    client: OpenCodeClient,
    directory: str,
    current_session_id: str = None,
) -> str:
    try:
        sessions = await client.session_list(directory)
        return format_session_list(sessions, current_session_id)
    except Exception as e:
        return f"获取会话列表失败: {e}"


async def send_message(
    client: OpenCodeClient,
    session_id: str,
    text: str,
    directory: str,
    model_body: dict = None,
    agent: str = None,
    variant: str = None,
) -> str:
    try:
        result = await client.session_prompt(
            session_id, text, directory=directory,
            model=model_body, agent=agent, variant=variant
        )
        parts = result.get("parts", [])
        response_text = extract_text_from_parts(parts)
        return response_text or "(无文本响应)"
    except Exception as e:
        return f"请求失败: {e}"


async def abort_session(
    client: OpenCodeClient,
    session_id: str,
    directory: str,
) -> str:
    try:
        await client.session_abort(session_id, directory)
        return f"已停止会话 {session_id[:12]}"
    except Exception as e:
        return f"停止失败: {e}"


async def create_session_with_title(
    client: OpenCodeClient,
    title: str,
    directory: str,
) -> str:
    try:
        session = await client.session_create(title=title, directory=directory)
        sid = session.get("id", "")
        return f"已创建会话: {title}\nID: {sid}"
    except Exception as e:
        return f"创建失败: {e}"
