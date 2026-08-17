# MiniC 介绍

MiniC 是一个本地优先的 DeepAgent：所有数据和计算都留在你自己的机器上，核心服务只监听 `127.0.0.1`。

## 主要能力

MiniC 通过对话帮助你完成本地任务：

- **知识库问答**：把 Markdown 知识库入库，检索后回答问题并带 `文件路径 + 章节` 来源。
- **本地工具**：读文件、写文件、全文搜索、执行终端命令、查看 Git 状态等。
- **会话与记忆**：多会话管理，长期记忆自动记录用户偏好与项目约定。
- **MCP / SKILL / 子 Agent**：接入外部 MCP 工具，按技能白名单执行，委托子任务。

## 使用方式

```powershell
minic
```

在任意目录输入 `minic` 即可进入会话。核心未运行时会自动启动，退出时自动关闭本次启动的核心。

## 默认模型

- 对话模型：DeepSeek `deepseek-v4-flash`
- Embedding：阿里云百炼 `text-embedding-v3`

API Key 放在 `~/.minic/minic.json`，不会进入代码仓库。
