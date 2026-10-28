# 第三方组件说明

0.8.3 桌面材质设计参考 [Javis603/token-monitor](https://github.com/Javis603/token-monitor)（MIT，Copyright (c) 2026 Javis）的 Acrylic 与透明渲染分层思路，以及 Lens / Uiverse 玻璃视觉；实现为独立的 Python/ctypes 与 CSS，不内嵌上述项目的运行时代码。具体参考版本和链接见 `gui/web/DESIGN.md`。

0.8.4 将 Token Monitor 的 `src/electron/windowsBackdrop.js` 中 Accent 配方移植为 Python/ctypes，以保留失焦透明效果。上游版本 `71ce5c6caed60f4f6afd3df16c2ca15d3c012c3a`；版权与完整 MIT 许可证保留在 `third_party_licenses/token-monitor/LICENSE.txt`。未引入 Electron 运行时或 Token Monitor 的账号、采集功能。

本程序运行或打包使用 Python、Tcl/Tk、Pillow、Playwright（含其 driver/Node.js 组件）、PyYAML、psutil、greenlet、pyee、typing_extensions，以及 PyInstaller 引导程序。

0.6.0 的默认桌面新增 pywebview 6.2.1，以及其实际安装的 pythonnet、clr_loader、bottle、proxy_tools、cffi、pycparser 等传递依赖。HTML/CSS/JavaScript 玻璃界面由本项目自行实现，不使用外部主题框架或 CDN。

Microsoft Edge 浏览器和 Microsoft Edge WebView2 Runtime 由用户安装，分别用于任务浏览器和桌面渲染，并遵循 Microsoft 附带的许可条款。WebView2 Runtime 下载入口为 [Microsoft 官方页面](https://developer.microsoft.com/en-us/microsoft-edge/webview2/consumer/)；程序 zip 不内嵌完整 WebView2 Runtime。

Windows 发行包的 `third_party_licenses/` 保存从实际构建环境复制的许可证和声明；Playwright 自带声明也保留在其打包目录中。源码运行时请同时遵守所安装依赖附带的许可证。

`scripts/collect_licenses.py` 根据本机安装包元数据收集许可证并记录版本，构建环境变化后应重新执行。这里的第三方声明不替代各组件的完整许可证。

`proxy_tools` 的旧版 wheel 未附带许可证文件，源码保留其上游许可证副本于 `third_party_licenses/proxy_tools/LICENSE.txt`，打包脚本会一并复制。其他组件的实际构建版本记录在发行包的 `third_party_licenses/versions.json` 中。

## 雨课堂图标

官方图标取自雨课堂官网 favicon。图标与品牌标识归原权利人所有，项目代码许可证不覆盖品牌标识；来源与校验值见 `gui/assets/ICON_SOURCE.md`。
