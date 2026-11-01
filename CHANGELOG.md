# 更新记录

## 使用报告修复候选 — 2026-11-01

- 增加候选归属过滤、学校登录墙识别和落盘后的本地核验；可疑文件隔离保留，证据不足仍待核验。
- batch 增加逐篇进度文件与校验后续跑；import-file 单行 JSON、保留原名、--name 与 --version，CSV 保留稿件版本。
- 修正代理模式与单独安装的凭据路径说明，移除启动脚本的第三方缓存运行时路径。
- 该分支只经过离线验证；学校认证、出版社下载和中文 DOI 路径仍需现场接续，见 skill-src/references/usage-report-handoff.md。

## main 文档更新 / Documentation on main

- 丰富中英文项目介绍、适用场景、入口选择、输出与结果状态、智能体任务示例和常见问题。
- 新增双语流程概览图，完善仓库首页导航；既有 v1.0.0 / v2.0.0 标签与附件保持固定。
- Expanded bilingual project overview, use cases, entry points, output states, agent examples and FAQ. Existing release snapshots remain unchanged.

## v2.0.0 — 2026-10-31

- 白底深蓝校园门户界面，支持桌面、平板和手机布局。
- 中英文页面切换、独立英文说明及项目展示首页。
- 明确 main、功能分支与版本标签的职责，新增 v1/v2 发布归档。
- 账号配置、学校认证、全文核验分别显示状态。
- 完善密码切换、字段校验、保存反馈与原配置保留。
- 增加无账号静态演示、截图和本机前台启动方式。
- 下载流程补充验证等待、Cookie 重定向处理和 PDF 核验。
- 补齐 Git 忽略规则及离线测试工作流。
- 公开源码移除论文全文、下载清单和过期的生成文件哈希清单；已有 Git 历史保留。

真实学校账号登录及下载链路仍需按环境现场验证。凭据保持本机使用，不提交仓库。

### v2 bilingual update

- Chinese/English portal with translated static text, input hints and feedback.
- Separate Chinese and English README files, version comparison and workflow diagrams.
- v1.0.0 retained as a historical snapshot; v2.0.0 remains the current portal release.

## v1.0.0 — 初始版本 / Original version

保留初始 main 提交 `3c1df5475c15609128da9a93bfebd60d1a91b673`，提供旧版命令行实现供兼容与历史对照。v2 不覆盖这个版本。

Preserves the original main commit for CLI compatibility and historical comparison. Version 2 does not overwrite this snapshot.
