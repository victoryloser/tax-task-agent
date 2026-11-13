# 配置与运行

## 模块与业务边界

domain/ 负责金额、发票写入、授权、规则、记忆与批次；context/ 组织历史和预算；storage/ 保存业务与执行状态；runtime/ 实现队列、worker 和服务；ui/ 提供本地工作台。

模型选择工具，程序用整数分计算金额。发票仅允许修改用途与备注，绑定用户原话、企业权限、行版本和操作台账，提交后独立查回。流水、票号、日期和金额不可修改。

当前缺失检查限定样例规则的 2026 年 9 月：C001 全部发票应填用途，C002 金额 ≥1000 元应填。扩展月份或执行条件需同时修改领域校验及测试。

正常运行的用户、权限和企业从工作簿导入，格式见 [数据格式](data-format.md)。历史本地回归的 U001/U002 不是正常运行的内置账号。任务与偏好按用户/企业隔离。企业部署须接入真实认证及受信会话身份。

## 配置

| 配置 | 用途 |
|---|---|
| LLM_BASE_URL / LLM_API_KEY / LLM_MODEL | 单一模型服务 |
| TAX_AGENT_DATABASE_URL | 可选 PostgreSQL URL |
| READ_RETRY_LIMIT | 同轮规范化只读请求的重试预算，默认 2 |
| HISTORY_MODE | extractive 或 semantic |
| TOKENIZER_DIR / TOKENIZER_MODEL | 匹配模型的本地 tokenizer |
| CONTEXT_TOKENS | 模板 Token 上限 |

无 tokenizer 时使用 48,000 字节预算；每轮最多 18 次模型调用和 16 次工具调用，清单解析、授权与语义摘要共用预算。节点状态和事务边界定义在 [执行与恢复协议](runtime.md) 中。

## 数据与规则

备份保留 data/ 与 runs/。迁移和恢复前停止写入方，保持业务库、checkpoint 和回执一致。

规则由管理员 CLI 维护，模型无修改权限。以下 C001 是演示编号，使用时替换为实际企业；示例 JSON 的 enterprise_ids 也须匹配导入企业：

```powershell
.venv/Scripts/python.exe -m tax_agent.cli rules list
.venv/Scripts/python.exe -m tax_agent.cli rules search --user YOUR_USER_ID --enterprise C001 --month 2026-09
.venv/Scripts/python.exe -m tax_agent.cli rules import docs/example-rule.json
.venv/Scripts/python.exe -m tax_agent.cli rules activate R002 3.0 --reviewed-hash <实际content_hash> --supersede 2.0
```

import/update 创建草稿，审核原文、条件与摘要后激活。示例规则 10 月生效；条件支持信息性内容、全部发票及金额门槛，不自动编译任意自然语言。

## PostgreSQL 与服务

deploy/compose.yaml 提供 PostgreSQL 17，复制 deploy/.env.example 为 deploy/.env 并设置密码。应用配置 TAX_AGENT_DATABASE_URL。空库执行 init；迁移已有 SQLite：

```powershell
.venv/Scripts/python.exe -m tax_agent.cli migrate-postgres --source data/business.sqlite3
.venv/Scripts/python.exe -m tax_agent.cli worker --workers 2
```

迁移目标须为空；各主机保留自己的 checkpoint。SQLite 使用同机文件锁，不能用共享盘协调多机。

```powershell
./scripts/install_autostart.ps1 -Action Install -Scope User -Workers 2
./scripts/install_autostart.ps1 -Action Status -Scope User
./scripts/install_autostart.ps1 -Action Remove -Scope User
```

User 模式登录启动；Machine 模式需管理员安装开机任务。Linux 使用 scripts/tax-agent.service，调整账号、工作目录及 Python 路径。日志为 runs/service/worker.log。

recover-legacy 默认预览旧 running 记录的核对结果，--apply 保存；缺回执不能推断成功。

## 开发检查

默认 `python -m pytest -q` 包含无数据启动、临时虚构工作簿导入、校验、金额及界面测试，不调用真实模型。依赖本地 material/ Excel/Word 的历史回归在文件缺失时明确跳过；不应为使这些测试通过而提交样例数据。CLI 的 init 现在仅初始化空库，可用 --source 指定工作簿；不再自动加载历史样例。

PostgreSQL 集成测试使用专用测试库，配置 TEST_POSTGRES_URL 后执行 `python -m pytest integration -q`。该组历史测试还需要本地 material/ 文件及 psycopg。scripts/verify_extensions.py 还会运行本机服务一次，适用于已配置开发环境；不属于默认离线检查。

历史浏览器演示和 `python -m tax_agent.evaluation` 需要本地 material/ 文件及有效模型配置。浏览器脚本还需 Edge 和可选 playwright==1.65.0。执行 `python -m playwright install ffmpeg` 后运行 scripts/browser_demo.py；脚本使用隔离数据库，截图与视频写入 .build/browser-demo/。该过程不是无数据首启检查，不应把结果中的业务内容直接用于公开展示。

源码打包执行 `python scripts/build_release.py`，内容校验执行 `python scripts/check_archive.py`；需要完整隔离安装验证时再执行 `python scripts/verify_release.py`。源码包包含双语说明、许可证、CI 和数据格式文档，排除 material/、data/、runs/、.env、secrets.toml、Excel/Word、CSV 与数据库文件。

README 截图使用 `python scripts/capture_readme.py` 生成。另行安装 Playwright，Windows 使用 Edge，Linux/macOS 先执行 `python -m playwright install chromium`。脚本仅在临时目录生成虚构记录，通过真实浏览器上传工作簿，检查金额和发票页面，不调用模型，也不读取本地 material/ 或业务库。图片写入 docs/images/。

逐票修改有事务回执，批次不支持全成全败；模型响应丢失后可能重发，外部系统须定义操作标识与查回契约。
