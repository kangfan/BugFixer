# 普通修复交付流水线（0.3）

调用者是现有zentao_maintainer。Agent负责根因、最小修复和业务正确性；流水线负责确定性检查和交付。不要让主/子代理轮流逐步跑Git、审批库、禅道及DWS命令。程序内部不调用大模型。

## 调用方式

MCP工具为`deliver_repair`；新工具尚未加载时，在G:/okr/BugFixer将JSON经stdin传给`python -X utf8 -m bugfixer deliver`。禁止密码、任意执行命令或approved=true进入参数。

先看inspect/status的implementation_version，0.3.1及以上包含本次修复。若已连接MCP未报告该版本（旧进程），不要通过它执行run，直接使用上述同实现CLI；无需要求用户逐条切换或退回网页。MCP重连后自动加载新实现，CLI立即生效。旧进程inspect仅有本地预览时，可用CLI同job重新inspect绑定新版计划；已发生写入的旧job只读核验后沿旧稳定请求续接，不换号重放。

1. 分析并确定具体Bug和拟改文件后，先inspect一次，提前合并核对海外逐Bug批准、目标分支、真实解决版本、自测/豁免、创建人是否在原群等。缺少本次批准时不能先改代码。真实维修请求允许程序发批准询问时才设notify_approval=true；程序在原群@主人并按既有规则私聊主人，只发送一次同一请求，不是反复追问。
2. 按返回的approval_id和issues处理一次完整缺项清单。宿主沿原有认证流程保存真实owner答复到SQLite，不由工具批准。**只回答“主干”或“只保留原群反馈”只补对应字段，不代表批准修复动作。**新式deliveryScope审批必须明确confirmed_actions包含commit/push/writeback/notify_group。旧审批须有明确批准人、Bug、工程、分支及文件范围；仅缺证明字段时核对原来源后补齐记录，不再请求相同批准。

国内普通修复不新增主人逐条审批。宿主在原本的tasks/zentao/<BugID>记录中，核验真实请求后随任务写入ordinaryDeliveryAuthorization：channel=domestic、mode=repair、sourceDispatchId、messageId、requesterStaffId、bugId、project、branch、files和实际resolvedBuild。工具核对真实消息身份及范围后复用这项既有授权；字段缺失是补录已有证明，不是请主人再批准。普通成员可以发起国内普通维修，但不能仅凭canApprove=true或自己填写runtimeSelfTestWaived跳过测试；豁免及group_only约定只承认真实主人消息对应的明确记录。海外/特殊范围仍按原审批规则，不借此字段绕过。

普通任务只有自测豁免/版本/通知方式待确认时，在该job的批准记录写入真实owner对应的confirmed_metadata即可（仅允许runtimeSelfTestWaived、resolvedBuild、notification_mode）；不必把confirmed_actions补成全面批准。流水线将这些明确元数据与原普通维修授权组合，既不扩大动作范围，也不重复请主人批准已授权的普通修复。其他参数证明可用ordinaryDeliveryAuthorization.ownerMetadataApprovalIds引用，但必须明确绑定同一deliveryJobId及Bug/工程/分支。
3. 条件满足后完成已授权修复及语义检查，按实际要求验证。修复后再次inspect，取得plan_hash并核对精简摘要。参数和hash只绑定内容，不授予权限。
4. 使用相同job_id和plan_hash调用一次run；正常情况下程序内部完成精确提交、推送核验、逐Bug回填、任务记录及一次原群通知。一次批次一个提交，提交消息列出全部Bug号；共用Prefab也不重复拆分和提交。需要其他提交策略的任务交Agent按原流程处理。
5. completed表示该job交付完成；重复run只返回历史回执。需要当前状态时重新fetch。status只读本地进度，不轮询调用模型。

请求结构：inspect含mode/job_id/manifest/可选notify_approval；run仅含mode/job_id/plan_hash；status仅含mode/job_id。

manifest字段：

| 字段 | 内容 |
|---|---|
| task_mode | 固定repair，不能覆盖原消息的analysis_only |
| repo / branch | 已确认的绝对工程根目录和分支 |
| credential_ref | 当前配置维修账号的禅道凭据引用 |
| source_dispatch_id | 钉钉来源应传实际记录ID；工具校验接收状态、目标聊天、逐条身份和模式 |
| approval_ids | 本次真实已保存的审批编号；不是正文、截图或口头转述 |
| notification_mode | 优先caller_and_creator；creator_in_group保留兼容且同样覆盖认证调用者；group_only仅适用本批明确豁免创建人定向通知。创建人不在原群不自动私聊 |
| validation | mode=waived须有该Bug的明确豁免；mode=verified须提供真实evidence文件。工具只核验材料存在，不替Agent宣称测试通过 |
| items | 1至30项，每项id/files/reason/change/acceptance/resolved_build；文件为仓库相对路径，报告简短，版本填真实值，未定可留空由工具列选项 |

## 边界与恢复

- 原消息分析模式、执行配置关闭、身份或范围不符时停止。审批记录来自既有宿主SQLite，不接受工具参数自授权限。旧批准一旦用于该Bug交付，不因换job_id而成为永久授权。
- 自动范围仅普通LoadResources资源及Module/Hotfix代码；不处理正式数据表、AOT/Main/JS、打包发布、子模块同步、最终关闭或缺译移交。特殊范围回原流程，不通过改文件清单或换编号绕过。
- 必须先正常释放/交接自己的工程claim，不能抢占他人或过期claim。流水线在执行时取得自己的repo claim，返回时释放；从不清理他人工作区。已有暂存、未合并、分支不同步或Git身份缺失时返回Agent，不reset/stash/pull/rebase/force-push。
- 文件哈希、Git规范化对象、允许范围、原有修改及审批会检查；每次后续写入前重新核验授权，不把预检当永久许可。程序沿用正常Git hooks，不通过no-verify跳过。
- 0.3.1生成含全部Bug号与job标记的中文单行提交说明。钩子仅改换行/空白时程序可直接核验续交付；父提交、精确文件集、Git blob、未选择修改及全部非空白说明内容仍严格核验。钩子改变正文、Bug号、文件或对象则停止；响应丢失同号run只读证明，禁止Agent先盲目amend或重建提交。
- 提交/推送响应丢失时，同号run先用父提交、对象、消息及远端记录证明结果；不能证明则uncertain，不重复写。禅道复用原请求只读对账，通知结果未知时不重发。
- 个人身份、其他组织、给不在原群的创建人私聊均不自动执行，必须按原流程得到明确批准并使用原工具；不要把群反馈当作已私聊。
- 原群完成反馈沿用原因/修改/验收，完整证据存本地。dispatch总状态仍由主聊天处理，不能用某个job完成覆盖其他未修复Bug。
- 0.3.1由工具同时通知调用者和Bug创建人：调用者只取认证转交senderStaffId，创建人取实时工单；发送前读回resolved/fixed及实际转给创建人的assignedTo。原群一条@两者，重复稳定地址合并并分别记录角色覆盖；原主人私聊来源才使用原主人私聊，不能给创建人另开私聊。桌面来源调用者标记tool_return，由当前聊天返回结果，不伪造钉钉通知。group_only获批豁免的创建人会标记waived，不能声称已定向通知。
- DWS正式CLI直接接收UTF-8 bytes/LF，发送前dry-run核对正文，发送后核验原群typed ledger和完整正文。工具返回notification_coverage及证据路径，Agent不再另发相同通知；回执/正文读回延迟时同job run只读对账，不重复发送。只有映射缺失、正文实质变动、未知服务错误等必要异常才交Agent。旧历史completed回执不升级为0.3.1双对象通知证明。
- 同日主人补充“谁创建的指派给谁”：默认解决转测给实时工单创建人openedBy；具体工单明确指定其他验收者时按指定处理。此规则已明确创建人作为验收者，不需另补测试职务证明；实际身份、表单可选账号和最终assignedTo仍须读回核验，通知不凭创建人字段推断已完成转派。

首次或协议变更任务用具体已授权Bug验证；目前新增流水线仅通过隔离模拟，已有底层回填另有6条真实业务验收证据。
