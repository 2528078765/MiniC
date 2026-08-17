# examples 示例

本目录包含一个最小的中文 Markdown 知识库，用于快速体验 MiniC 的知识库问答功能。

## 目录结构

```text
examples/
├── README.md
└── knowledge-base/
    ├── MiniC介绍.md    # 项目简介（可检索「MiniC 是什么」）
    ├── 使用示例.md      # 用法演示（可检索「如何入库 / 如何查询」）
    └── 工具与审批.md    # 工具与审批说明（可检索「Bash 需要审批吗」）
```

## 快速开始

前提：已按根目录 [README](../README.md) 完成安装并配置好 API Key。

```powershell
cd MiniC
minic ingest .\examples\knowledge-base
minic query "MiniC 的默认模型是什么" --top-k 5
```

交互式问答（首次会自动入库该目录）：

```powershell
minic chat --path .\examples\knowledge-base
```

进入会话后可以这样提问：

1. 「MiniC 是什么」
2. 「有哪些只读工具」
3. 「Bash 需要审批吗」（回答会引用 `工具与审批.md` 的来源）

## 说明

- 示例内容与 MiniC 项目相关，方便验证检索与来源展示。
- 替换为自己的 Markdown 文件夹（含 Obsidian 知识库）即可用于真实场景。
