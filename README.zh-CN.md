![Paper Workflow Orchestrator Banner](assets/banner.png)

# Paper Workflow Orchestrator

**面向 Codex 的科研到论文工作流控制器。**

**简体中文** | [English](README.md)

> 将研究问题、证据、实验和写作组织在同一套可审计的流程中。

![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)
![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB.svg)
![Codex Skill](https://img.shields.io/badge/Codex-Skill-8A2BE2.svg)
![核心依赖](https://img.shields.io/badge/Core%20dependencies-Python%20stdlib-success.svg)

**工作流版本：v1.0**

## 概述

Paper Workflow Orchestrator 是一个 Codex skill，用于组织完整的科研到论文流程。它把研究想法、文献、研究设计、实验、写作、完整性审计、同行评审和修订连接起来，并为每个阶段保留确认记录和证据。

主模型担任主编，负责路由、证据账本、提示词设计、冲突解决和最终合稿。Codex 内部子代理只执行边界清晰、可以独立审查的任务。每个阶段结束前都会更新进度，并向用户提供 2 至 5 个下一步选项，其中一个标记为**推荐**。未经用户确认，流程不会静默进入下一阶段。

## 核心特性

每条特性都对应本仓库中的具体组件：

| 特性 | 实现组件 |
|---|---|
| 13 阶段闸门式工作流，每阶段均设用户确认闸门 | `SKILL.md`、`references/stage-contracts.md` |
| 四种入口模式：想法引导、草稿审计、写作/修订、实验 | `SKILL.md` |
| 持久、并发安全的只追加进度记忆与证据账本（v1.0） | `scripts/progress_manager.py`、`references/progress-schema.md` |
| 关键有效性阻断：冻结摘要、评审与定稿 | `references/progress-schema.md` |
| 边界清晰的子代理委派：阶段上下文包与派发收据 | `references/stage-contracts.md` |
| 作者引导终稿编辑：受保护收据验证通过方可应用 | `references/final-editor-integration.md`、`scripts/final_edit_receipt_validator.py` |
| 内置配套 skill：修订 / 审计 / 学习三种模式的作者声音终稿编辑 | `companion-skills/academic-manuscript-final-editor/SKILL.md` |
| 只读全文风格扫描：四级严重度与 CI 退出码 | `companion-skills/academic-manuscript-final-editor/scripts/scan_manuscript_style.py` |
| 全稿修订的作者风格规则与双语同步参考 | `companion-skills/academic-manuscript-final-editor/references/author-style-rules.md` |
| 格式安全的 humanizer 适配器：契约缺失即 fail-closed | `scripts/humanizer_preflight.py`、`references/humanizer-adapter.md` |
| 自主实验启动前必须通过实验合同验证 | `scripts/experiment_contract_validator.py` |
| 论文结构闸门：必需章节与顺序校验 | `scripts/paper_section_validator.py`、`references/paper-section-contract.md` |
| 科研图件路由：主张绑定收据与 fail-closed 验证 | `references/scientific-visualization-integration.md`、`scripts/figure_contract_validator.py` |
| core、standard、full 三种配置的固定版本一键安装 | `dependencies.lock.json`、`scripts/install_workflow.py` |
| 编排器核心脚本仅使用 Python 标准库 | 全部 `scripts/*.py` |

## 工作流阶段

orchestrator 每个阶段只路由一个主下游 skill。每个阶段产出必需的制品，并以用户确认闸门结束。

1. **入口诊断：** 诊断输入，确认入口模式和约束
2. **方向探索：** 探索 3 至 5 个候选研究方向
3. **文献发现：** 发现并分拣已验证的来源
4. **题目筛选：** 判断 gap、贡献和可行性，然后锁定研究问题
5. **研究设计：** 制定可证伪的研究设计与实验矩阵
6. **架构冻结：** 冻结论文结构、章节画像和大纲
7. **协作写作：** 由边界清晰的内部代理撰写章节，再由主模型合稿
8. **完整性审计：** 审计引用、数字、主张、泄漏和可复现性
9. **评审与实质性修订：** 进行同行评审模拟，建立修订矩阵并修订科学内容
10. **作者引导终稿编辑：** 根据作者反馈学习，扫描全文同类问题并验证受保护内容
11. **语言自然化：** 可选的格式安全 humanizer 处理与主张/证据 diff
12. **终稿编辑复核：** naturalization 后只读检查作者声音
13. **终稿交付：** 完成最终完整性审计、渲染和交付

每个闸门处，用户会收到进度快照、已完成工作、剩余风险、2 至 5 个下一步选项，以及继续所需的明确确认。一个选项会标记为**推荐**。摘要只能在正文、结果、解释和结论稳定后撰写；结论始终是必需部分。

## 目录结构

```
paper-workflow-orchestrator/
├── SKILL.md                          # orchestrator skill 定义与路由
├── CHANGELOG.md                      # 版本变更记录
├── agents/
│   └── openai.yaml                   # agent 接口声明
├── assets/                           # Logo 与 README 横幅
├── references/
│   ├── paper-section-contract.md     # 标题/摘要/方法/结果/结论契约
│   ├── progress-schema.md            # 进度记忆 v1.0 schema 与错误规则
│   ├── stage-contracts.md            # 阶段表、委派与验收谓词
│   ├── scientific-visualization-integration.md # 图件路由与收据契约
│   ├── final-editor-integration.md   # 作者引导终稿编辑握手协议
│   └── humanizer-adapter.md          # 格式安全 humanizer 适配器协议
├── scripts/
│   ├── progress_manager.py           # 进度初始化/验证/迁移/记录/恢复
│   ├── install_workflow.py            # 跨平台固定版本 Skill 安装器
│   ├── figure_contract_validator.py   # 图件来源与输出验证器
│   ├── humanizer_preflight.py        # humanizer 预检（fail-closed）
│   ├── paper_section_validator.py    # 章节顺序与必需章节检查
│   ├── final_edit_receipt_validator.py # 终稿编辑受保护收据验证器
│   └── experiment_contract_validator.py  # 有界实验合同验证器
├── dependencies.lock.json             # 固定 GitHub 来源与安装配置
├── tests/                             # 零依赖工作流与安装器测试
└── companion-skills/
    ├── research-skill-router/         # 内置路由 skill
    └── academic-manuscript-final-editor/  # 终稿编辑阶段的配套 skill
```

## 安装

在仓库根目录执行下面的命令即可安装默认配置：

```bash
python scripts/install_workflow.py
```

Windows 可使用 `py -3` 或 `python`，具体取决于本机的 Python 启动器；macOS 和
Linux 使用 `python3`。`core` 配置以及仅安装仓库内置 skill 的手动方式可以
离线运行。`standard` 和 `full` 会通过 HTTPS 从 GitHub 下载固定提交，因此这
两种配置需要在安装时访问 GitHub。

默认的 `standard` 配置会安装 orchestrator、router、终稿编辑器、研究阶段、
写作与审稿 skill、humanizer 以及 `scientific-visualization`。`--profile core`
只安装仓库内置工作流；`--profile full` 还会安装受明确授权约束的
`autoresearch` 和 ARA 审查器。安装不等于同时加载，路由器仍然保证每个阶段
只选择一个主 skill。

安装器使用上游公开名称 `academic-paper`。如果宿主已经提供
`academic-research-suite`，仍可将其作为通用论文 skill 的兼容别名使用；安装
器不会重复安装同名副本。

常用选项：

```bash
python scripts/install_workflow.py --profile standard --dry-run
python scripts/install_workflow.py --profile standard --verify
python scripts/install_workflow.py --profile standard --update
# 有意缩小已安装配置时：
python scripts/install_workflow.py --profile core --update --prune
```

安装器从 `dependencies.lock.json` 指定的固定 Git 提交下载外部 skill。它会拒绝
不安全的压缩包路径和符号链接，只把经过校验的相对目录别名物化为普通目录，
并阻止覆盖已被修改的非托管目录。安装过程使用临时目录，完成后写入
`.paper-workflow-install.json` 验证收据。安装器只处理 Skill 文件，不会静默安装
Python、`uv`、Chrome/Chromium 或绘图库。

缩小已安装配置默认会阻断。只有在明确需要移除配置外的托管 skill 时才使用
`--prune`，安装器会先创建备份。

如需手动/离线安装仓库内置 skill：

**Windows PowerShell**（在仓库根目录执行）：

```powershell
$skillsHome = if ($env:CODEX_HOME) { Join-Path $env:CODEX_HOME 'skills' } else { Join-Path ([Environment]::GetFolderPath('UserProfile')) '.codex\skills' }
$main = Join-Path $skillsHome 'paper-workflow-orchestrator'
New-Item -ItemType Directory -Force -Path $main | Out-Null
Copy-Item -Recurse -Force SKILL.md, agents, assets, references, scripts $main
Copy-Item -Force LICENSE (Join-Path $main 'LICENSE')
$finalEditor = Join-Path $skillsHome 'academic-manuscript-final-editor'
$router = Join-Path $skillsHome 'research-skill-router'
New-Item -ItemType Directory -Force -Path $finalEditor, $router | Out-Null
Copy-Item -Recurse -Force companion-skills\academic-manuscript-final-editor\* $finalEditor
Copy-Item -Recurse -Force companion-skills\research-skill-router\* $router
```

**macOS / Linux**（在仓库根目录执行）：

```bash
SKILLS_HOME="${CODEX_HOME:-$HOME/.codex}/skills"
mkdir -p "$SKILLS_HOME/paper-workflow-orchestrator"
cp -R SKILL.md agents assets references scripts "$SKILLS_HOME/paper-workflow-orchestrator/"
cp -f LICENSE "$SKILLS_HOME/paper-workflow-orchestrator/LICENSE"
mkdir -p "$SKILLS_HOME/academic-manuscript-final-editor" "$SKILLS_HOME/research-skill-router"
cp -R companion-skills/academic-manuscript-final-editor/. "$SKILLS_HOME/academic-manuscript-final-editor/"
cp -R companion-skills/research-skill-router/. "$SKILLS_HOME/research-skill-router/"
```

安装后重新打开 Codex，或重新加载 skills 列表。

科研图件路由使用来自 [K-Dense scientific-agent-skills](https://github.com/K-Dense-AI/scientific-agent-skills/tree/36d8f13a1e754618794bf42f417884940077b4ae/skills/scientific-visualization)
的固定版本 `scientific-visualization` 子 skill。其示例需要 Python 3.11+、
`uv` 和所选绘图库。这些是运行时前提，不是安装器的隐藏操作。作者引导终稿
编辑阶段需要 `academic-manuscript-final-editor` 2.1.0 或更高版本，并在 YAML
frontmatter 中声明 `capability_schema: final-editor-v1`。

### 固定的第三方来源

外部 skill 只会按 [`dependencies.lock.json`](dependencies.lock.json) 中记录的
固定提交下载，安装器不会静默跟随可变分支。清单同时记录每个条目的上游许可证：
K-Dense 科研绘图（MIT）、research-hub（MIT）、Orchestra AI Research Skills
（MIT）、humanizer（MIT）以及 Academic Research Skills（CC BY-NC 4.0）。固定
仓库中的 `clarify-research-idea` 没有声明许可证，重新分发前请审阅其条款。安装
skill 不会授予超出适用上游许可证的权利。

## 使用方式

使用下面的触发语句启动完整工作流：

```
Use paper-workflow-orchestrator to run a gated, evidence-tracked research-to-paper workflow.
```

该 skill 是工作流控制器，不是单一任务工具。如果只需要文献矩阵、研究设计、论文润色或引用审计，应让 `research-skill-router` 选择更窄的专用 skill，避免加载完整流程。

编排器和验证器只使用 Python 标准库，支持 Python 3.10+。科研图件执行路径
遵循上游 Skill 的 Python 3.11+ 和 `uv` 要求。

## 安全边界

- `autoresearch` 不会自动加载，也不会在缺少明确、完整且通过验证的合同
  时启动无人值守实验。
- 子代理不能改变研究方向、修改最终稿、直接写入 `progress.md`，也不能编造
  引用、数字或实验结果。
- humanizer 必须经过格式适配器、保护清单、主张/证据 diff、完整性收据和回滚
  目标等检查。
- 终稿编辑器只在实质性修订和完整性检查通过后加载。它会生成候选稿和绑定的
  回滚收据，不会静默覆盖规范原稿。
- DOCX、PDF 和 LaTeX 需要对应的格式适配器。解析能力不足时，流程保持
  `blocked`。
- 关键有效性问题不能通过改写摘要、结论或语言风格来掩盖。

## 贡献

欢迎贡献。请保持脚本零依赖，只使用 Python 标准库，并说明每项变更的验证
方式。请勿提交个人论文、`.research/`、`.paper/`、实验数据、凭据或本地缓存。

## 许可证

本项目基于 [MIT 许可证](LICENSE) 开源。
