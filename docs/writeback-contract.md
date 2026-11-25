# 禅道修复回填（0.2）

工具负责已授权修复交付的确定性写入；维修员负责授权、真实修复、分支、提交推送及说明内容。客服工单仍只读，不新增搜索、翻译、Git、发布、最终关闭或自动通知功能。

## 接口和部署契约

MCP暴露`fetch_context`、`prepare_writeback`、`apply_writeback`；CLI分别为`fetch`、`prepare-writeback`、`apply-writeback`，请求均从JSON stdin读取。取证入口保持只读，不能用operation字段变成写操作。

真实部署ZenTao12.3.3只读GET已核验：

- `/bug-edit-ID.json`和`.html`：当前Bug、历史、完整编辑表单。表单携带lastEditedDate，工具保留成功控件、多选版本和原始steps，排除上传控件。
- `/bug-resolve-ID.json`和`.html`：真实builds、用户与解决表单。builds包含主干等既有选项，不创建或猜测新版本。设resolution=fixed、status=resolved、assignedTo=指定测试账号，comment写简短交付说明。
- 写操作只允许POST至上述edit/resolve的固定同源HTML路由；带AJAX头和表单编码。POST永不自动重试。
- 详情页会向图片注入`onload="setImageSize(this,0)"`，编辑页给出存储HTML。比较时仅消除此已观察到的展示属性及CRLF差异；追加时使用编辑页原文，不把展示脚本写回。
- 实际resolve会把尚未确认的Bug的confirmed从0改为1。仅最终resolved/fixed核验允许这一转移；编辑后的confirmed及其他原字段仍严格核验。首条实际写入因旧校验误报，修正后以原请求只读对账成功，没有重发POST。

## 计划、冲突与回执

`prepare_writeback`要求明确repair模式、Bug、凭据引用、预期维修人、测试人、真实解决版本、原因/修改/验收、分支及完整提交SHA，返回精简预览和preview_hash。它只读取服务，计划保存在本机SQLite的`writeback_requests`表，不返回密码。preview_hash用来绑定计划，不是审批凭据。

`apply_writeback`只能应用该计划；重新登录/读取并校验Bug快照、历史、指派、表单字段和目标选项。先编辑描述，核验原内容及附件保留，再重取解决表单并解决转测试，最后读回状态、解决方式、版本、测试人和完整正文。成功返回completed/verified及readback_path。

request_id绑定账号和内容；相同编号不能换内容。SQLite事务领取，阻止同一Bug的其他未确认写入；成功后重复调用不重放。超时、保存缺失或部分成功返回uncertain，后续同号调用只读核对；匹配最终结果才补记completed，否则需要核对现场，禁止自行换号重放。未解决的uncertain会阻止该Bug的新写入，不能把本地记录删除当作问题已解决。

写入前、编辑后、最终回读保存到`artifacts/writeback-<request_id>/`。完整计划和原始描述视为本地敏感证据，不全量回传Agent；SQLite与产物不提交Git。

## 验收范围和限制

58项核心测试（含23项回填）及本机真实TCP服务验证表单编码、多选、原文保留、状态转换、保存失败、并发变化、超时去重和只读对账。真实服务在主人明确批准的6条已修复、已推送Bug（263761/263662/263660/263626/263625/263624）上完成POST及回读：全部resolved/fixed/trunk并转正确测试人，正文、提交号、解决评论和原始附件元数据保留。证据见`acceptance/live/writeback-six-tour-bugs-20261124.json`。没有拿未修复的业务Bug做试验；未因此证明其他账号、产品或状态的写权限。

当前部署的resolve表单没有可用的原子revision字段；工具的多次预检及edit表单lastEditedDate不能替代服务端事务锁。编辑和解决是两次远程操作，可能部分成功，没有自动回滚。工具保留uncertain及证据，不把其称为成功，不重复提交。
