# 维护与验证

本轮修订：2026-11-02。修复 2026-11-01 使用报告中的 P0/P1/P2 缺陷（静默错篇、登录墙误判、campus_net 空字段、批处理串行、中文 DOI、import 输出/命名、退出码、代理模式冲突、凭据路径/DPAPI、运行时不固定、错误吞掉等）。已做离线回归；真实学校登录仍需现场验收。

## 离线回归

在源码包根目录执行；Python 3.10+、Node 22+、Windows PowerShell 5.1+。完整 PDF 测试需同一个 Python 环境可导入 `pypdf`。不会读取账号、浏览器 profile 或向学校/出版商发请求。

```powershell
python -m unittest discover -s skill-src/tests -p "test_*.py" -v
node --test skill-src/tests/test_browser.js skill-src/tests/test_login.js
powershell -NoProfile -File skill-src/tests/test_network.ps1
python skill-src/scripts/webvpn_url.py --selftest
```

测试覆盖：DOI 括号和无效标识、超限二级 PDF、HTML/截断/损坏文件、错误篇目、缺解析器、清单缺文件/哈希变化、免联网缓存、部分失败批次、非 OA 位置、导入与索引、地址幂等与未验证端口、浏览器参数、验证码、日志 URL、CDP 超时/断线、公共页面和独立会话误判、CIDR 掩码、登录墙识别、PDF 候选白名单、来源/版本标注、退出码契约、campus_net 字段/内置网段/代理绕过、batch resume/并发/限速、中文 DOI 解析、import 单行 JSON/--name、DPAPI 加密保存。

## 退出码契约

`0` 成功/已保存（含 `saved_pending_verification`）；`1` 参数/配置/文件错误或 `wrong_document`；`2` 需要用户（登录、验证码、凭据缺失、`no_oa`/`source_unavailable`）；`3` 部分完成/未拿到 PDF/待补路径。脚本化时以 JSON 的 `status` 为主、退出码为辅。

## 用户辅助验收

每次只测一篇用户选定论文，保存到单独测试目录，保留命令和脱敏结果即可，不发密码/Cookie。

1. 在用户日常使用的校园网或 WebVPN 中，确认文章题名、DOI、版本与全文权限。认证仍由用户操作。
2. 若宿主支持浏览器操作，直接在该会话下载；独立 CLI 需在它启动的 Edge/Chrome 中重新登录。不要混淆两套会话。
3. 测一篇 OA 和一篇用户能手动下载的订阅论文；知网/万方/维普各自独立记验收结果。搜索必须先列候选，选定后才下载。
4. 验证一次真实验证码续接、一次下载触发新窗口/阅读器、一次会话过期。确认返回 `need_user` 或具体失败，不误报完成。ScienceDirect 需验证 `--no-proxy` 下机构 IP 直连，以及落盘后 `pdf_check.py` 的 DOI/题名核验。
5. 保存后 `import-file`，看 PDF 首页与目标一致，再 `organize`；检查状态、路径、哈希、`version_type` 和 `source_type`。
6. 若失败，只提供 DOI、脱敏文章落地页、工具返回状态、卡住步骤。需要时用户可提供页面可见信息；不要采集完整登录页 HTML、Cookie 或浏览器配置库。

尚待现场确认的点：搜索 DOM 是否仍适配、下载 frame 事件是否能归属、跨站/阅读器保存、当前机构授权范围、学校认证入口是否变化。网站变化可调整定位器，不应通过导出全部 Cookie、关闭浏览器安全机制或模拟人机验证补救。
