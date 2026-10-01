# 审计修复 A1–A3 / B1–B6 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在不改变生产功能语义的前提下，修复 codex-paper-workflow 审计清单中的 4 项实际缺陷与 6 项低风险改进。

**Architecture:** 改动分为四类：(1) 测试在 Windows 下的符号链接守卫；(2) 统一/收敛严格 JSON 解析与其错误文案（顺带消除重复 helper）；(3) 让 Studio 投影读取走严格 JSON；(4) 为两个被哈希锁定的校验脚本补充功能测试。文档/图片的收敛为独立低风险动作。

**Tech Stack:** Python 3.10–3.13（unittest，无 pytest/conftest），`scripts/workflow_engine/*`，`scripts/verify_workflow_studio_bundle.py`。

**硬性约束（底线）：**
- 不得改变生产功能语义；仅修错误文案、解析严格度与测试守卫。
- **不得修改**哈希锁定的 5 个脚本（`scripts/experiment_contract_validator.py`、`figure_contract_validator.py`、`final_edit_receipt_validator.py`、`humanizer_preflight.py`、`paper_section_validator.py`）的字节——B-6 只新增测试。
- 不触碰 `assets/workflow-studio/`；保持 LF 行尾（`.gitattributes`：`* text=auto eol=lf`）。
- 不做 git commit，除非用户明确要求。

**本地基线（非管理员 Windows，`py -3` = 3.14.2）：** 631 tests / ~156s / **4 errors** / 28 skipped。
4 个 errors 精确定位为：
1. `tests/test_confirmed_artifacts.py::ConfirmedArtifactTests::test_sources_reject_leaf_and_parent_links_hardlinks_and_fifo`（L442，symlink 在 L447/L461）
2. `tests/test_confirmed_artifacts.py::ConfirmedArtifactTests::test_current_nested_link_is_preserved_by_explicit_backup`（L465，symlink 在 L469）
3. `tests/test_workflow_manager.py::TaskProtocolTests::test_changed_skill_and_symlink_outputs_are_rejected_without_writes`（L554，symlink 在 L558）
4. `tests/test_workflow_validators.py::ValidatorAdapterTests::test_identity_symlink_and_path_tamper`（L311，symlink 在 L320）

**已确认的决策（用户跳过追问，采用推荐项）：**
- A-2 基准文案：统一为 `duplicate JSON member: {key}` / `non-finite JSON constant: {value}`（与 `fs.py`、`store.py` 及设计文档一致）。
- B-2：精简保留（不删除/不移动文件，仅裁冗余）；若判断裁改价值低于风险，则以"报告说明"收口。
- B-3：优化压缩 PNG，文件名与 README 引用不变；若无可用无损工具则报告跳过。
- B-5：保守处理（不做大文件拆分）；经核查命名 helper 并非同名多份，避免为无收益的重构引入回归风险。

---

## Task 1（A-1 / B-1）：Windows 符号链接守卫

**Files:**
- Modify: `tests/test_confirmed_artifacts.py:442,465`
- Modify: `tests/test_workflow_manager.py:554`
- Modify: `tests/test_workflow_validators.py:311`

- [ ] **Step 1: 补守卫**（沿用仓库既有约定文案）

在 4 个测试方法上各加一行：
```python
    @unittest.skipIf(os.name == "nt", "symlink creation requires platform privileges")
```
注意：`tests/test_workflow_validators.py` 当前**未导入 `os`**，需补 `import os`。

- [ ] **Step 2: 验证** → 单文件回归，errors 应从 4 降至 0、skipped 相应增加。

---

## Task 2（B-4 + A-2）：收敛严格 JSON helper 与错误文案

**Files:**
- Modify: `scripts/workflow_engine/fs.py:24-34,117,711-712,726-727`
- Modify: `scripts/workflow_engine/store.py:29-39,156-175`
- Modify: `scripts/workflow_engine/receipts.py:16,50-55`
- Modify: `scripts/workflow_engine/studio_server.py:32,147-157,1018-1019`
- Modify: `scripts/verify_workflow_studio_bundle.py:245`
- Modify: `tests/test_workflow_studio_bundle.py:80`

- [ ] **Step 1:** `fs.py` 将 `_reject_duplicate_pairs`/`_reject_constant` 改为公开名 `reject_duplicate_pairs`/`reject_nonfinite_constant`，更新 4 处内部调用点（117、711-712、726-727）。文案保持 `duplicate JSON member: {key}` / `non-finite JSON constant: {value}`。
- [ ] **Step 2:** `store.py` 删除本地重复 helper（156-166），改从 `.fs` 导入公开名，`_strict_json_loads` 使用之。
- [ ] **Step 3:** `receipts.py` 的 `_pairs` 改为调用 `reject_duplicate_pairs` 并把 `ValueError` 转成 `ReceiptError`（保持 `receipt.invalid` 错误码不变）。
- [ ] **Step 4:** `studio_server.py` 删除本地 helper，改从 `.fs` 导入；请求体解析使用导入名。
- [ ] **Step 5:** `verify_workflow_studio_bundle.py:245` 文案改为 `f"duplicate JSON member: {key}"`；同步更新 `tests/test_workflow_studio_bundle.py:80` 断言为 `"duplicate JSON member: schema_version"`。
- [ ] **Step 6: 验证** → 全量测试。

---

## Task 3（A-3）：Studio 投影读取走严格 JSON

**Files:**
- Modify: `scripts/workflow_engine/studio_server.py:434-437`

- [ ] **Step 1:** `_projection()` 的 `json.loads` 增加 `object_pairs_hook=reject_duplicate_pairs, parse_constant=reject_nonfinite_constant`。
- [ ] **Step 2: 验证** → Studio server 相关测试。

---

## Task 4（B-6）：为锁定校验脚本补功能测试

**Files:**
- Create: `tests/test_locked_validator_scripts.py`

只新增测试，**不修改** `humanizer_preflight.py` / `paper_section_validator.py`。

- [ ] **Step 1:** `paper_section_validator.validate(...)` 的边界用例：缺标题、`validity_status=blocked`、body 阶段出现 abstract、final 阶段缺语义回执、zh 小节别名、顺序错误。
- [ ] **Step 2:** `humanizer_preflight` 的纯函数与失败闭合：`detect_format`（后缀/覆盖冲突）、`sha256_file`、`verify_immutable_copy` 检出篡改、`verify_humanizer_skill` 拒绝非 humanizer 包、`main` 对不存在输入返回 blocked/退出码 1。
- [ ] **Step 3: 验证** → 新测试文件单独跑通，且 registry 的 5 个脚本字节未变（`test_validator_registry_matches_repository` 仍通过）。

---

## Task 5（B-2）：docs/superpowers 收敛（保守）

**Files:**
- Modify/评估: `docs/superpowers/**`（3 个文件，约 167KB）
- Reference: `docs/README.md:47-49`

- [ ] **Step 1:** 评估是否存在不影响历史信息完整性的删减空间；若无，保持原样并在报告中说明（不做破坏性改写）。

---

## Task 6（B-3）：PNG 资产优化

**Files:**
- Modify: `assets/banner.png`、`assets/logo.png`

- [ ] **Step 1:** 探测可用无损优化工具；可用则重编码（文件名/引用不变），否则报告跳过。
- [ ] **Step 2:** 确认 `assets/workflow-studio.png` 未被改动（安装器期望清单依赖）。

---

## Task 7（B-5）：测试 fixture 现状核查（保守）

- [ ] **Step 1:** 已核查：`compiled_node`/`task_plan`/`plan_for_document`（`test_workflow_store.py`）与 `task_result`/`task_receipt`（`test_workflow_manager.py`）各只定义一次且仅本文件使用，非同名多份重复。不做无收益重构，仅报告。

---

## Task 8：整体验证（verification-before-completion）

- [ ] **Step 1:** `py -3 -B -m unittest discover -s tests` → 期望 **631 tests / 0 errors / skipped ≥32**。
- [ ] **Step 2:** `git diff --stat` 复核改动范围；确认 5 个锁定脚本与 `assets/workflow-studio/` 未被改动。
- [ ] **Step 3:** 汇报前后对照，不 commit。