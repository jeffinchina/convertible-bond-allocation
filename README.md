# 可转债配债

本项目集中保存可转债配债机会的日常监控、官方公告证据、事件日分析和邮件投递配置。

## 自动化结构

- Windows 计划任务 `可转债配债-混合监控`：每周一至周五 08:30 启动。
- Windows 计划任务 `可转债配债-申购提醒`：正式发行文件确认申购日后，在申购日 08:50、09:50 各发送一次结构化新债申购提醒；提醒纯程序运行并复用既有私有收件人配置。
- Windows 计划任务 `可转债配债-出签上市提醒`：每周一至周五 09:15 独立查询上交所、深交所一手公告，在中签结果公告日或新债上市日发送结构化提醒；错过时点只在当日 15:00 前补发。
- `work/hybrid_monitor/monitor.py`：判断交易日，检索上交所、深交所、巨潮资讯和证监会，执行规则过滤、交叉核验与去重。
- 无新增：生成 HTML 日报并通过 `work/Send-QQMail.ps1` 发到本机私有配置中的收件人列表；脚本使用本机 Python/OpenSSL 完成 STARTTLS，避免 Windows Schannel 兼容问题。
- 有目标公告：先保存证据包，再调用本地 Codex 运行时做事件日深度分析；优先使用全局新版 CLI，项目内置 0.165.0 作为兜底，并只对模型兼容错误尝试下一个显式候选模型。
- 新增事件关键数据表与日报存量池统一显示单一字段“行业｜主营业务”，同一单元格内行业、主营业务上下两行展示；两项各不超过 10 个字符并保留可复核原始数据。
- 独立分析入口：双击桌面 `配债数据分析.cmd`，输入股票名称或代码，调用本机微服务生成关键数据 HTML/JSON 报告。
- 原 Codex heartbeat 自动化保留为暂停状态，避免与 Windows 计划任务重复发送。

## 目录

- `work/hybrid_monitor/raw/`：官方接口原始响应与分类审计。
- `work/hybrid_monitor/evidence/`：目标公告证据包。
- `work/hybrid_monitor/reports/`：日报 HTML。
- `work/hybrid_monitor/subscription_reminders.json`：本机申购提醒队列与两个发送批次状态（不进入 Git）。
- `work/hybrid_monitor/bond_lifecycle_reminders.json`、`bond_lifecycle_state.json`：本机出签/上市提醒队列与独立检索状态（不进入 Git）。
- `work/hybrid_monitor/logs/`：计划任务日志。
- `work/bond_analysis_service/`：独立配债关键数据分析微服务、桌面启动器和报告。
- `docs/conversation-archive/`：迁移的对话原始记录、原自动化配置和用户附件。

邮件授权配置由 Windows DPAPI CurrentUser 加密。发件人与收件人只保存在本机 `work/qqmail-config.json`（`to` 数组）中，该文件已加入 `.gitignore`；公开脚本和示例配置不包含真实地址。

## 产品文档

- [可转债配债自动日报产品规格说明书](docs/可转债配债自动日报产品规格说明书.md)
- [可转债配债自动日报更新记录](docs/可转债配债自动日报更新记录.md)
- [可转债配债分析微服务产品规格说明书](docs/可转债配债分析微服务产品规格说明书.md)
- [可转债配债分析微服务更新记录](docs/可转债配债分析微服务更新记录.md)
