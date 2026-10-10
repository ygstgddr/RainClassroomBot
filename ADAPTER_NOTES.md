# 站点适配依据与未验证项

## 0.1.1 实站修正

2026-10-10 复用用户专用 Edge 登录目录进行只读测试，确认必须先 GET `/v2/api/web/classrooms/{classroom_id}?role=5`，取得 `course_sign` 与 `uv_id`。章节和进度请求均携带 `cid`、`classroom_id`、`sign`、`term=latest` 和学校参数。旧版只有 `cid`，会遇到章节假空和进度 HTTP 400。

修复后 36 门课程均扫描成功，发现 439 个任务（385 已确认完成、54 未完成或待确认），其中 7 门确认为空课程。没有播放或提交真实学习任务。完成证据不保存课程 sign，发布包不含用户 profile 或数据库。

下文为初版公开代码研究记录；有关“没有账号验证”的历史描述，以以上 0.1.1 实站验证范围为准。

检查日期：2026-10-10。公开页面：`https://www.yuketang.cn/v2/web`。
公开脚本：`https://fe-static-yuketang.yuketang.cn/fe/static/web/1.2.321/js/pc.87fb3ab3.js`。

已从公开前端读取以下定义及调用方式，不是猜测 API：

| 用途 | 路径 / 参数 | 前端使用结构 |
|---|---|---|
| 当前账号 | `/v2/api/web/userinfo` | `errcode === 0`，`data[0].user_id` |
| 学生课程 | `/v2/api/web/courses/list?identity=2` | `data.list`，`classroom_id`、`course.name` |
| 章节 | `/mooc-api/v1/lms/learn/course/chapter?cid=...` | `success`，`data.course_chapter`，`section_leaf_list`，`leaf_list` |
| 章节进度 | `/mooc-api/v1/lms/learn/course/schedule?cid=...` | `data.leaf_schedules[leaf_id]` |
| 类型 | `leaf_type` | 0 视频，3 图文；其他类型进入人工队列 |
| 完成状态 | 前端进度组件 `isFinish` | 视频/图文数值等于 1；直播 >= 0.6；考试状态 3/4/5；课件/作业 `done > 0 && done == total` |
| 图文页面 | `/v2/web/lms/{classroom_id}/graph/{leaf_id}` | Vue 路由 `lms-graph` |
| 学校上下文 | `xtbz: ykt`，`uv-id`、`university-id` 和 `uv_id` | 前端请求拦截器设置 |

只读 GET 接口响应体在每次处理后释放，避免长期缓存累积。课程 ID/叶子 ID 必须是数字字符串。缺失字段、未知类型的进度和非数值进度不自动转成完成。章节接口不支持的旧课程保留为“扫描异常”，不会静默遗漏。

以下仍需真实账号验证：

- 本账号的全部课程是否均属于已适配 LMS，是否有归档、校园定制或新版 AI 学习中心。
- 登录后接口是否需要额外的租户、课程或平台参数。
- 视频任务在课程页的可见入口；公开 pc 脚本提及 `lms-video` 名称，但没有在已检查的路由表中明确对应路径，因此没有默认编造视频 URL。
- 图文正文选择器与阅读确认按钮的实际结构。
- 正常播放/图文操作之后，服务器何时将对应 leaf_schedules 记为 1。
- 连续数小时/数天运行、登录过期与断网后的实站恢复。

这些差异可能影响实际可用性。本地 mock 测试只能验证引擎、生命周期和已声明的数据契约，不能证明雨课堂已接受真实学习记录。


## 0.2.0 课程时间与选择

- 同一课程详情只读接口中的 `class_start` / `class_end` 为起止时间，兼容秒/毫秒时间戳，统一显示 UTC+8。未知、零值、非法值不猜测。
- 课程目录只持久化字段白名单（课程 ID、名称、教学班、学期、起止时间、读取错误），不保存 `course_sign`。
- 以 classroom_id 区分不同教学班；目录和用户勾选按账号隔离。默认不选课程，空选择不回退为全选。
- 启动/刷新目录与章节扫描分开。Scanner 仅请求所选课程章节；Scheduler 同样过滤历史任务，并要求当前选课集合与已扫描集合一致。
- GUI 在扫描、运行及暂停等待操作结束期间禁用改选，避免扫描与选课互相覆盖。


## 0.4.0 长江站适配依据

0.4.1 更正：预加载验证 iframe 可以同时满足 Playwright is_visible 和内部标签可见，但其父容器 opacity=0 且位置在屏外。需逐层检查宿主 iframe/祖先透明度、视口位置和遮挡，不能仅依据 iframe URL 或内部文本。XT 桌面倍速选项的 mouseover 会重置鼠标坐标；必须在同一选项内部产生 mousemove 后正常点击，否则站点主动忽略 click。旧版对这两项判断不充分，已实站复现并修复。

`https://changjiang.yuketang.cn/web` 登录后，既有 `/v2/api/web/*` 和 `/mooc-api/v1/lms/learn/course/*` 的只读结构可复用；33 门课程均实际读取成功。学生课程入口仍为 `/v2/web/studentLog/{id}`，但 `tab=content` 不足以激活内容面板，需点击 `.rain-tabs__nav-item` 的“学习内容”。目标章节在新的 iframe 内；同一任务名称必须唯一可见才点击。

按站点独立数据根目录和 Edge profile；跨站不复用自定义任务直达模板，防止旧路径错误。首次登录、服务器验证及未知进度保留明确待处理状态。实际发现 18 个可学习类型缺少 schedule 项，不推断缺失值的计分语义。原生 XT 倍速菜单在媒体加载后最终设置为 2×；不以菜单点击或播放结束替代服务器完成记录。

## 0.3.1 视频倍速适配依据

实际新版 `/ai-workspace/lms-graph/.../video/...` 播放器使用 `.rate-btn-box` / `.rate-config-item`，目标视频最近的公共容器为 `.digital-human-player-box`。video 上有画布和封面，因此悬停容器再操作真实菜单；旧版使用 `xt-speedbutton li[data-speed]`。仅选择可见、未禁用的正数档位。读取 playbackRate 校验是否生效，失败保持播放并限频重试。未匹配菜单时不直接写 playbackRate。每条任务创建独立处理器，播放器刷新后重新探测，不改变服务器确认流程。

## 0.3.0 PPT 适配依据

- 网页 `leaf_type`：0 视频，1 音频，2 直播，3 图文，4 讨论，5 考试，6 作业，7 课件，8 课堂，9 试卷，10 公告。类型标签与自动处理能力分离。
- GET `/edu_admin/leaf_level_info/?leaf_level_id=...` 在当前实站返回裸对象，包含 classroom_id、courseware_id、activity_id；只接受三个 ID 都存在且课程一致的入口。
- 观察实际点击所得路由 `/v2/web/studentCards/{classroom_id}/{courseware_id}/{activity_id}/ppt`，无需点击同名章节或调用 Vue 内部函数。
- GET `/v2/api/web/cards/detlist/{courseware_id}?classroom_id=...` 的 Slides 提供顺序页码和答题页信息；GET `/v2/api/web/cards/view_depth` 的 depth_detail 是从 0 开始的服务器已读页码。
- `.pptSwiper` 的活动 slide 及图片加载状态决定当前页面是否可读，网站支持的 ArrowUp/ArrowDown 键负责真实翻页。逐页 GET 确认后才进入下一未读页；最终仍以章节 schedule 的 done==total 且 total>0 为完成依据。
- 网站内部自行进行 WebSocket 阅读计时/上报。助手不调用网页内部提交函数、不发送模拟学习消息，不把 UI 的本地“未读”标志当作服务器证据。
- 当前站点课程内容嵌在 `/pro/lms/` iframe，通用按名称入口需检查所有 frame；PPT 直接使用已验证 ID 路由。
