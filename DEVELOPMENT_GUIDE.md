# Paper Workflow Orchestrator 开发入口

> 后续开发者或 Agent 应先完整阅读本文件，再修改任何代码、Skill 合同或公开文档。

## 1. 项目身份

- 项目名称：`paper-workflow-orchestrator`
- 当前正式版本：`1.0.0`
- 工作流版本：`paper-workflow-orchestrator-v1.0`
- 主要运行环境：Codex
- 核心脚本要求：Python 3.10+，仅使用 Python 标准库
- 上游仓库：`https://github.com/shuohui-air-technology/codex-paper-workflow`

本项目是一个研究到论文的工作流控制器。它负责阶段路由、用户确认闸门、证据与进度约束、子代理边界、完整性审计和终稿交付。它不是无人监管的论文生成器，也不保证论文被期刊接收。

## 2. 开始开发前的阅读顺序

请按以下顺序建立上下文：

1. `DEVELOPMENT_GUIDE.md`：开发规则、文件职责和验收门槛。
2. `README.md` 或 `README.zh-CN.md`：公开定位、功能和安装方式。
3. `CHANGELOG.md`：当前正式版本及已发布能力。
4. `SKILL.md`：完整工作流、路由、用户确认和安全边界。
5. `references/stage-contracts.md`：各阶段的输入、产物和验收条件。
6. `references/paper-section-contract.md`：论文各章节的写作契约。
7. `references/progress-schema.md`：进度状态、错误规则和迁移协议。
8. 与本次修改直接相关的其他 `references/*.md`。
9. 对应的 `scripts/*.py` 与 `tests/*.py`。
10. `dependencies.lock.json`：安装配置、固定提交和第三方许可状态。

不要为了建立上下文一次性加载全部下游 Skill。先确定修改涉及的阶段，再读取一个主 Skill 和至多一个用途明确的辅助 Skill。

## 3. 首轮检查

从仓库根目录执行：

```powershell
git status --short --branch
git log --oneline --decorate -5
python -B -m unittest discover -s tests -v
```

继续开发前必须确认：

- 工作树中的既有修改是否属于用户；不得覆盖或丢弃未知改动。
- 当前分支及目标基线是否明确。
- 全量测试是否通过；若失败，应先区分既有失败与本次修改引入的失败。
- 本次任务的范围、预期产物和不修改内容是否明确。

建议后续 Agent 在开始时给出以下简短状态：

```text
repository_read: yes
workflow_version: paper-workflow-orchestrator-v1.0
baseline_tests: pass | fail
current_branch: <branch>
change_goal: <one sentence>
files_in_scope: <paths>
```

## 4. 目录与职责

```text
paper-workflow-orchestrator/
├── DEVELOPMENT_GUIDE.md              # 本开发入口
├── README.md                          # 英文公开说明
├── README.zh-CN.md                    # 中文公开说明
├── CHANGELOG.md                       # 正式版本记录
├── SKILL.md                           # 顶层工作流与路由合同
├── dependencies.lock.json             # 固定依赖、安装配置与版本元数据
├── agents/openai.yaml                 # Codex Agent 接口声明
├── assets/                            # Logo 与 README 横幅
├── companion-skills/                  # 仓库内置配套 Skill
├── references/                        # 按需读取的阶段与安全合同
├── scripts/                           # 标准库验证器、状态管理器和安装器
└── tests/                             # 回归测试与历史迁移样例
```

关键文件映射：

| 修改类型 | 主要文件 | 必须同步检查 |
|---|---|---|
| 阶段、路由或用户闸门 | `SKILL.md`、`references/stage-contracts.md` | router、README、回归测试 |
| 论文章节规则 | `references/paper-section-contract.md` | section validator、摘要与结论闸门测试 |
| progress schema 或迁移 | `references/progress-schema.md`、`scripts/progress_manager.py` | 历史 fixtures、版本测试、备份兼容性 |
| scientific figures | `references/scientific-visualization-integration.md`、`scripts/figure_contract_validator.py` | receipt 测试、Skill 路由、README |
| humanizer | `references/humanizer-adapter.md`、`scripts/humanizer_preflight.py` | 受保护内容、回滚和 fail-closed 行为 |
| 终稿编辑 | `references/final-editor-integration.md`、配套 final-editor Skill | 收据验证、作者风格与双语一致性 |
| 自动实验 | `scripts/experiment_contract_validator.py` | 显式授权、边界合同和 `autoresearch` 路由 |
| 安装器或依赖 | `scripts/install_workflow.py`、`dependencies.lock.json` | profile、许可证、receipt、README |
| 公开文档 | `README.md`、`README.zh-CN.md` | 双语命令、链接、版本和专有名称 |

## 5. 不可破坏的工作流约束

- 每个阶段结束前先更新 progress，再给用户 2 至 5 个下一步选项，并标出一个推荐项。
- 主模型负责路由、证据账本、提示词、冲突解决和最终合稿。
- 子代理只处理边界清晰的中间任务，不得修改规范终稿或直接并发写入 `progress.md`。
- 正式引用必须可追溯；不得编造引用、数据、实验结果、统计量或机制解释。
- 摘要必须在正文、结果、解释和结论稳定后撰写，并经过单独的一致性检查。
- 结论是必需部分。独立的 Discussion 标题可以省略，但讨论功能不能缺失。
- 可能改变核心结论的问题必须设置关键有效性阻断，补充数据、实验或方法验证前不得继续定稿。
- `scientific-visualization` 只在存在数据、实验或图像证据的图件任务中加载；不得把概念图伪装成数据图。
- humanizer 只能处理语言表达，必须保护主张、数字、单位、公式、引用、否定、模态、范围和统计限定语。
- `autoresearch` 不能自动加载。只有用户明确授权且实验合同验证通过后，才能作为有界下游运行器使用。
- 安装了多个 Skill 不等于同时加载；每个阶段保持一个主 Skill，至多一个用途明确的辅助 Skill。

## 6. README 与公开内容约束

中英文 README 使用以下 v1.0 正式版副标题：

```text
Helping you turn any vague idea into a paper built to top-journal standards.
帮助您将任何一个模糊的想法落地为顶刊级别的论文。
```

公开 README 不应把内部连续性元数据描述成单独的用户功能。现有测试会阻止重新加入已经删除的相关表述。

修改双语 README 时：

- 保持命令、路径、链接目标、版本号和技术事实一致。
- 不得为了语言自然化改写代码块。
- 中英文可以自然表达，但不能产生功能差异。
- 不得使用无法验证的“顶刊保证”“自动发表”等承诺。
- 修改后检查所有相对链接均指向实际文件。

## 7. 开发与测试规则

实现功能或修复缺陷时采用以下顺序：

1. 写出能够复现缺口的失败测试。
2. 运行测试，确认它因预期原因失败。
3. 实现最小、边界清晰的修改。
4. 运行目标测试。
5. 运行全量测试。
6. 检查文档、版本元数据和依赖清单是否需要同步。
7. 运行 `git diff --check`，审阅实际 diff 后再提交。

全量测试命令：

```powershell
python -B -m unittest discover -s tests -v
```

主要测试范围：

- `tests/test_figure_workflow.py`：科研图件收据、哈希、来源、预览和视觉审查。
- `tests/test_install_workflow.py`：安装 profile、路径安全、回滚、许可证和幂等更新。
- `tests/test_progress_version.py`：v1.0 元数据、v0.2 至 v0.6 迁移、README 约束和安装收据。

当前正式基线为 48 项测试。新增行为应增加对应测试，不要通过删除断言降低门槛。

## 8. 安装器与依赖边界

安装器提供三个 profile：

| Profile | 内容 |
|---|---|
| `core` | orchestrator、内置 final editor、内置 research router |
| `standard` | `core` 加标准研究、写作、审稿、humanizer 和科研绘图能力 |
| `full` | `standard` 加显式授权的 `autoresearch` 与 ARA 审查能力 |

常用命令：

```powershell
python scripts/install_workflow.py --profile core --dry-run
python scripts/install_workflow.py --profile standard --verify
python scripts/install_workflow.py --profile standard --update
```

修改依赖时必须：

- 固定仓库、子目录和完整 Git commit SHA。
- 记录上游许可证；缺少许可证时标记为需要人工审查。
- 保持路径穿越、符号链接和 Windows 保留名防护。
- 保持非托管目录冲突时 fail closed。
- 更新安装器测试、双语 README 和 `CHANGELOG.md`。

## 9. 版本升级清单

版本升级不是只改 README。至少检查：

- `SKILL.md` frontmatter 中的 `version` 与 `workflow_version`。
- `dependencies.lock.json` 中的 `release_version` 与 `workflow_version`。
- `scripts/progress_manager.py` 中的当前版本和旧版集合。
- `references/progress-schema.md` 中的规范版本与迁移说明。
- 双语 README、配套 Skill README 和 `CHANGELOG.md`。
- 安装 receipt 的版本绑定。
- 对新增 legacy 版本提供独立、不可变的迁移 fixture 与断言。

版本规则：发布版本使用三段式，例如 `1.0.0`；工作流 schema 使用主次版本，例如 `paper-workflow-orchestrator-v1.0`。

## 10. Git 与发布流程

- 从最新 `main` 创建命名清晰的功能分支。
- 不使用 `git reset --hard` 或强制推送处理未知状态。
- 提交信息应准确描述改动，例如 `feat:`、`fix:`、`test:`、`docs:`。
- 在目标提交上运行全量测试，不引用旧测试结果作为发布证据。
- 合并后在干净的 `main` 上再次验证。
- 对外发布前确认版本、README、CHANGELOG、依赖锁和测试处于同一提交。
- 不发布个人论文、`.research/`、`.paper/`、实验数据、凭据、缓存或临时审查记录。

用于生成开发快照 ZIP 的推荐命令：

```powershell
git archive --format=zip --prefix=paper-workflow-orchestrator-v1.0-development/ --output=<output.zip> HEAD
```

该命令只打包受版本控制的文件，不包含 `.git` 或本机临时内容。

## 11. 变更完成条件

只有同时满足以下条件，才可以报告完成：

- 用户要求的行为已经实现，而不只是写入说明。
- 目标测试和全量测试全部通过。
- `git diff --check` 通过。
- 双语公开文档与实现一致。
- 没有未解释的关键 warning 或发布阻断。
- 工作树状态、提交范围和是否已推送被准确报告。

如果验证工具只能检查合同完整性，应明确说明它不能自动证明科学正确性。研究证据、统计推断和论文结论仍需要人工与领域审查。
