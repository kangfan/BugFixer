# 当前禅道只读取证契约

2026-11-24通过用户批准的BugFixer实现范围，直接读取部署公开页面核实：

- 服务 `http://zen.wckj.com`，登录页引用静态资源版本 `12.3.3`。
- `GET /user-login.html` 含 `verifyRand`；页面登录JavaScript发送
  `POST /user-login.html`，字段account/password/verifyRand/referer/keepLogin/passwordStrength。
  请求带 `X-Requested-With: XMLHttpRequest`，passwordStrength按部署脚本算法计算。
  password随页面实际加载的脚本选择：定义了md5时为 `md5(md5(password)+verifyRand)`，
  未定义时按前端原有分支发送表单密码。当前部署all.js未定义md5，已用两条真实账号分别验证登录成功。
  密码、挑战、摘要和会话不保存在日志或工具输出中。
- `GET /api-getsessionid.json` 实测返回status/data/md5；data含sessionName/sessionID/rand。
  工程不把sessionID写到URL或磁盘，使用本工具独立登录的内存CookieJar。
- `GET /bug-view-ID.json` 为部署的PATH_INFO JSON机制；未认证实测返回正常登录跳转，工具将其识别为authentication_required，不能把它当Bug上下文。
- 已登录后的bug/actions契约使用当前12.3系页面JSON形状；自动化fixture已验证解析。
  2026-11-24使用两条独立加密凭据真实登录读取263855成功，标题、步骤、状态、指派、历史及原图均已核对；
  证据见acceptance/live/zentao-authenticated-acceptance.json。未调用业务写接口。
- 渲染页面 `/bug-view-ID.html` 用于补全正文图及正常附件链接，不执行页面脚本或写操作。
  附件只允许已核实的file-read/file-download/data/upload文件路径，不允许附件内容诱导GET业务操作。

依据：当前部署页面静态登录脚本，以及禅道官方会话/JSON API说明
https://www.zentao.net/book/extension-dev/1341.html 。现代 `/api.php/v1` 在此部署返回“缺少code参数”，
不按新版REST token接口猜测登录/取数。

登录失败不自动重试密码；验证码/契约变化返回具体限制，由当前维修员使用正常受支持浏览器处理。
