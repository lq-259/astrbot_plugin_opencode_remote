OUTPUT_LEVELS = ["silence", "simple", "summary", "detail"]

OUTPUT_LEVEL_DESC = {
    "silence": "仅推送权限、提问、错误和完成提醒",
    "simple": "推送思考中、最终文本、权限、提问、错误",
    "summary": "任务完成时推送最终文本摘要，过滤工具过程",
    "detail": "推送工具过程合并摘要、最终文本、权限、提问、错误",
}

TOOL_NAMES_CHINESE = {
    "read": "读取文件",
    "Read": "读取文件",
    "write": "写入文件",
    "Write": "写入文件",
    "edit": "编辑文件",
    "Edit": "编辑文件",
    "bash": "执行 Shell",
    "Bash": "执行 Shell",
    "task": "创建子会话",
    "Task": "创建子会话",
    "todowrite": "更新任务",
    "TodoWrite": "更新任务",
    "glob": "搜索文件",
    "Glob": "搜索文件",
    "grep": "搜索内容",
    "Grep": "搜索内容",
    "webfetch": "获取网页",
    "WebFetch": "获取网页",
    "question": "提问",
    "Question": "提问",
}

TOOL_DETAIL_EXTRACTORS = {
    "read": ["file_path", "filePath", "path"],
    "Read": ["file_path", "filePath", "path"],
    "write": ["file_path", "filePath", "path"],
    "Write": ["file_path", "filePath", "path"],
    "edit": ["file_path", "filePath", "path"],
    "Edit": ["file_path", "filePath", "path"],
    "bash": ["command", "cmd"],
    "Bash": ["command", "cmd"],
    "task": ["description", "subagent_type", "subagentType"],
    "Task": ["description", "subagent_type", "subagentType"],
    "glob": ["pattern"],
    "Glob": ["pattern"],
    "grep": ["pattern"],
    "Grep": ["pattern"],
    "webfetch": ["url", "URL"],
    "WebFetch": ["url", "URL"],
}

MODEL_VARIANTS = ["", "none", "minimal", "low", "medium", "high", "xhigh", "max"]

DEFAULT_DESTRUCTIVE_KEYWORDS = [
    "删除", "格式化", "清空",
    r"rm\b", r"delete\b", r"format\b",
    r"wipe\b", r"destroy\b", r"shutdown\b", r"reboot\b",
    r"mkfs", r"dd\b", r"> /dev/",
]

HELP_TOPICS = {
    "基础": [
        ("help [主题]", "显示帮助信息"),
        ("health", "检查 OpenCode Server 状态"),
        ("status", "显示当前窗口状态"),
        ("pwd", "显示当前项目路径信息"),
        ("config", "显示当前配置"),
    ],
    "路径": [
        ("cd <路径>", "切换工作路径"),
        ("dirs", "显示可用工作路径"),
    ],
    "会话": [
        ("list", "列出当前目录会话"),
        ("list all", "列出所有目录会话"),
        ("new [标题]", "创建新会话"),
        ("switch <序号|ID前缀>", "切换会话"),
        ("rename [序号]", "重命名会话"),
        ("delete [序号]", "删除会话（需确认）"),
        ("archive [序号]", "归档会话"),
        ("unarchive [序号]", "取消归档"),
        ("share", "分享当前会话"),
        ("unshare", "取消分享"),
        ("messages [轮数]", "查看最近消息"),
        ("agent [build|plan]", "设置 Agent 模式 (build/plan)"),
    ],
    "消息": [
        ("ask <任务>", "发送任务到当前会话"),
        ("stop", "停止当前任务"),
        ("> 消息", "快捷发送到当前会话"),
        (">N 消息", "快捷发送到第N个会话"),
    ],
    "指令": [
        ("commands", "列出 OpenCode 内置命令"),
        ("cmd <命令> [参数]", "执行 OpenCode 命令"),
        ("shell <命令>", "执行 Shell（需确认）"),
    ],
    "模型": [
        ("models [provider]", "列出可用模型"),
        ("model [provider/model]", "设置默认模型"),
        ("variant [等级]", "设置思考等级"),
    ],
    "文件": [
        ("read <路径>", "读取工作目录中的文件内容"),
        ("write <路径> <内容>", "向工作目录写入文件（覆盖)"),
        ("files [路径]", "列出工作目录中的文件和子目录"),
    ],
    "通知": [
        ("bind", "设置当前窗口为默认通知窗口"),
        ("bind status", "查看绑定状态"),
        ("bind reset", "清除通知绑定"),
        ("output [级别]", "切换推送级别"),
    ],
    "审批": [
        ("pending", "查看待审批列表"),
        ("allow <序号>", "批准指定请求"),
        ("deny <序号>", "拒绝指定请求"),
        ("approve", "全部批准"),
    ],
}

HELP_TOPIC_LIST = list(HELP_TOPICS.keys())

EVENT_TYPES_CHINESE = {
    "server.connected": "OpenCode 已连接",
    "session.created": "创建新会话",
    "session.updated": "会话已更新",
    "session.deleted": "会话已删除",
    "session.status": "会话状态变更",
    "session.idle": "任务已完成",
    "session.error": "任务出错",
    "session.diff": "会话变更",
    "session.compacted": "上下文已压缩",
    "message.updated": "新消息",
    "message.part.updated": "消息内容更新",
    "message.removed": "消息已移除",
    "message.part.removed": "消息部分移除",
    "permission.updated": "权限请求",
    "permission.asked": "权限请求",
    "permission.replied": "权限已响应",
    "file.edited": "文件已编辑",
    "file.watcher.updated": "文件变更",
    "todo.updated": "任务列表更新",
    "command.executed": "命令已执行",
    "pty.created": "创建终端",
    "pty.updated": "终端更新",
    "pty.exited": "终端退出",
    "tui.prompt.append": "TUI 追加提示词",
    "tui.command.execute": "TUI 执行命令",
    "tui.toast.show": "TUI 提示消息",
}
