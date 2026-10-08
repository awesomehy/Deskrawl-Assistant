# Deskrawl 装备助手

Windows x64 装备筛选工具，当前版本 **v1.1.2**。使用 WebView2 独立窗体显示网页界面，外部读取游戏的背包和仓库，根据玩家配置的词条规则锁定装备。

## 快速使用

1. 打开 [Releases 下载页面](https://github.com/awesomehy/Deskrawl-Assistant/releases)，选择最新版；也可直接下载 [Deskrawl-Assistant-v1.1.2.exe](https://github.com/awesomehy/Deskrawl-Assistant/releases/download/v1.1.2/Deskrawl-Assistant-v1.1.2.exe)。
2. 双击 exe，稍等片刻即可打开助手窗体，无需安装 Python。若提示缺少 WebView2，请使用[微软官方安装程序](https://go.microsoft.com/fwlink/p/?LinkId=2124703)联网安装后重试。
3. 启动 Deskrawl 并进入角色，在助手顶部点击“连接游戏”，自动读取背包与仓库。主城和战斗场景均可使用。
4. 进入“装备筛选规则”，按职业、部位或名称选择装备；勾选主词条并设置“至少命中 n 类”，按需启用副词条，保存并启用规则。
5. 进入“背包与仓库”，点击“一键按规则锁定”检查已有装备；需要自动处理后续新装备时，开启“持续监控”。点击装备可查看数值、命中高亮及筛选结果。

更新前请通过助手右上角退出旧版，再运行新版。个人规则会继续保留。各版本的更新记录和适配范围见对应 Release 页面。

## 使用截图

**装备筛选规则**：按职业、部位或名称找到装备，再配置可接受的主副词条及命中数量。

![装备筛选规则：选择装备并配置主副词条](docs/screenshots/rule-settings.png)

**背包与仓库**：批量管理装备锁定状态，查看装备数值与筛选结果；绿色和 ✓ 表示命中规则的词条。

![背包与仓库：装备清单、数值预览及命中高亮](docs/screenshots/inventory-preview.png)

## 功能

- 顶部显示连接按钮与游戏连接状态；连接后自动读取背包和仓库。
- 按职业、部位、名称筛选 52 件传说装备，并显示游戏装备图标。
- 为每件装备勾选可接受的主词条类型，配置“至少命中 n 类（≥n）”。同类型只计一次，任意 n 类符合即可。
- 副词条可选；启用时，主词条和副词条两组均达标才锁定。
- 装备详情显示词条数值与百分比，包含已经核验的装备强化换算；命中已启用规则的词条显示绿色和 ✓，部分命中也会高亮。
- 基础护甲、基础武器伤害与速度单独显示，不参与主副词条计数或高亮。
- 背包和仓库支持全部解锁、锁定所选装备、按规则锁定，以及持续监控新增装备。
- 支持规则导入导出、清单导出、停止操作与操作记录。

操作不要求游戏位于前台或打开背包，不占用鼠标键盘。持续监控默认关闭；开启后先记录当前装备，再检查新增装备。解锁全部装备会停止监控，避免立即锁回。

## 使用与数据保存

在 [GitHub Releases](https://github.com/awesomehy/Deskrawl-Assistant/releases) 下载打包好的程序，推荐使用 [v1.1.2](https://github.com/awesomehy/Deskrawl-Assistant/releases/tag/v1.1.2) 的 exe。直接双击即可打开窗体，无需安装 Python。页面每个版本只上传 exe，并提供更新记录；缺少 WebView2 时，可使用[微软官方安装程序](https://go.microsoft.com/fwlink/p/?LinkId=2124703)联网安装。

历史版本 v1.1.0、v1.1.1 提供原始发布程序；这两个标签只保存发行说明和组件许可，没有对应构建时的完整源码快照。当前完整源码对应 v1.1.2。

发行版规则与日志保存在 `%LOCALAPPDATA%\Deskrawl装备助手\`，移动或更新 exe 后仍保留。源代码版保存到工作目录的 `config/` 和 `data/runtime/`，这些个人文件不纳入仓库。全新启动没有任何启用的筛选规则。

助手界面和服务在本机运行，默认地址为 `http://127.0.0.1:18741/`。重复启动复用已有助手；关闭窗体会退出服务并停止监控，最小化时监控继续。更新时先正常退出旧版本，再打开新版。

## 从源码运行

使用 **Windows 和 64 位 Python 3.12**；已验证的构建环境为 Python 3.12.14。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-runtime.txt
.\.venv\Scripts\pythonw.exe run_assistant.pyw
```

也可在创建 `.venv` 并安装依赖后双击 `启动装备助手.cmd`。

## 测试

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -q
```

当前包含 128 项自动检查，使用合成或脱敏用例及模拟内存，不要求运行游戏或提供个人装备数据。检查覆盖规则计数、基础属性排除、数值与强化展示、匹配高亮、身份验证、锁状态写入范围、监控基线、配置保存、网页来源校验和窗体退出。

已另外验证单文件 exe 在独立中文目录及没有外部 Python 的环境启动，并只读连接实际游戏。v1.1.2 的排版和交互已由用户确认。尚未在另一台电脑或全新 Windows 虚拟机上测试。

## 构建单文件 exe

```powershell
python -m venv .build-venv
.\.build-venv\Scripts\python.exe -m pip install -r requirements-build.txt
Invoke-WebRequest -Uri 'https://go.microsoft.com/fwlink/p/?LinkId=2124703' -OutFile 'packaging/MicrosoftEdgeWebview2Setup.exe'
.\.build-venv\Scripts\python.exe tools/build_exe.py
```

构建结果输出到 `release/`，包含单文件 exe、使用说明、第三方许可和可选的官方 WebView2 安装程序。资源清单只包含必要静态字典、网页和 209 张图标，个人规则、装备快照、日志及游戏程序均不打包。

可用以下命令验证打包后的独立窗体与数据持久化；最后一项需要本机正在运行受支持的游戏：

```powershell
.\.build-venv\Scripts\python.exe tools/smoke_exe.py --desktop
.\.build-venv\Scripts\python.exe tools/smoke_exe.py --desktop --connect-game
```

## 适配范围

只适配当前已核验的游戏版本；读取前会核对程序和元数据哈希。游戏更新后需要重新核验，版本不匹配时拒绝装备写入。

- `GameAssembly.dll` SHA256：`25067eba0b1b294cd7dbe7ee34761e70603c6b9b9822b01f4e243ed05b3694cc`
- `global-metadata.dat` SHA256：`c1bebda964147ef5511f559162cc10ec3abf1c65ab0ece930396d1db39c90e05`

锁定动作会重新核验装备身份和字段，写入锁状态及游戏待保存标记，并回读确认。游戏自身负责正常保存；游戏锁图标可能在重新打开背包后刷新。

当前规则只按词条类型计数，不提供数值门槛。未揭示黑雾、词条分组无法确认或读取不完整的装备不自动锁定；已经出售或分解的装备无法追回。披风的已验证随机词条池为空，暂不支持该部位的词条筛选。切换角色后请关闭再开启监控，以重建基线。

## 代码与资源

| 路径 | 内容 |
| --- | --- |
| `deskrawl_assistant/web/` | 网页界面、图标、数值与高亮展示 |
| `deskrawl_assistant/web_service.py` | 筛选规则与背包/仓库操作 |
| `deskrawl_assistant/native_window.py` | WebView2 窗体与单实例管理 |
| `deskrawl_assistant/native_reader.py` | 外部只读装备读取 |
| `deskrawl_assistant/background_lock.py` | 经身份校验的锁状态写入 |
| `deskrawl_assistant/stat_display.py` | 数值、百分比与强化显示换算 |
| `data/` | 必要静态字典和版本结构信息 |
| `tests/` | 自动检查与脱敏测试用例 |
| `packaging/`、`tools/build_exe.py` | Windows 构建设置与脚本 |

部分旧界面源码保留用于兼容和排查。`tools/verify_container_transfer.py`、`tools/verify_carriage_transfer.py` 是搬运功能的开发原型，未接入 v1.1.2 窗体或发行包；相关测试只验证模拟事务。

资源提取工具只读解析本地游戏安装文件，额外依赖 `requirements-unity.txt`；日常运行无需 UnityPy。版本变更见 [版本记录](docs/版本记录.md)，第三方资源说明见 [NOTICE.md](NOTICE.md)。

## 贡献

欢迎通过提 [Issue](https://github.com/awesomehy/Deskrawl-Assistant/issues) 或 [Pull Request](https://github.com/awesomehy/Deskrawl-Assistant/pulls) (如果提供了仓库链接) 的方式贡献代码、报告 Bug 或提出建议。

## 许可证

本项目软件采用 [AGPL-3.0](LICENSE) 许可证。
