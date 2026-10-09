# Academic Manuscript Final Editor — 使用说明

面向学术手稿的终稿编辑 skill。适用场景:科学内容已稳定的手稿的**终稿阶段编辑**——根据已提供的编辑反馈归纳写作偏好、在全文范围内搜索同类问题、在不改动受保护科学内容的前提下修订文字、同步双语版本。

## 包内结构

```
academic-manuscript-final-editor/
├── SKILL.md                             # skill 主文件(优化版,agent 的核心指令)
├── README.md                            # 本使用说明
├── agents/openai.yaml                   # agent 接口配置(显示名、默认提示词、隐式调用策略)
├── references/editorial-style-rules.md  # 全稿修订的编辑决策规则参考(供 agent 在全稿任务时阅读)
└── scripts/
    ├── scan_manuscript_style.py         # 风格候选定位脚本(只定位,不改文件)
    └── check_scan_dispositions.py       # 逐条处置骨架生成与校验前自检
```

运行依赖:Python 3.9+(仅标准库,无需安装任何第三方包)。

## 安装

将整个 `academic-manuscript-final-editor/` 文件夹放入您的 agent 的 skills 目录:

- **Codex CLI**:`~/.codex/skills/academic-manuscript-final-editor/`
- **其他兼容 SKILL.md 规范的 agent 框架**:放入其对应的 skills 目录(目录名保持与 skill `name` 一致)

安装后重启 agent 会话即可生效。skill 支持隐式调用(`agents/openai.yaml` 中 `allow_implicit_invocation: true`),当您的请求匹配其描述时会自动触发;也可以显式点名调用。

## 使用方法

### 三种工作模式

| 模式 | 触发条件 | 行为 |
|---|---|---|
| **Revise(修订)** | 您授权生成候选修订稿 | 生成独立候选稿；保护检查通过后，等待用户决定是否应用 |
| **Audit(审计)** | 您只需要评审、诊断或问题清单 | 只读不改,输出问题列表 |
| **Learn(学习)** | 您提供批注或修订记录(track changes) | 推断每处修改背后的最小规则,并按作用域分类 |

### 独立调用示例

```text
使用 academic-manuscript-final-editor 审查这份已完成实质修订的稿件，
找出全文同类表达问题，并列出需要统一的地方。
```

```text
Use academic-manuscript-final-editor to audit this substantively revised draft,
find analogous expression problems throughout it, and list the needed corrections.
```

### 按模式调用

```
# Revise 模式:应用编辑反馈
这是我的论文和审阅批注,请按批注全文修订,并检查同类问题。

# Audit 模式:只要问题清单
帮我通读手稿,列出所有风格和一致性问题,先不要改文件。

# Learn 模式:从修订记录学习规则
这是我带修订记录的文件,请推断每条修改背后的写作规则、
标注每条规则的作用域(段落级/章节级/项目级/跨项目可复用),
不要动原文件。

# 双语同步
中文版是我最近手工修订的权威版本,请先完成中文,
再按语义(而非逐词)同步英文版。
```

### 交付物

每次全稿任务结束,skill 会返回一份紧凑摘要:改动的文件、归纳出的编辑规则及其作用域与处理的同类位置、受保护科学内容的核对确认、未解决的科学/编辑问题、以及(适用时的)渲染与独立评审状态。

## 风格扫描脚本

`scripts/scan_manuscript_style.py` 用于在 Markdown / 纯文本 / DOCX 中定位风格审查候选(内部工作流残留、重复防御性表述等)。**脚本只定位候选**——每条发现都需要您亲自编辑判断

JSON 输出中每条发现都带 `finding_id`(由 path/line/rule_id/evidence 派生的内容指纹,校验器会自行重算);终稿编辑回执需要逐条给出决定(accept/reject/defer/not_applicable)与相应的证据

DOCX 扫描仅覆盖 `word/document.xml` 中的正文文本，并在 JSON 中标记 `coverage_status: main-document-text-only`。批注、修订记录、域、页眉页脚、脚注、文本框和布局必须由文档工作流及逐页渲染收据检查，不能以本扫描器的结果代替。

```bash
# 基本用法:扫描一个或多个文件,输出人类可读报告
python3 scripts/scan_manuscript_style.py manuscript.md

# 输出机器可读 JSON(供 agent 或管线消费)
python3 scripts/scan_manuscript_style.py --json manuscript.docx

# 追加受保护的科学术语(可重复使用)
python3 scripts/scan_manuscript_style.py \
    --protected-term "非负" --protected-term "zero catch" manuscript.md

# 调整每条发现显示的上下文长度(默认 70 字符)
python3 scripts/scan_manuscript_style.py --context 100 manuscript.md

# CI 用:发现 high 级问题时以非零退出码结束
python3 scripts/scan_manuscript_style.py --fail-on high manuscript.md
```

终稿编辑要求**逐条处置**:报告的每条发现都带 `finding_id`,处置记录必须逐条给出决定与证据。

```bash
# 生成骨架(覆盖报告中每个 finding_id,决定与证据留空)
python3 scripts/check_scan_dispositions.py --scan editorial_scan.json \
    --template-out editorial_scan_dispositions.json

# 填好后做校验前自检(决定词表、证据引用、ID 一一对应、计数一致、哈希绑定)
python3 scripts/check_scan_dispositions.py --scan editorial_scan.json \
    --dispositions editorial_scan_dispositions.json
```

## 修订时如何保留论文原意

终稿编辑主要改善表达、组织和一致性，同时检查修改是否影响研究含义。

- **改善表达，核对科学内容。** 修改冗长、重复或含糊的句子，并对照原稿检查数字、单位、公式、引用和结论。例如，“存在相关性”应保持原有含义，不能润色成“导致”；“未发现显著差异”也应完整保留。
- **保留原稿，提供可比较的修订稿。** 修改在独立候选稿中完成，便于查看差异、选择采用哪些改动，以及恢复原来的版本。已投稿或正式评审的稿件保留历史版本。
- **单独处理引用编号。** 删除一条引用后，其他引用和文献条目先保持原编号。需要重新编号时，将按您确认的要求统一处理。
- **核对中英文是否表达同一意思。** 先完成本轮指定语言的修订，再同步另一版本，检查结论、限制条件、数字和引用是否对应。两份稿件均有独立修改、无法确定以哪份为准时，先与您确认。
- **清理混入正文的编辑过程记录。** 将“已完成第几轮检查”等编辑备注留在工作记录中，让论文正文聚焦研究内容；研究方案、预注册或审计报告所需的流程说明仍然保留。

完整编辑规则见 [SKILL.md](SKILL.md) 和 [编辑规则参考](references/editorial-style-rules.md)。
