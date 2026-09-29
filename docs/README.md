# 文档导航

按你当前要完成的事选择入口。日常使用从 README 开始；需要调整流程时阅读 Studio 教程；修改代码时再查开发指南和实现合同。

| 我想…… | 先读这里 |
|---|---|
| 安装并用默认流程推进论文 | [中文 README](../README.zh-CN.md#快速安装) · [English README](../README.md#quick-installation) |
| 自行增删阶段、选择 Skill | [Workflow Studio 入门指南](workflow-studio-guide.md) |
| 理解保存草稿、启用和实际执行的区别 | [草稿与当前流程](workflow-studio-guide.md#草稿与当前流程) |
| 找到自定义流程的常见问题处理方法 | [问题排查](workflow-studio-guide.md#问题排查) |
| 接着推进长期项目，找到目前认可的成果 | [进度与已确认产物使用指南](progress-and-artifacts-guide.md) |
| 区分工作草稿、当前确认版本与历史版本 | [修改、确认与撤回](progress-and-artifacts-guide.md#修改确认与撤回) |
| 修改程序或参与贡献 | [开发指南](../DEVELOPMENT_GUIDE.md) |
| 了解版本变化 | [更新记录](../CHANGELOG.md) |

## 用户操作与代码目录如何对应

| 使用场景 | 相关目录或入口 | 什么时候需要打开 |
|---|---|---|
| 在 Codex 对话中使用工作流 | `SKILL.md`、`companion-skills/` | 了解能力定义与阶段路由时 |
| 在浏览器中编辑自定义流程 | `scripts/workflow_studio.py` | 启动编辑器时；命令见教程 |
| 了解自定义流程如何校验和推进 | `scripts/workflow_manager.py`、`scripts/workflow_engine/` | 开发、调试或扩展执行机制时 |
| 更新官方进度或查看当前确认版本 | `scripts/progress_manager.py`、`scripts/artifact_manager.py` | 调试进度更新、产物采纳和恢复时 |
| 修改编辑器界面 | `studio/` | 前端开发时 |
| 安装后运行图形界面 | `assets/workflow-studio/` | 由启动器自动加载；日常使用无需处理这些文件 |
| 检查实现是否符合预期 | `tests/`、`studio/src/` 中的测试、`studio/e2e/` | 开发和验证改动时 |

论文材料和运行记录保存在你自己的论文项目目录。这个仓库保存工作流工具本身；安装后的 Skill 目录保存运行所需的工具文件。

## 实现合同

需要确认某个字段、阶段或验证规则的精确定义时，查阅对应合同：

- [默认工作流阶段](../references/stage-contracts.md)
- [自定义工作流执行合同](../references/custom-workflow-contract.md)
- [进度与证据记录格式](../references/progress-schema.md)
- [已确认产物的生命周期与请求格式](../references/confirmed-artifacts.md)
- [论文章节规则](../references/paper-section-contract.md)
- [科研图件集成](../references/scientific-visualization-integration.md)
- [终稿编辑集成](../references/final-editor-integration.md)
- [语言自然化集成](../references/humanizer-adapter.md)

## 设计与验证记录

这些文件记录实现时的决策与当时的检查结果，适合回溯设计原因。当前功能和用法以 README、用户教程及现有代码为准。

- [自定义工作流设计](superpowers/specs/2026-08-31-custom-workflow-studio-design.md)
- [工作流引擎实施计划](superpowers/plans/2026-08-31-custom-workflow-engine.md)
- [Studio 实施计划](superpowers/plans/2026-08-31-workflow-studio.md)
- [v1.1.0 验证记录](release-verification-v1.1.0.md)
