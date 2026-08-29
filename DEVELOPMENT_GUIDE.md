# Paper Workflow Orchestrator 开发指南

本文面向希望理解、调试或扩展本项目的人类开发者。它介绍代码结构、关键设计、常见修改路径和验证方法。工作流在运行时如何约束模型与子代理，以 `SKILL.md` 和 `references/` 中的合同为准；本指南关注这些合同如何落实到代码，以及修改后如何证明行为仍然正确。

## 1. 项目概览

Paper Workflow Orchestrator 是一套面向 Codex 的研究到论文工作流。顶层 Router Skills 负责判断当前阶段，并只加载该阶段需要的主要能力；Python 脚本负责处理状态、验证结构化产物、检查交付完整性以及安装依赖。

| 项目属性 | 当前值 |
|---|---|
| 发布版本 | `1.0.0` |
| 工作流 schema | `paper-workflow-orchestrator-v1.0` |
| Python | 3.10+ |
| Python 运行时依赖 | 仅标准库 |
| 支持平台 | macOS、Linux、Windows |
| 仓库 | `https://github.com/shuohui-air-technology/codex-paper-workflow` |

项目由两类内容组成：

1. **声明式工作流**：Markdown 和 YAML 文件定义阶段、路由、输入输出合同与 Codex 接口。
2. **可执行保障层**：Python 脚本验证合同、管理进度、检查回执，并以可回滚方式安装 Skills。

理解这种分工很重要。修改文案不一定改变程序行为，修改 Python 校验器也不一定改变工作流语义；涉及同一能力时，两侧通常需要同步更新。

## 2. 仓库结构

```text
codex-paper-workflow/
├── SKILL.md                         # 顶层工作流、阶段路由和运行时合同
├── agents/openai.yaml               # Codex 中展示和调用 Skill 的接口元数据
├── companion-skills/                # 与主工作流共同发布的 Router 和终稿编辑 Skill
├── references/                      # 阶段、章节、进度及集成合同
├── scripts/                         # 验证器、状态管理器和安装器
├── tests/                           # unittest 测试与历史版本夹具
├── dependencies.lock.json           # 安装 profile、固定依赖版本和许可证信息
├── README.md                        # 英文用户文档
├── README.zh-CN.md                  # 中文用户文档
└── CHANGELOG.md                     # 面向发布版本的变更记录
```

### 2.1 运行时请求如何流动

```text
用户请求
   │
   ▼
research-skill-router
   │  选择当前阶段的一项主要能力
   ▼
paper-workflow-orchestrator
   ├── 读取阶段合同与 progress 状态
   ├── 调用对应 Skill 或脚本
   ├── 验证阶段产物
   └── 记录状态并等待下一步决定
```

Router 不承担具体研究工作。它解决的是“当前应该由哪一种能力接管请求”；Orchestrator 解决的是“阶段如何衔接、产物如何验收、失败后如何恢复”。这一边界是阅读 `SKILL.md` 和 `companion-skills/research-skill-router/SKILL.md` 时最值得关注的部分。

## 3. 建立本地开发环境

项目核心脚本不依赖第三方 Python 包。推荐使用虚拟环境隔离解释器，但这不是运行测试的硬性要求。

### macOS 或 Linux

```bash
git clone https://github.com/shuohui-air-technology/codex-paper-workflow.git
cd codex-paper-workflow

python3 --version
python3 -m venv .venv
source .venv/bin/activate
python -B -m unittest discover -s tests -v
```

### Windows PowerShell

```powershell
git clone https://github.com/shuohui-air-technology/codex-paper-workflow.git
Set-Location codex-paper-workflow

py -3.10 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -B -m unittest discover -s tests -v
```

`-B` 禁止生成 `__pycache__` 和 `.pyc`，可以让测试后的工作树保持整洁。当前基线是 49 项测试；GitHub Actions 还会在 macOS、Ubuntu 和 Windows 上分别使用 Python 3.10 与 3.13 执行同一测试命令。

开始修改前建议记录当前状态：

```bash
git status --short --branch
git log --oneline --decorate -5
python -B -m unittest discover -s tests -v
```

这样可以区分修改前已经存在的问题与本次改动造成的回归。

## 4. 按开发目标寻找入口

不必从头阅读整个仓库。先根据任务找到主要入口，再沿测试和引用关系向外阅读。

| 开发目标 | 首要入口 | 相关实现与测试 |
|---|---|---|
| 修改阶段或路由 | `SKILL.md`、`references/stage-contracts.md` | Router Skill、README、版本测试 |
| 修改论文章节规则 | `references/paper-section-contract.md` | `paper_section_validator.py` 及对应测试 |
| 修改进度状态 | `scripts/progress_manager.py` | `progress-schema.md`、历史 fixtures、版本测试 |
| 修改科研图件规则 | `scripts/figure_contract_validator.py` | visualization integration、figure tests |
| 修改语言自然化检查 | `scripts/humanizer_preflight.py` | humanizer adapter、受保护内容测试 |
| 修改终稿回执 | Final Editor Skill 与回执验证器 | final-editor integration、完整性测试 |
| 修改实验合同 | `scripts/experiment_contract_validator.py` | 实验字段、授权与失败分支测试 |
| 修改安装行为 | `scripts/install_workflow.py` | `dependencies.lock.json`、installer tests |
| 修改公开说明 | `README.md`、`README.zh-CN.md` | 链接、命令、版本一致性测试 |

阅读一个 Python 模块时，可以按以下顺序进行：

1. 查看模块顶部的常量和数据结构，理解输入格式。
2. 找到 `main()` 或公开入口函数，画出主要调用路径。
3. 阅读对应测试，观察正常输入和失败输入分别是什么。
4. 再进入路径处理、解析或校验等辅助函数。

测试往往比函数注释更能说明一个边界为何存在。

## 5. Python 代码的共同模式

### 5.1 标准库优先

核心脚本只使用 Python 标准库，以降低安装成本并保持离线可运行。引入第三方运行时依赖前，需要说明标准库无法满足的原因，并同步评估安装器、许可证和跨平台测试。

### 5.2 数据从不默认可信

JSON、Markdown、ZIP、DOCX 和 progress 文件都可能来自外部。解析代码通常遵循以下顺序：

```text
读取字节或文本
  → 验证编码与顶层类型
  → 验证必需字段
  → 规范化受控值
  → 检查字段之间的关系
  → 返回结构化结果或明确错误
```

新增校验时，应让错误信息指出具体字段或路径。不要用宽泛的 `except Exception` 把格式错误、I/O 错误和实现缺陷合并成同一种失败。

### 5.3 失败关闭

无法证明输入安全或完整时，安装器和验证器会拒绝继续，而不是猜测用户意图。例如：

- 未知的 schema 版本不会按当前版本解释；
- 安装目标包含符号链接或重解析点时会被拒绝；
- 文件哈希与回执不一致时不会视为成功；
- 归档中的绝对路径和 `..` 路径会被拒绝。

修改这些分支时，必须同时保留成功测试和拒绝测试。只验证“合法输入能运行”不足以证明安全边界仍然存在。

### 5.4 写入必须可恢复

`progress_manager.py` 和 `install_workflow.py` 会采用临时文件、原子替换、备份或事务回执，避免中途失败留下半写入状态。修改写入流程时，应逐一考虑：

- 目标文件原本不存在；
- 目标文件已经存在；
- 写入或替换中途失败；
- 多个进程同时访问；
- 失败后再次运行。

## 6. 测试结构与运行方法

本项目使用标准库 `unittest`。

```bash
# 全量测试
python -B -m unittest discover -s tests -v

# 单个测试模块
python -B -m unittest tests.test_install_workflow -v

# 单个测试类或方法
python -B -m unittest \
  tests.test_install_workflow.InstallerContractTests.test_install_rejects_user_controlled_symlink_parent -v
```

主要测试文件：

- `tests/test_figure_workflow.py`：图件合同、哈希、来源、预览和视觉审查回执。
- `tests/test_install_workflow.py`：安装 profile、路径安全、备份、回滚、许可证和幂等更新。
- `tests/test_progress_version.py`：v1.0 元数据、旧 progress 迁移、README 约束和安装回执。

历史 progress fixtures 是兼容性样本，不应随当前格式一起改写。需要支持新的旧版本时，应增加一份独立 fixture，并明确测试迁移前输入和迁移后输出。

### 6.1 为缺陷编写回归测试

推荐流程：

1. 用最小输入复现缺陷。
2. 添加一个会因该缺陷失败的测试。
3. 确认测试失败原因与缺陷一致。
4. 修改实现并运行目标测试。
5. 运行全量测试和 `git diff --check`。

跨平台代码尤其要避免把当前系统的字符串形式当作唯一事实。例如 macOS 上 `/var` 可能解析到 `/private/var`，Windows 路径分隔符和保留文件名也与 POSIX 系统不同。测试安全规则时，应保留规则本身，只调整测试夹具以使用平台真实路径。

## 7. 常见修改场景

### 7.1 增加一条验证规则

先确定规则属于语法、单字段约束还是跨字段关系。语法解析失败通常应立即返回；可以同时报告的字段问题则适合累积到错误列表。

至少增加三类测试：

- 合法输入通过；
- 目标非法输入被拒绝，并包含可定位的错误信息；
- 相邻但合法的输入不被误伤。

如果规则改变了公开的输入合同，还要同步修改对应的 `references/*.md`。

### 7.2 修改 progress schema

progress 文件承担跨阶段恢复功能，因此兼容性比字段命名是否简洁更重要。修改时检查：

1. 当前 schema 常量；
2. 解析与写入逻辑；
3. `references/progress-schema.md`；
4. 从所有受支持旧版本到当前版本的迁移；
5. 迁移前备份与重复迁移行为；
6. `tests/fixtures/` 中不可变的历史样本。

删除旧版本支持属于破坏性变更，需要在版本计划和 `CHANGELOG.md` 中明确记录。

### 7.3 修改依赖或安装 profile

`dependencies.lock.json` 是安装器的数据源。每项 GitHub 依赖必须固定到完整 commit SHA，而不是分支或可移动标签。修改后至少运行：

```bash
python scripts/install_workflow.py --profile core --dry-run

target_dir="$(mktemp -d)/skills"
python scripts/install_workflow.py --profile core --target "$target_dir"
python scripts/install_workflow.py --profile core --target "$target_dir" --verify
```

Windows 开发者可以用 `$env:TEMP` 下的新目录替代 `mktemp`。不要在测试安装器时直接覆盖日常使用的 `~/.codex/skills`。

### 7.4 修改工作流或 Router 合同

合同类改动应回答四个问题：

1. 什么输入会触发这个阶段？
2. 该阶段选择哪一个主要 Skill？
3. 产物满足什么条件才算完成？
4. 中断后依靠哪些状态继续？

模型或子代理的运行规则写入 `SKILL.md` 或对应 reference；面向开发者的实现原因、调试方式和测试方法写入本指南。不要把两类文档混在一起。

### 7.5 修改双语 README

中英文可以采用自然表达，不要求逐句直译，但以下事实必须一致：

- 版本号和兼容环境；
- 安装命令及 profile 含义；
- 文件路径和链接目标；
- 功能范围与限制；
- 项目和第三方 Skill 的专有名称。

代码块应保持可复制执行。修改后运行全量测试，因为部分公开文档约束已经编码为回归测试。

## 8. 安装器的安全模型

安装器支持三个 profile：

| Profile | 用途 |
|---|---|
| `core` | Orchestrator、Research Router 和 Final Editor |
| `standard` | `core` 加常用研究、写作、审稿、自然化和科研绘图能力 |
| `full` | `standard` 加需要显式授权的实验与扩展审查能力 |

安装过程可以概括为：

```text
读取并验证 lock manifest
  → 下载固定提交或读取仓库内置 Skill
  → 在隔离目录检查归档和文件树
  → 计算文件哈希
  → 检查目标目录所有权
  → 备份旧版本
  → 原子安装
  → 写入安装回执
```

以下保护属于安装器的核心设计，不应为了简化代码而移除：

- 拒绝路径穿越、绝对路径、符号链接和 Windows 重解析点；
- 限制归档成员数、单文件大小、总大小和压缩比；
- 第三方依赖固定到完整 commit SHA；
- 不覆盖没有本项目安装回执的同名目录；
- 更新或裁剪前创建备份；
- 失败时使用事务信息恢复原状态。

`--dry-run` 用来预览解析后的安装计划；`--verify` 根据安装回执重新计算状态；`--update` 更新已托管内容；`--prune` 只有显式传入时才移除当前 profile 之外的已托管 Skill。

## 9. 版本与兼容性

项目同时维护两个版本概念：

- `release_version` 使用三段式版本，例如 `1.0.0`；
- `workflow_version` 标识 progress 与工作流合同，例如 `paper-workflow-orchestrator-v1.0`。

发布版本变化不一定要求 schema 变化；只有 progress 结构或阶段合同发生不兼容变化时，才需要提升 workflow version。

版本升级时检查：

- `SKILL.md` frontmatter；
- `dependencies.lock.json`；
- `scripts/progress_manager.py`；
- `references/progress-schema.md`；
- 安装回执与历史迁移测试；
- `README.md`、`README.zh-CN.md` 和 `CHANGELOG.md`。

## 10. 提交前检查

完成修改后，从仓库根目录执行：

```bash
python -B -m unittest discover -s tests -v
git diff --check
git status --short
git diff --stat
git diff
```

提交或发起 Pull Request 前确认：

- 新行为有测试，缺陷修复有回归测试；
- 全量测试在目标提交上通过；
- 修改合同后，相关实现和 reference 已同步；
- 修改公开行为后，中英文 README 已同步；
- 没有凭据、个人研究材料、实验数据、缓存或临时审查文件；
- `CHANGELOG.md` 记录了值得用户注意的变化；
- 提交信息准确区分 `feat:`、`fix:`、`test:`、`docs:` 等类型。

GitHub Actions 通过后，才能确认改动在全部受支持平台上成立。本地测试结果不能代替跨平台结果。

## 11. 调试提示

### 安装器拒绝目标路径

先检查目标或其父目录是否是符号链接，以及该目录是否包含其他工具创建的同名 Skill。安装器宁可停止，也不会接管所有权不明的目录。

### progress 文件无法迁移

保留原文件和自动备份，确认文件声明的 workflow version 是否受支持。不要手工改成当前版本来绕过迁移，因为这会隐藏真实的结构差异。

### 本地通过但 CI 失败

优先比较操作系统、Python 版本、路径表示、默认编码和换行符。路径安全测试应使用目标平台生成的临时路径，不应在 POSIX 系统上用普通字符串假装完整的 Windows 文件系统行为。

### 验证器通过是否代表论文正确

不代表。验证器能够检查结构、引用位置、哈希、字段关系和交付证据，但无法自动证明研究假设、统计推断或科学结论正确。领域审查仍然是论文质量保障的一部分。
