# 软件内更新维护约定

`core/updater.py` 使用独立、无账号 Cookie 的 HTTPS 请求读取本项目 GitHub Releases。仅接受 `vX.Y.Z` / `X.Y.Z` 正式版，按数字比较版本。用户点击后才联网，网络线程不占用 UI 线程；不修改学习模块请求。

每次发布应提供：

- `RainClassroomBot-X.Y.Z-Windows-x64-Setup.exe`，其中版本应与 tag 一致。
- `SHA256SUMS-X.Y.Z.txt`，使用 `64 位十六进制 SHA-256 + 两个空格 + 文件名` 格式；GitHub 附件自带 `sha256:` digest 时优先使用该值。
- Release 正文中的用户可读更新说明；界面仅按纯文本显示。

更新模块不接受前端传入 URL、路径或启动参数。原始附件地址必须完全匹配本仓库和 tag；重定向仅允许 GitHub 的 HTTPS 分发域名。安装包下载到系统临时目录 `RainClassroomBotUpdates` 下的独立子目录，校验尺寸和 SHA-256 后才转为可安装状态，安装前再次核验内容。失败/取消清理本次 `.part`；成功下载的安装包暂存于系统临时目录，用户可通过 Windows 临时文件清理移除。SHA-256 是完整性检查，不代替发布者代码签名。

下载与检查可以在学习过程中运行；安装要求学习和打字均停止。桥接先锁住新操作并保存 `desired=paused`，复用退出流程等待所属 watchdog/worker 停止后启动安装程序。采用 Inno Setup 交互向导，传入当前安装目录、禁止自动重启和强制关闭程序；保留用户取消安装的机会。启动失败恢复窗口可操作状态，不继续学习。安装后用户从安装向导或快捷方式重新打开程序。

安装器应始终保留稳定 `AppId` 与当前数据保护规则：已有配置 `onlyifdoesntexist`，不对账号、登录目录、任务数据库执行清空。发布前应补做安装版到下一版的真实升级验证。本轮不构建、不上传新包，验证使用本地虚构下载流与启动桩，不能当作已完成新安装版升级的证据。

实现参考：[GitHub Releases REST API](https://docs.github.com/en/rest/releases/releases#get-the-latest-release)、[Inno Setup 命令行参数](https://jrsoftware.org/ishelp/topic_setupcmdline.htm)。
