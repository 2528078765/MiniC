# Security Policy

MiniC 是一个本地优先的 Agent 工具。它的安全模型针对的是「Agent 误操作」，而不是「恶意软件或本地攻击者」。以下说明当前版本的安全边界与防护措施。

> This document covers the security posture of MiniC. Please report
> vulnerabilities using the contact placeholder at the bottom.

## 本地接口与访问令牌

- 核心服务只监听 `127.0.0.1`，不对外网开放。
- 每次核心启动生成随机的 Bearer 访问令牌（`secrets.token_urlsafe(32)`），写入 `~/.minic/runtime.json`，仅当前用户可读。
- 除 `GET /health` 外，所有接口都要求 `Authorization: Bearer <token>`；无有效令牌返回 `401 UNAUTHORIZED`。
- 核心重启后令牌重新生成，旧令牌失效；请勿将 `runtime.json` 提交到代码仓库或分享给他人。

## API Key 存储

- API Key 存放在用户级配置文件 `~/.minic/minic.json`（model 与 embedding 各一个），不写入代码仓库；仓库内只提供不含 Key 的模板 `minic.example.json`。
- `GET /settings` 返回设置时会移除 `api_key` 字段，不在响应中回显密钥。
- 日志（请求日志 `requests.jsonl`、工具执行日志 `tool_execution.jsonl`）会先做 PII 脱敏，API Key、手机号、身份证号不会以明文写入日志。
- MCP 配置 `minic_mcp_settings.json` 的 `headers` 字段允许直接写密钥，但会以明文落盘；请勿提交含密钥的配置文件。基于 keyring 的安全存储计划在后续版本提供。
- 长期目标：密钥经系统 keyring（Windows 凭据管理器）存储，不可用时用 Windows DPAPI 加密文件兜底，两者都不可用时拒绝保存并提示。

## 工具与审批

- 写操作（Write / Edit / Format / GitCommit / GitBranch）执行前必须经过审批，默认支持 `allow_once` / `allow_session` / `allow_always` / `deny`。
- **Bash 必须人工审批**，且审批菜单只提供 `allow_once` / `deny`，不允许长期放行；命令输出做 PII 脱敏。
- 写操作与 `Format` 执行前把目标文件备份到 `<project>/.minic/backups/`，支持回滚。
- 只读工具在工作区内自动放行；工作区外读取需要确认。
- 权限记录 `permissions.json` 只持久化 `allow_always` 与 `deny`，且 `deny` 优先于 `allow_always`；`allow_once` 与 `allow_session` 不落盘。
- 启用 SKILL 时其 `allowed-tools` 白名单由工具调用层强制执行，审批是另一道独立闸门。

## 提交给仓库时的注意事项

- 提交前检查是否误包含：`minic.json`、`runtime.json`、`permissions.json`、`.minic/` 目录、`~/.minic/` 下的任何文件、含密钥的 MCP 配置。
- `.gitignore` 已排除 `.minic/rag-data/`、`.minic/memory/`、`.minic/logs/` 等目录；如发现遗漏请一并补充。

## 报告漏洞

发现安全漏洞或安全隐患时，请通过以下方式之一报告（当前为占位联系方式，正式渠道将在发布后确认）：

- GitHub Issues：https://github.com/2528078765/MiniC/issues（请勿在公开 issue 中直接粘贴密钥或敏感日志）
- 邮件：`security@example.com`（占位，请勿当真发送）

请描述：影响的版本、复现步骤、影响范围以及建议的修复方向。我们会在收到后尽快确认与处理。

## 支持的版本

| 版本 | 支持状态 |
| --- | --- |
| 0.1.x（当前开发版） | 支持 |
