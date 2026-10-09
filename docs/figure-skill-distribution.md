# 绘图 Skill 分发说明

`reference-first-figures` 是本项目开发的参考设计与视觉对照 Skill，源码位于
`companion-skills/reference-first-figures/`，按本项目 MIT 许可证分发。

`nature-figure` 是第三方实现 Skill。`standard` 和 `full` 安装器从
[`Yuan1z0825/nature-skills`](https://github.com/Yuan1z0825/nature-skills/tree/f3941a1722e39af78b24bc7a34167b8880629545/skills/nature-figure)
的 `skills/nature-figure/` 获取，固定提交为
`f3941a1722e39af78b24bc7a34167b8880629545`。该提交的根目录许可证为 MIT，
版权归 Yuan Yizhe；安装器将其保存在安装目录的 `UPSTREAM-LICENSE` 中。
清单记录来源和版本，安装回执记录实际文件哈希。上游后续版本的许可或行为
变化需要另行评估，固定提交保持不变。

主仓库保存项目自建 Skill 和接续合同，第三方 Skill 由安装器按清单下载。
安装后的第三方内容与固定上游保持一致，项目适配集中在
[绘图实现适配](../references/figure-implementation-adapter.md)和
[接续合同](../references/reference-led-figures.md)中。控制器在派发任务时传递
认可的设计、后端、面板范围、固定输入与回执要求。

上游包中可能含有演示脚本、图像及其自身来源说明。安装不会执行这些脚本；
使用示例或外部图像前应查阅其来源和授权，论文示例数据与实际研究数据分别管理。
项目的 MIT 许可适用于自建内容，第三方材料继续适用原始授权。

独立使用参考设计 Skill 时，可按任务选择适合的实现工具。自定义运行中，
每个 Skill 绑定到独立节点，通过冻结输入和管理器回执交接；已有运行保持
启用时固定的 Skill 身份。更新安装后，需验证并显式启用新的运行版本。
新增资源时同步检查相对链接、许可、安装回执和真实图件输出。
