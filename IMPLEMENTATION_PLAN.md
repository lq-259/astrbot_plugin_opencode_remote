# 从 astrbot_plugin_opencode_remote 初始仓库到聊天-编程双模式助手的实施计划

## 1. 文档目的

本文档从公开仓库 `https://github.com/lq-259/astrbot_plugin_opencode_remote` 的初始状态出发，规划如何把现有 OpenCode 远程控制插件改造成一个更完整的 AstrBot + OpenCode 聊天-编程双模式助手。

初始仓库已经具备较完整的 OpenCode 远程控制能力，但缺少“路由层”：用户和 AstrBot 普通聊天时应由 AstrBot 自己响应，用户提出代码工作任务时才交给 OpenCode。本文档重点描述如何在现有插件基础上增量实现这条能力链路。

本文档不是从零设计一个新系统，而是面向已有插件的演进计划。

## 2. 初始仓库状态

### 2.1 仓库地址

```text
https://github.com/lq-259/astrbot_plugin_opencode_remote
```

### 2.2 初始文件结构

初始仓库主要包含以下文件：

| 文件 | 初始职责 |
|---|---|
| `main.py` | 插件入口，生命周期管理，`/oc` 主命令，快捷前缀 `>`，SSE 初始化 |
| `command_handlers.py` | `/oc` 子命令路由和实现，如会话、目录、模型、文件、审批、Shell |
| `opencode_client.py` | OpenCode Server HTTP API 客户端 |
| `llm_integration.py` | AstrBot LLM function calling 工具集成，提供 `opencode_*` 工具 |
| `sse_listener.py` | 监听 OpenCode SSE 事件 |
| `notification_manager.py` | 向 AstrBot 会话推送 OpenCode 通知 |
| `state_manager.py` | 管理窗口状态、用户状态、会话归属、错误记录 |
| `path_manager.py` | 工作路径、白名单路径、模型配置管理 |
| `pending_manager.py` | 权限请求和插件确认队列 |
| `session_ops.py` | 会话创建、发送消息、停止会话等封装 |
| `formatters.py` | 输出格式化 |
| `constants.py` | 常量、帮助主题、敏感关键词等 |
| `_conf_schema.json` | AstrBot 插件配置 schema |
| `metadata.yaml` | 插件元信息 |

### 2.3 初始仓库已有能力

初始仓库已经具备以下能力：

- 连接 OpenCode Server。
- 检查 OpenCode 健康状态。
- 管理 OpenCode 会话。
- 切换工作目录。
- 使用工作目录白名单保护路径访问。
- 使用 `/oc ask` 向当前 OpenCode 会话发送任务。
- 使用快捷前缀 `>` 向当前会话发送消息。
- 使用 `>N` 向列表中的第 N 个会话发送消息。
- 管理模型、variant、agent。
- 执行 OpenCode 内置 command。
- 执行 Shell，并可要求确认。
- 读取、写入、列出文件。
- 监听 SSE，并推送执行过程、错误、完成事件。
- 管理 OpenCode 权限请求和待审批项。
- 注册一批 `opencode_*` LLM tools，让 AstrBot 主 LLM 可以通过 function calling 控制 OpenCode。

### 2.4 初始仓库的核心入口

初始仓库已有两个主要入口。

#### `/oc` 命令入口

示例：

```text
/oc health
/oc status
/oc cd /path/to/project
/oc ask 帮我查看项目结构
/oc stop
```

特点：

- 明确、可控。
- 用户必须知道命令格式。
- 不会误触发普通聊天。
- 适合管理员和熟练用户。

#### `>` 快捷前缀入口

示例：

```text
> 帮我继续修复这个问题
>2 给第二个会话追加说明
```

特点：

- 比 `/oc ask` 更短。
- 仍然需要用户显式触发。
- 不具备自然语言自动路由能力。

### 2.5 初始仓库已有安全能力

初始仓库已经有较好的基础安全设计：

- `only_admin`：可限制只有管理员使用插件。
- `allowed_workdirs`：限制可访问工作目录。
- `check_path_safety`：启用路径安全检查。
- `confirm_delete`：删除会话前确认。
- `confirm_shell`：执行 Shell 前确认。
- `destructive_keywords`：对敏感任务文本进行关键词检测。
- `pending_manager`：管理 OpenCode 权限请求和插件侧确认请求。

这些能力应保留，并作为后续路由功能的安全基础。

## 3. 初始仓库缺口分析

### 3.1 缺少消息路由层

初始仓库没有一个独立模块判断用户消息应该交给 AstrBot 普通聊天，还是交给 OpenCode 工作。

现状：

```text
用户要让 OpenCode 工作时，必须使用 /oc ask 或 >。
用户普通聊天时，AstrBot 自己处理。
```

目标：

```text
用户普通聊天 -> AstrBot 自己回复
用户明确工作任务 -> OpenCode 执行
用户模糊工作任务 -> 先询问确认
```

### 3.2 LLM tools 和路由职责重叠

初始仓库有 `tool_config.enable_llm_tools` 配置，但在初始代码中，`llm_integration.py` 的工具可见性控制没有优先判断该开关。

风险：

- 即使配置中关闭自然语言工具，OpenCode 工具仍可能暴露给 AstrBot 主模型。
- 主 LLM 可能绕过预期的路由层，直接调用 OpenCode 工具。
- 路由决策和工具调用决策混在一起，难以控制。

目标：

```text
enable_llm_tools=false 时，所有 opencode_* tools 都不暴露。
第一阶段由路由层统一决定是否调用 OpenCode。
```

### 3.3 缺少结构化工作命令

初始仓库已有 `/oc ask`，但它是直接发送用户原文。

问题：

- 用户消息可能太短。
- OpenCode 输出格式不稳定。
- 默认约束不够明确，如不要提交、不要 push、修改后要验证。

目标：新增 `/oc work <任务>`，用标准工程任务 prompt 包装用户请求。

### 3.4 发送 OpenCode 任务逻辑分散

初始仓库中至少有多个地方会向 OpenCode 发送消息：

- `command_handlers.py` 中的 `/oc ask`。
- `main.py` 中的 `quick_prefix_handler`。
- `llm_integration.py` 中的 `tool_send_message`。

目标：新增共享方法 `send_task_to_opencode()`，供自动路由、显式路由和快捷入口复用。

### 3.5 配置中缺少路由配置

初始 `_conf_schema.json` 没有 `router_config`。

目标：新增配置块，允许用户控制：

- 是否启用自动路由。
- 路由模式。
- 显式工作前缀。
- 确认阈值。
- 自动执行阈值。
- 路由确认超时。
- 工作关键词。

## 4. 改造目标

### 4.1 总目标

在不破坏初始仓库已有功能的前提下，增加“聊天-编程双模式路由能力”。

最终用户体验：

```text
用户：今天天气怎么样？
AstrBot：正常聊天回复。

用户：帮我修复登录接口 500 的问题
AstrBot：这看起来是代码工作任务，是否交给 OpenCode 执行？
用户：确认
OpenCode：开始分析、修改、验证，并返回结果。
```

### 4.2 第一阶段目标

第一阶段只接一个 OpenCode，不引入 Codeg 和其他 Agent。

第一阶段完成后应具备：

- `/oc work <任务>`。
- `/work <任务>` 或 `/code <任务>` 显式工作前缀。
- 普通自然语言工作任务自动识别并询问确认。
- 高置信度任务可在 `auto` 模式下直接执行。
- 普通聊天不被拦截。
- `enable_llm_tools=false` 真正关闭 OpenCode LLM tools。
- 旧有 `/oc` 命令和 `>` 快捷前缀保持兼容。

## 5. 设计原则

### 5.1 最小改动

不重写已有命令系统、状态系统、OpenCode client、SSE 监听和审批系统。

新增能力尽量集中在：

- `router.py`
- `main.py`
- `command_handlers.py`
- `_conf_schema.json`
- `llm_integration.py`

### 5.2 安全优先

默认策略：

- 自动路由默认关闭或使用 `confirm` 模式。
- 只允许管理员或授权用户触发 OpenCode。
- 保留目录白名单。
- 保留 Shell 和删除确认。
- 默认关闭 LLM tools，避免主模型绕过路由。

### 5.3 显式优先

处理优先级：

```text
/oc 命令 > > 快捷前缀 > /work 显式路由 > 自动路由 > AstrBot 普通聊天
```

### 5.4 可观测

用户应该清楚知道：

- 当前消息是否被路由到 OpenCode。
- 当前 OpenCode 工作目录是什么。
- 当前绑定的会话是什么。
- 哪些操作需要确认。
- 任务最终做了什么、验证了什么。

## 6. 目标架构

```text
IM 消息
  |
  v
AstrBot 事件系统
  |
  |-- /oc 命令 ----------------------> CommandHandlers
  |-- > 快捷前缀 --------------------> quick_prefix_handler
  |-- 普通消息 ----------------------> auto_route_handler
                                      |
                                      v
                                MessageRouter
                                      |
              +-----------------------+-----------------------+
              |                       |                       |
              v                       v                       v
            chat                   confirm                 opencode
              |                       |                       |
              v                       v                       v
       AstrBot 正常回复         用户确认后执行          send_task_to_opencode
                                                              |
                                                              v
                                                        OpenCode Server
                                                              |
                                                              v
                                                        项目工作目录
```

## 7. 文件级改造计划

### 7.1 新增 `router.py`

新增文件：

```text
router.py
```

职责：

- 定义 `RouteDecision`。
- 定义 `MessageRouter`。
- 根据显式前缀和关键词计算路由结果。

核心接口：

```python
decision = self.router.classify(raw_text)
```

返回：

```python
RouteDecision(
    action="chat" | "confirm" | "opencode",
    reason="命中原因",
    confidence=0.0,
    rewritten_task="任务文本",
)
```

验收标准：

- 输入普通聊天，返回 `chat`。
- 输入 `/work 修复 bug`，返回 `opencode`。
- 输入 `帮我修复 bug`，返回 `confirm`。
- 路由模式为 `off` 时，不自动路由普通消息。

### 7.2 修改 `_conf_schema.json`

新增配置块：

```json
{
  "router_config": {
    "enable_auto_route": false,
    "mode": "confirm",
    "work_prefixes": ["/work", "/code", "！代码", "!code"],
    "confirm_threshold": 0.65,
    "auto_threshold": 0.85,
    "confirm_timeout": 60,
    "ignore_group_messages_without_mention": true,
    "work_keywords": ["修复", "bug", "代码", "error", "failed"]
  }
}
```

要求：

- 必须保留初始仓库已有 `workspace_config`。
- 必须保留初始仓库已有 `tool_config`。
- `enable_auto_route` 默认建议为 `false`，避免安装后直接拦截用户消息。
- 生产配置可改为 `true`。

验收标准：

- JSON 有效。
- AstrBot 插件后台能显示新增配置项。
- 原有配置项不丢失。

### 7.3 修改 `llm_integration.py`

改造点：在 `on_llm_request_hook()` 开头优先判断 `enable_llm_tools`。

目标逻辑：

```python
tool_cfg = self.plugin.config.get("tool_config", {})
if not tool_cfg.get("enable_llm_tools", False):
    self._remove_all_tools(request)
    return
```

原因：

- 第一阶段不建议让 AstrBot 主 LLM 直接调用 OpenCode tools。
- 避免主模型绕过路由层。
- 让配置含义和实际行为一致。

验收标准：

- `enable_llm_tools=false` 时，`opencode_*` tools 不暴露。
- `enable_llm_tools=true` 时，保留原有工具可见性控制逻辑。

### 7.4 修改 `main.py`

改造点一：初始化路由器。

```python
self.router = MessageRouter(config)
```

改造点二：提取公共发送方法。

```python
async def send_task_to_opencode(self, event, text, session_id=None):
    ...
```

该方法负责：

- 获取当前窗口 `umo`。
- 获取或设置默认工作目录。
- 创建或复用当前 OpenCode 会话。
- 读取本地模型、variant、agent 覆盖。
- 调用 `client.session_prompt()`。
- 格式化并返回结果。

改造点三：让 `quick_prefix_handler` 复用公共发送方法。

改造点四：新增 `auto_route_handler`。

建议优先级：

```python
@filter.event_message_type(filter.EventMessageType.ALL, priority=20)
```

处理流程：

```text
如果未开启 enable_auto_route -> return
如果无权限 -> return
如果是 /oc 或 > -> return
调用 MessageRouter.classify()
如果 chat -> return
如果 confirm -> stop_event，询问确认，确认后发送 OpenCode
如果 opencode -> stop_event，直接发送 OpenCode
```

验收标准：

- `/oc` 命令行为不变。
- `>` 快捷前缀行为不变。
- 普通聊天不被拦截。
- 工作类消息能触发确认流程。
- 确认后能调用 OpenCode。

### 7.5 修改 `command_handlers.py`

新增 `/oc work <任务>` 命令。

改造点：

```python
ROUTES["work"] = ("消息", True)
```

新增方法：

```python
async def cmd_work(self, event, text=""):
    ...
```

`cmd_work` 应做两件事：

- 检查任务文本是否为空。
- 用结构化 prompt 包装用户请求，并调用 `self.plugin.send_task_to_opencode()`。

推荐 prompt：

```text
你是 OpenCode，负责在当前仓库完成代码任务。

用户请求：{text}

执行要求：
1. 先理解问题和相关代码。
2. 只做必要修改，避免过度改动。
3. 修改后运行最小必要验证。
4. 不要推送远程分支，不要提交 git，除非用户明确要求。
5. 最后用中文总结：根因、修改内容、验证结果、后续建议。
```

验收标准：

- `/oc work` 无参数时提示用法。
- `/oc work 修复 bug` 可以触发 OpenCode。
- 输出格式比 `/oc ask` 更稳定。

## 8. 路由策略细节

### 8.1 三种模式

| 模式 | 行为 | 推荐场景 |
|---|---|---|
| `off` | 自动路由关闭 | 初始安装、调试、安全敏感环境 |
| `confirm` | 疑似代码任务先询问确认 | 默认推荐 |
| `auto` | 高置信度代码任务直接执行 | 私聊、可信用户、稳定后 |

### 8.2 显式路由

显式路由不需要评分。

示例：

```text
/work 修复登录接口
/code 给 service 层补测试
!code review 当前改动
```

处理结果：

```text
直接进入 opencode 动作。
```

### 8.3 自动路由

自动路由只处理没有被 `/oc` 和 `>` 捕获的普通消息。

评分规则第一版采用简单规则：

- 每个关键词命中增加分数。
- 某些句式模式命中增加分数。
- 分数超过 `confirm_threshold` 进入确认。
- 分数超过 `auto_threshold` 且模式为 `auto` 才直接执行。

### 8.4 典型判断结果

| 消息 | 动作 |
|---|---|
| `你好` | `chat` |
| `今天心情不好` | `chat` |
| `Python 装饰器是什么` | `chat` |
| `这个报错怎么解决` | `confirm` |
| `帮我修复登录 bug` | `confirm` |
| `/work 修复登录 bug` | `opencode` |
| `> 继续刚才的修改` | 快捷前缀处理，不走路由 |

## 9. 安全策略

### 9.1 保留初始仓库安全边界

改造不能削弱初始仓库已有安全能力。

必须保留：

- `only_admin`。
- `allowed_workdirs`。
- `check_path_safety`。
- `confirm_delete`。
- `confirm_shell`。
- `destructive_keywords`。
- `pending_manager`。

### 9.2 默认建议配置

```json
{
  "basic_config": {
    "only_admin": true
  },
  "workspace_config": {
    "check_path_safety": true
  },
  "security_config": {
    "confirm_delete": true,
    "confirm_shell": true
  },
  "tool_config": {
    "enable_llm_tools": false
  },
  "router_config": {
    "enable_auto_route": false,
    "mode": "confirm"
  }
}
```

### 9.3 开启自动路由后的建议

如果要开启自动路由：

```json
{
  "router_config": {
    "enable_auto_route": true,
    "mode": "confirm"
  }
}
```

不要一开始就使用：

```json
{
  "router_config": {
    "mode": "auto"
  }
}
```

除非环境是私聊、可信用户、白名单目录明确且已有 Git 保护。

## 10. 阶段实施计划

### Phase 0：基线确认

目标：确认初始仓库可正常工作。

操作：

```text
/oc health
/oc dirs
/oc status
/oc cd <项目路径>
/oc ask 查看项目结构
> 继续说明一下项目结构
```

验收：

- OpenCode Server 可连接。
- `/oc ask` 可用。
- `>` 快捷前缀可用。
- SSE 推送可用。

### Phase 1：路由配置与工具开关

改造文件：

- `_conf_schema.json`
- `llm_integration.py`

目标：

- 新增 `router_config`。
- 修复 `enable_llm_tools` 实际生效。

验收：

- 配置 schema 有效。
- AstrBot 后台能看到路由配置。
- `enable_llm_tools=false` 时不暴露 OpenCode tools。

### Phase 2：路由器模块

改造文件：

- 新增 `router.py`

目标：

- 实现 `MessageRouter`。
- 支持显式前缀。
- 支持关键词评分。
- 支持 `off`、`confirm`、`auto` 模式。

验收：

- 单独构造 `MessageRouter` 可返回正确决策。
- 不依赖 OpenCode Server 也能测试路由结果。

### Phase 3：公共任务发送方法

改造文件：

- `main.py`

目标：

- 新增 `send_task_to_opencode()`。
- 让 `quick_prefix_handler` 复用该方法。

验收：

- `>` 行为不变。
- `>N` 行为不变。
- 发送逻辑集中，后续自动路由可复用。

### Phase 4：显式工作命令

改造文件：

- `command_handlers.py`

目标：

- 新增 `/oc work <任务>`。
- 使用结构化 prompt 包装任务。

验收：

- `/oc work` 无参数时提示用法。
- `/oc work 查看项目结构` 可触发 OpenCode。
- `/oc ask` 原行为不变。

### Phase 5：自动路由入口

改造文件：

- `main.py`

目标：

- 新增 `auto_route_handler`。
- 普通消息进入 `MessageRouter`。
- `confirm` 分支询问用户确认。
- `opencode` 分支直接发送。
- `chat` 分支放行。

验收：

- `enable_auto_route=false` 时完全不影响普通聊天。
- `enable_auto_route=true` 且 `mode=confirm` 时，工作任务先询问确认。
- 用户回复“确认”后调用 OpenCode。
- 用户回复其他内容时取消。

### Phase 6：体验优化

改造文件：

- `router.py`
- `formatters.py`
- `notification_manager.py`

目标：

- 优化关键词。
- 优化确认提示。
- 优化最终结果格式。
- 增加群聊 @ 检测。

验收：

- 误触发率下降。
- 用户能清晰知道是否进入工作模式。
- 群聊中未 @ 不触发自动路由。

## 11. 测试计划

### 11.1 回归测试

这些初始仓库能力必须不被破坏。

| 测试项 | 输入 | 预期 |
|---|---|---|
| 健康检查 | `/oc health` | 返回 OpenCode 健康状态 |
| 状态查看 | `/oc status` | 显示当前窗口状态 |
| 路径切换 | `/oc cd <path>` | 成功切换或安全拒绝 |
| 会话列表 | `/oc list` | 列出会话 |
| 直接任务 | `/oc ask test` | 发送给 OpenCode |
| 快捷前缀 | `> test` | 发送给当前会话 |
| Shell 确认 | `/oc shell ls` | 按配置确认 |
| 删除确认 | `/oc delete` | 按配置确认 |

### 11.2 新功能测试

| 测试项 | 输入 | 预期 |
|---|---|---|
| `/oc work` 空参数 | `/oc work` | 提示用法 |
| `/oc work` 任务 | `/oc work 查看项目结构` | 调用 OpenCode |
| 显式前缀 | `/work 修复 bug` | 直接或按实现调用 OpenCode |
| 自动路由关闭 | `帮我修复 bug` | 不拦截 |
| 自动路由确认 | `帮我修复 bug` | 询问确认 |
| 确认执行 | 回复 `确认` | 调用 OpenCode |
| 取消执行 | 回复 `取消` | 不调用 OpenCode |
| 普通聊天 | `讲个笑话` | AstrBot 正常处理 |

### 11.3 安全测试

| 测试项 | 输入 | 预期 |
|---|---|---|
| 非管理员 | `/oc status` | 权限不足 |
| 白名单外路径 | `/oc cd /etc` | 拒绝 |
| 危险文本 | `/oc ask 删除所有文件` | 要求确认或阻止 |
| Shell | `/oc shell rm -rf tmp` | 要求确认 |
| LLM tools 关闭 | 普通 LLM 请求 | 不暴露 `opencode_*` tools |

## 12. 验收标准

改造完成后必须满足：

- 初始仓库所有核心命令仍可用。
- 新增路由功能不影响 `/oc` 和 `>`。
- `router_config` 可配置。
- `enable_llm_tools=false` 行为正确。
- `/oc work` 可用。
- 自动路由可区分普通聊天和代码任务。
- 自动路由默认安全，不会安装后直接误触发。
- 文档能说明从初始仓库到目标形态的每一步改造。

## 13. 当前分支实现状态

当前增强分支已实现：

- 新增 `router.py`。
- 新增 `router_config`。
- 修改 `llm_integration.py`，让 `enable_llm_tools=false` 生效。
- 修改 `main.py`，新增 `send_task_to_opencode()`。
- 修改 `main.py`，让 `quick_prefix_handler` 复用公共发送方法。
- 修改 `main.py`，新增 `auto_route_handler`。
- 修改 `command_handlers.py`，新增 `/oc work`。
- 修改 `command_handlers.py`，新增 `/oc diff`（查看会话 Git 变更）。
- 修改 `command_handlers.py`，新增 `/oc commit`（封装 git add + git commit）。
- 修改 `router.py`，新增群聊 @ 精准检测（未 @ 不触发自动路由）。
- 修改 `router.py`，优化路由关键词和确认提示文案。
- 新增 `README.md`。
- 新增本实施计划文档。

当前仍未实现：

- LLM 意图分类。
- 多项目别名。
- 每项目任务队列。
- Git diff 专用摘要命令。

## 14. 后续路线图

### 14.1 群聊安全增强

目标：在群聊中只有 @ Bot 或显式 `/work` 时才触发自动路由。

涉及文件：

- `main.py`
- `router.py`

### 14.2 LLM 意图分类

目标：在规则路由不确定时，使用轻量模型判断是否为工作任务。

要求：

- 只返回 JSON。
- 不直接执行工具。
- 不替代安全确认。

### 14.3 项目别名

目标：用户可以用项目名而不是路径操作。

示例：

```text
/oc project add api /projects/api-service
/oc project use api
/work 修复登录接口
```

### 14.4 任务队列

目标：避免同一个仓库同时跑多个 OpenCode 任务导致文件冲突。

规则：

- 同一目录同时只允许一个 active task。
- 新任务进入队列。
- 支持查看、取消、暂停队列。

### 14.5 Git 工作流

目标：让用户在聊天中完成安全的 Git 操作。

命令设想：

```text
/oc diff
/oc commit <message>
/oc branch <name>
```

默认仍禁止自动 `git push`。

## 15. 最终目标形态

```text
用户：帮我看下 CI 为什么失败
AstrBot：这看起来是代码任务，要交给 OpenCode 吗？
用户：确认
OpenCode：我会先检查 CI 配置和最近提交。
OpenCode：发现 lint 失败，原因是 src/auth.ts 缺少类型声明。
OpenCode：已修复并运行 npm test，全部通过。
AstrBot：任务完成。修改 1 个文件，测试通过。是否需要提交 commit？
用户：提交
AstrBot：提交信息建议：fix(auth): add missing type annotation。确认吗？
用户：确认
OpenCode：已提交本地 commit，未推送远程。
```

这个最终形态中：

- AstrBot 始终是聊天入口。
- OpenCode 始终是工程执行者。
- 插件负责连接、状态、安全、审批和通知。
- 路由层负责决定什么时候从聊天切换到工作。
