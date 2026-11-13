# 当前支持的数据格式 / Supported data format

当前版本通过界面或 `import-data` 命令接入一个 `.xlsx` 工作簿；不支持 CSV、旧版 `.xls` 或任意表结构。资料保存在本地数据库中，仓库和源码压缩包不包含数据文件。

The current version imports one `.xlsx` workbook through the UI or `import-data`. CSV, legacy `.xls`, and arbitrary schemas are not supported. Data stays in a local database and is excluded from Git and source archives.

## 企业与用户 / Enterprises and users

工作表名称必须为 `企业与用户`。上半部分为企业表，下半部分为用户权限表；两个表之间可以有空行。第一列编号必须作为文本保存，企业编号为 C 加数字，用户编号为 U 加数字。企业和用户编号各自唯一。

Use a sheet named `企业与用户`, containing an enterprise table followed by a user permission table. Blank rows may separate them. IDs are text: C followed by digits for enterprises, U followed by digits for users. Each ID must be unique within its entity type.

企业表按以下顺序填写表头。用户表从另一行开始，前两列固定为 `用户编号`、`用户名称`，之后每列的表头填写上方企业编号，顺序与企业表一致。每位用户须填写所有企业权限。

Use these enterprise headers in order. The user table starts on a later row with `用户编号`, `用户名称`, followed by one enterprise ID per column, in the same order as the enterprise table. Supply every permission cell.

| 企业表头 / Enterprise header | 含义 / Meaning |
|---|---|
| 企业编号 | Enterprise ID; required and unique |
| 企业名称 | Full enterprise name; required |
| 常用简称 | Display name; required |

用户权限取值 / Permission values:

| 值 / Value | 权限 / Access |
|---|---|
| 无权访问 | No access |
| 仅可查看 或 可查看 | Read only |
| 可查看并修改 | Read and modify invoice purpose/note |

至少提供一家企业、一位用户，以及一个可查看的用户企业组合。此版本的用户选择器是本地工作台身份切换入口，并非登录认证；对外部署时须接入真实认证及受信身份。

At least one enterprise, one user, and one readable user/enterprise combination are required. The local identity selector is not authentication. Hosted deployments need authenticated, trusted identities.

## 发票 / Invoices

工作表名称为 `发票`，按以下顺序填写表头。表头前可有说明和空行，表头后的非空行均作为数据处理。

Use sheet `发票` with these exact headers in order. Notes and blank rows may precede the header; every nonblank row after it is a record.

| 表头 / Header | 类型与要求 / Type and requirement |
|---|---|
| 企业编号 | Must reference an enterprise above |
| 发票编号 | Nonempty text; unique per enterprise |
| 开票日期 | Excel date or ISO `YYYY-MM-DD` |
| 销售方名称 | Required text |
| 项目名称（票面） | Required text |
| 价税合计（元） | Positive amount in yuan, rounded half up to two decimals |
| 用途 | Optional text; blank means unspecified |
| 备注 | Optional text |

## 银行流水 / Bank transactions

工作表名称为 `银行流水`，按以下顺序填写表头。发票和银行流水表可以只有表头而没有业务记录。

Use sheet `银行流水` with the following headers in order. Invoice and bank sheets may contain only headers when no records exist.

| 表头 / Header | 类型与要求 / Type and requirement |
|---|---|
| 企业编号 | Must reference an enterprise above |
| 流水编号 | Nonempty text; unique per enterprise |
| 交易日期 | Excel date or ISO `YYYY-MM-DD` |
| 收支方向 | `收入` (income) or `支出` (expense) |
| 金额（元） | Positive amount in yuan; direction determines income/expense |
| 对方名称 | Required text |
| 备注 | Optional text |

金额使用整数分计算，收入和支出分别求和，不自动关联付款。请使用静态值；公式仅能读取 Excel 已缓存的计算结果，导入程序不会执行公式。

Amounts are stored as integer cents. Income and expense are summed independently; payments are not matched automatically. Use static cells: formulas only expose cached Excel results and are not evaluated by the importer.

## 导入与规则 / Import and rules

导入前校验工作表、字段、日期、金额、主键和企业关系，业务记录在同一事务中写入。仅允许导入空业务库，不能覆盖已有企业、会话或发票。修改现有发票请使用对话中的用途与备注保存功能。

The importer validates sheets, fields, dates, amounts, keys, and enterprise references before writing business records in one transaction. Import requires an empty business database and refuses to overwrite existing enterprise/session/invoice data. Existing invoice purpose and note can be edited through the assistant.

Excel 不会自动创建用途检查规则。管理员通过 `rules import` 导入 JSON 草稿，再审核并激活，格式见 [example-rule.json](example-rule.json) 和 [开发文档](development.md)。支持 `info`、`all_invoices`、`amount_gte_cents` 三种条件。现有用途缺失检查只接受 `2026-09`；其他月份支持金额查询和明确授权的用途/备注修改。导入规则不会解除这个月份限制。

Excel import does not create purpose-check rules. An administrator imports a JSON draft, reviews it, and activates it; see [example-rule.json](example-rule.json) and the [development guide](development.md). Conditions are limited to `info`, `all_invoices`, and `amount_gte_cents`. Purpose-completeness checks currently accept **September 2026 only**. Other months support totals and explicitly authorized purpose/note edits. Importing a rule does not remove this month restriction.

历史本地回归另有专用的 Excel/Word 样例加载器；该加载器并非通用 DOCX 导入功能，样例文件不会发布。

A separate loader exists for historical local Excel/Word regressions. It is not a general DOCX importer, and its sample files are not distributed.
