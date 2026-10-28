# 毛玻璃材质说明

2026-10-28：0.8.4 本地界面更新，未变更学习执行逻辑。

0.8.4 使用 Token Monitor 的 Accent 配方并保留其 MIT 许可证，修复系统 Acrylic 失焦后退成实色的问题；`SetWindowCompositionAttribute` 为可选扩展接口，失败后回退系统 Acrylic。窗口框架处理 `WM_NCPAINT` 和 `WM_NCACTIVATE`，跳过多余系统框线重绘，同时保留原生激活消息。初始化材质后，通过窗口消息重新计算非客户区合成并恢复实际激活状态，解决首次打开纯黑、切换窗口才恢复透明的问题；不改变输入焦点。以下 0.8.3 的仅公开 DWM 实现描述为历史设计。

## 参考

- [Javis603 / Token Monitor](https://github.com/Javis603/token-monitor/tree/71ce5c6caed60f4f6afd3df16c2ca15d3c012c3a)：阅读 `src/electron/main.js` 的 `backgroundMaterial: 'acrylic'`、`windowsBackdrop.js` 和透明 renderer CSS。借鉴原生材质与透明网页分离的方法，使用 Python/ctypes 独立实现，没有引入 Electron 或其业务代码。
- [Microsoft DWM_SYSTEMBACKDROP_TYPE](https://learn.microsoft.com/en-us/windows/win32/api/dwmapi/ne-dwmapi-dwm_systembackdrop_type)：公开的 `DWMSBT_TRANSIENTWINDOW` 是 0.8.3 的默认实现，0.8.4 保留为 Accent 不可用时的回退。

- [ruiqichenbiec / Design Systems](https://ruiqichenbiec.github.io/design-systems/)，重点查看 [Lens 展示](https://ruiqichenbiec.github.io/design-systems/lens/lens-showcase.html)：边缘高光、接触阴影、不同厚度的玻璃，以及操作时的形变反馈。
- [codebykay101 / Uiverse glass card](https://uiverse.io/codebykay101/ugly-lion-23)：半透明渐变、背景模糊、悬停反馈。作者 CodeByKay，页面标注 MIT。

本次按上述视觉原则独立编写 CSS，没有引入参考网站的运行时代码、着色器、字体或远程资源。这里实现的是 CSS 毛玻璃与光泽边缘，不声称具有 Lens 的 WebGL 光学折射。

## 实现与维护

- `app.css` 末尾的 Frosted lens material 段集中定义材质变量，复用到侧栏、课程、任务、工具、日志与设置。
- 静态曲面色带让背景在卡片内外产生可见区别，替代无限漂动的光团。保留悬停、按下与页面切换过渡，支持系统减弱动态；没有增加 JS 帧循环。
- 半透明度只作用于背景，正文不降透明度。课程名保持 18px，任务名 15px（小窗口 14px），任务说明 12px，长说明允许换行。
- 更多菜单采用更厚的玻璃，避免其父级 backdrop stacking context 使下方统计文字透出。保留 `.hero` 的层级和设置弹窗的独立层级。
- 不支持 backdrop-filter、减弱透明度和强制高对比模式时，使用实色回退。
- 原生窗口逻辑在 `gui/desktop_backdrop.py`：检查系统版本、透明偏好和高对比度；扩展 DWM 客户区，启用深色 Acrylic，再将 WebView2 底色设为透明。仅成功时通过快照启用 `html.desktop-glass`；失败时恢复原底色。
- 不调整窗口整体 Opacity，不设置穿透鼠标的窗口样式。正文保持完全不透明，卡片与按钮只有背景半透明；采用柔和灰边，减少白色高光，进度条使用青蓝色。
- 原生改动需要重新打包并同时更新 exe 与 `_internal`；`--opaque` 可以禁用原生材质。保留现有登录与任务数据。

## 验证

- 现有 `tests/test_web_frontend.py` 的 6 项真实 Edge 检查：菜单点击与层级、课程选择、主操作暂停、搜索、减弱动态与最小窗口。
- 离线演示截图检查：1440×960 的课程、任务、工具、日志、菜单、设置；900×660 的课程、任务、工具。使用虚构数据，无账号请求。
- 本次没有重新执行真实课程播放或任务完成验证。
