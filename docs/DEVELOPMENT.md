# 开发与打包

## 开发环境

当前验证环境是 Windows 11 x64、Python 3.12.10 和 Microsoft Edge。推荐独立虚拟环境，运行路径使用项目根目录。

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest -q tests
.\.venv\Scripts\python.exe main.py --self-check
.\.venv\Scripts\python.exe main.py
```

测试含真实 Edge、本地 HTTP 模拟站和桌面 GUI 检查，因此需要能启动图形界面的 Windows 会话。模拟测试不需要真实雨课堂账号。

桌面界面使用 Tkinter 与 Pillow。Pillow 随 `requirements.txt` 安装，用于绘制渐变背景、柔和阴影和圆角；文字、列表和输入控件仍可实际操作。玻璃效果由程序绘制，不依赖 macOS 或额外的网页服务。安装 Python 时请保留 Tcl/Tk 组件。

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
│   └── visibility.py        # 验证框实际可见性
├── core/                    # 配置、数据存储、课程、类型、站点与日志
├── task/                    # 扫描、调度、执行
├── handler/                 # 视频、原生倍速、图文、PPT
├── recovery/watchdog.py     # 工作进程心跳与恢复
├── gui/interface.py         # 页面导航与主操作状态
├── gui/glass.py             # 玻璃风格背景和可操作控件
├── config/config.yaml       # 默认配置
├── tests/                   # 本地模拟站及回归测试
├── scripts/                 # 发行辅助脚本
├── docs/                    # 教程与演示图
└── build.ps1                # 测试并构建 Windows onedir 包
```

GUI 与浏览器工作进程分离，以消息传递状态和操作；SQLite 保存账号、课程选择、任务、尝试次数及完成证据。守护监控心跳并限制重启频率。多站点使用独立运行目录。

修改交互流程时，请使用临时 Store 和虚构课程进行界面测试，并验证开始、暂停、继续、改选课程、扫描失败和切站状态。工作进程尚未停止时，不得允许更改当前执行范围。主按钮发出操作意图，是否需要扫描由工作进程根据当前账号和选课范围判断；不要在 GUI 的定时刷新中绕过扫描直接执行任务。

## 修改适配逻辑

- 将站点差异集中到适配器；先核验实际网页和请求，再添加字段或选择器。
- 未知状态不得推断为完成。完成判定需要服务器记录，不能用固定等待时间替代。
- 视频使用网站原生速度菜单；XT 控件需要在目标选项内移动鼠标后再点击。设置后读取实际 `playbackRate` 验证。
- Playwright 的 `is_visible` 本身不排除透明元素；验证框还需检查祖先透明度、屏幕位置和遮挡。
- 对新接口补充本地模拟和回归用例；不要把真实账号、Cookie、课程记录或响应全文复制进测试夹具。
- 遇到作业、考试或无法识别的任务，保留人工处理/需适配状态。

接口说明见 [ADAPTER_NOTES.md](../ADAPTER_NOTES.md)，实站验证边界见 [VALIDATION.md](../VALIDATION.md)。

## 打包 exe

在项目根目录执行：

```powershell
powershell -ExecutionPolicy Bypass -File .\build.ps1
```

脚本会安装开发依赖、运行测试，再用 PyInstaller 构建。任何一步失败都会停止。输出为 `dist/RainClassroomBot/`，入口为 `RainClassroomBot.exe`。

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
