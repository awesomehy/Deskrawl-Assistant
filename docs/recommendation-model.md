# 当前角色自动刷图推荐模型

推荐不要求先逐图计时。它读取角色当前等级、经验加成、最终属性和当前普通攻击技能，用游戏安装资源里的怪物、波次、随机权重及原生经验规则计算首次结果；正常刷图的完整记录随后校准时间。模型本身只做纯计算，不调用游戏方法、不操作游戏和存档。

## 目录和接口

- `load_recommendation_catalog(path=None)` 读取 `data/recommendation-catalog.json`。
- `profile_effective_combat(profile, catalog=None)` 返回 `combat` 字典，供推荐和观察开始时保存。已有正数 `normal_dps`/`boss_dps` 优先；否则优先计算带 Basic 标签 `0` 的技能实伤，最后才用最终面板属性初始化。
- `enemy_health(base_health, level, difficulty_dict, config, elite=False)` 返回最终血量的 double 工作量。高难度基础血量的正数覆盖须先应用。
- `estimate_run(profile, summary_or_waves, catalog=None, options=None)` 给当前已生成计划独立计算阶段血量、奖励及理论时间。Boss 波包含该波所有小怪。
- `recommend_maps(profile, catalog=None, calibration=None, options=None)` 返回 `available/reason/rows/best/warnings/coverage`。

`profile` 使用普通数值字段：`level`、`xp_gain_multiplier`、`fingerprint`、`difficulty`、`combat`、`skills`、`timing`、`unlocked_maps`、`entry_items`、`is_demo`。经验倍率是真正的 `1 + bonus`，不是 `RunFinds.xpGain` 原始 bonus。`unlocked_maps` 按难度分组；完成关卡集须通过前置关卡规则派生为可选关卡，不能直接当作可选集。未读取的其他难度进度显示待确认，不作为最佳图候选。

`options` 为 `minutes`（默认60、最多10080）、`difficulty`（默认current）、`overhead_seconds`（默认8）、`include_locked`（默认true）、`sort`（completed或rate）。默认按预算内完成刷图尝试的预期经验排序；rate按长期经验速度排序，不要求预算够一轮。预算模式只有当前可进入且预计至少完成一次通关的关卡能成为最佳图。

资源目录含76张图，其中71张常规图参与排序、65种引用敌人；5张特殊/神话图依赖动态参数，列在 `coverage.excluded`，不把常规图最佳结果宣称为所有特殊图的最佳。地图和难度名称来自游戏 `World`/`UI` 简中本地化表。

## 已核验规则和证据

只适用于以下游戏构建；提取工具先校验两个 hash，再按已验证序列化布局读取资源。更新构建后应重新核验布局及公式。

- metadata SHA256：`2c0ae47e1ee26b6c787d5294f04680b6b875d84e8d5f6db1e574446892e3f2ad`
- GameAssembly SHA256：`c242676ed070072388b4231a5c215812c8bc3d53c21f9277c7c0afa4fa4fa2da`

| 规则 | 已核验原生方法 RVA |
| --- | --- |
| 敌人基础经验、等级线性缩放 | Enemy.cmg `0x73BC80` |
| 正数计划经验直接发放；非正数fallback | GameManager死亡奖励 `0x77DFC0`；fallback `0x77E080` |
| 普通地图难度经验倍率 | MapData.cha `0x711870` |
| 普通敌人等级范围；高难度等级70 | MapData.cgt/cgu `0x7114F0/0x711590` |
| 血量/伤害分段增长 | GameConfig.ccv `0x707D50`；Enemy.bjv `0x744320` |
| 高难度正数基础血量/伤害覆盖 | Spawn coroutine `0x76AFD1`，血量写入 `0x76B017` |
| Boss替换末波 | WaveManager.esn `0x7F5420` |
| 每个非Boss波独立精英抽签 | WaveManager.eso `0x7F59E0` |
| 遭遇池权重选择 | WaveManager.esp `0x7F5BF0` |

基础奖励为 `max(1, round_even(baseHP × XPperBaseHP × (1 + float32((enemyLevel-1) × scale))))`。HP与外层乘积为double，等级乘法为float32。fallback再依次做float32等级惩罚、角色经验倍率、难度经验倍率的乘法，最后round-even并至少给1。等级惩罚为 `clamp((6 - (playerLevel-enemyLevel))/6, 0, 1)`。低等级怪惩罚到零时原生仍给1，不自行改成0。

正数 `PlanEnemy.xp` 已包含当时的等级、经验加成和难度规则，死亡路径直接发放，不能再乘加成。零或负数计划XP走当前角色fallback，不能一概认为所有计划奖励都是精确正数。

血量增长的三个指数分别为 `min(level-1,29)`、`clamp(level-30,0,20)`、`clamp(level-50,0,20)`，基数为 `1 + float32(.12/.08/.06)`，使用double幂；精英血量×3，难度血量普通×1/噩梦×3.5/炼狱×6。正数高难度基础覆盖先替换asset基础HP，也影响基础XP。

普通波按资源池非负权重求期望；权重合计零时等概率。Normal使用资源等级区间均匀求期望，噩梦/炼狱普通图使用等级70。精英抽签作用于整波，不把概率错误地当成每个怪独立生成。精英实际HP增加，基础XP不因精英直接翻倍。Boss替换总波次中的最后一波。

例如维尔达克高地共7关，包括第4关小Boss和第7关大Boss。第4关是11普通波+1Boss波，Boss波含冰猎犬1只及史莱姆10只；第7关是9普通波+1Boss波，含冰霜龙1只及幼龙6只。并非7普通关再另加两个Boss关。

实机47级普通难度、bonus1.322（倍率2.322）：57级僵尸战士assetHP300的奖励4598；幼龙assetHP350的奖励5364。当轮维尔达克高地5生成15战士+15幼龙，计划全清奖励149430。相同角色反复刷该图的资源权重期望约147956.923，不能用本轮149430覆盖重复刷图的随机期望。这些是单轮理论奖励，不能直接称作实测一小时收益。

原始只读证据保存在 `D:\Download\Deskrawl-Assistant\cache\feasibility\xp_*.txt/json/bin`，其中 `xp_live_plan.json` 为当轮计划，`xp_recommendation-smoke.json` 为当前角色自动推荐验证输出。

## 时间、校准和失败收益

普通阶段理论秒数为普通/精英总HP除以normalDPS；Boss阶段为整Boss波HP除以bossDPS。再加首次生成等待、波间等待、已知接近距离/移速，以及重开开销。已读取等待为首波1.5秒、波间2.5秒。没有可靠接近距离时不编造距离，结果附移动/覆盖提示。

普通攻击优先使用已经按技能等级、攻击缩放、属性和伤害类型加成计算的 `DamageEffect.damage`，按可暴击标识加入暴击期望。施放周期取有效CD、全局CD、站桩和引导时间中的最大值。可读取的EveryNth替换攻击按完整周期平均；额外弹道不假设全部命中同一个目标。AoE缺少可靠密度时默认只覆盖1目标。额外技能、耗蓝循环、穿透命中、控制和未知触发不假装精确，结果附提示并由实测时间校准。

`calibration.runs` 记录起始fingerprint、map_id、family、difficulty、level、combat、阶段HP、阶段时间、阶段固定等待、总run_seconds、outcome、retained xp和可观察的overhead_seconds。配装/角色指纹与难度不一致的记录不混用。阶段校准以 `(observedPhaseSeconds-phaseFixed)/(phaseHP/savedPhaseDPS)` 的中位数校正当前阶段，可迁移同系列。只有整轮总时长时只校准同图，不能虚构普通/Boss分别的DPS。手填的重开开销优先；默认8秒可由同图或同系列连续重开的观测中位数替代。

失败记录不凭等级差或血量自行生成成功概率。至少3个同级、同配装、同图、同难度的完整成功/失败观察，并且失败保留经验已知，才估计成功比例及含失败部分XP的周期收益。旧记录失败经验按保存的起始经验倍率归一化到当前倍率，避免临时buff混用。少量样本仍提示波动；资料不足时只标失败风险和成功全清收益。

`completed_runs_xp` 只表示预计完整成功通关部分；`effective_budget_xp` 表示预算可完成尝试的期望收益，满足失败采样条件时包含失败保留经验。界面有失败周期时应显示“计划可获经验”。`projected_xp` 为长期速度按预算折算，不能混作预算内完整通关经验。当前属性和等级用于本次估计，升级/换装后刷新，不预言未来升级或掉落改变配装。

## 重提取与验证

提取只读取安装文件，UnityPy可安装在指定D盘临时target；工具不导入/执行游戏DLL。使用 `tools/extract_recommendation_catalog.py --game E:\Steam\steamapps\common\Deskrawl --unitypy-path D:\Download\Deskrawl-Assistant\cache\feasibility\libs` 可重建目录。`tests/test_recommendation.py` 覆盖原生float32经验、关卡资源、难度准入、完整预算/rate排序、技能周期、校准隔离和失败保留收益。
