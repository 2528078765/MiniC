# Contributing to MiniC

感谢你愿意为 MiniC 贡献代码。本文档说明如何搭建环境、运行测试以及提交 Pull Request。

> English: contributions are welcome in either Chinese or English. Please keep
> the same code style used in the existing codebase (brief Chinese comments
> above key statements, module docstrings at the top of each file).

## 环境搭建

```powershell
cd MiniC
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
```

要求 Python 3.13+（开发环境为 3.14）。`pip install -e ".[dev]"` 会安装运行时依赖与 pytest 等开发依赖。

## 运行测试

提交代码前必须跑一遍全量测试，且确认**没有回归**：

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

- 测试使用 mock 模型、mock embedding、临时目录与 fixture，不依赖任何真实 API Key。
- 新增功能必须补充对应测试（核心服务测试用 `create_app` + `httpx` / TestClient，纯逻辑用普通函数测试）。
- 测试必须使用临时目录（`tmp_path`），**禁止**把数据写入真实 `~/.minic` 或仓库内 `.minic`，防止污染开发者本地数据。

## 代码风格

- 遵循现有代码风格：模块顶部写中文模块 docstring，关键语句上方用短中文注释说明意图，类与公共函数写简洁中文 docstring。
- 变量、函数、类命名遵循 PEP 8（`snake_case` / `CamelCase`）。
- 接口统一返回 `{"error": {"code", "message", "detail"}}` 错误格式；接口变更必须同步更新 README 的接口列表。
- 新增工具必须登记到 `src/minic/graph/tools.py` 的工具注册表，并同步扩展 CLI `minic tool` 的 `choices`。
- 新增配置项必须在 `src/minic/core/config.py` 中定义默认值，并在 `minic.example.json` 与 README 配置节同步。

## 提交 Pull Request

1. 从 `main` 拉取最新代码，新建一个功能分支（如 `feat/xxx`、`fix/xxx`）。
2. 完成修改并补充测试，本地跑通全量 `pytest`（结果必须保持 `172 passed` 或高于该基线）。
3. 提交信息使用简洁的中文或英文，例如 `G9: 新增 xxx 功能` / `fix: 修复 xxx`。
4. 创建 Pull Request，在描述中说明：改动目的、改动文件、测试结果（pytest 数字）、是否需要更新文档。
5. 若改动涉及接口或配置，请同步更新 `README.md` 与 `README.en.md`（两份需结构对齐）。

## 文档约定

- README 中英文同等优先级：`README.md` 与 `README.en.md` 章节一一对应。
- 新增功能请同步更新 `minic.example.json`（模板，不含 api_key）、SECURITY.md（如涉及安全策略）。

## 行为准则

- 不提交含 API Key、token 或任何敏感信息的文件。
- 不修改其他贡献者未完成的功能而不说明理由。
- 遇到不确定的设计取舍，先在 issue / PR 中讨论再实现。
