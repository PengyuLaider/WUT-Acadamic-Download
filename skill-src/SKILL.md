---
name: campus-library-papers
description: 通过武汉理工大学校园网、图书馆、CARSI 或 WebVPN 检索和下载有权限的指定论文，支持 DOI、题名、链接、文献清单以及合法 OA，核验 PDF 并归档。用户要求校园网论文下载、学校数据库全文、OA 下载或排查图书馆访问时使用。
---

# 校园网论文下载

交付用户指定论文的本地 PDF、来源和核验结果。默认学校为武汉理工大学；沿用用户已登录的浏览器和已指定的目录。原包面向 DSH，执行时以当前宿主实际可用的工具为准。

## 先确定目标与访问路径

- DOI、文章链接或用户确认的清单可直接执行。只有宽泛关键词时，先列出题名、作者、年份、DOI 和来源，让用户选定；不要默认下载搜索第一篇。已选定的清单不反复确认。
- 从会话判断校内直连、已登录 WebVPN/CARSI、校外或未知；信息已有时不重复提问。只有访问订阅资源且路径无法判断时，再询问网络环境。纯 OA 可直接定位。
- 保留用户给定的版本号；预印本、作者接受稿和期刊出版稿分别记录（`version_type`/`source_type` 字段），不能用预印本冒充出版稿。
- `campus_net.ps1 status` 是独立 HTTP 会话的诊断，探测默认绕过系统代理直连；输出必须看 `on_campus`、`on_campus_reason`（campus_cidr/public_ip_cidr/cernet_as/probe/unknown）、`vpn_state` 和 `recommendation`，不要只看 public IP。公共首页可达、CIDR 命中和 HTTP 200 都不足以证明目标论文已授权；`vpn_connected=null` 不能否定用户浏览器已登录。以同一浏览器中的文章全文权限和实际文件为证据。

学校入口和诊断配置读 [campus-net-whut.md](references/campus-net-whut.md)；WebVPN 地址转换和会话续接读 [vpn-webvpn.md](references/vpn-webvpn.md)。

## 执行下载

1. **沿用访问方式。** 校内用数据库正常文章页；校外按数据库支持选择 CARSI 或 WebVPN。用户指定 WebVPN 时直接沿用，不强制改换。用户已授权从本机 `登录配置.private.dpapi`（优先，Windows DPAPI 加密）或 `登录配置.private.json`（明文回退）读取其账号密码并在学校登录页面填入；具体入口与保密规则见 [login-handoff.md](references/login-handoff.md)。验证码、扫码和二次验证仍由用户在页面完成，不让用户把密码、Cookie 或签名下载链接发进聊天。
2. **使用宿主支持的工具。** 优先现有浏览器工具或服务连接器；只操作获授权的会话。`browser_dl.js` 是原包的独立 Edge/Chrome CLI，需要 Node 22+。仅在宿主允许这种自动化时运行；宿主限制直接 CDP 时，不以此脚本规避限制，可提供命令由用户本机运行。该脚本的独立 profile 与宿主内置浏览器登录态互不共享。按实际出口选择代理模式；只有代理影响学校 IP 识别时才按需使用 `--no-proxy`。代理模式变化时保留会话并提示本人决定，不自动关闭浏览器；人机验证窗口被挡住时运行 `scripts/raise_browser.ps1 -KeepTopMost` 置前；验证码/扫码仍由用户点。登录墙（WebVPN/CAS/统一身份认证）会被识别为 `need_user`，不会当成文章页继续点下载。
3. **OA 路径。** 使用 `paper_oa.py oa` 定位，再 `fetch` 下载。直接 URL 是用户给定的下载位置，不自动证明其 OA 许可。元数据服务不可达时报告 `source_unavailable`，不能断言论文没有 OA。
4. **订阅路径。** 在已确认的文章页触发 PDF 下载；检索入口只列候选。出现验证码、访问拒绝或机构登录墙时停下对应下载，保留标签与进度，给用户具体页面和动作。用户完成后继续原会话，不从头重复认证。不要尝试解验证码、伪造机构访问或绕过付费墙。
5. **核验和登记。** 用下面的本地命令处理浏览器或人工下载文件。文件头与体积仅是初筛；需要结构解析、SHA-256，以及题名/DOI 与目标匹配。没有解析器、扫描件提取不到文字、题名不同或 DOI 缺失时保留文件并标待核验，不报已完成。自动匹配仍需关注勘误、附件和同题不同版本。

数据库页面差异读 [library-databases.md](references/library-databases.md)；OA 源与失败解释读 [open-access.md](references/open-access.md)。

## 命令

路径相对于 skill 根目录，若在其他目录执行需换为绝对路径。Python 3.10+；建议在同一 Python 环境安装 `pypdf`，未安装仍可检索/保存，但不能自动完成结构和篇目核验。

```powershell
python scripts/paper_oa.py search "检索词" --limit 8
python scripts/paper_oa.py oa "10.1016/j.esci.2023.100174"
python scripts/paper_oa.py fetch "10.1016/j.esci.2023.100174" --out "papers"
# 批处理：默认 3 并发，断点续跑；进度写 <out>/batch-state.jsonl
python scripts/paper_oa.py batch "dois.txt" --out "papers" --workers 3 --resume

# 仅本地核验，不修改文件、不联网
python scripts/pdf_check.py "已下载.pdf" --doi "目标DOI" --title "已核对的完整题名"
# 核验并登记：默认单行 JSON（便于逐行解析）；--name 指定归档名
python scripts/paper_oa.py import-file "已下载.pdf" --doi "目标DOI" --title "已核对的完整题名" --source-url "文章落地页" --out "papers" --name "2024_作者_短题名.pdf" --version-type published
python scripts/paper_oa.py organize "papers"

# 用户本地 CLI；先列候选，确认后优先改用文章 URL
node scripts/browser_dl.js --site cnki --search "检索词"
node scripts/browser_dl.js --site cnki --search "检索词" --select 2 --title "刚才选定的完整题名"
node scripts/browser_dl.js --url "文章URL" --doi "目标DOI" --title "完整题名" --out "papers" --no-proxy --wait-user 240
# 下载后自动用本地 pdf_check 核验 DOI/题名；不匹配会删除并返回 wrong_document（退出码 1）
# 界面：默认用 Windows DPAPI 加密凭据；--no-dpapi 才回退明文
python campus_portal.py
# 人机验证/登录窗口被挡住时置前（可选 -KeepTopMost 保持置顶）
powershell -NoProfile -File scripts\raise_browser.ps1 -KeepTopMost
# CARSI：打开学校门户，由用户选择数据库并登录（不代填验证码）
powershell -NoProfile -File scripts\campus_net.ps1 -Action carsi -Json
# 校园网/代理/WebVPN 诊断（直连探测，绕过 Clash）
powershell -NoProfile -File scripts\campus_net.ps1 -Action status -Json
```

**退出码（全项目统一）**：`0` 成功/已保存（含待核验）；`1` 参数、配置、文件错误或下载到错误文献；`2` 需要用户操作（登录、人机验证、凭据缺失、无合法 OA）；`3` 部分完成/未拿到 PDF/待补路径。

Python `--config` 位于子命令之前。显式配置不存在或格式损坏时失败，避免悄悄用错学校/输出目录；OA 脚本未配置则用默认值。网络脚本未配置时会用 `config/config.example.json`。

## 结果与收尾

| 状态 | 含义与动作 |
|---|---|
| `downloaded` / `exists` | 当前文件存在、哈希有效，且通过本地自动篇目核验；仍不等于许可已确认 |
| `saved_pending_verification` | 已保存，解析器或篇目证据不足；用 `import-file` 补核验，必要时人工看首页 |
| `selection_required` | 搜索完成，等待用户指定篇目 |
| `need_user` | 在保留的浏览器中完成登录/验证后继续；`reason=login_wall` 表示当前是认证页不是文章页 |
| `wrong_document` | 下载内容与目标 DOI/题名不一致，文件已删除，不得登记为成功 |
| `source_unavailable` | 元数据源失败，OA 结论尚不完整 |
| `no_oa` | 已查询链路未定位 OA，不等于全网无免费版本 |
| `download_failed` / `no_pdf_found` | 未获得有效 PDF；先查原因，再决定是否换路径 |

`manifest.jsonl` 记录来源、路径、哈希、核验、许可、`version_type`（preprint/accepted/published/unknown）和 `source_type`（publisher/repository/oa/local）；`organize` 只为已登记文件生成索引，不会扫描目录自动导入。浏览器 CLI 落盘后会调用本地 `pdf_check.py`：DOI/题名与目标不符会删除文件并返回 `wrong_document`（退出码 1）；解析器缺失或证据不足才返回待核验（退出码 0）。批次有未完成项时返回 3，不把部分成功报成全部完成。

遇限流或验证避免连续重试同一地址。顺序下载用户指定清单；按服务要求限速。没有访问权限时说明具体文章与失败证据，可给合法替代版本、图书馆文献传递建议或作者索取邮件草稿；提交申请、付费或发送邮件需另有用户授权。

汇报每篇的路径、版本/来源、核验状态及失败原因。待用户处理时保存最小续接信息：目标 DOI、浏览器/标签、步骤、输出目录和下一步；不保存账号、Cookie、验证码或签名 URL。未实际跑通的站点明确写未验证。

维护与离线测试见 [maintenance.md](references/maintenance.md)。

## 实战知识与易错点（2026-11-02 报告修复）

### Elsevier / ScienceDirect（订阅访问）
- 校园网直连时文章页应显示机构名与 `Full text access`；若显示 `Access through your institution` 或反复人机验证，先检查系统代理（Clash/VPN）并用 `--no-proxy` 重启独立浏览器。
- 不要用 WebVPN 重写 ScienceDirect 文章 URL：WebVPN 会改写脚本与 Cookie 域，Kasada/Cloudflare 的 JS 挑战无法在重写页完成。
- 页面里的 `pdf.sciencedirectassets.com` 图片、参考文献中的 `pdfft?md5` 都是错链。只接受本文 PII 的 `/pii/<PII>/pdfft`、`stampPDF`、`getPDF`、`pdfdirect` 等白名单地址；`browser_dl.js` 已按页面 PII 过滤并排除 `[class*="reference"]` 区块。
- 授权页面首次需要人机验证时由用户点一次；通过后同一独立浏览器 profile 保留 Cookie，后续批次无需再点。不要写程序模拟点击过验证。
- 落盘后必须用 `pdf_check.py` 校验 DOI/题名；`browser_dl.js` 已内置该检查，可疑文件隔离保留，不自动删除并报 `wrong_document`。

### 中文 DOI（10.16383/10.19721/10.15918/10.3969 等）
- Crossref/OpenAlex/S2 对部分中文 DOI 返回 404，不能据此判断无全文。
- `paper_oa.py` 会先尝试 `https://www.chndoi.org/Resolution/Handler?doi=...`，失败再回退 `https://doi.org/<doi>`，从落地页提取期刊官网 PDF/全文链接；仍失败才报 `source_unavailable` 并给中文提示，不伪造官网地址。

### 凭据存储
- 配置界面默认用 Windows DPAPI 加密保存为 `登录配置.private.dpapi`（仅当前 Windows 用户可解密），成功后删除明文 JSON；`--no-dpapi` 才回退明文。
- `login_credentials.js` 同时接受 `.dpapi` 和明文 `.json`；显式传入 `.json` 时若同目录存在 `.dpapi` 会优先使用加密文件。只有显式指定 `--credentials` 才按该路径读取，独立安装 skill 时不要把凭据文件复制进 skill 目录。

### 登录墙与续接
- WebVPN/CAS/统一身份认证页（标题含“资源访问控制系统”“统一身份认证”或 `/login`、`/cas`、`/sso` 且有密码框）会被识别为 `need_user`（`reason=login_wall`），不会继续点下载。
- `need_user` 时输出 `continuation` 语义并写 `<out>/browser-last-state.json`：目标 URL/DOI/题名、输出目录、下一步；不含账号、Cookie、验证码或完整签名 URL。用户完成认证后重跑同一命令即可。
- 登录墙/验证码必须由用户完成；不提供模拟人类过验证、自动填验证码或绕过付费墙的代码。

### CARSI 与 WebVPN 选择
- 优先 CARSI 按数据库做机构登录（ScienceDirect、Wiley、IEEE 等支持）；WebVPN 作为兜底，且只用于能承受 URL 重写的站点。
- 校外时先 `campus_net.ps1 status` 看 `recommendation`：`direct` 直连、`vpn` 用已登录 WebVPN、`oa_or_vpn` 先试 OA 再登录；`vpn_connected=null` 只表示脚本无法读取浏览器会话，不代表未登录。



## 2026-10-31 界面升级

完整工具包的配置界面入口为 `python campus_portal.py`，终端前台运行；`--demo` 不读写真实配置。旧的后台 UI 启动脚本已停用，不要求关闭防护或执行策略绕过。界面只保存账号，学校登录与 PDF 核验分别进行。
