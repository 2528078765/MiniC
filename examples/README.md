# examples 示例

本目录包含一个最小化的多格式知识库，用于快速体验 MiniC 的「多格式文档入库」与知识库问答功能。样例覆盖 Markdown、TXT、JSON、HTML 四种文本格式（PDF 为二进制格式，本轮不提供样例文件，能力见根目录 [README](../README.md) 的「RAG」章节）。

## 目录结构

```text
examples/
├── README.md
└── knowledge-base/
    ├── MiniC介绍.md    # 项目简介（可检索「MiniC 是什么」）
    ├── 使用示例.md      # 用法演示（可检索「如何入库 / 如何查询」）
    ├── 工具与审批.md    # 工具与审批说明（可检索「Bash 需要审批吗」）
    ├── 使用示例.txt     # TXT 格式演示（可检索「如何配置模型」）
    ├── 术语表.json      # JSON 格式演示，按顶层 key 切分（可检索「MiniC 是什么」）
    └── 常见问题.html    # HTML 格式演示（可检索「Bash 需要审批吗 / 如何配置模型」）
```

## 快速开始

前提：已按根目录 [README](../README.md) 完成安装并配置好 API Key。

```powershell
cd MiniC
minic ingest .\examples\knowledge-base
minic query "MiniC 的默认模型是什么" --top-k 5
```

> 注意：默认入库扩展名为 `.md` / `.txt` / `.html` / `.pdf`，`.json` 默认不入库。要让 `术语表.json` 入库，需在入库时显式指定扩展名（见根目录 README「RAG」章节）。

交互式问答（首次会自动入库该目录）：

```powershell
minic chat --path .\examples\knowledge-base
```

进入会话后可以这样提问：

1. 「MiniC 是什么」
2. 「有哪些只读工具」
3. 「Bash 需要审批吗」（回答会引用 `工具与审批.md` 或 `常见问题.html` 的来源）
4. 「如何配置模型」（回答会引用 `使用示例.txt` 或 `常见问题.html` 的来源）

## 说明

- 示例内容与 MiniC 项目相关，方便验证检索与来源展示。
- Markdown、TXT、JSON、HTML 四种文本格式均可用同样方式入库与检索；PDF 同样支持，但为二进制格式，本轮不提供样例文件。
- `.json` 默认不入库，需显式指定扩展名才会入库；其余默认入库扩展名为 `.md` / `.txt` / `.html` / `.pdf`。
- 替换为自己的知识库文件夹即可用于真实场景；Obsidian 知识库同样支持。
