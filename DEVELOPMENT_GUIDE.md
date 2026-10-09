# Paper Workflow Orchestrator 开发指南

本文面向希望理解、调试或扩展本项目的人类开发者。它介绍代码结构、关键设计、常见修改路径和验证方法。工作流在运行时如何约束模型与子代理，以 `SKILL.md` 和 `references/` 中的合同为准；本指南关注这些合同如何落实到代码，以及修改后如何证明行为仍然正确。

只想安装并使用项目时，先看[中文 README](README.zh-CN.md#快速安装)。自行编排阶段可跟着 [Workflow Studio 入门指南](docs/workflow-studio-guide.md)操作；全部文档按用途收录在[文档导航](docs/README.md)中。

阅读代码时可先选一条路径：

| 当前目标 | 建议阅读顺序 |
|---|---|
| 理解安装与默认使用 | 安装器 → 顶层 Skill → 阶段合同 → 进度管理器 |
| 调整编辑体验 | `studio/src/App.tsx` → `components/` → `workflow.ts` → `api.ts` |
| 改变自定义流程行为 | 自定义流程合同 → `schema.py` → `compiler.py` → `scheduler.py` → `workflow_manager.py` |
| 调试保存或恢复 | `studio_server.py` → `store.py` → 对应的状态与端到端测试 |
| 理解进度与确认产物的分工 | `progress_manager.py` → `workflow_engine/reporting.py` → `artifact_manager.py` |

## 1. 项目概览

Paper Workflow Orchestrator 是一套面向 Codex 的研究到论文工作流。顶层 Router Skills 负责判断当前阶段，并只加载该阶段需要的主要能力；Python 脚本负责处理状态、验证结构化产物、检查交付完整性以及安装依赖。

| 项目属性 | 当前值 |
|---|---|
| 项目代码版本 | `1.1.0` |
| 工作流 schema | `paper-workflow-orchestrator-v1.0` |
| Python | 3.10+ |
| Python 运行时依赖 | 仅标准库 |
| Studio 开发环境 | Node.js 22.12.0+、npm |
| Studio 使用环境 | 无需 Node.js；安装包内含预构建界面 |
| 支持平台 | macOS、Linux、Windows |
| 仓库 | `https://github.com/shuohui-air-technology/codex-paper-workflow` |

项目由三类内容组成：

1. **声明式工作流**：Markdown 和 YAML 文件定义阶段、路由、输入输出合同与 Codex 接口。
2. **可执行保障层**：Python 脚本验证合同、管理进度、检查回执，并以可回滚方式安装 Skills。
3. **可视化编排器**：`studio/` 保存 Workflow Studio 前端源码；`assets/workflow-studio/` 是用户安装后直接运行的离线界面文件。

理解这种分工很重要。修改文案不一定改变程序行为，修改 Python 校验器也不一定改变工作流语义；涉及同一能力时，两侧通常需要同步更新。

## 2. 仓库结构

```text
codex-paper-workflow/
├── SKILL.md                         # 顶层工作流、阶段路由和运行时合同
├── agents/openai.yaml               # Codex 中展示和调用 Skill 的接口元数据
├── companion-skills/                # 本项目维护的 Router、终稿编辑和参考优先绘图 Skill
├── references/                      # 阶段、章节、进度及集成合同
├── scripts/                         # 验证器、状态管理器和安装器
│   ├── workflow_studio.py            # 面向使用者的本地界面启动器
│   ├── workflow_manager.py           # 自定义流程的 JSON 命令行接口
│   ├── artifact_manager.py           # 已确认产物的索引、解析、采纳与展示恢复
│   ├── material_manager.py           # 候选材料登记、哈希差异与批量确认
│   ├── confirmed_artifacts.py        # 版本快照、确认目录与可重建展示存储
│   └── workflow_engine/              # 流程解析、校验、调度、存储与本地服务
├── tests/                           # unittest 测试与历史版本夹具
├── studio/                          # React 前端、单元测试和浏览器测试
├── assets/workflow-studio/           # 预构建、可离线运行的界面文件
├── docs/                            # 用户教程、文档导航、设计与验证记录
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

`-B` 禁止生成 `__pycache__` 和 `.pyc`，可以让测试后的工作树保持整洁。GitHub Actions 还会在 macOS、Ubuntu 和 Windows 上分别使用 Python 3.10 与 3.13 执行同一测试命令。

### Workflow Studio 前端与浏览器测试

Python 管理器可以独立运行；只有修改 Studio 界面或重建离线界面包时，才需要 Node.js。前端依赖固定在 lockfile 中，建议使用 Node.js 22.12.0 或更高版本：

```bash
cd studio
npm ci --ignore-scripts
npm run typecheck
npm test -- --run
npm run build
cd ..
python3 scripts/verify_workflow_studio_bundle.py assets/workflow-studio
```

浏览器测试使用 Playwright，第一次运行前需安装对应浏览器：

```bash
cd studio
npx playwright install chromium
npm run e2e -- --project=chromium
```

更新 README 截图时，先在 `studio/` 目录运行 `npm run build`，再从仓库根目录运行 `python3 scripts/verify_workflow_studio_bundle.py assets/workflow-studio`，最后回到 `studio/` 执行 `npm run capture:screenshot`。截图测试通过真实的本地 Python 服务读取预构建界面，在 1440×900 Chromium 视口中保存 `assets/workflow-studio.png`。保存前请检查截图没有临时路径、令牌、个人资料或调试界面。

Workflow Studio 用户使用的是仓库随附的 `assets/workflow-studio/` 预构建文件，不需要 Node.js 或 npm。安装器只复制用户运行所需的文件，不会把 React 源码、开发依赖或浏览器测试工具放进 Skill 安装目录。

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
| 修改已确认产物管理 | `scripts/artifact_manager.py` | 确认索引、快照完整性、并发确认及展示恢复测试 |
| 修改候选材料与恢复摘要 | `scripts/material_manager.py`、[材料合同](references/materials.md) | `tests/test_material_manager.py`：身份、差异、事务、旧项目与冻结输入 |
| 修改科研图件规则 | `scripts/figure_contract_validator.py` | visualization integration、figure tests |
| 修改语言自然化检查 | `scripts/humanizer_preflight.py` | humanizer adapter、受保护内容测试 |
| 修改终稿回执 | Final Editor Skill 与回执验证器 | final-editor integration、完整性测试 |
| 修改实验合同 | `scripts/experiment_contract_validator.py` | 实验字段、授权与失败分支测试 |
| 修改安装行为 | `scripts/install_workflow.py` | `dependencies.lock.json`、installer tests |
| 修改可视化工作流编辑器 | `studio/src/` | `studio/src/**/*.test.tsx`、`studio/e2e/`、离线构建验证 |
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
- 纳入摘要的安装文件与回执不一致时，不视为安装成功；
- 归档中的绝对路径和 `..` 路径会被拒绝。

安装回执用于核对受管理 Skill 的安装记录与源码树摘要，本身不是数字签名。Python `__pycache__` 下运行时生成的 `.pyc`/`.pyo` 不计入源码树摘要，因此回执不提供对本机字节码缓存的防篡改保证。

修改这些分支时，必须同时保留成功测试和拒绝测试。只验证“合法输入能运行”不足以证明安全边界仍然存在。

### 5.4 写入必须可恢复

`progress_manager.py` 和 `install_workflow.py` 会采用临时文件、原子替换、备份或事务回执，避免中途失败留下半写入状态。修改写入流程时，应逐一考虑：

- 目标文件原本不存在；
- 目标文件已经存在；
- 写入或替换中途失败；
- 多个进程同时访问；
- 失败后再次运行。

## 6. 测试结构与运行方法

Python 测试使用标准库 `unittest`；Studio 前端使用 Vitest 和 React Testing Library，浏览器端到端测试使用 Playwright。

```bash
# 全量测试
python -B -m unittest discover -s tests -v

# 单个测试模块
python -B -m unittest tests.test_install_workflow -v

# 单个测试类或方法
python -B -m unittest \
  tests.test_install_workflow.InstallerContractTests.test_install_rejects_user_controlled_symlink_parent -v

# Studio 前端测试与构建（需先在 studio/ 执行 npm ci）
cd studio
npm run typecheck
npm test -- --run
npm run build
npm run e2e -- --project=chromium
cd ..
python3 scripts/verify_workflow_studio_bundle.py assets/workflow-studio
```

主要测试文件：

- `tests/test_figure_workflow.py`：图件合同、哈希、来源、预览和视觉审查回执。
- `tests/test_install_workflow.py`：安装 profile、路径安全、备份、回滚、许可证和幂等更新。
- `tests/test_progress_version.py`：v1.0 元数据、旧 progress 迁移、README 约束和安装回执。
- `studio/src/**/*.test.tsx`：表单、图形界面和编辑交互。
- `studio/e2e/`：启动真实本地 Python 服务后的浏览器流程、修订冲突与只读界面检查。
- `tests/test_workflow_studio_bundle.py`：离线包哈希、资源引用和安装包内容检查。

历史 progress fixtures 是兼容性样本，不应随当前格式一起改写。需要支持新的旧版本时，应增加一份独立 fixture，并明确测试迁移前输入和迁移后输出。

### 6.1 为缺陷编写回归测试

推荐流程：

1. 用最小输入复现缺陷。
2. 添加一个会因该缺陷失败的测试。
3. 确认测试失败原因与缺陷一致。
4. 修改实现并运行目标测试。
5. 运行全量测试和 `git diff --check`。

跨平台代码尤其要避免把当前系统的字符串形式当作唯一事实。例如 macOS 上 `/var` 可能解析到 `/private/var`，Windows 路径分隔符和保留文件名也与 POSIX 系统不同。测试安全规则时，应保留规则本身，只调整测试夹具以使用平台真实路径。

## 自定义工作流引擎

引擎为希望自行编排阶段的高级用户提供独立的 DAG（有向无环图）执行路径。Official v1.0 仍是默认流程；仅在明确启用自定义流程后，项目才使用 `.research/custom-workflow/` 中的自定义状态。自定义流程不会把节点状态写进 Official v1.0 的 `progress.md`。

Studio 的前端通过随安装包提供的静态资源运行。Python 启动器只监听本机回环地址，为当前编辑会话生成随机访问凭据；状态变更还需通过来源和 CSRF 校验。前端不需要外部字体、脚本或 API；`scripts/verify_workflow_studio_bundle.py` 会检查清单哈希、文件完整性以及运行资源是否留在包内。这些限制使编辑器可以离线使用，并缩小本机服务的访问范围。

### 组件职责

| 组件 | 职责 |
|---|---|
| `scripts/workflow_engine/schema.py` | 解析版本化工作流文件，检查字段与类型，并计算稳定的语义哈希。 |
| `catalog.py` | 识别本机已安装的 Skill，并读取仓库内固定注册的验证器；不执行 Skill 文件内容。 |
| `compiler.py` | 检查节点、连接、条件、产物映射和并行写入范围，生成可执行计划。 |
| `scheduler.py` | 以纯状态转换方式推进任务、条件分支、并行节点和 join 汇合。 |
| `reporting.py` | 从已验证的运行快照汇总执行进展，供 CLI 与 Markdown 展示复用。 |
| `store.py` | 保存选择状态、草稿、运行快照与追加式事件，并在重启后验证和恢复一致状态。 |
| `receipts.py`、`validators.py` | 构造并核对节点调用/结果回执；只运行代码中注册且哈希固定的验证器适配器。 |
| `scripts/workflow_manager.py` | 提供 JSON 命令行接口，连接校验、激活、调度、结果登记和状态查询。 |

一项自定义任务由用户在 Studio 中绑定一个本机 Skill。管理器验证绑定并生成带有输入、输出和尝试标识的调用凭据；Skill 的执行由 Codex 完成，管理器负责核对其提交的结果和实际文件哈希。条件节点与 join 节点由调度器自动推进，不作为 Skill 任务运行。验证器节点走独立的固定适配器，不生成论文文件。

### 官方与自定义状态

模式查询是执行入口。没有选择记录或显式选择 Official v1.0 时，管理器返回官方模式且不读取或创建自定义运行目录。自定义模式使用自己的选择记录、激活计划、`state.json`、`events.jsonl` 和回执。摘要读取不会修复选择投影；不一致或无法验证的记录会阻止继续，而不是切换到另一套状态。显式读取已保存的自定义草稿与激活流程分离，读取本身不会让它成为执行依据。

Skill 目录采用安全校验：候选 Skill 文件树中发现符号链接或重解析点时，目录发现会报告错误，工作流校验将保持 blocked。开发环境中的 Skill 根目录应是完整、可核验的安装树；不要为了让校验通过而绕过这项检查。

### 官方进度的常规更新

`progress_manager.py update-snapshot` 用于更新官方流程的当前阶段、状态和下一步。调用前先确认 `workflow_manager.py summary` 返回官方模式，然后取得当前进度摘要：

```bash
python3 scripts/workflow_manager.py summary --project "/path/to/paper-project" --json
python3 scripts/progress_manager.py summary --file "/path/to/paper-project/.research/progress.md"
```

摘要返回的 `document_sha256` 对应规范化后的文档文本；它处理 BOM 与换行差异，不能用任意文件字节摘要代替。核对摘要后，将下例中的项目路径和摘要占位符替换为实际值：

```bash
python3 scripts/progress_manager.py update-snapshot \
  --project "/path/to/paper-project" \
  --expected-sha256 "<document_sha256>" \
  --summary "已整理文献比较表，下一步核对研究方案" \
  --refs ".research/literature_matrix.md" \
  --current-status "文献比较完成，研究方案待确认" \
  --next-action "核对文献比较表与研究问题的对应关系"
```

命令仅修改明确传入的有限字段：

| 参数 | 更新内容 |
|---|---|
| `--current-stage` | 官方规范阶段标识 |
| `--entry-mode` | `guided_idea`、`draft_audit`、`write_or_revise` 或 `autonomous_experiment` |
| `--research-question`、`--selected-direction` | 当前研究问题与方向 |
| `--current-status`、`--completed-milestones` | 当前状态与已完成里程碑的简短描述 |
| `--next-action`、`--blockers` | 下一步与阻碍 |
| `--hub-status`、`--last-stage-receipt` | 文献工具状态与最近阶段回执位置 |
| `--resume-instruction` | 恢复会话时的简短指引；未指定时随 `--next-action` 同步 |

`--entry-mode` 是官方流程的研究入口，写入原有 `mode` 元数据；它与工作流选择器的 `official/custom` 是两层不同概念。此命令不改 `validity_status`、错误规则的严重程度或解决状态，也不替代阶段验收或用户采纳。

写入时先取得模式选择锁，再取得官方进度锁，验证输入文档和期望摘要，更新有限字段，并追加一条带有摘要及证据引用的事件。更新保留 `.bak` 备份。出现摘要冲突时重新读取和核对，不能把过时修改直接覆盖上去；无字段修改、无效进度或当前为自定义模式时拒绝写入。

### 自定义运行的进展与恢复

自定义 `summary` 保留原来的 `run_status`、节点、产物等字段，并新增 `progress`：

| 字段 | 含义 |
|---|---|
| `phase` | `ready`、`running`、`needs_attention`、`waiting`、`finished`、`stopped` 或 `empty` |
| `total_nodes` | 包含条件、汇合等控制节点的总数 |
| `counts` | 各节点状态的数量，成功与跳过分别计算 |
| `node_ids_by_status` | 按同一状态分类的节点标识列表 |

`reporting.py` 对已经验证的状态做纯汇总。CLI 与运行目录中的 `summary.md` 复用该逻辑；摘要查询不运行任务、不推进条件节点，也不因展示格式变化而重写权威状态。`run_status: active` 表示该运行仍被选用，`phase: finished` 表示所有节点已成功结束或跳过，两者可以同时成立。这个结果不表示论文通过科学审查，也不表示全部产物已经被用户采纳。

需要恢复中断运行时，先明确旧任务和验证器进程已经停止，再执行：

```bash
python3 scripts/workflow_manager.py recover \
  --project "/path/to/paper-project" --json --confirm-interrupted
```

恢复根据持久化记录重建一致状态，将不确定的中断执行留待处理。它不会自动执行阶段。随后读取摘要和可执行阶段，根据实际情况使用 `retry` 重试失败或中断任务，或使用 `rerun-stale` 重新处理过时的结果。恢复报告损坏或证据冲突时，应先解决该问题，再继续调度。

### 本地检查

下面的验证示例引用 `tests/fixtures/workflow_valid_linear.json` 中的 `clarify-research-idea` 和 `research-design-helper`。运行前需确保这两个 Skill 已安装且 Skill 根目录通过目录完整性检查。`summary` 查询当前项目模式；在新建项目中，结果应为 `official`，且不会创建自定义运行状态。

```bash
python3 scripts/workflow_manager.py validate --project . --workflow tests/fixtures/workflow_valid_linear.json
python3 scripts/workflow_manager.py summary --project .
python3 -B -m unittest tests.test_custom_workflow_end_to_end -v
```

## 进度、工作文件与已确认产物

使用者的操作说明见[进度与已确认产物指南](docs/progress-and-artifacts-guide.md)。实现上需要保持三种状态的分工：

| 状态 | 权威记录 | 回答的问题 |
|---|---|---|
| 官方流程进度 | `.research/progress.md` | 目前处于哪个阶段，下一步是什么？ |
| 自定义运行 | 已激活计划、状态、事件与回执 | 哪些阶段已执行，各产物依据是什么？ |
| 用户采纳的成果 | `.research/confirmed-artifacts/catalog.json` | 哪个内容版本目前得到用户认可？ |

已确认产物管理是一层独立的采纳记录。文件生成或阶段成功不会自动等价为用户采纳；完成相关检查且用户确认后，Skill 调用产物管理器完成收录。普通用户通过自然语言作出确认，无需自行编辑请求 JSON。

官方确认请求必须包含当前摘要的 `expected_progress_sha256` 和非空 `evidence` 文件清单。管理器核对进度与证据哈希；`validity_status: blocked` 时拒绝新的确认，已有完整确认快照仍可读取。相关科学检查仍需由调用方实际执行并判读，证据哈希本身不会解释报告是否证明结论成立。自定义请求省略官方进度摘要，为每个文件提供 `source_artifact_id`，并与当前运行中成功任务的产物路径、哈希、attempt 及已提交回执一致。成功执行回执同样不能代替科学审查或用户采纳。

`scripts/artifact_manager.py` 提供以下操作；请求 schema 与完整示例见[已确认产物合同](references/confirmed-artifacts.md)：

| 操作 | 用途 |
|---|---|
| `status` | 只读查询紧凑索引与展示元数据；默认 `verification: not_checked`，不会全面校验文件内容 |
| `status --verify` | 额外校验当前确认快照及展示，报告缺失或改动 |
| `resolve --artifact-id --expect-revision` | 校验指定当前快照，返回 `snapshot_path`、`current_path` 和文件清单；读取优先使用 `snapshot_path` |
| `accept --request --confirm` | 根据验收依据和显式确认收录成果 |
| `withdraw --confirm` | 撤回当前采纳状态，保留历史快照 |
| `repair --confirm` | 从确认快照重建展示；修复用户修改冲突时需再显式使用 `--backup-conflicts` 保存备份 |

确认版本保存于 `.research/confirmed-artifacts/versions/`。`artifacts/current/<type>/<artifact_id>/` 和 `artifacts/INDEX.md` 展示当前采纳结果；原工作源文件留在原处，继续承担编辑、运行及历史路径绑定的职责。确认版本由 catalog 的明确指向决定，不能通过文件名或修改时间推断。

开发时可先读取索引，再解析需要的产物。将项目路径、产物标识和 revision 替换为实际值：

```bash
python3 scripts/artifact_manager.py status --project "/path/to/paper-project" --json
python3 scripts/artifact_manager.py resolve \
  --project "/path/to/paper-project" --artifact-id main-manuscript \
  --expect-revision 1 --json
```

采纳请求文件位于项目内。准备好合同所列字段、实际哈希、证据和用户的采纳决定后，通过以下入口提交：

```bash
python3 scripts/artifact_manager.py accept \
  --project "/path/to/paper-project" \
  --request ".research/confirm-main-manuscript.json" --confirm --json
```

同一个逻辑产物的新版本保留 `artifact_id` 和 `artifact_type`。`operation_id` 标识一次采纳决定，重试同一请求时保持不变；新的采纳决定使用新的标识。接受请求的期望版本放在 JSON 的 `expected_catalog_revision` 中；`withdraw` 和 `repair` 则使用 CLI 的 `--expected-revision`，其中撤回还要求 `--artifact-id`、`--operation-id` 和 `--reason`。

`accept` 不更新官方进度的 `next_action`，也不推进自定义阶段。采纳完成后，需要更新官方下一步时，重新核对进度摘要并单独执行 `update-snapshot`；自定义执行按原管理器继续。这两个动作拥有各自的提交点，不能将其中一个成功报告为另一个已经完成。

### 提交点与恢复

收录前必须核对验收依据，并校验快照复制得到的字节与对应哈希。版本快照准备好后，原子替换 catalog 是确认事实的提交点；current、INDEX 与摘要是后续可恢复的展示。读取确认版本时只信任已提交的索引与可验证快照，未被 catalog 引用的准备文件不能被当成当前成果。

如果确认事实已经提交而展示生成中断，返回值会保留 `committed: true` 和 `projection_pending: true`，CLI 可返回 `status: attention` 且退出码为 0。调用方必须检查这些字段；应修复展示，而不是重复采纳出一个新版本。同一 operation ID 的重试用于识别已提交操作，不保证重建展示。并发确认需要期望 revision 与幂等检查，防止较旧的操作覆盖较新决定。

current 中的手工改动不是新版本。确认保留备份后，使用带有 `--backup-conflicts` 的 `repair` 将冲突展示保存在 `.research/confirmed-artifacts/projection-backups/`，再重建 current；工具返回实际备份位置。撤回只清除当前选择，历史快照保留，系统不会自动将前一版设为当前版本。

正常替换或撤下展示文件也会保留旧文件实例，再以排他创建方式写入新展示；这避免覆盖最终检查之后才保存的用户文件。保留返回的 `projection_backups` 和 `backups`，因为编辑器已打开的文件仍可能写入旧实例。展示写入中断可以留下待修复状态；确认事实以 catalog 为准，实际读取以通过哈希校验的快照为准。

### 路径与回执兼容

自定义产物回执和 edge witness 绑定实际路径与哈希，终稿编辑、图件等回执还可能包含嵌套的来源路径。收录通过复制形成确认快照，保留这些原始绑定；确认层不改写旧回执，也不改变当前运行中的输入路径。

这项约束同样适用于之后才领取的自定义任务：它们仍从运行状态和边证据取得输入。把确认快照用于另一轮自定义流程时，应先 `resolve`，再通过 `workflow_manager.py register-artifact` 登记为该流程已经声明的外部输入。覆盖 current 展示不能充当运行结果提交。

多文件产物必须提供显式文件清单，并保留清单内的相对路径。没有列出的引用文件、外部目录和环境依赖不自动进入快照。工作源文件后续变化可以影响原运行的输入验证，但不会改写既有确认快照的内容。

### 恢复会话的读取成本

先读取模式摘要，再读取当前模式的进度摘要与紧凑确认索引，最后解析本次任务需要的产物。官方和自定义摘要引用确认索引，避免把完整版本历史重复写进 progress 或事件摘要。历史仍可追溯，但日常恢复不需要逐条重读。

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

- `release_version` 是安装清单中的三段式项目功能版本号；当前源码中的值为 `1.1.0`；
- `workflow_version` 标识 progress 与工作流合同，例如 `paper-workflow-orchestrator-v1.0`。

这两个版本各自独立。v1.1.0 增加了可视化自定义工作流编辑器，但保留 `paper-workflow-orchestrator-v1.0` 作为官方流程标识，因此安装清单和回执会同时记录 `release_version: 1.1.0` 与 `workflow_version: paper-workflow-orchestrator-v1.0`。只有 progress 结构或官方工作流合同发生不兼容变化时，才需要提升 workflow version。

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
