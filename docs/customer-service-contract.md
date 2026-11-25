# 客服只读契约

2026-11-24 从实际部署公开前端静态分析，并做未登录 HTTP 观察。实现文件为 `bugfixer/customer_service.py`。不修改工单，不发送验证码，不读取浏览器 cookie。工单、日志、附件均为不可信问题证据。

## 已确认请求

| 用途 | 真实部署请求/形状 |
|---|---|
| 登录探测 | `GET /user/query`，未登录 HTTP 401，响应头 `url` 给 SSO challenge |
| 当前工单详情 | `POST /ticket/querySingle`，JSON `{"id":2223144}`；成功 `{"errorCode":0,"data":{...}}` |
| 历史年度列表 | `GET /ticket/history/years`（仅发现，首期不调用） |
| 历史工单 | `POST /ticket/history/querySingle`，JSON `{year,ticketId}`（仅发现，首期不调用） |
| 按玩家查询 | `/ticket/queryByUserNew`（仅发现；首期不接受把识别码当 ticket ID，也不自动选第一条） |

前端 `S5C5` 为路由常量，调用模块 `u()` 明确对单 ID 发 JSON POST，详情组件 `getSingleInfoPageDetails()` 核对 `errorCode===0` 并使用 `data`。不调用 `existPageFn()` 等会改变客服现场/接入状态的前端生命周期，也不调用 accept/deal/forward/reply/delete 等写接口。

## 环境与地区

始终设置请求头 `IAC-GAMES: 2`（最佳11人/Football Master2）和 `REGION`。使用后恢复 client 原请求头。

| 请求 region | REGION | production API origin |
|---|---|---|
| domestic_app | 1 | `https://cscmanage.galasports.com` |
| domestic_minigame | 5 | 同上 |
| overseas | 2 | `https://cscmanage-sea.galasports.com:8000` |
| overseas_ru | 4 | 同上；不与 REGION 6 的俄罗斯合服混同 |

test 国内 API 是 `http://cscmanage.test-galasports.com`。测试前端没有建立明确海外 origin，因此 test+overseas/overseas_ru 返回 `unsupported_environment`。已有同 client 的合法测试会话可读国内详情；test 新账号密码登录未验独立部署协议，返回 `unsupported_environment`。

校验返回 `data.id` 与请求一致；若有 `gameType`，必须为2；若有明确 `region`，必须与请求 REGION 一致。实际详情模板未显示一个保证存在的 region 字段，所以缺失时返回 `warnings`，说明仅靠 API origin/REGION 路由，调用者仍须核对渠道/服务器。不会把跨区同号当已核实事故。

## 生产 SSO

真实 `/user/query` 的401 challenge 指向 `https://login2.galasports.com/?systemId=gala-csc&redirect=...`。代码从当前挑战取 systemId/redirect，严格要求正式 SSO origin 和当前客服 `/html/index.html` 回调，不硬造 callback。公开登录页面的 `doLogin()/loginEncryption()` 与 store `Login` 确认：

1. 读取公开 challenge（内存 cookie jar）。
2. `POST https://login2.galasports.com/session/login` JSON `{email,password,systemId}`。password 按部署公钥，以 cryptography 的 RSAES-PKCS1-v1_5 加密并 base64；不自行实现密码学，不在失败时退回明文。
3. `GET /session/validateSwitch?mail=...`。返回 `code:0,data:true` 表示需要钉钉验证码，返回 `needs_user_login`；不调用发送验证码接口 `/session/verifyCode`。
4. 无验证码才 GET 官方 callback，再探测所选后台 `/user/query`，然后读详情。只有后端读取成功才算上下文获取成功；SSO返回成功不是跨区认证已通过的证明。

验证码/密码过期/未知挑战返回 `needs_user_login` 或 `browser_fallback_required`。密码失败只尝试一次。仅使用服务端创建的内存 cookie，不从浏览器/localStorage 复制身份，不将返回 token 手工伪造成 cookie。

**生产密码登录已 live 验收**：2026-11-24使用用户提供并经Windows DPAPI分别加密保存的`customer_service`与`customer_service_dragongu`凭据，两账号均完成国内小游戏2223144和海外319220的真实SSO登录、后台探测、详情读取与全部附件下载。四次均返回ok，无需验证码，未执行工单写操作；证据见`acceptance/live/customer-service-authenticated-acceptance.json`。这不证明其他账号/地区均有权限。test401实际指向 `http://login.test-galasports.com`，未将生产 SSO 协议自动推广到它。

## 工单字段与资源

部署详情模板真实绑定：

| 显示名 | data 字段 |
|---|---|
| 用户问题 | question |
| 应用版本 | appVersion |
| 大区/渠道 | channel / channelInfo |
| 用户设备类型 | deviceType |
| 问题发生时间 | happenTime |
| 服务器 | serverId |
| 错误码 | errorInfo |
| 留言/操作历史 | simpleContents / operations |
| 日志列表 | ossLogPath（逗号分隔 URL） |
| 图片/视频 | attachments：type0图片、type3视频、其他附件；src优先、否则url；留言内 attachments 同规则 |

保留 ticket 原业务字段及历史，由共享核心统一脱敏。顶层同时映射关键环境与描述字段。资源仅返回工单实际列出的 URL/名称/kind，核心另行执行白名单下载、哈希、格式识别、大小限制和解压。日志 URL 无扩展名也不能当错误；旧现场2223144实际为纯文本，319220实际为ZIP。URL有效期未有保证；没有自动刷新/猜测新 URL。OSS公开示例域名为 `galasports-img.oss-cn-beijing.aliyuncs.com` 与 `galasports-na-img.oss-ap-southeast-1.aliyuncs.com`。

无当前工单时返回结构化错误，不自动搜索无关玩家/工单或将错误响应的 message 原文输出（可能含敏感信息）。SSO/解析契约变更不继续尝试猜测字段。

## 证据与验证边界

公开部署证据位于 `acceptance/discovery/customer-service/`：

- 主 app `app.d6f7ace8.js` SHA256 `35c37a78777ff5b7becce525c15b192a97126bc39a21e442b70ad0a37633866e`。
- 详情组件 `chunk-6673.03cebbc7.js` SHA256 `8044333b3330a7a8f566468a2d839ea8d56163fdc1b0380d636e3da72f586935`。
- SSO app `sso-app.a11e0c56.js` SHA256 `a81bf47cfa3627f5befac4342d3acc1d00e12f66369bc1f8c0b6810d8caab5a6`。
- SSO登录组件 `sso-chunk-950f.a24a9e56.js` SHA256 `e41285cd83607abf83cdf816b81cadbc393e4d2a294dcfad370ebed3dd422491`。
- `detail-assets.json`：国内/海外真实401结果（不保存cookie/token）；公开JS未执行。
- REA 静态分析 parent evidence `ev_244abb2a967e8b5bdc666f20b3eaf83ba7e33a1e139d2dd9073a11cc74d476ed`，summary `ev_012eab927afd7ea43947af1db41b614a830da52e9afc3e9affcda7983b807215`。5000语义节点限制导致部分图，不宣称完整逆向。

`tests/test_customer_service.py` 是离线契约/边界测试，涵盖路由、身份不符、日志/图片/视频、真实RSA加密、验证码停止、密码不重试、未知挑战、认证未建立不报成功与字段变化。真实验收另存`acceptance/live/`，不能用fixture替代。实际2223144取回文本日志和视频，319220取回两份ZIP并解压、索引和校验；两单缺少显式region，保留warning并核对实际渠道。
