# BugFixer 验收记录

日期：2026-11-24。工程：`G:/okr/BugFixer`。

**状态：0.3.1提交钩子兼容、通知编码及调用者/创建人双对象通知验收通过，已接入维修员流程。真实DWS dry-run及既有消息只读读回通过；本次没有重发业务通知或重复修改工单，新增整链路采用隔离服务验收。此前真实交付证据单独保留。**

|项目|结果与证据|
|---|---|
|核心/禅道/客服/凭据、回填及交付流水线单元与契约测试|106项通过，最终读回契约专项10项通过；`pipeline-v031-test-results.txt`及`pipeline-v031-final-contract-test-results.txt`|
|独立维修员原验收脚本的取证回归|root重跑16项通过，含真实子进程MCP/CLI；新增工具列举已更新，写操作不计入这16项；见`independent/summary.json`|
|真实工单2223144文本日志|下载1633909 bytes，检测text，SHA256 `077498346789490496e525765dc0bf1aaab5fd84e1dde14dd24980dd599ec355`|
|真实工单319220已匹配首ZIP|下载124800 bytes，检测ZIP，安全解压并索引，SHA256与先前取证一致：`b649335472d736ff2a0c502a66ace804272b0f77bb26a5c45dc048b468ae319f`|
|真实工单2223144视频|下载1925998 bytes，检测MP4，SHA256与先前取证一致：`b76c1cdfdafb413a2451d6be4a3d114cdda8ce1c41116e09283adaa9ec755447`|
|真实附件条件缓存|重复视频请求验证；结果见`live/latest-download-smoke.json`，没有用历史缓存代替当前Bug状态|
|Windows DPAPI|当前用户环境中用虚构字符串加解密往返成功；未打印真实秘密|
|凭据SQLite表|`data/bugfixer.sqlite3/web_credentials`已创建；无password明文列，密码由当前用户DPAPI加密BLOB存储|
|禅道真实认证取数|`zentao`与`zentao_dragongu`分别登录读取263855成功，原图1228296 bytes；见`live/zentao-authenticated-acceptance.json`|
|客服真实认证取数|`customer_service`与`customer_service_dragongu`分别真实SSO登录成功；两账号均取回2223144国内小游戏上下文、文本日志、视频与319220海外上下文、两份ZIP；见`live/customer-service-authenticated-acceptance.json`|
|skill校验|skill-creator的quick_validate通过；PyYAML仅安装在验收目录validation-deps，不进入运行依赖|
|MCP注册|全局BugFixer stdio入口已注册并读回；当前聊天是否已动态加载以实际工具列表为准，未加载可直接用同实现CLI|
|维修员接入|角色增加取证skill入口，原规则保留并备份；桥接AGENTS增加首期工具边界；不递归派发agent|

## 0.3.1维修发现修正及双对象通知验收（2026-11-25）

- 正常Git hooks保持开启。生成中文含Bug号单行说明；纯空白/换行变化可通过严格父提交、文件集及blob证明，实质正文改写仍停止。真实本地提交、裸远端推送和响应丢失续接均有回归覆盖。
- DWS使用官方安装包二进制和UTF-8 bytes/LF stdin，发送前dry-run核验原文及短标题。真实已安装CLI验证中文、法语、换行和调用者userId/创建人openDingtalkId参数，executed=false；见`dws-v031-actual-cli-dry-run.json`。
- 从认证转交记录读取调用者，从实时Bug读取创建人，解决后再核验最终assignedTo。原群一条消息同时@两者、合并重复稳定地址并分角色保存notification_coverage。原主人私聊来源才向该原私聊反馈；没有转交记录的桌面来源只通过工具结果返回，不推断钉钉身份。获批豁免的定向通知明确记录waived。
- 本地整链路验收同时运行真实Git hooks、真实子进程UTF-8字节收发和模拟DWS/禅道，一次run完成提交推送、回填、双对象通知、全文核验及本地任务记录；重复run不再发送。
- 发送回执及全文读回分别保存；响应丢失、读回延迟时同号run只读对账，不重发。真实只读验收发现快捷查询投影丢失content/sendType，已改用DWS原始消息响应及明确上海时间边界；现有263856通知的全文、机器人类型和消息ID已核验，未发送新消息。见`dws-v031-existing-message-readback.json`。
- 106项回归（原58项+48项流水线/协议专项）通过；最后读回契约调整另有10项专项通过；原取证验收脚本root重跑16项及skill校验通过。没有扩大解释为独立代理执行了新整链路真实业务维修。
- 当前已连接MCP仍是旧进程，已通过只读status核实；新版CLI的implementation_version=0.3.1已核实。skill自动优先使用新版CLI，MCP重连后加载新实现，无需Agent再手拼Git/DWS步骤。见`pipeline-v031-runtime-check.json`。
- 本次不重复处理263856/264098，不推送真实业务仓库、不执行真实禅道写入、不发新群消息；历史业务结果不冒充0.3.1发送验收。结构化结果和源码哈希见[pipeline-v031-acceptance.json](pipeline-v031-acceptance.json)。

## 0.3交付流水线验收（历史记录）

- 新增`deliver_repair`及CLI `deliver`：inspect合并Git、来源、现有批准、禅道版本/人员及通知条件；run连续完成提交、推送、回填和原群通知；status返回简短本地进度。
- 90项全量测试通过，新增32项包含真实临时Git仓库与本地裸远端、批量提交、CRLF对象校验、审批撤回、他人暂存/占用保护、响应丢失续接、重复请求不重放、MCP/CLI及DWS回执边界。禅道与DWS为模拟，没有真实业务写入、真实远端推送或群消息。
- 原独立验收脚本由root重跑16项通过，范围仍为取证回归；skill-creator校验通过。未将这16项称为新增编排的独立代理验收。
- 国内普通维修复用已认证请求的现有授权，不新增主人逐条审批。海外及特殊范围沿用原门禁；真实主人补充的版本、豁免和通知约定只授权对应元数据。
- 已安装维修员角色、工程skill及钉钉接收流程同步更新。角色其他规则保留；备份见`workflow-update/20261124-181120-pipeline/zentao-maintainer.toml`。新MCP进程加载第四个工具，已启动服务需要重连，CLI立即可用。
- 自动范围限普通资源及Module/Hotfix；已存在提交、特殊资源或不确定异常交原流程。程序不调用大模型，异常只返回必要信息；没有测量或承诺实际token节省比例。
- 结构化验收及当前源码哈希见[pipeline-acceptance.json](pipeline-acceptance.json)，使用说明见[交付流水线](../docs/delivery-pipeline.md)。

## 0.2维修回填验收

- 新增`prepare_writeback`与`apply_writeback`，及对应CLI；本地SQLite保存计划、执行状态和回执。精简预览绑定正文、版本和测试人，不作为用户审批。
- 23项回填测试包括本机真实TCP表单POST/回读、多选编码、原描述/附件保留、HTML转义、快照冲突、账号与计划变化、响应丢失、部分保存、重复请求、保存回执失败等。
- 已通过当前部署12.3.3真实GET表单的字段、解决版本及测试人核验；修复详情页注入图片展示属性造成的误冲突。见`write-form-contract-verification.json`。
- 0.2使用康自军账号只读回归263856成功；状态仍active、指派kangzijun。见`live/zentao-263856-v02-readonly-result.json`。
- 取证skill、已安装维修员角色与钉钉接收流程已接入，其他授权规则保留。skill校验通过。
- **已实际回填6条获批Bug**：263761→yizewen，263662/263660/263626/263625/263624→yuhao；全部resolved/fixed/trunk，描述、解决评论、真实提交号及原字段/附件保留核验通过。见`live/writeback-six-tour-bugs-20261124.json`。
- 首条因resolve自动将confirmed从0改1，被旧校验误判uncertain。修正为仅最终resolved/fixed允许这一合法转移，新增严格编辑阶段及合法解决阶段回归测试；原请求只读对账成功，没有重放POST。见`live/writeback-263761-resolve-confirmation-regression.json`。
- 编辑与解决为两次远程操作，没有服务端原子事务保证；不确定时只读对账，不自动重试或回滚。接口与限制见`../docs/writeback-contract.md`，综合结果及源码哈希见`writeback-acceptance.json`。

## 修复并复测的验收发现

1. Content-Length或chunked下载被截断时不再误报完整，返回partial并保留上下文。
2. 失败附件URL中的userinfo密码从公开清单和missing字段中移除。
3. ZenTao错误类型的region输入不再导致CLI traceback，返回结构化错误。
4. 附件不能诱导已登录客户端调用业务GET路径；下载及重定向均限制为已核实文件路由/OSS。
5. 登录协议按部署前端选择密码分支，补齐AJAX头和密码强度字段；修正后两条真实禅道账号均登录成功。

## 真实认证验收

早期缺少凭据时263855/2223144请求明确返回blocked/credentials_required，历史记录见`live-prerequisites.json`。
随后用户提供两条禅道凭据及两条客服凭据，分别以`zentao`、`zentao_dragongu`、`customer_service`、`customer_service_dragongu`加密保存并完成真实取数。古龙与康自军账号独立保存，未相互覆盖。客服URL未保存临时challenge id，运行时从服务端获取当前挑战。

客服2223144已核对H5_WX渠道、版本、问题正文及留言，文本日志10654行、视频下载完整。
客服319220已核对GP_JP渠道、海外API、版本、问题正文及留言，两份ZIP共23个成员解压并逐个核对SHA256。
两单接口均未返回显式region字段，工具保留warning；实际API origin/REGION及渠道已核对，没有将缺失字段冒充服务端区域证明。
禅道263855的标题、步骤、状态、指派、历史和原图已通过工具取回，并与此前交付证据核对。
此验收不修改工单、不发验证码、不执行修复。若SSO要求交互验证码，应准确返回needs_user_login，不绕过。

## 明确未覆盖

- 真实认证覆盖生产禅道、国内小游戏与海外日本客服；未逐一实测国内APP、海外俄罗斯或其他账号权限。
- 未手工输入真实密码操作录入窗口；DPAPI核心及SQLite隔离已分别验证。
- 测试海外后端和测试SSO密码契约未证实，工具返回unsupported_environment，不猜接口。
- 未实现玩家识别码搜索、客服工单写入、缺译转派专用工具、最终关闭、发布或自动启动维修；0.2仅增加已授权修复的禅道描述追加与解决转测试。
- 未通过工具启动新agent；已有维修员通过CLI/MCP取得结果后继续当前任务。

所有单元/fixture、真实取证与实际业务写入分别记录。客服系统未执行写操作；禅道写入仅限本次具体获批且已实际修复交付的6条Bug，没有最终关闭。
