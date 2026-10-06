# 来源与适用边界

按需用于解释、规则维护和期刊要求核查，不是每次绘图都要填的报告。保留既有核查范围；摘要核对不能表述为已核读全文。

解释依据时区分：期刊明文要求只约束该刊、图类和投稿阶段；作者建议提供可尝试的方法；感知理论提供可能解释；实验结果限于研究对象、条件与指标；本图判断用于当前内容和尺寸适配。这些依据不能互相替代，也不要求普通绘图逐项登记。

参考中出现某种形式不证明其效果；外观不能判断制作者身份或科学质量。

### 已核查的来源

核查记录日期为 2026-10-06；以下标明当时核查范围，投稿时重新核对目标期刊。Points of View 专栏原文也可通过 [UCSF 公开合集](https://mcmanuslab.ucsf.edu/sites/mcmanuslab.ucsf.edu/files/event/file-attachments/data-visualization-nature-methods-selected-1.pdf) 核读，以下 PDF 页为从 1 开始的文件页码。

| 来源与类型 | 可迁移的依据与限制 |
|---|---|
| Wong：[Gestalt principles (Part 2)](https://www.nature.com/articles/nmeth1210-941)（PDF 第 6 页）；[Negative space](https://www.nature.com/articles/nmeth0111-5)（第 7 页）；感知原则的设计应用与作者建议 | 隐含几何引导线组织内容；组间留白与组内间距表达层级。Part 2 重点是视觉补全与连续性，并结合前篇的分组原则；不据此规定统一网格或固定留白比例 |
| Krzywinski：[Labels and callouts](https://www.nature.com/articles/nmeth.2405)（第 18 页）；作者建议 | 示意图预先安排标签，控制引线与对齐；数据点标签保持相对其数据点的位置，不能为了标签互相对齐而削弱对应关系 |
| Wong：[Arrows](https://www.nature.com/articles/nmeth.1676)（第 20 页）；作者建议，文中另引用箭头理解实验 | 区分方向/序列箭头与标注引线；箭头头部需可辨且不抢主体，端点留白可帮助分辨。文中的线宽、头部缩放和形状偏好属于作者建议，不直接复制为标准 |
| Wong：[Typography](https://www.nature.com/articles/nmeth0411-277)（第 16 页）、[Salience to relevance](https://www.nature.com/articles/nmeth.1762)（第 8 页）；作者建议 | 字族、字重、字号和间距组织层级；显眼程度应服务任务相关性。具体字体和层级仍由期刊、参考与内容确定 |
| Krzywinski：[Elements of visual style](https://www.nature.com/articles/nmeth.2444)（第 9 页）、[从草稿到发表的制作记录](https://mk.bcgsc.ca/points-of-significance/figures.mhtml)；作者建议与制作案例 | 相关含义使用相关形式，逐对象修订。案例中的工具和历史字号是该作者的做法，不是质量证明或当前投稿要求 |
| Krzywinski 与 Wong：[Plotting symbols](https://www.nature.com/articles/nmeth.2490)（第 19 页）；作者建议，援引符号辨识研究 | 考虑遮挡、类别区分及符号的视觉面积；相同外接宽度不保证相同视觉大小。不把空心圆等建议套给所有数据密度 |
| Reber、Schwarz 与 Winkielman：[处理流畅性与审美综述](https://journals.sagepub.com/doi/10.1207/s15327957pspr0804_3)（[本次核对摘要](https://pubmed.ncbi.nlm.nih.gov/15582859/)）；理论综述 | 提出更流畅的处理与更积极的审美反应有关，可解释清楚分组与稳定符号的可能作用；不推出“越简单越好”或固定几何比例 |
| Cheng 等：[Proving the value of visual design in scientific communication](https://www.benjamins.com/catalog/idj.23.1.09che)；图形摘要实验 | 本次核读出版方摘要：按经典设计原则设计的图形摘要改善读者对趣味性、表达清晰度和科学严谨性的第一印象。不补写未核实的实验细节，不外推为研究真实质量提高 |

综合这些来源，本流程用**稳定的视觉语法、顺畅的阅读路径、清楚的主次和经修订的局部关系**描述完成度。micro-style.md 的锚点、光学校正、透视构造和完成判断，是将上述方法用于当前图的设计判断，并非来源逐条规定。

## 符号与实现机制

- [NIST SI 指南第 10 章](https://www.nist.gov/pml/special-publication-811/nist-guide-si-chapter-10-more-printing-and-using-symbols-and-numbers)：量/变量、单位、描述性或变量下标、命名函数和算子的排版规则；适用领域的符号规范，不替代稿件或期刊规定。
- [Matplotlib errorbar](https://matplotlib.org/stable/api/_as_gen/matplotlib.pyplot.errorbar.html)：下/上误差、端帽长度、点与误差线绘制顺序属于工具机制，外观参数仍由参考与最终尺寸决定。
- [ArcGIS Pro 比例尺](https://doc.esri.com/en/arcgis-pro/latest/help/layouts/scale-bars.html)：比例尺关联地图框并随尺度更新，不能据此要求所有地图添加比例尺或改用 ArcGIS。

## 期刊指南

投稿时核对当前官方说明及适用阶段，区分必需与建议；不能从参考图倒推出要求，也不能跨期刊照搬数值。

- [Nature：Preparing figures — our specifications](https://research-figure-guide.nature.com/figures/preparing-figures-our-specifications/)：字体、标签、坐标、编辑性、字体嵌入及遮挡。
- [Nature：Building and exporting figure panels](https://research-figure-guide.nature.com/figures/building-and-exporting-figure-panels/)：最终尺寸、面板空间、可编辑图层、信息性插图与装饰。
- [PLOS Biology：Figures](https://journals.plos.org/plosbiology/s/figures)：该刊字体、尺寸与文件要求。

## 维护时

新增通用设计规则前核读相关原始来源；只将来源实际支持的范围写成规则。执行动作和完成判断放在 micro-style.md，解释与链接留在本文件。当前图中因内容、密度、字形或尺寸作出的合理适配，标为本图判断即可，不包装成已证实的普遍标准。
