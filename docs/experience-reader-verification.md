# 经验读取核验记录

适配 Deskrawl 1.0.2 / Steam build 25817367。新增模块仅复用 `NativeReader` 的只读进程句柄，不调用游戏函数、不注入、不写内存或存档。读取前依赖既有 DLL / metadata 哈希核验，每个新对象额外比对运行时 `FieldInfo` 的字段名、offset 和 token。复制后回读全部可变字段，变化中的快照重试三次，再返回明确的不可用状态。

## 读取入口

- `GameManager` TypeInfo：RVA `0x3A18BF8`，既有马车读取也使用此入口。
- `PlayerData` TypeInfo：RVA `0x3A1CBB8`；本地 getter `flw` @ `0x5202B0` 的 `0x5202D5..0x5202E3` 从该 TypeInfo 的 `+0xB8` 静态字段块读取唯一实例。
- `PlayerData.Level` @ `+64`：16 字节 `ObscuredInt`。
- `PlayerData.CurrentXP` @ `+80`：32 字节 `ObscuredLong`。
- 角色身份使用已经核验的 `SaveSystem` mode / slot 及 `PlayerData.PlayerName`。实例指针只加入连接会话标识，换角色或实例会改变会话。
- 关卡从 `GameManager.ActiveMap` / `RunPlan.mapId` 交叉核对；难度取 `RunPlan.difficulty`。原始 `runId` 可能带账户前缀，公开快照仅给出 SHA256，不泄露原始值。
- 主菜单 `GameState=0`、字段身份不匹配、版本改变、无角色或切图期间均不产生零经验样本。

## 数值包装

- `ObscuredInt.keh` @ `0x454710`：`sub edx,ecx` 后 `xor edx,ecx`，即 `((hidden-key) mod 2^32) XOR key`。
- `ObscuredLong.kfb` @ `0x451340`：相同的 64 位过程。
- 两者完整性算法使用种子 `0x811C9DC6` 和乘数 `0x01000192`，不能套用普通 ACTk 或普通 FNV 常数。
- Long 读函数 @ `0x4513C3..0x451461` 分别散列明文的高低 32 位；结果是 `signed(hash_low) >> 2 XOR (hash_high | 1)`。模块保留有符号移位语义和位宽回绕。
- 尚未初始化的全零包装按游戏只读语义返回 0；有效等级范围另行核验。

## CurrentXP 是本级经验

`Player.fkl` @ `0x518280` 先加本次收益，再逐级比较需求。`0x518474` 调用 `fki(level)`；`0x518479` 的 `sub %rax,%rdi`（AT&T） 从本级 CurrentXP 减去该需求；`0x518498..0x51849F` 将剩余经验写回包装，随后升级。因此累计经验可以由已完成各等级的需求之和，加当前等级内经验重建，不能把 CurrentXP 本身视为终身累计量。

## 升级需求

`Player.fcx` @ `0x500050` 读取当前等级并调用 `Player.fki` @ `0x517E60`。

- `0x517F0B..0x517F9B` 根据等级区间读取 `GameConfig` 的怪物数量锚点，默认值为 `(1,2), (5,35), (15,900), (50,20000), (70,50000)`。
- 插值 helper @ `0x5181E0` 在 `0x518248` 做 `end/start`，`0x518255` 调用 Pow，再乘 start。因此采用几何插值，不能使用线性插值。
- `GameConfig.ccy` @ `0x707EC0` 计算单怪基准经验：`base = float32(XpPerBaseHealth*100)`；`scale = float32(float32(level-1)*XpPerLevelScale)`；结果 `max(1,Round(base*(1+scale)))`。
- `0x517FBE` 调用上述 `ccy`，`0x517FE3` 将单怪经验乘插值怪物数，`0x517FE7` 使用最近偶数取整。舍入 helper @ `0xEC10` 在半整数处分支检查整数奇偶。
- 模块读取当前 GameConfig 值，不把默认锚点写死。每一级分别按游戏步骤舍入，再求和。

默认配置已核对的阈值：

| 当前等级 | 升下一级所需经验 |
| --- | ---: |
| 1 | 200 |
| 2 | 450 |
| 5 | 4,900 |
| 15 | 216,000 |
| 47 | 8,585,767 |
| 50 | 11,800,000 |
| 69 | 37,253,545 |

## 验证与边界

2026-10-09 实机只读两次快照：47 级角色在普通难度 `RedForest5` 的合成累计经验从 `75,859,462` 增加到 `75,861,273`；间隔约 2 秒，本级经验增加 `1,811`，角色和关卡保持一致。未改变游戏操作或存档，也未人为触发升级。

专用检查覆盖数值校验、32/64 位回绕、正负边界、等级锚点、float32 中间计算、跨一次及两次升级、版本和字段身份拒绝、主菜单和切图、无原始账户 runId、满级状态。

满级时游戏将 CurrentXP 清零，后续经验转到 `hh` 巅峰系统（`Player.fkl` @ `0x51850B..0x51855A`）。该路径尚未通过满级角色实机核验，当前版本明确返回不可用并保留手工记录入口，不将 0 误记为没有收益。

RunPlan 可能保留上一场计划，不能仅凭计划存在来划分完整通关周期。“经验收益”页使用手动开始/结束计时，样本应涵盖重复进入和等待；“刷图推荐”的自动观察另外核验新场次、波次和最终收尾标志，参见 [推荐模型](recommendation-model.md)。
