# 第三方资源

装备图标、装备名称及游戏本地化字典来自 Deskrawl，相关权利归游戏权利人所有。本仓库没有包含游戏程序或游戏存档。README 中的使用截图由用户提供。

运行及打包使用 Python、pywebview、pythonnet、PyInstaller 等组件；发行包由 `tools/build_exe.py` 自动收集对应组件的许可证，写入“第三方许可.txt”。提取资源时使用 UnityPy，日常运行助手不依赖它。

WebView2 Runtime 属于 Microsoft。可选安装程序从微软官方下载，不纳入源码仓库；安装程序显示其适用许可条款。

本项目软件源码采用 [GNU AGPL-3.0](LICENSE) 许可证。上述游戏资源及第三方组件适用各自的许可。
