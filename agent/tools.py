MEMORY_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "save_memory",
            "description": "将重要信息写入长期记忆，供未来对话复用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": "要记住的内容，尽量简洁明确。",
                    },
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "可选标签，便于检索。",
                    },
                },
                "required": ["content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_memory",
            "description": "按关键词检索长期记忆。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "检索关键词或问题。"},
                    "limit": {
                        "type": "integer",
                        "description": "返回条数，默认 8。",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_memories",
            "description": "列出全部长期记忆。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]

ROLE_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "save_role",
            "description": "创建或更新 role，自动写入 roles 目录下的 ROLE.md 文件并立即生效。",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "role 标识，小写字母/数字/连字符，如 reviewer。",
                    },
                    "title": {
                        "type": "string",
                        "description": "展示名称，如「代码审查员」。",
                    },
                    "description": {
                        "type": "string",
                        "description": "一句话说明该 role 的用途。",
                    },
                    "body": {
                        "type": "string",
                        "description": "Markdown 正文，描述身份与风格偏好（不含 frontmatter）。",
                    },
                    "switch_to": {
                        "type": "boolean",
                        "description": "保存后是否立即切换到该 role，默认 true。",
                    },
                },
                "required": ["name", "title", "description", "body"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_role",
            "description": "删除指定 role 及其 ROLE.md 文件。",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "要删除的 role 名称。"},
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_roles",
            "description": "列出 roles 目录中的全部 role。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]

PLAN_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "create_plan",
            "description": "为复杂任务创建分步执行计划。创建后按步骤推进，并用 update_plan_step 更新状态。",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "计划标题，简短概括任务。"},
                    "goal": {"type": "string", "description": "要达成的最终目标。"},
                    "steps": {
                        "type": "array",
                        "description": "有序步骤列表，每步需唯一 id 与 description。",
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {
                                    "type": "string",
                                    "description": "步骤标识，如 step-1、analyze、fix。",
                                },
                                "description": {
                                    "type": "string",
                                    "description": "这一步要做什么。",
                                },
                            },
                            "required": ["id", "description"],
                        },
                    },
                },
                "required": ["title", "goal", "steps"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_plan_step",
            "description": "更新计划中某一步的状态与结果。执行某步前设为 in_progress，完成后设为 completed 并写入 result。",
            "parameters": {
                "type": "object",
                "properties": {
                    "step_id": {"type": "string", "description": "步骤 id。"},
                    "status": {
                        "type": "string",
                        "enum": [
                            "pending",
                            "in_progress",
                            "completed",
                            "failed",
                            "skipped",
                        ],
                        "description": "步骤状态。",
                    },
                    "result": {
                        "type": "string",
                        "description": "可选，这一步的执行结果或发现。",
                    },
                },
                "required": ["step_id", "status"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "complete_plan",
            "description": "标记当前计划已完成，并给出总结。",
            "parameters": {
                "type": "object",
                "properties": {
                    "summary": {
                        "type": "string",
                        "description": "计划执行总结。",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_plan",
            "description": "查看当前计划及全部步骤进度。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]

EXEC_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "读取工作区内的文本文件内容。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "相对工作区或绝对路径。",
                    },
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "写入或覆盖工作区内的文本文件。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "目标文件路径。"},
                    "content": {"type": "string", "description": "文件内容。"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_directory",
            "description": "列出工作区目录下的文件与子目录。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "目录路径，默认 .",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": "在工作区内执行 shell 命令，返回 stdout/stderr 与退出码。",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "要执行的 shell 命令。"},
                    "cwd": {
                        "type": "string",
                        "description": "可选，命令工作目录，默认工作区根目录。",
                    },
                },
                "required": ["command"],
            },
        },
    },
]

WORKSPACE_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_workspace",
            "description": "查看当前工作区路径及 .env 默认工作区。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_workspace",
            "description": (
                "切换文件读写与 run_command 的工作区根目录。"
                "传入绝对路径、相对路径或 ~ 路径；传 default 恢复 .env 中的 WORKSPACE_DIR。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "目标目录路径，或 default 恢复默认。",
                    },
                },
                "required": ["path"],
            },
        },
    },
]

CHANNEL_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "send_attachment",
            "description": (
                "向当前微信用户发送工作区内的图片或文件附件（PDF、文档、压缩包等）。"
                "先用 write_file 等工具生成文件，再调用本工具发送；不要告诉用户你无法发送附件。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "工作区内文件路径（相对或绝对）。",
                    },
                },
                "required": ["path"],
            },
        },
    },
]

AGENT_TOOLS = MEMORY_TOOLS + ROLE_TOOLS + PLAN_TOOLS + WORKSPACE_TOOLS + EXEC_TOOLS

DISCUSS_READ_TOOLS = [
    tool
    for tool in (EXEC_TOOLS + WORKSPACE_TOOLS)
    if tool["function"]["name"] in {"read_file", "list_directory", "get_workspace"}
]

ASSIGN_READ_TOOLS = list(DISCUSS_READ_TOOLS)

ASSIGN_WRITE_TOOLS = list(DISCUSS_READ_TOOLS) + [
    tool for tool in EXEC_TOOLS if tool["function"]["name"] == "write_file"
]
