# Deskrawl 装备助手

为 Deskrawl 提供装备筛选、物品整理与自动刷图的 Windows x64 工具，无需安装 Python。当前版本：**v1.6.0**。

**[下载 Windows 版](https://github.com/awesomehy/Deskrawl-Assistant/releases/download/v1.6.0/Deskrawl-Assistant-v1.6.0.exe)** · [大陆下载](https://hhyan.me:25667/s/a1247f09c37c4b4bb7) · [官网](https://deskrawl.hhyan.me/) · [全部版本与更新说明](https://github.com/awesomehy/Deskrawl-Assistant/releases)

## 快速开始

1. 双击 exe，启动游戏并进入角色，在助手顶部点击“连接游戏”。
2. 配置并启用装备筛选规则，在“背包与仓库”检查命中结果。
3. 按需配置托管关卡、换装与回城整理策略，保存后手动启动。

已有版本可使用顶部“一键更新”，升级后保留配置。缺少 WebView2 时，安装[微软官方运行时](https://go.microsoft.com/fwlink/p/?LinkId=2124703)。

## 功能预览

截图取自[官网](https://deskrawl.hhyan.me/)，具体界面以当前版本为准。

### 装备筛选规则

角色传说装备按主／副词条筛选，仆从传说装备按一组随机词条筛选；可配置多套组合与命中数量，支持复制、导入与导出。

![装备筛选规则：选择装备并配置词条组合](docs/screenshots/rules.png)

### 背包与仓库

查看物品详情与换装战力预估，按品质、规则结果或可交易状态筛选；支持自动锁定、双向搬运、分区整理及主城批量上架。

![背包与仓库：查看属性、筛选结果并管理物品](docs/screenshots/inventory.png)

### 托管模式

自动推图、双图轮刷、神话裂隙与南瓜王优先刷取，配合自动换装、回城整理和累计掉落统计。支持神秘商人消费、成长预设、宝石／符文合成、断线重连及交易所／Steam 市场上架。

![托管模式：配置自动刷图与回城整理，查看累计掉落](docs/screenshots/managed.png)

### 掉落预览

查看在线关卡各波次的计划奖励、物品词条与规则命中，高价值掉落可保存原始计划和实际观测记录。

![掉落预览：查看当前关卡计划与高价值掉落监控](docs/screenshots/loot.png)

此外，“日志”可查询物品操作记录，“交流&反馈”提供 QQ 群入口。右上角[助手设置](docs/助手设置说明.md)支持 Steam 社区加速、老板键、打开数据目录与分类重置；关闭窗口会隐藏到托盘，电源按钮或托盘菜单用于退出。

## 兼容性与数据

- 已验证 **Deskrawl 1.1.0a / Steam build 26337088**；游戏更新后可能需要适配。
- 配置与日志保存在 `%LOCALAPPDATA%\Deskrawl装备助手\`，升级保留；Steam 登录成功后自动关闭窗口，凭证使用当前 Windows 用户的 DPAPI 在本机加密保存，重开助手后自动核验。
- 自动合成、交易所上传与 Steam 出售默认关闭，启用前请确认配置；战力和市场报价仅供参考，掉落以游戏实际结算为准。
- Steam 登录确认过期后会提示重新登录并取消自动出售，游戏交易所上传继续；待手机确认数量只显示近期核验成功的数据。

[使用与数据说明](docs/使用与数据说明.md)包含收纳规则、合成与上架限制、旧配置迁移及异常恢复细节。

## 文档与反馈

[版本记录](docs/版本记录.md) · [掉落预览说明](docs/掉落预览说明.md) · [交易所与 Steam 市场](docs/拍卖行与Steam市场验收说明.md) · [源码运行与构建](docs/开发与构建.md) · [第三方资源说明](NOTICE.md)

欢迎通过 [Issue](https://github.com/awesomehy/Deskrawl-Assistant/issues) 报告 Bug 或提出建议，也欢迎点亮 **Star** 支持项目。提交 [Pull Request](https://github.com/awesomehy/Deskrawl-Assistant/pulls) 前，请阅读[贡献与额外授权](CONTRIBUTING.md)：仅为向本项目提交 PR，允许 Fork、修改、编译、测试和提供代码供审阅；是否合并及正式发布由维护者决定。

## 许可证

本项目当前源码采用 [PolyForm Strict License 1.0.0](LICENSE)，另有[仅为提交 PR 的补充授权及贡献约定](CONTRIBUTING.md)。

- 允许免费、非商业使用；未经另行授权，不允许商业使用、修改、制作衍生作品或再分发。
- 为提交 PR 而进行的 Fork、修改和测试适用上述补充授权，不包含独立发布修改版或分发安装包的权利。
- 这是源码可见、限制使用的项目，不属于 OSI 定义的开源软件。游戏资源及第三方组件适用各自许可，见 [NOTICE.md](NOTICE.md)。

新许可自首次包含本次许可变更的提交起，适用于该提交及后续明确采用新许可的版本。此前已按 AGPL-3.0 发布的代码和发行包继续保留原授权，不因本次变更而撤回。
