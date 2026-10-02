![Paper Workflow Orchestrator Banner](assets/banner.png)

# Paper Workflow Orchestrator

**帮助您将任何一个模糊的想法落地为顶刊级别的论文。**

**简体中文** | [English](README.md)

![Version: v1.1.0](https://img.shields.io/badge/version-v1.1.0-2EA44F.svg)
![Default workflow: v1.0](https://img.shields.io/badge/default%20workflow-v1.0-5271C4.svg)
![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)
![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB.svg)
![Codex Skill](https://img.shields.io/badge/Codex-Skill-8A2BE2.svg)
![核心依赖](https://img.shields.io/badge/Core%20dependencies-Python%20stdlib-success.svg)

Paper Workflow Orchestrator 是一套以 Codex skill 形式提供的科研工作流。它根据研究目标和项目阶段选择合适的专业 skill，并将选题、文献、研究设计、实验、写作、审查与终稿交付组织成一条连续、可追踪的论文生产流程。

它适合需要跨文件、跨会话持续推进的论文项目。你可以从一个模糊想法、已有草稿、已确定的研究方案或实验任务开始；工作流会识别当前阶段，调用相应能力，保存关键依据，并在重要决定处等待确认。

`v1.1.0` 在保留官方 `paper-workflow-orchestrator-v1.0` 默认流程的基础上，加入了可选的本地图形编排器和确认产物管理能力。

## 从这里开始

| 你想做什么 | 下一步 |
|---|---|
| 使用默认流程推进论文 | [安装并开始使用](#快速安装)，然后在 Codex 中描述你的研究目标 |
| 自己安排阶段和 Skill | [打开图形编辑器](#自定义工作流编排)，跟着[三阶段入门示例](docs/workflow-studio-guide.md)建立第一个流程 |
| 理解代码或参与开发 | 阅读[开发指南](DEVELOPMENT_GUIDE.md)；全部文档见[文档导航](docs/README.md) |

初次使用建议从默认流程开始。需要调整阶段、替换 Skill 或安排分支时，再进入 Workflow Studio。

## 核心设计：Router Skills

完整论文项目会用到多种专业能力：研究方向探索、文献检索、选题评估、研究设计、实验、论文写作、科研绘图、引用审查和终稿编辑。Paper Workflow Orchestrator 在这些能力之上加入两层 Router Skills，让使用者可以直接从研究目标出发，由 Router 处理每个 skill 的选择范围和调用顺序。

- **`research-skill-router` 负责选择能力**：理解当前任务与研究阶段，从可用能力中选择一个最合适的主 skill。
- **`paper-workflow-orchestrator` 负责组织过程**：接管跨阶段项目，依次调用专业 skill，衔接阶段成果，并维护整个项目的进度、证据与决策。

```mermaid
flowchart TD
    U["用户描述研究目标"] --> R["Research Skill Router"]

    R -->|"任务明确"| S["选择一个专业 skill"]
    R -->|"跨越多个阶段"| O["Paper Workflow Orchestrator"]
    R -->|"目标存在歧义"| Q["确认当前目标与阶段"]
    Q --> R

    S --> T["完成当前研究任务"]

    O --> C["识别当前阶段"]
    C --> D["调用该阶段的专业 skill"]
    D --> V["检查并记录阶段成果"]
    V --> G{"确认下一步"}

    G -->|"继续"| C
    G -->|"补充或调整"| D
    G -->|"完成"| F["终稿与项目完成"]
```

### Router Skills 带来的改变

**用研究目标启动任务**

你可以直接提出“判断这个选题是否值得研究”“比较这组论文的方法差异”或“从现有草稿继续完成论文”。Router 会把请求映射到合适的能力，并在目标不够明确时先确认任务范围。

**一次聚焦一个主要能力**

方向探索、选题评估和研究设计彼此相关，但解决的问题不同。Router 根据当前阶段选择一个主 skill，使任务上下文保持清晰，减少相邻能力之间的指令干扰。

**保留专业 skill 的独立性**

文献检索、论文写作、科研绘图和完整性审查分别由专用 skill 处理。每项能力都可以独立使用、升级或替换，同时通过 Router 接入统一工作流。

**让阶段成果自然衔接**

文献证据影响选题，研究设计约束实验，实验结果决定论文主张，审查意见可能要求返回前面的阶段补充证据。Orchestrator 负责这些衔接关系，使上一阶段的结果成为下一阶段的有效输入。

**同时支持局部任务和长期项目**

边界明确的请求可以直接进入专业 skill；需要完整论文流程、持续进度记录或跨阶段协作的任务则进入 Orchestrator。同一套能力体系可以覆盖一次性研究任务和长期论文项目。

## 进度与当前确认产物

长期项目将两类信息分开保存：**进度记录下一步做什么，确认目录记录现在采用哪一版成果**。工作稿可以继续修改；经过检查并确认采用的新版本会自动归档到对应的产物子目录，更新当前版本，同时保留原工作文件和历史确认版本。

你可以直接说：“将这份检查通过的文献矩阵确认为当前版本”，或者“从当前确认的稿件继续修改”。后续任务按确认记录读取文件，避免把旧稿或尚未确认的新草稿当成当前成果。

打开 `artifacts/INDEX.md` 查看当前成果，`artifacts/current/` 按类型和用途分类。多文件成果可以将正文、插图和参考文献一起归档。使用说明见[进度与确认产物指南](docs/progress-and-artifacts-guide.md)。

### 项目状态保存在哪里

仓库存放工作流工具；真正的论文项目目录保存你在使用过程中产生的持续状态：

```text
论文项目/
├── 你的工作文件/                       # 继续修改的草稿、数据、代码和笔记
├── artifacts/
│   ├── INDEX.md                        # 当前确认成果的可读索引
│   └── current/<类型>/<产物标识>/       # 最新确认版本的展示副本
└── .research/
    ├── progress.md                     # 官方阶段、下一步、风险与继续工作所需信息
    ├── confirmed-artifacts/
    │   ├── catalog.json                # 当前版本的权威选择
    │   └── versions/                   # 不可变的确认快照
    └── custom-workflow/                # 自定义计划、运行状态、事件与回执
```

`progress.md` 回答“下一步做什么”；确认目录回答“应该使用哪一版成果”。`current/` 便于阅读和交付，真正用于校验的来源仍由确认目录指向的快照决定。

## 工作流程

工作流根据项目实际情况选择起点和路径。下面的流程图将内部阶段归纳为五个使用者可理解的部分，并展示实验、审查和修订形成的反馈回路。

```mermaid
flowchart TD
    A["明确目标与研究方向"] --> B["检索文献并评估选题"]
    B --> C["制定研究方案"]
    C --> D{"是否需要实验？"}

    D -->|"是"| E["执行实验并检查结果"]
    D -->|"否"| F["确定论文结构"]
    E --> F

    F --> G["协作写作与图表制作"]
    G --> H["引用与完整性审查"]
    H --> I["同行评审模拟与实质修订"]

    I -->|"需要补充证据"| C
    I -->|"通过"| J["终稿编辑"]

    J --> K{"是否需要语言自然化？"}
    K -->|"是"| L["语言优化"]
    K -->|"否"| M["终稿复核与交付"]
    L --> M
```

选题、研究方案、实验范围和终稿采纳等关键决定由用户确认。具体阶段、输入输出和验收规则记录在 [`references/stage-contracts.md`](references/stage-contracts.md) 中。

## 功能一览

下面按使用场景说明每项功能解决的具体问题，避免把不同能力都概括成“工作流支持”。

| 当你需要…… | 项目提供…… | 对应实现 |
|---|---|---|
| 把尚不明确的研究请求变成下一项具体任务 | `research-skill-router` 读取研究意图和当前阶段，只选择一个主要专业 skill 保持上下文聚焦 | `companion-skills/research-skill-router/` |
| 从研究想法或已有草稿推进到完整论文 | Orchestrator 按阶段组织任务，衔接已验收成果，并在关键决定处等待确认 | `SKILL.md`、`references/stage-contracts.md` |
| 隔了几次会话继续工作 | 紧凑进度摘要保存当前阶段、下一步、风险、规则和继续工作所需信息；事件记录保留审计线索 | `scripts/progress_manager.py`、`references/progress-schema.md` |
| 防止旧稿被误认为当前成果 | 明确确认后生成不可变版本，更新产物角色的当前指向，同时保留原工作文件继续编辑 | `scripts/artifact_manager.py`、`scripts/confirmed_artifacts.py`、`references/confirmed-artifacts.md` |
| 判断实验是否已经具备启动条件 | 实验合同验证目标、资源、评估方案和停止条件，再交给后续执行 | `scripts/experiment_contract_validator.py` |
| 在深入审查前检查论文结构 | 章节验证器解析 Markdown 标题和代码围栏，报告缺失、重复或顺序不当的章节 | `scripts/paper_section_validator.py`、`references/paper-section-contract.md` |
| 让科研图件与证据保持绑定 | 图件合同记录数据来源、主张关系、结构关系和图片元数据，形成失败即阻断的回执 | `scripts/figure_contract_validator.py`、`references/scientific-visualization-integration.md` |
| 处理引用、数字和主张问题 | 完整性检查与同行评审模拟把发现转成可执行的实质修订计划 | `SKILL.md`、`references/stage-contracts.md` |
| 在不破坏受保护内容的前提下统一终稿 | 内置 Final Editor 扫描全文、维护编辑规则，并校验受保护内容回执 | `companion-skills/academic-manuscript-final-editor/` |
| 科学内容稳定后再改善机械化表达 | Humanizer Preflight 对照受保护内容检查差异，并保存内容变化证据 | `scripts/humanizer_preflight.py`、`references/humanizer-adapter.md` |
| 不编辑 JSON 就安排阶段 | Workflow Studio 提供本地图形画布，可配置任务、验证器、条件、并行、汇合、输入和输出 | `scripts/workflow_studio.py`、`studio/`、`assets/workflow-studio/` |
| 在另一台机器复现同一套环境 | 锁定清单记录来源、提交、许可证、备份和安装回执，支持 `core`、`standard`、`full` 三种配置 | `dependencies.lock.json`、`scripts/install_workflow.py` |

编排器核心脚本使用 Python 标准库，适用于 Python 3.10 及以上版本。

## 适用场景

- 只有初步研究想法，需要逐步形成可验证的研究问题。
- 已有一组论文，需要比较方法、数据、结论与局限。
- 已确定选题，需要完成研究设计、实验计划和论文结构。
- 已有草稿，需要检查引用、主张、图表和论证完整性。
- 需要组织多轮写作、评审、修订与终稿编辑。
- 需要在多个 Codex 会话之间延续项目状态。
- 只知道想完成的研究任务，希望由 Router 选择合适的专业 skill。

## 快速安装

克隆仓库并运行安装器：

```bash
git clone https://github.com/shuohui-air-technology/codex-paper-workflow.git
cd codex-paper-workflow
python3 scripts/install_workflow.py
```

如果 `python` 已指向 Python 3，也可以使用：

```bash
python scripts/install_workflow.py
```

Windows 可以使用：

```powershell
py -3 scripts/install_workflow.py
```

默认安装 `standard` 配置。安装完成后，重新打开 Codex，或重新加载 skills 列表。

接着在 Codex 中打开存放论文材料的项目目录，发送：

```text
使用 paper-workflow-orchestrator，从当前研究材料开始运行完整论文工作流。先说明当前处于哪个阶段、还需要我提供什么，再引导我完成下一步。
```

以后在同一项目继续对话即可延续进度。[更多使用示例](#使用方式)涵盖已有草稿、文献比较和实验任务。

如果需要包含自主实验和 ARA 审查 skill 的完整固定清单，请明确安装 `full` 配置：

```bash
python3 scripts/install_workflow.py --profile full
```

### 安装配置

| 配置 | 内容 | 适合场景 |
|---|---|---|
| `core` | Orchestrator、Research Skill Router 和仓库内置终稿编辑能力 | 离线安装、最小工作流 |
| `standard` | `core` 加上研究、写作、审稿、语言自然化和科研绘图能力 | 大多数论文项目 |
| `full` | `standard` 加上自主实验与 ARA 审查能力 | 已明确实验合同与高级审查需求的项目 |

常用命令：

```bash
# 预览 standard 配置将执行的操作
python3 scripts/install_workflow.py --profile standard --dry-run

# 验证当前安装
python3 scripts/install_workflow.py --profile standard --verify

# 更新到锁定清单中的版本
python3 scripts/install_workflow.py --profile standard --update

# 将现有配置缩小为 core，并备份后移除多余的托管 skill
python3 scripts/install_workflow.py --profile core --update --prune
```

`dependencies.lock.json` 记录外部 skill 的仓库、目录、固定提交和许可证。安装器根据该清单完成下载、校验和更新，并在本地生成 `.paper-workflow-install.json` 安装记录。

`core` 配置支持离线安装。`standard` 和 `full` 通过 HTTPS 获取清单中指定的外部 skill，需要访问 GitHub。

科研图件功能使用 Python 3.11+、`uv` 和所选绘图库。完成 skill 安装后，请根据绘图任务配置相应的 Python 运行环境。

## 自定义工作流编排

官方 v1.0 流程仍是默认流程，安装后即可直接使用。希望自行安排阶段的进阶用户，可以在论文项目目录中启动图形界面。第一次使用可跟着[Workflow Studio 入门指南](docs/workflow-studio-guide.md)，完成一个“整理文献 → 撰写引言 → 审查草稿”的三阶段流程。

在终端切换到你的论文项目目录后，macOS 和 Linux 使用以下命令：

```bash
python3 "${CODEX_HOME:-$HOME/.codex}/skills/paper-workflow-orchestrator/scripts/workflow_studio.py" --project .
```

`--project .` 指当前论文项目目录。浏览器会自动打开；使用期间请保持终端运行。

<details>
<summary>Windows 与自定义安装目录</summary>

Windows PowerShell 用户可运行：

```powershell
$skillsHome = if ($env:CODEX_HOME) { Join-Path $env:CODEX_HOME 'skills' } else { Join-Path $HOME '.codex\skills' }
py -3 (Join-Path $skillsHome 'paper-workflow-orchestrator\scripts\workflow_studio.py') --project .
```

如果安装到了自定义 Codex 主目录，请在启动 Studio 的当前终端中导出同一个 `CODEX_HOME`，这样 Studio 进程也能读取它；安装时设置的值不会自动带入之后的新终端。未设置时，命令会从 `~/.codex` 查找。在 macOS 或 Linux 的同一终端中先设置路径，再启动 Studio：

```bash
export CODEX_HOME="/你的 Codex 主目录"
python3 "$CODEX_HOME/skills/paper-workflow-orchestrator/scripts/workflow_studio.py" --project .
```

</details>

你可以从官方流程创建副本，也可以新建空白流程；随后在画布上排列阶段、选择已安装的 Skill、连接阶段，并加入条件、并行和汇合步骤。检查流程并查看风险提示后，才可以启用自定义版本。整个过程无需编辑 JSON 文件；Skill 的安装仍由安装器完成。运行界面需要 Python 3.10 或以上版本，不需要 Node.js。

按照“编辑阶段 → 验证流程 → 保存或启用”的顺序操作。保存草稿会保留修改；启用后，Codex 才按这份自定义流程推进。草稿与当前运行版本的区别、产物如何衔接，以及常见错误的处理方法，都在[入门指南](docs/workflow-studio-guide.md)中。

从官方流程复制时，系统会加入四个需要等待明确确认的关键关卡：选题锁定、研究设计确认、实验启动授权和终稿交付。关卡保留原有的产物连线，但在你确认对应的上游执行版本前，下游阶段不会变为可领取状态。提出退回修改会保留旧回执，并将上游结果标记为过时；检查后使用管理器的 `rerun-stale` 命令显式重开。

新启用的自定义流程还会固定所选 Skill 的根目录，并使用 `node-result-v2` 来源声明。任务提交结果时，需要列出实际使用的项目相对路径及其 SHA-256 哈希；管理器会与领取时冻结的输入逐项核对，并写入 `stage-receipt-v3`，供恢复时重放校验。这是可审计的来源声明；没有操作系统级的文件访问隔离时，无法证明执行者没有读取未声明的文件。

Studio 显示启用成功后，回到在同一个论文项目目录中打开的 Codex 对话，发送：

```text
使用 paper-workflow-orchestrator 继续我在这个项目中已启用的自定义工作流。先检查当前模式和可执行阶段，告诉我下一阶段需要提供什么，再引导我按流程推进。
```

![Workflow Studio 图形化工作流编排界面](assets/workflow-studio.png)

## 安装补充

通常使用上面的安装器即可。需要自行复制仓库内置 Skill 时，可展开下面的命令；手动复制不会生成安装器回执，Studio 会将相应 Skill 显示为本地版本。

<details>
<summary>手动安装仓库内置 skills</summary>

### Windows PowerShell

在仓库根目录执行：

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

### macOS / Linux

在仓库根目录执行：

```bash
SKILLS_HOME="${CODEX_HOME:-$HOME/.codex}/skills"
mkdir -p "$SKILLS_HOME/paper-workflow-orchestrator"
cp -R SKILL.md agents assets references scripts "$SKILLS_HOME/paper-workflow-orchestrator/"
cp -f LICENSE "$SKILLS_HOME/paper-workflow-orchestrator/LICENSE"

mkdir -p "$SKILLS_HOME/academic-manuscript-final-editor" "$SKILLS_HOME/research-skill-router"
cp -R companion-skills/academic-manuscript-final-editor/. "$SKILLS_HOME/academic-manuscript-final-editor/"
cp -R companion-skills/research-skill-router/. "$SKILLS_HOME/research-skill-router/"
```

</details>

## 使用方式

### 启动完整论文工作流

```text
使用 paper-workflow-orchestrator，从当前研究材料开始运行带阶段确认和进度记录的完整论文工作流。
```

### 从已有草稿开始

```text
使用 paper-workflow-orchestrator 审查这份草稿，识别当前阶段、证据缺口和下一步修订计划。
```

### 让 Router 选择专业 skill

```text
使用 research-skill-router 判断这个研究任务应该交给哪个 skill：比较这些论文的方法、数据和主要结论。
```

### 启动范围明确的实验

```text
使用 paper-workflow-orchestrator 为这个实验建立合同，确认目标、资源、评估方法和停止条件后再开始执行。
```

单一、明确的研究任务由 `research-skill-router` 选择专业能力；需要跨阶段推进或项目进度记录的任务由 `paper-workflow-orchestrator` 负责。

## 项目结构

```text
paper-workflow-orchestrator/
├── SKILL.md                              # Orchestrator 定义与阶段路由
├── agents/
│   └── openai.yaml                       # Agent 接口声明
├── assets/                               # Logo、README 横幅、界面截图与离线 Studio 文件
│   ├── workflow-studio.png               # Workflow Studio 界面截图
│   └── workflow-studio/                  # 预构建运行文件，使用者无需安装 Node.js
├── references/
│   ├── paper-section-contract.md         # 论文章节契约
│   ├── progress-schema.md                # 项目进度与证据记录格式
│   ├── confirmed-artifacts.md            # 确认产物的索引、版本与恢复
│   ├── stage-contracts.md                # 阶段、委派与验收规则
│   ├── scientific-visualization-integration.md
│   ├── final-editor-integration.md
│   └── humanizer-adapter.md
├── scripts/
│   ├── progress_manager.py               # 进度初始化、迁移、记录与恢复
│   ├── artifact_manager.py               # 确认产物的查询、采纳、撤回与修复
│   ├── confirmed_artifacts.py            # 版本快照与当前成果目录管理
│   ├── install_workflow.py               # 跨平台固定版本安装器
│   ├── workflow_studio.py                # 图形编辑器启动入口
│   ├── workflow_manager.py               # 自定义流程执行与状态接口
│   ├── workflow_engine/                  # 校验、调度、存储与本地界面服务
│   ├── experiment_contract_validator.py  # 实验合同验证
│   ├── figure_contract_validator.py      # 科研图件验证
│   ├── paper_section_validator.py        # 论文章节验证
│   ├── final_edit_receipt_validator.py   # 终稿编辑结果验证
│   └── humanizer_preflight.py            # 语言自然化预检
├── dependencies.lock.json                # 外部 skill 来源与版本清单
├── studio/                               # Workflow Studio 前端源码
├── docs/                                 # 用户教程、开发记录与文档导航
├── tests/                                 # 工作流与安装器测试
└── companion-skills/
    ├── research-skill-router/             # 科研任务路由入口
    └── academic-manuscript-final-editor/  # 终稿编辑
```

## 第三方 skill 来源

外部 skill 按照 [`dependencies.lock.json`](dependencies.lock.json) 中记录的固定提交获取。清单同时记录每个上游项目的许可证：

- K-Dense 科研绘图：MIT
- research-hub：MIT
- Orchestra AI Research Skills：MIT
- humanizer：MIT
- Academic Research Skills：CC BY-NC 4.0

`clarify-research-idea` 的固定上游仓库当前未声明许可证，使用或重新分发时应单独核对其授权条款。

安装 skill 时，各上游项目仍适用其原始许可证。

## 贡献

欢迎提交问题和改进建议。代码贡献应保持编排器核心脚本仅使用 Python 标准库，并在 Pull Request 中说明验证方式。

准备参与代码贡献？可先阅读[开发指南](DEVELOPMENT_GUIDE.md)，了解仓库结构、测试方法，以及 Python 运行时与 Workflow Studio 前端之间的关系。

用户操作说明与实现合同分别收录在[文档导航](docs/README.md)中，可按目标选择阅读路径。

个人论文、`.research/`、`.paper/`、实验数据、凭据和本机缓存应保留在本地，并由 `.gitignore` 管理。

## 许可证

本项目基于 [MIT 许可证](LICENSE) 开源。
