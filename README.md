# BugFixer

禅道维修员的取证与修复交付工具：获取Bug/客服工单上下文、下载日志与附件；已授权的禅道修复可通过预览、执行和回读完成描述补充及解决转测试。由当前 `zentao_maintainer` 管控，不在工具中启动agent。

0.3.1交付流水线`deliver_repair`（CLI `python -m bugfixer deliver`）：inspect合并检查，run在程序内完成提交推送、禅道解决转创建人、调用者与创建人通知及全文读回。已兼容项目钩子压平提交换行，并使用UTF-8字节输入避免钉钉中文/CRLF错误；读回延迟同号续接只读对账。未知异常或缺权限才交回Agent。参见[流水线skill](.agents/skills/bugfixer-context/references/delivery.md)及[实现说明](docs/delivery-pipeline.md)。

## 本机运行

使用当前安装的Python 3.12，工作目录 `G:\okr\BugFixer`。项目仅SSO RSA认证使用已安装的 `cryptography`，其余为Python标准库；依赖声明在 `pyproject.toml`。

```powershell
$python = 'C:\Users\admin\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
'{"source":"zentao","id":"263855","credential_ref":"zentao"}' | & $python -X utf8 -m bugfixer fetch
'{"source":"customer_service","id":"2223144","region":"domestic_minigame","credential_ref":"customer_service"}' | & $python -X utf8 -m bugfixer fetch
& $python -X utf8 -m unittest discover -s tests -v
```

Windows沙箱不能访问当前用户DPAPI或网络时，调用者须通过正常执行审批获得这次取证所需访问；不能复制其他会话身份或自行关闭沙箱。密码不通过命令行传递。

## 网页登录凭据表

数据库：`G:\okr\BugFixer\data\bugfixer.sqlite3`，表：`web_credentials`。完整建表语句见 `credentials-schema.sql`。

|字段|含义|
|---|---|
|name|工具的 `credential_ref`，例如 zentao / customer_service|
|source|zentao / customer_service|
|environment|production / test|
|login_url|网页登录URL，校验它属于该系统/环境|
|account|登录账号|
|password_encrypted|当前Windows用户DPAPI加密密码BLOB，不是明文|
|protection / updated_at|加密方式和更新时间|

运行 `录入网页登录凭据.cmd` 打开本机录入窗口，分别保存两个系统。也可使用隐藏输入：

```powershell
& $python -m bugfixer credentials save --name zentao --source zentao --login-url http://zen.wckj.com/user-login.html
& $python -m bugfixer credentials save --name customer_service --source customer_service --login-url https://cscmanage.galasports.com/html/index.html
& $python -m bugfixer credentials list
```

登录URL作为系统/环境凭据归属信息；底层只访问本项目核实的固定服务契约，不能用任意URL使工具发送密码。DPAPI密文不能直接从其他用户/机器复制使用。SQLite数据库、日志和缓存不提交Git。密码不会出现在工具返回、诊断异常或索引中；原始业务附件在本地保留，按敏感证据管理。

也支持JSON标准输入的 `credentials:{account,password}` 或 `credential_env:{account:"ENV_NAME",password:"ENV_NAME"}`；agent工具调用优先 `credential_ref`，避免secret进入对话记录。

## 输出与边界

- 每次生成新 `artifacts/<来源>-<ID>-<随机号>/context.json`、`manifest.json`，避免旧状态冒充新状态。
- `ok/partial/blocked/error` 明确取证完整性；某附件失败保留其他文件和上下文。
- 原始文件按实际签名字节识别为文本/ZIP/图片/视频，不依赖扩展名。ZIP先全量验证路径、符号链接、数量与展开大小，再提取；从不执行附件。
- 清单记录来源、时间、大小、SHA256和本地路径。文本提供行号及异常候选索引；索引不等于根因。
- 服务和附件主机白名单、跨域凭据重定向拒绝、有限超时重试。密码登录不自动重试。缓存会验证内容hash，不用历史缓存替代当前状态。
- 取证仅按确切Bug/工单ID查询；不支持玩家码跨区域搜索、发送验证码、Git或发布。客服始终只读。禅道维修回填使用独立的prepare/apply接口，fetch_context保持只读。

## 已授权维修回填

0.2新增 `prepare_writeback` → `apply_writeback`：保存精简交付预览，再按既有授权追加描述、解决fixed并转测试，读回确认。保留原始描述及附件，要求真实解决版本和测试账号；请求编号去重，冲突或结果不确定时不重放POST。

CLI入口为 `python -m bugfixer prepare-writeback` / `python -m bugfixer apply-writeback`，均从stdin接收JSON。字段、调用流程及权限边界见[回填skill](.agents/skills/bugfixer-context/references/writeback.md)，实现契约及验收限制见[回填说明](docs/writeback-contract.md)。预览不是用户审批，工具不判断修复是否有效。已在6条具体获批、完成修复及推送的海外7.1 Bug上通过真实回填验收，见[实际回读证据](acceptance/live/writeback-six-tour-bugs-20261124.json)。

客服使用部署前端核实的 `/ticket/querySingle` 和SSO契约；国内/海外区域显式路由。测试海外缺可靠后端映射，返回 `unsupported_environment`；测试SSO密码协议未核实，需交互登录时返回具体限制。不会为“支持全部环境”猜接口或验证码流程。

## MCP与维修员接入

stdio入口：`python -X utf8 G:\okr\BugFixer\serve.py`。暴露 `fetch_context`、`prepare_writeback`、`apply_writeback`、`deliver_repair`；stdin/stdout仅JSON-RPC。配置示例：

```toml
[mcp_servers.BugFixer]
command = "C:/Users/admin/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe"
args = ["-X", "utf8", "G:/okr/BugFixer/serve.py"]
```

角色使用 `.agents/skills/bugfixer-context/SKILL.md`。新MCP注册是否已被当前聊天加载，应以实际工具列表为准；未加载时可立即使用CLI同一实现。

实际验收与未覆盖项记录在 `acceptance/acceptance-report.md`。单元/fixture通过不代表真实账号登录与下载已经通过。
"# BugFixer" 
