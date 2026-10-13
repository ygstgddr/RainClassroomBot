# 第三方组件说明

本程序运行或打包使用 Python、Tcl/Tk、Pillow、Playwright（含其 driver/Node.js 组件）、PyYAML、psutil、greenlet、pyee、typing_extensions，以及 PyInstaller 引导程序。玻璃界面为本项目自行实现，不依赖外部主题框架。

Windows 发行包的 `third_party_licenses/` 保存从实际构建环境复制的许可证和声明；Playwright 自带声明也保留在其打包目录中。源码运行时请同时遵守所安装依赖附带的许可证。

`scripts/collect_licenses.py` 根据本机安装包元数据收集许可证并记录版本，构建环境变化后应重新执行。这里的第三方声明不替代各组件的完整许可证，也不为本项目源码另外指定许可证。
