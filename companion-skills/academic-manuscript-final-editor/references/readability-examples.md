# Meaning-first readability examples

Use these cases when fluent prose is repetitive, defensive, nominalized, or difficult to unpack. They are synthetic teaching examples, not quoted manuscript findings or reusable scientific conclusions. Read the surrounding argument before applying a pattern.

## Decision map

Ask what would be lost if the sentence disappeared. A definition, measured result, supported inference, useful signpost, or qualification that changes interpretation is information. Merely repeating an explicit premise or announcing that several analyses form an "interpretation framework" is not.

Choose the smallest useful action: **delete** a no-gain restatement, **rewrite** needed information, **split** different arguments, **move** reproducibility details, **retain** a consequential limit, or **query** an undefined concept. Do not fill the space left by a deletion with a smoother but equally empty summary.

## 1. Repeating a stated scope — delete

Before: “研究采用数据库定义的近岸海域网格；海域范围沿用数据库定义。”

After: “研究采用数据库定义的近岸海域网格。”

The second clause repeats the first. Retain it if it actually distinguishes a different spatial boundary, exclusion, or source convention. Keep any citation supporting the remaining definition.

## 2. Restating a condition's consequence — delete in context

Context: the preceding sentence already says all groups use the same input variables, model specification, and filtering rules.

Before: “因此，各组采用相同的变量、模型和筛选条件。”

After: omit the sentence; continue to the result being compared.

If equal specifications are being contrasted with unequal sample support, retain that contrast once: “各组使用相同模型，但有效样本数不同。” Do not suppress the unequal-support qualification.

## 3. Abstract packaging after concrete tasks — delete, not rebrand

Before: “模型预测下一月的观测值，方向性分析在给定控制项和滞后下检验序列关系。二者共同构建预测支持与方向识别的解释对象界定。”

After: “模型预测下一月的观测值，方向性分析在给定控制项和滞后下检验序列关系。”

Rejected replacement: “二者共同界定解释范围。” It adds no relation beyond the stated tasks. If the analyses disagree, state the supplied disagreement instead; do not invent agreement, integration, or causality.

## 4. A defensive catalogue of unused methods — replace with the operative limit

Before: “特征遮蔽以训练均值替换一个输入，比较替换前后的预测。该分析不能替代完整解释分解，也不能替代干预研究，更不能证明生态因果效应。”

After: “特征遮蔽以训练均值替换一个输入，比较替换前后的预测；所得差异反映模型对该输入的依赖，而非干预效应。”

The intervention distinction is material; the catalogue is not needed here. If the same limit already governs this paragraph unambiguously, remove the repeated tail instead of adding it again. Preserve a specific reason an unused method matters when method selection itself is under discussion.

## 5. Let a contrast carry its conclusion — preserve the target distinction

Before: “A 面板的误差低于基线，B 面板高于基线。因此，不能保证所有面板都获益，也不能证明绝对丰度变化。”

After: “A 面板的误差低于基线，B 面板高于基线。” Keep the observation-versus-abundance distinction at the response definition or at this claim if the reader otherwise could mistake the measured target.

The measured contrast already disproves uniform benefit. It does not establish abundance change, ecological causation, or the reason for the difference. A necessary target distinction must not disappear from the manuscript just because the uniform-benefit tail is redundant.

## 6. Nominalized operations — name what is done

Before: “通过对环境输入替换操作的开展，实现模型条件输入依赖的量化表征。”

After: “用训练均值替换一个环境输入，计算替换前后模型预测的差值。”

Use this replacement only if those operations and the difference measure are supplied. Otherwise ask which replacement and comparison were used. Do not infer a causal effect or an unreported metric from “量化表征”. Nominalization itself is not an error: defined quantities and standard technical nouns remain useful.

## 7. Mixed limitations — split by the inference affected

Before: “部分面板的测试性能较差，方向估计受到自相关和遗漏变量影响，外部指数与样本的年龄组成不一致，图连接还需敏感性检查。”

After, as separate paragraphs in the relevant discussion:

“部分面板的测试性能较差；图连接的敏感性尚待检查。”

“方向估计仍受到自相关和遗漏变量的影响。”

“外部指数与样本的年龄组成不一致，限制了两者的可比性。”

This reorganization preserves the supplied concerns; it does not prove their causes. Do not turn failed directional confirmation into a claim of failed prediction transfer. Retain each citation beside the claim it actually supports.

## 8. Undefined compound terminology — query, do not guess

Before: “相邻档对称压力进入组成包络的推进。” No supplied definition identifies the quantity, direction, or denominator.

After: record an unresolved question: “这里转移的是数量、质量还是比例？转移方向、系数和归一化方式分别是什么？” Leave the scientific formula unchanged pending clarification.

Do not replace it with a plausible population-transport mechanism. An unfamiliar term is not necessarily fabricated; check supplied definitions first. An editorial query belongs in feedback, not as a new scientific assertion in the manuscript.

## 9. A defined implementation term — explain, preserve

Context: the methods define a fallback vector whose every component equals one.

Before: “该面板使用全一回退权重。”

After: “该面板的各项输入权重均设为 1。” Preserve the defined term too if equations, tables, or code use it and readers need the correspondence.

This describes the operation without implying why confirmation failed or how prediction performance changed. Similarly, preserve a formally defined observation kernel rather than relabeling it because its name sounds abstract.

## 10. Bookkeeping beside a result — move or delete

Before: “两个面板满足预设转换判据。系数、区间和判据均按面板保存。”

After in Results: “两个面板满足预设转换判据。”

A bare storage announcement can be omitted. A useful, supplied retrieval location belongs in data/code availability or the supplement; do not invent a repository or accession. Keep storage details in a reproducibility audit whose object is the stored evidence itself.

## 11. Implicit to the programmer, not to the reader — retain

Before: “缺失节点以零填充，并以掩码排除出损失和指标；填充值不表示观测为零。”

After: retain, or shorten to “缺失节点以零填充，但不参与损失和指标计算；零填充与观测零值分别处理。”

A filler value and a measured zero have different meanings. This distinction is not an empty reminder just because the implementation already enforces it. Graph edges representing computation rather than observed movement are another potentially consequential distinction.

## 12. A substantive issue with no numbers — retain

Before: “部分输入的来源尚未核实。未来暴露负对照未通过，因此该面板的方向信号未获确认。”

After: retain the provenance issue in the appropriate disclosure and retain the failed-confirmation result. Address the evidence gap separately if authorized.

No digit or citation is needed for either statement to affect trust or inference. Deleting these sentences to make prose confident would conceal unresolved validity issues.

## 13. Useful synthesis versus a hollow summary — decide by the added relation

Hollow: “这些结果共同形成多层证据的解释框架。” Delete if the paragraph already lists the findings.

Useful, when supported: “方向检验通过的面板仍出现测试误差上升，说明方向确认与预测改善在本研究中并不一致。” Retain: it relates two measured outcomes rather than simply naming their coexistence. Preserve the local study scope; it is not a universal claim that directional information is useless.

## 14. Navigation is a legitimate function — retain when useful

Before: “式（1）定义预测误差，式（2）给出重采样区间。”

After: retain if readers need this map, particularly when adjacent formulas serve different estimands. Omit if each formula is already directly labeled and the map merely repeats those labels. Do not judge navigation only by whether it reports a new result.

## Check the edited passage

Confirm that each surviving sentence now has a discernible function; that deletion did not widen a claim or orphan a citation; that concrete verbs follow the supplied method; and that unresolved definitions remain questions rather than invented explanations. Numbers, units, equations, formal names, uncertainty, and comparison direction still follow the protected baseline.
