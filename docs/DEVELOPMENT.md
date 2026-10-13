# 开发与打包

## 开发环境

0.8.0 的验证环境是 Windows 11 x64、Python 3.12.10、Microsoft Edge 和已安装的 Microsoft Edge WebView2 Runtime。推荐独立虚拟环境，运行路径使用项目根目录。缺少 WebView2 时先从 [Microsoft 官方页面](https://developer.microsoft.com/en-us/microsoft-edge/webview2/consumer/)安装 Evergreen WebView2 Runtime。

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest -q tests
.\.venv\Scripts\python.exe main.py --self-check
.\.venv\Scripts\python.exe main.py
```

测试含真实 Edge、本地 HTTP 模拟站和桌面 GUI 检查，因此需要能启动图形界面的 Windows 会话。模拟测试不需要真实雨课堂账号。

默认桌面使用 `pywebview==6.2.1`，指定 `edgechromium` 渲染器。课程、任务、日志、其他功能和设置均由本地 HTML/CSS/JavaScript 渲染：半透明背景结合 `backdrop-filter`，使彩色背景透过各面板；CSS 负责悬停、按压、页面过渡与勾选动画，并支持 `prefers-reduced-motion`。0.7.0 的课程名称采用 18px，日期和状态采用 12px，提升对比度并允许名称完整换行。

`gui/web_assets.py` 将可信的 HTML/CSS/JavaScript 内联后交给窗口加载，界面不依赖 CDN，也不把源码、登录目录或数据库所在目录公开为静态 HTTP 目录。`window.expose` 只暴露 `snapshot()` 与 `action()` 两个桥接接口，不把整个控制器或 Store 暴露给 JavaScript。

Tkinter/Pillow 保留给兼容排查和启动错误提示。`main.py --classic` 只用于开发排查旧界面，不是日常用户的主流程。安装 Python 时请保留 Tcl/Tk 组件。

只预览界面、不打开浏览器或读取本机学习记录时，可运行虚构数据演示：

```powershell
.\.venv\Scripts\python.exe main.py --demo
```

## 项目结构

```text
RainClassroomBot/
├── main.py                  # GUI 入口与自检
├── worker.py                # 独立工作进程
├── browser/
│   ├── edge_manager.py      # 持久化 Edge 生命周期
│   ├── site_adapter.py      # 课程、任务、进度与页面入口适配
│   ├── manual_typing.py     # 原始元素绑定、逐字输入、焦点及导航保护
│   └── visibility.py        # 验证框实际可见性
├── core/                    # 配置、数据存储、课程、类型、站点与日志
├── task/                    # 扫描、调度、执行
├── handler/                 # 视频、原生倍速、图文、PPT
├── recovery/watchdog.py     # 工作进程心跳与恢复
├── gui/
│   ├── web_interface.py     # 快照/动作桥接、选课与异步切站关闭
│   ├── web_assets.py        # 内联本地界面资源，无外部 CDN
│   ├── window_frame.py      # 绑定窗口的原生控制、工作区与尺寸约束
│   ├── assets/              # 官网原始 PNG、Windows ICO 与来源记录
│   ├── web/
│   │   ├── index.html      # 统一桌面结构与图标
│   │   ├── app.css         # 半透明玻璃、背景模糊与交互动画
│   │   └── app.js          # 快照同步、筛选与动作反馈
│   ├── interface.py        # --classic 开发兼容界面
│   └── glass.py            # 开发兼容界面绘制组件
├── config/config.yaml       # 默认配置
├── tests/                   # 本地模拟站及回归测试
├── scripts/                 # 发行辅助脚本
├── docs/                    # 教程与演示图
└── build.ps1                # 测试并构建 Windows onedir 包
```

GUI 与浏览器工作进程分离，通过本地 SQLite 交换学习状态和操作意图；SQLite 保存账号、课程选择、任务、尝试次数及完成证据。手动打字的正文使用当前工作进程专属的内存队列传递，SQLite 仅保存目标摘要、阶段与计数。守护监控心跳并限制重启频率。多站点使用独立运行目录。

WebView 的桥接调用可能并发到达，动作通过锁串行检查选课范围与当前状态；快照在一次读取事务中获取账号、目录、所选任务与日志，再转换为前端字段白名单。切站、恢复和退出使用后台线程，等待守护退出时不持有界面操作锁。必须先退出旧工作进程，再创建新站工作进程；中途关闭、写配置失败或新守护启动失败都需清理新进程并保持可恢复状态。

修改交互流程时，请使用临时 Store 和虚构课程进行界面测试，并验证开始、暂停、继续、改选课程、扫描失败和切站状态。工作进程尚未停止时，不得允许更改当前执行范围。主按钮发出操作意图，是否需要扫描由工作进程根据当前账号和选课范围判断；不要在 GUI 的定时刷新中绕过扫描直接执行任务。

## 内置标题栏与图标

0.8.0 的默认 WebView 工作台使用 `frameless=True`、`easy_drag=False`、`resizable=True`。HTML 的 44px 玻璃标题栏在右上角从左到右提供黄色最小化、绿色最大化/还原、红色关闭按钮，仅空白拖动区使用 `pywebview-drag-region`；`DRAG_REGION_DIRECT_TARGET_ONLY` 防止拖动区域内的按钮和表单交互带动整个窗口。双击空白区调用最大化切换，窗口四边与四角提供八方向缩放入口。

`gui/window_frame.py` 的 `WindowFrame` 只控制 `bind_window` 绑定的工作台窗口。`snapshot.window` 返回 `maximized` 和 `native_controls`，前端据此显示最大化/还原状态，并在原生窗口初始化前禁用窗口按钮。原生控件的操作参数白名单为：

| 动作 | 参数 / 行为 |
|---|---|
| `window_close` | 不接受参数，调用原有 `WebInterface.close()`，停止、保存和清理后销毁窗口 |
| `window_minimize` | 不接受参数，最小化工作台，不修改学习操作意图 |
| `window_toggle_maximize` | 不接受参数，按当前窗口状态最大化或还原 |
| `window_resize` | 仅接受 `{edge}`，值为 `n`、`s`、`e`、`w`、`ne`、`nw`、`se`、`sw` |

页面不能提交任意 HWND、PID、屏幕坐标或窗口尺寸。原生操作在所绑定 WinForms 窗口的 GUI 线程执行，窗口缩放交由 Windows 处理；最大化依据当前显示器工作区域，保留任务栏和界面最小尺寸。关闭必须复用原有异步工作进程清理流程，不能直接退出进程或调用全局结束 Edge。最小化仍执行当前学习任务，主动暂停需使用学习主按钮。

图标位于 `gui/assets/yuketang.png` 和 `yuketang.ico`。PNG 是官网指定 favicon 的原始 64 × 64 响应，图形内容未改；ICO 将同一图片转为 Windows 所需容器及尺寸。标题栏和侧栏使用 PNG，窗口、任务栏、启动提示与 exe 使用 ICO；资产随程序加载，不在运行时下载。`icon_path()` 同时支持源码目录和 PyInstaller 的 `_MEIPASS` 资源路径，`set_app_id()` 为 Windows 任务栏设置本助手应用标识。来源与文件校验值见 [ICON_SOURCE.md](../gui/assets/ICON_SOURCE.md)，标识归原权利人所有，本项目为独立工具。

修改窗口控制后，在真实 Windows 桌面验证三色按钮、标题栏拖动/双击、八方向缩放、最大化工作区域、最小尺寸、图标和优雅退出；本地 HTML 预览只能验证前端交互，不能替代这些原生窗口检查。最终源码与冻结 exe 的验证范围见 [VALIDATION.md](../VALIDATION.md)。

## 指定文本框打字的实现与约束

`browser/manual_typing.py` 的 `ManualTyping` 只使用本助手的 `EdgeManager.context`。选择模式在受管理网页及其 iframe 中安装临时标记，用户点击可编辑文本框后保留原 `ElementHandle`；后续输入不会用相同 selector 搜索另一个元素。工具支持普通单行输入框、`textarea` 与 `contenteditable`，拒绝密码、文件、只读和不可见元素。

前端确认目标只保存当前 token；直到 `typing_start` 才通过桥接发送 `{token, text, speed, confirmed: true}`。`worker.py` 的 `TypingCommands` 在 Playwright 所在 asyncio 循环内消费动作，状态回调只持久化白名单摘要。`recovery/watchdog.py` 为每次工作进程启动创建新 `multiprocessing.Queue` 和 generation；重启、切站或退出会关闭旧队列，旧正文和命令不重新播放。不得把输入正文加入日志、异常全文、SQLite、配置或前端 `localStorage`。

快照的 `typing` 字段包含 `phase`、`available`、`connected`、`target`、`sent`、`total`、`speed`、`message` 与可用动作标志。target 仅含 token、label、URL/iframe 来源、tag/type、字符数量与 multiline，不包含现有全文。允许动作如下：

| 动作 | 参数 / 行为 |
|---|---|
| `typing_select` | 进入现场点选；只有学习操作停止后可用 |
| `typing_cancel` | 取消选择并清除目标 |
| `typing_start` | `token`、`text`、`speed`、`confirmed: true`；再次检查目标与正文 |
| `typing_pause` | 暂停，保留同一目标和尚未输出的内存文字 |
| `typing_resume` | 必须携带同一 `token`；向原目标继续 |
| `typing_speed` | `speed`，范围 1～60 字符/秒，默认 20 |
| `typing_stop` | 停止并释放正文、目标与临时页面监听器 |

每次正文上限 50,000 字符，不裁掉首尾空格。引擎将 CRLF/CR 规范化为 LF；单行目标拒绝换行，控制字符与制表符不接受。输入使用浏览器逐字符键盘/文本事件；中文与换行通过 `insert_text` 发送输入事件，不走剪贴板，也不直接修改网页 `value` 或编辑器内部状态。换行不按 Enter，工具不自动提交表单。

每个字符前后检查目标身份、可编辑性与焦点，并安装页面事件护栏，避免焦点在异步调用期间跳到另一个框。失焦暂停；导航、原元素替换或浏览器断开停止并清除目标。暂停和继续不得重复已接受的字符。选择及输出期间锁定学习开始与扫描；切站、关闭则先结束旧工作进程的输入，再建立新站点连接。

状态变化即时发布，连续进度更新节流，避免每个字符都写入 SQLite。输入节奏使用异步等待，空闲时不做全局键盘监测或持续截图。暂停/停止先改变控制状态，再等待当前已在途的字符输入请求完成，以避免遗留输入；普通页面控制快速，网页脚本阻塞时不能承诺立即返回。不要用取消 RPC 后无条件重试字符来换取表面的即时停止。

修改此模块后，用本地真实 Edge 页面覆盖目标确认、多个输入框、iframe、焦点变化、DOM 替换、导航、暂停继续、停止、速度边界、换行和重启丢弃；验证未指定框没有收到输入，正文没有进入状态快照或持久文件。`--demo` 只模拟工具状态与计数，不会输入真实网页。本次没有对真实作业写入答案，具体测试结果见 [VALIDATION.md](../VALIDATION.md)。

## 修改适配逻辑

- 将站点差异集中到适配器；先核验实际网页和请求，再添加字段或选择器。
- 未知状态不得推断为完成。完成判定需要服务器记录，不能用固定等待时间替代。
- 视频使用网站原生速度菜单；XT 控件需要在目标选项内移动鼠标后再点击。设置后读取实际 `playbackRate` 验证。
- Playwright 的 `is_visible` 本身不排除透明元素；验证框还需检查祖先透明度、屏幕位置和遮挡。
- 对新接口补充本地模拟和回归用例；不要把真实账号、Cookie、课程记录或响应全文复制进测试夹具。
- 遇到作业、考试或无法识别的任务，保留人工处理/需适配状态。

接口说明见 [ADAPTER_NOTES.md](../ADAPTER_NOTES.md)，实站验证边界见 [VALIDATION.md](../VALIDATION.md)。

## 效率验证

0.6.0 将页面状态采样与服务器查询分开调度，在服务器请求未返回时仍检查暂停命令；同一轮扫描复用课程详情，相同本地状态不重复写入数据库。PPT 仍按默认每页约 5 秒翻页，并在整份浏览后统一等待服务器确认。

本地基准使用真实 Microsoft Edge 与 MockSite，把模拟 GET 请求延迟设为 0.35 秒。与 0.5.0 同一组操作相比，暂停响应为 0.813 → 0.109 秒，同一轮详情请求 2 → 1 次，100 次相同状态操作的数据库提交 100 → 0 次。该结果验证减少阻塞和重复工作，不证明真实站点回写会更快；不得据此缩短为“到时即完成”。完整测试与冻结程序验收范围见 [VALIDATION.md](../VALIDATION.md)。

## 打包 exe

在项目根目录执行：

```powershell
powershell -ExecutionPolicy Bypass -File .\build.ps1
```

脚本会安装开发依赖、运行测试，再用 PyInstaller 构建。任何一步失败都会停止。输出为 `dist/RainClassroomBot/`，入口为 `RainClassroomBot.exe`。

WebView 桌面除了 Playwright，还需收集 `webview`、`pythonnet`、`clr_loader` 与 `clr`。使用 `--add-data 'gui/web;gui/web'` 打包本地界面，使用 `--add-data 'gui/assets;gui/assets'` 打包图标与来源说明，并用 `--icon gui/assets/yuketang.ico` 写入 exe 图标。相应选项已经包含在 `build.ps1` 中；请直接使用脚本，避免遗漏 CSS/JavaScript、图标或 Windows 渲染器组件。用户端仍需安装 Microsoft 官方 WebView2 Runtime，发行 zip 不内嵌完整运行时。

需要排查 PyInstaller 参数时，脚本中的构建命令为：

```powershell
.\.venv\Scripts\python.exe -m PyInstaller --noconfirm --clean --windowed --onedir --name RainClassroomBot --collect-all playwright --collect-all webview --collect-all pythonnet --collect-all clr_loader --hidden-import clr --icon gui/assets/yuketang.ico --add-data 'gui/web;gui/web' --add-data 'gui/assets;gui/assets' main.py
```

脚本只复制默认 `config/config.yaml`，不复制本机的 `config/site.json`。它也会附带 README、验证记录、开发文档和第三方许可证。必须分发整个目录，不能只分发 exe。

```powershell
Compress-Archive -Path .\dist\RainClassroomBot -DestinationPath .\RainClassroomBot-Windows-x64.zip -Force
Get-FileHash .\RainClassroomBot-Windows-x64.zip -Algorithm SHA256
```

主要依赖在 requirements 中固定版本；传递依赖和本机 Python/Edge 仍可能随环境变化，应重新运行测试并验证最终 exe。当前版本的实际验证范围记录于 [VALIDATION.md](../VALIDATION.md)。

## 发布前检查

1. 更新版本、README、CHANGELOG 和验证边界。
2. 在本地模拟站验证最终 exe，不依赖真实账号完成发布测试。
3. 检查源代码及压缩包：排除 `.venv`、`edge_profile`、`sites`、`data`、`logs`、备份、令牌及个人配置。
4. 确认第三方许可证与当前打包环境匹配，重新计算压缩包 SHA-256。
5. 源码提交仓库；Windows 包、源码包和 SHA256SUMS 上传 Release。

`.gitignore` 仅用于防止误提交，不是脱敏工具。已经加入 Git 跟踪的文件不会因为新增忽略规则自动消失。
