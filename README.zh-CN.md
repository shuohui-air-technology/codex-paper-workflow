![Paper Workflow Orchestrator Banner](assets/banner.png)

# Paper Workflow Orchestrator

**从模糊想法到高质量论文的完整工作流控制器。**

**简体中文** | [English](README.md)

> 帮助您将任何一个模糊的想法落地为顶刊级别的论文。

![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)
![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB.svg)
![Codex Skill](https://img.shields.io/badge/Codex-Skill-8A2BE2.svg)
![Dependencies](https://img.shields.io/badge/Dependencies-None-success.svg)

## 概述

Paper Workflow Orchestrator 是一个 Codex skill，将完整的科研到论文流程 —— 从研究想法、文献、研究设计，到实验、写作、完整性审计、同行评审、修订和 AI 交接 —— 组织成带确认闸门的阶段化、证据可追踪流程。

主模型担任主编角色：负责路由、证据账本、提示词设计、冲突解决和最终合稿。Codex 内部子代理只执行边界清晰、可独立审查的任务。每个阶段结束前都会更新进度，并向用户提供 2–5 个下一步选项（其中一个标记为推荐项）；未经用户确认不会静默进入下一阶段。

## 核心特性

每条特性都对应本仓库中的具体组件：

| 特性 | 实现组件 |
|---|---|
| 13 阶段闸门式工作流，每阶段均设用户确认闸门 | `SKILL.md`、`references/stage-contracts.md` |
| 四种入口模式：想法引导、草稿审计、写作/修订、实验 | `SKILL.md` |
| 持久、并发安全的只追加进度记忆与证据账本（v0.4） | `scripts/progress_manager.py`、`references/progress-schema.md` |
| 关键有效性阻断：冻结摘要、评审与定稿 | `references/progress-schema.md` |
| 边界清晰的子代理委派：阶段上下文包与派发收据 | `references/stage-contracts.md` |
| 作者引导终稿编辑：受保护收据验证通过方可应用 | `references/final-editor-integration.md`、`scripts/final_edit_receipt_validator.py` |
| 内置配套 skill：修订 / 审计 / 学习三种模式的作者声音终稿编辑 | `companion-skills/academic-manuscript-final-editor/SKILL.md` |
| 只读全文风格扫描：四级严重度与 CI 退出码 | `companion-skills/academic-manuscript-final-editor/scripts/scan_manuscript_style.py` |
| 全稿修订的作者风格规则与双语同步参考 | `companion-skills/academic-manuscript-final-editor/references/author-style-rules.md` |
| 格式安全的 humanizer 适配器：契约缺失即 fail-closed | `scripts/humanizer_preflight.py`、`references/humanizer-adapter.md` |
| 自主实验启动前必须通过实验合同验证 | `scripts/experiment_contract_validator.py` |
| 论文结构闸门：必需章节与顺序校验 | `scripts/paper_section_validator.py`、`references/paper-section-contract.md` |
| 零运行时依赖（仅 Python 标准库） | 全部 `scripts/*.py` |

## 工作流阶段

orchestrator 每个阶段路由一个主下游 skill。每个阶段产出必需的制品，并以用户闸门结束。

1. **入口诊断** —— 诊断输入，确认入口模式和约束
2. **方向探索** —— 探索 3–5 个候选研究方向
3. **文献发现** —— 发现并分拣已验证的来源
4. **题目筛选** —— 研究 gap / 贡献 / 可行性判定与问题锁定
5. **研究设计** —— 可证伪的研究设计与实验矩阵
6. **架构冻结** —— 冻结论文结构、章节画像、大纲
7. **协作写作** —— 边界清晰的内部代理写章节；主模型合稿
8. **完整性审计** —— 审计引用、数字、主张、泄漏、可复现性
9. **评审与实质性修订** —— 同行评审模拟、修订矩阵和科学内容修订
10. **作者引导终稿编辑** —— 从作者反馈学习、扫描全文同类问题并验证受保护内容
11. **语言自然化** —— 可选的格式安全 humanizer 处理与主张/证据 diff
12. **终稿编辑复核** —— naturalization 后只读检查作者声音
13. **终稿交付** —— 最终完整性审计、渲染、交接卡片与交付

每个闸门处，用户会收到已完成的工作、进度快照、剩余风险、2–5 个下一步选项（一个标记为**推荐**），以及继续所需的明确确认。摘要只能在正文、结果、解释和结论稳定后撰写；结论始终是必需部分。

## 目录结构

```
paper-workflow-orchestrator/
├── SKILL.md                          # orchestrator skill 定义与路由
├── agents/
│   └── openai.yaml                   # agent 接口声明
├── assets/                           # Logo 与 README 横幅
├── references/
│   ├── paper-section-contract.md     # 标题/摘要/方法/结果/结论契约
│   ├── progress-schema.md            # 进度记忆 v0.4 schema 与错误规则
│   ├── stage-contracts.md            # 阶段表、委派与验收谓词
│   ├── final-editor-integration.md   # 作者引导终稿编辑握手协议
│   └── humanizer-adapter.md          # 格式安全 humanizer 适配器协议
├── scripts/
│   ├── progress_manager.py           # 进度初始化/验证/迁移/记录/恢复
│   ├── humanizer_preflight.py        # humanizer 预检（fail-closed）
│   ├── paper_section_validator.py    # 章节顺序与必需章节检查
│   ├── final_edit_receipt_validator.py # 终稿编辑受保护收据验证器
│   └── experiment_contract_validator.py  # 有界实验合同验证器
└── companion-skills/
    └── academic-manuscript-final-editor/  # 终稿编辑阶段的配套 skill
```

## 安装

本仓库包含两个 skill，需分别安装：

1. **`paper-workflow-orchestrator`** —— 主工作流控制器。将仓库根目录的 skill 文件复制到 Codex skills 目录（`CODEX_HOME/skills`，未设置 `CODEX_HOME` 时默认为 `~/.codex/skills`）。
2. **`academic-manuscript-final-editor`** —— 作者引导终稿编辑阶段使用的配套 skill。将 `companion-skills/academic-manuscript-final-editor/` 作为独立目录复制到同一个 skills 目录。

**Windows PowerShell**（在仓库根目录执行）：

```powershell
$skillsHome = if ($env:CODEX_HOME) { Join-Path $env:CODEX_HOME 'skills' } else { Join-Path ([Environment]::GetFolderPath('UserProfile')) '.codex\skills' }
$main = Join-Path $skillsHome 'paper-workflow-orchestrator'
New-Item -ItemType Directory -Force -Path $main | Out-Null
Copy-Item -Recurse -Force SKILL.md, agents, assets, references, scripts $main
Copy-Item -Recurse -Force companion-skills\academic-manuscript-final-editor (Join-Path $skillsHome 'academic-manuscript-final-editor')
```

**macOS / Linux**（在仓库根目录执行）：

```bash
SKILLS_HOME="${CODEX_HOME:-$HOME/.codex}/skills"
mkdir -p "$SKILLS_HOME/paper-workflow-orchestrator"
cp -R SKILL.md agents assets references scripts "$SKILLS_HOME/paper-workflow-orchestrator/"
cp -R companion-skills/academic-manuscript-final-editor "$SKILLS_HOME/"
```

安装后重新打开 Codex，或重新加载 skills 列表。

作者引导终稿编辑阶段需要配套的 `academic-manuscript-final-editor` skill，最低版本为 `2.1.0`，并且其 YAML frontmatter 必须声明 `capability_schema: final-editor-v1`；匹配版本已收录在 `companion-skills/` 中。若该 Skill 不可用或不兼容，orchestrator 会报告能力缺失，并询问是安装还是明确跳过该阶段，不会伪造终稿编辑收据。

## 使用方式

使用以下触发语句启动完整工作流：

```
Use paper-workflow-orchestrator to run a gated, evidence-tracked research-to-paper workflow.
```

该 skill 是工作流控制器，不是单一任务工具。当仅需文献矩阵、研究设计、论文润色或引用审计时，应让 `research-skill-router` 选择更窄的专用 skill，避免不必要地加载完整工作流。

所有脚本只使用 Python 标准库（Python 3.10+），无需安装任何第三方依赖。

## 安全边界

- `autoresearch` 不会被自动加载，也不会在缺乏明确、完整、已验证合同的情况下启动无人值守实验。
- 子代理不能改变研究方向、修改最终稿、直接写 `progress.md`，或编造引用、数字和实验结果。
- humanizer 不能绕过格式适配器、保护清单、主张/证据 diff、完整性收据或回滚目标。
- 终稿编辑器只在实质性修订和完整性检查通过后加载；它生成候选稿和绑定回滚收据，不会静默覆盖规范原稿。
- DOCX、PDF 和 LaTeX 需要对应的格式适配器；解析能力不足时流程保持 `blocked`。
- 关键有效性问题不能通过改写摘要、结论或语言风格来掩盖。

## 贡献

欢迎贡献。请保持所有脚本零依赖（仅 Python 标准库），并说明您如何验证了变更。请勿提交个人论文、`.research/`、`.paper/`、实验数据、凭据或本机生成的缓存文件。

## 许可证

本项目基于 [MIT 许可证](LICENSE) 开源。
