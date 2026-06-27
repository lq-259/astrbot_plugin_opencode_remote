import json
import re
from .constants import HELP_TOPICS, HELP_TOPIC_LIST, EVENT_TYPES_CHINESE


def format_health(health_data: dict) -> str:
    lines = [
        "OpenCode Server 状态:",
        f"  健康: {health_data.get('healthy', False)}",
        f"  版本: {health_data.get('version', 'unknown')}",
    ]
    return "\n".join(lines)


def format_path_info(path_data: dict, directory: str) -> str:
    lines = [
        "当前项目信息:",
        f"  绑定目录: {directory or '未设置'}",
        f"  directory: {path_data.get('directory', 'N/A')}",
        f"  worktree: {path_data.get('worktree', 'N/A')}",
        f"  state: {path_data.get('state', 'N/A')}",
        f"  config: {path_data.get('config', 'N/A')}",
    ]
    return "\n".join(lines)


def format_session_list(sessions: list, current_session_id: str = None,
                        start_index: int = 1, header: str = None) -> str:
    if not sessions:
        return "暂无会话"

    lines = []
    if header:
        lines.append(header)
        lines.append("")

    for i, s in enumerate(sessions, start_index):
        sid = s.get("id", "N/A")
        title = s.get("title", "无标题") or "无标题"
        marker = " <--" if sid == current_session_id else ""
        display_title = title if len(title) <= 50 else title[:47] + "..."
        display_id = sid[:12] if len(sid) > 12 else sid
        lines.append(f"{i:>2}. {display_title}{marker}")
        lines.append(f"    {display_id}")

        directory = s.get("directory", "")
        if directory:
            lines.append(f"    dir: {directory}")

    return "\n".join(lines)


def format_session_status(status_data: dict) -> str:
    lines = ["会话详情:"]
    sid = status_data.get("id", "N/A")
    title = status_data.get("title", "无标题")
    directory = status_data.get("directory", "N/A")
    created = status_data.get("time", {}).get("created", "N/A")

    lines.append(f"  ID: {sid}")
    lines.append(f"  标题: {title}")
    lines.append(f"  目录: {directory}")
    lines.append(f"  创建: {created}")

    summary = status_data.get("summary")
    if summary:
        lines.append("  变更:")
        additions = summary.get("additions", 0)
        deletions = summary.get("deletions", 0)
        files = summary.get("files", 0)
        lines.append(f"    +{additions} -{deletions} ({files} 文件)")

    return "\n".join(lines)


def extract_text_from_parts(parts: list) -> str:
    texts = []
    for part in parts or []:
        if part.get("type") == "text":
            texts.append(part.get("text", ""))
    return "\n".join(texts)


def format_messages(all_messages: list, rounds: int = 1) -> str:
    if not all_messages:
        return "(暂无消息)"

    visible = [m for m in all_messages
               if m.get("info", {}).get("role") == "assistant"]

    if not visible:
        return "(暂无 AI 响应)"

    latest = visible[-(min(rounds, len(visible))):]

    lines = []
    for msg_entry in latest:
        info = msg_entry.get("info", {})
        parts = msg_entry.get("parts", [])
        role = info.get("role", "unknown")
        text = extract_text_from_parts(parts)
        if text:
            lines.append(f"[{role}] {text}")

    return "\n\n".join(lines) if lines else "(暂无文本内容)"


def _truncate(text: str, max_len: int = 100) -> str:
    if len(text) <= max_len:
        return text
    return text[:max_len - 3] + "..."


def _extract_error_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("message", "error", "body", "response", "detail", "data"):
            if key in value:
                text = _extract_error_text(value.get(key))
                if text:
                    return text
        try:
            return json.dumps(value, ensure_ascii=False)
        except Exception:
            return str(value)
    if isinstance(value, list):
        return "\n".join(filter(None, (_extract_error_text(item) for item in value)))
    return str(value)


def format_config_summary(config: dict) -> str:
    basic = config.get("basic_config", {})
    notification = config.get("notification_config", {})
    workspace = config.get("workspace_config", {})
    model = config.get("model_config", {})

    lines = [
        "当前插件配置:",
        f"  连接模式: {basic.get('connection_mode', 'N/A')}",
        f"  Server URL: {basic.get('server_url', 'N/A')}",
        f"  仅管理员: {basic.get('only_admin', True)}",
        f"  超时: {basic.get('timeout', 300)}s",
        "",
        f"  SSE 推送级别: {notification.get('output_level', 'N/A')}",
        f"  快捷前缀: {notification.get('quick_prefix', '>')}",
        f"  重连上限: {notification.get('max_reconnect_attempts', 10)}",
        "",
        f"  默认路径: {workspace.get('default_workdir', '未设置')}",
        f"  路径安全检查: {workspace.get('check_path_safety', True)}",
        "",
        f"  默认模型: {model.get('default_model', '未设置')}",
        f"  默认思考等级: {model.get('default_variant', '未设置')}",
        f"  LLM 工具: query={tool.get('enable_llm_query_tools', False)} schedule={tool.get('enable_llm_schedule_tools', False)} action={tool.get('enable_llm_action_tools', False)}",
    ]
    return "\n".join(lines)


def format_help_text(topic: str = "") -> str:
    if topic:
        target = None
        for key in HELP_TOPIC_LIST:
            if key == topic or topic in key:
                target = key
                break
        if not target:
            av = ", ".join(HELP_TOPIC_LIST)
            return f"未知主题: {topic}\n可用主题: {av}"

        cmds = HELP_TOPICS.get(target, [])
        lines = [f"=== {target} ===", ""]
        for name, desc in cmds:
            lines.append(f"  /oc {name:<25} {desc}")
        lines.append("")
        return "\n".join(lines)

    lines = ["=== OpenCode Remote Controller ===", ""]
    lines.append("使用 /oc <命令> [参数] 控制 OpenCode")
    lines.append("快捷前缀: > 消息  (发送到当前会话)")
    lines.append("")

    for topic_name, cmds in HELP_TOPICS.items():
        lines.append(f"-- {topic_name} --")
        for name, desc in cmds:
            lines.append(f"  /oc {name:<25} {desc}")
        lines.append("")

    return "\n".join(lines)


def format_unknown_command(cmd: str) -> str:
    return (
        f"未知命令: {cmd}\n"
        "使用 /oc help 查看可用命令"
    )


def format_permission_description(permission: dict, props: dict = None) -> tuple[str, str]:
    """Return (title, detail) in OpenCode Web UI style."""
    props = props or permission or {}
    ptype = permission.get("type", "") if isinstance(permission, dict) else ""
    if not ptype:
        ptype = props.get("type", "")

    # OpenCode-style title mapping
    title_map = {
        "external_directory": "外部目录访问",
        "write": "文件写入",
        "edit": "文件编辑",
        "bash": "执行 Shell",
        "shell": "执行 Shell",
        "delete": "删除操作",
        "external_url": "外部链接访问",
        "task": "创建子任务",
        "subagent": "创建子任务",
    }
    title = title_map.get(ptype, ptype.replace("_", " ").title() if ptype else "权限请求")

    # Build natural-language detail based on type
    detail_parts = []
    patterns = permission.get("patterns", []) if isinstance(permission, dict) else []
    if not patterns:
        patterns = props.get("patterns", [])
    tool_info = permission.get("tool", {}) if isinstance(permission, dict) else {}
    if not tool_info:
        tool_info = props.get("tool", {})

    if ptype == "external_directory" and patterns:
        paths = ", ".join(str(p) for p in patterns)
        detail_parts.append(f"Agent 想访问当前工作目录之外的路径：{paths}")
    elif ptype in ("write", "edit") and patterns:
        paths = ", ".join(str(p) for p in patterns)
        detail_parts.append(f"Agent 想修改文件：{paths}")
    elif ptype in ("bash", "shell"):
        cmd = tool_info.get("input", {}).get("command", "") if isinstance(tool_info, dict) else ""
        if cmd:
            detail_parts.append(f"Agent 想执行命令：{cmd}")
        else:
            detail_parts.append("Agent 想执行 Shell 命令")
    elif ptype == "delete" and patterns:
        paths = ", ".join(str(p) for p in patterns)
        detail_parts.append(f"Agent 想删除：{paths}")
    elif ptype == "external_url" and patterns:
        urls = ", ".join(str(p) for p in patterns)
        detail_parts.append(f"Agent 想访问外部链接：{urls}")
    elif ptype in ("task", "subagent"):
        desc = tool_info.get("input", {}).get("description", "") if isinstance(tool_info, dict) else ""
        if desc:
            detail_parts.append(f"Agent 想创建子任务：{desc}")
        else:
            detail_parts.append("Agent 想创建子任务")
    else:
        desc = permission.get("description", "") if isinstance(permission, dict) else ""
        if desc:
            detail_parts.append(desc)
        else:
            detail_parts.append(str(props)[:200])

    # Add "always allowed" hint
    always = permission.get("always", []) if isinstance(permission, dict) else []
    if not always:
        always = props.get("always", [])
    if always:
        detail_parts.append(f"（你之前已始终允许：{', '.join(str(a) for a in always)}）")

    return title, "\n".join(detail_parts)


def format_permission_notification(permission: dict) -> str:
    title = permission.get("title", "未知权限")
    ptype = permission.get("type", "")
    session_id = permission.get("sessionID", "")[:12]
    index = permission.get("index", "?")

    lines = [
        f"请求 {index}: {title}",
        f"  会话: {session_id}",
        f"  类型: {ptype}",
        "",
        f"使用 /oc allow {index} 批准 或 /oc deny {index} 拒绝",
    ]
    return "\n".join(lines)


def format_event_notification(event_type: str, data: dict = None) -> str:
    label = EVENT_TYPES_CHINESE.get(event_type, event_type)

    if event_type == "session.idle":
        sid = (data or {}).get("sessionID", "")[:12]
        return f"{label} ({sid})"
    elif event_type == "session.error":
        info = data or {}
        sid = info.get("sessionID", "")[:12]
        err = info.get("error") or info.get("exception") or info
        err_msg = _extract_error_text(err) or "未知"
        return f"{label}: {_truncate(err_msg, 800)} ({sid})"
    elif event_type == "session.created":
        sid = (data or {}).get("info", {}).get("id", "")[:12]
        title = (data or {}).get("info", {}).get("title", "") or "无标题"
        return f"{label}: {title} ({sid})"
    elif event_type == "file.edited":
        fname = (data or {}).get("file", "?")
        return f"{label}: {fname}"
    elif event_type == "permission.updated":
        perm = data or {}
        return format_permission_notification(perm)
    elif event_type == "tool.execute.before":
        tool = (data or {}).get("tool", "?")
        return f"调用工具: {tool}"
    elif event_type == "tool.execute.after":
        tool = (data or {}).get("tool", "?")
        return f"工具完成: {tool}"

    return label


def format_consolidated_notification(
    session_title: str,
    session_id: str,
    ops: list[dict],
    done: bool = False,
    final_text: str = None,
) -> str:
    from .constants import TOOL_NAMES_CHINESE

    short_id = session_id[:12] if len(session_id) > 12 else session_id
    lines = [f"{session_title or '无标题'} ({short_id})"]

    if done:
        count = len(ops)
        if count > 0:
            lines.append(f"已完成 ({count} 项操作)")
        else:
            lines.append("已完成")
    else:
        lines.append(f"  {len(ops)} 项操作")

    seen = set()
    for op in ops:
        op_type = op.get("type", "")
        detail = op.get("detail", "")

        label = TOOL_NAMES_CHINESE.get(op_type, op_type)
        if op_type in ("edit", "Edit"):
            label = "编辑"
        elif op_type in ("write", "Write"):
            label = "写入"
        elif op_type in ("read", "Read"):
            label = "读取"
        elif op_type in ("shell",):
            label = "Shell"
        elif op_type in ("subsession",):
            label = "子会话"
        elif op_type in ("todo",):
            label = "任务"
        elif op_type in ("search",):
            label = "搜索"
        elif op_type in ("command",):
            label = "命令"

        if detail:
            short = detail if len(detail) <= 80 else detail[:77] + "..."
            line = f"  - {label}: {short}"
        else:
            line = f"  - {label}"

        if line not in seen:
            seen.add(line)
            lines.append(line)

    if final_text and final_text.strip():
        lines.append("")
        lines.append(final_text.strip())

    return "\n".join(lines)


def format_window_state(state: dict) -> str:
    if not state:
        return "当前窗口未绑定状态"

    lines = ["当前窗口状态:"]
    lines.append(f"  目录: {state.get('directory', '未设置')}")
    sid = state.get('current_session', '未绑定') or '未绑定'
    title = state.get('session_title', '')
    if title and sid != '未绑定':
        lines.append(f"  会话: {title} ({sid[:12] if len(sid) > 12 else sid})")
    else:
        lines.append(f"  会话: {sid}")
    # Show server model (actual) vs local override
    server_model = state.get('server_model', '')
    local_model = state.get('model', '')
    if server_model and local_model and server_model != local_model:
        lines.append(f"  模型: {server_model} (服务端)")
        lines.append(f"  本地覆盖: {local_model}")
    elif server_model:
        lines.append(f"  模型: {server_model}")
    elif local_model:
        lines.append(f"  模型: {local_model}")
    else:
        lines.append(f"  模型: 未设置")
    server_variant = state.get('server_variant', '')
    local_variant = state.get('variant', '')
    if server_variant and local_variant and server_variant != local_variant:
        lines.append(f"  思考等级: {server_variant} (服务端)")
        lines.append(f"  本地覆盖: {local_variant}")
    elif server_variant:
        lines.append(f"  思考等级: {server_variant}")
    elif local_variant:
        lines.append(f"  思考等级: {local_variant}")
    else:
        lines.append(f"  思考等级: 未设置")
    server_agent = state.get('server_agent', '')
    local_agent = state.get('agent', '')
    if server_agent and local_agent and server_agent != local_agent:
        lines.append(f"  agent: {server_agent} (服务端)")
        lines.append(f"  本地 agent 覆盖: {local_agent}")
    elif server_agent:
        lines.append(f"  agent: {server_agent}")
    else:
        lines.append(f"  agent: {local_agent or 'build'}")
    return "\n".join(lines)


def format_response_with_meta(response_text: str, state: dict) -> str:
    title = state.get("session_title", "")
    agent = state.get("agent", "build")
    model = state.get("model", "未设置")
    variant = state.get("variant", "未设置")

    lines = []
    if title:
        lines.append(title)
    lines.append(response_text)
    lines.append(f"{agent} | {model} | {variant}")
    return "\n".join(lines)
