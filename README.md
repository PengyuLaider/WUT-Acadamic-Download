# 校园网数字图书馆论文下载 · DSH Skill

一个可安装到 DeepSeek Harness（DSH）的 skill：**先询问用户是否在校园网环境 → 校内直连 / CARSI 或 WebVPN 登录数字图书馆 → 检索并选择论文 → 下载 PDF → 归档清单**；纯 OA 论文可校外直连下载。

- 适用：Windows（PowerShell 5.1+ / 7 均可）、有校园网或学校 WebVPN 的高校用户；默认按**武汉理工大学**配置，换学校只改 `config.json`。
- 依赖：零第三方库。`paper_oa.py` / `webvpn_url.py` 只用 Python 标准库；`campus_net.ps1` 只用 Windows 自带 PowerShell + .NET；`browser_dl.js` 只用 Node 22+ 内置 WebSocket（无需 npm install）。
- **订阅全文自动下载**：`browser_dl.js` 驱动本机 Edge/Chrome，在校园网机构授权下自动检索并下载知网/万方/维普/出版社 PDF；只有遇到滑块/统一认证时暂停让用户点一次，Cookie 持久化后不再打扰。
- 合规：只下载 OA 或学校已授权资源；不绕过付费墙、不使用 Sci-Hub 等镜像、不共享账号。
- 校外访问优先级：**CARSI（官方推荐，无需 VPN）→ WebVPN 网页版 → 纯 OA**；统一认证的人工验证码/扫码由用户本人在浏览器完成。

## 目录结构

```text
校园网论文skill/
├── skill-src/                         ← 可安装的 skill 本体（复制到 skills 根目录）
│   ├── SKILL.md                       ← 技能主文档（流程 + 硬规则 + 命令）
│   ├── references/
│   │   ├── campus-net-whut.md         ← 校园网/图书馆/统一认证排障
│   │   ├── vpn-webvpn.md              ← WebVPN 原理、登录、Cookie、降级
│   │   ├── open-access.md             ← OA API 链路与 DOI→PDF 决策流程
│   │   └── library-databases.md       ← 知网/万方/WoS/Scopus/IEEE/… 入口与下载差异
│   ├── scripts/
│   │   ├── campus_net.ps1             ← 网络检测、登录引导、URL 重写、打开浏览器、DPAPI 凭据
│   │   ├── browser_dl.js              ← 真实浏览器自动下载（知网/万方/维普/出版社订阅全文，零依赖）
│   │   ├── webvpn_url.py              ← 任意 URL → WebVPN URL（网瑞达 token 算法，含自检）
│   │   └── paper_oa.py                ← 检索 / OA 定位 / PDF 下载 / 批量 / 归档
│   └── config/
│       └── config.example.json        ← 学校、探测页、认证、WebVPN 配置模板
└── README.md                          ← 本文件
```

## 安装

DSH 会自动扫描两个 skills 根目录（`<项目>/.dsh/skills` 与 `~/.dsh/skills`）。推荐装到用户级，所有会话可用：

```powershell
$dest = "$env:USERPROFILE\.dsh\skills\campus-library-papers"
New-Item -ItemType Directory -Force $dest | Out-Null
Copy-Item -Recurse -Force ".\skill-src\*" $dest
# 已存在 config.json 时不覆盖用户配置（升级也安全）
if (-not (Test-Path "$dest\config\config.json")) {
    Copy-Item "$dest\config\config.example.json" "$dest\config\config.json"
}
# 清理 Python 缓存，避免把 __pycache__ 装进技能目录
Get-ChildItem -Recurse -Force $dest -Filter __pycache__ | Remove-Item -Recurse -Force
```

只在本项目内使用则复制到 `<项目>\.dsh\skills\campus-library-papers\`。

然后：

1. 编辑 `config.json`，至少填 `email`（真实邮箱，Unpaywall/Crossref 拒绝 `example.com`）与 `download_dir`。
2. 换学校：改 `on_campus_probes`（校内探测页）、`webvpn.base`、`auth.login_url`、`carsi.portal`、`campus_cidrs`。
3. 触发方式：
   - 对话里说「帮我登录图书馆下这篇论文」→ 模型自动加载本 skill；
   - 或输入 `/campus-library-papers` 显式调用。

安装后可用 `skill` 工具加载验证；在 DSH 中新建会话即可在技能目录看到 `campus-library-papers`。

## 使用流程（skill 会按这个顺序走）

1. **询问网络环境**：校内直连 / 已连 VPN / 校外未连 / 不确定 → 同时运行 `campus_net.ps1 -Action status` 取客观证据（含 `on_campus`、`vpn_connected`、WebVPN 重写探测、CARSI 门户可达性）。
2. **分支**：
   - 校内直连：直接进数字图书馆，浏览器已带机构授权；
   - 校外 + CARSI（优先）：在数据库官网做机构登录（武汉理工大学 → 统一认证），无需 VPN；
   - 校外 + WebVPN：打开 WebVPN 统一认证入口，用户手动登录（验证码/JS 加密密码不代填），之后所有库地址用 `webvpn_url.py` 重写；
   - 校外无 VPN：只走 OA 通道，脚本直接下载。
3. **检索选择**：`paper_oa.py search` 给结构化候选（DOI/OA 状态/引用量）→ 让用户选；中文库用 `browser_dl.js --site cnki/wanfang/vip --search "关键词"` 自动检索并下载。
4. **获取全文**：订阅全文走 `browser_dl.js`（真实浏览器自动下载，首次滑块点一次后 Cookie 记住）；OA 走 `paper_oa.py fetch`；浏览器也不支持的站点再 `campus_net.ps1 -Action open` 人工兜底。
5. **归档汇报**：`paper_oa.py organize` 生成 `index.csv`，给出路径、来源、许可与失败替代方案。

## 示例对话

```text
你：帮我下几篇关于「knowledge graph + RAG」的论文，英文的。
Agent：先确认网络环境——你现在在校园网吗？（校内直连 / 已连 VPN / 校外未连 / 不确定）
你：在校内。
Agent：[运行 campus_net.ps1 -Action status，on_campus=true]
       检索到 8 篇候选（表格：标题/年份/作者/DOI/OA），其中 5 篇有 OA 版本，3 篇需要校内授权。要下哪些？
你：前两篇 OA 的，加第 4 篇。
Agent：[paper_oa.py fetch ×2 → 下载并写 manifest]
       第 4 篇无 OA，已打开出版社页面（校内 IP 已授权），请在页面点「Download PDF」后告诉我文件名。
你：下好了，xxx.pdf。
Agent：[移动到 papers/，organize，汇报路径+来源+清单]
```

校外时同样的流程会多一步：检测到 `on_campus=false` 后先询问是否用 CARSI（推荐）或 WebVPN；需要 WebVPN 时打开统一认证页由你手动登录，之后 agent 把数据库 URL 重写后再打开。

## 脚本单独用法

> 首次使用先 `copy .\skill-src\config\config.example.json .\skill-src\config\config.json` 并填 `email`；下面的 `-Config` 示例用 `config.example.json`，避免文件不存在时取不到学校配置。

```powershell
# 网络状态（on_campus / vpn_connected / 强制门户 / WebVPN / 公网 IP / CARSI）
powershell -NoProfile -ExecutionPolicy Bypass -File .\skill-src\scripts\campus_net.ps1 -Action status -Json -Config .\skill-src\config\config.example.json

# 校外：打开 WebVPN 统一认证（用户手动登录）
powershell -NoProfile -ExecutionPolicy Bypass -File .\skill-src\scripts\campus_net.ps1 -Action open -Url "https://webvpn.whut.edu.cn/login?cas_login=true"

# 订阅全文自动下载：知网/万方/维普 检索并下载第一篇；也可给文章 URL / DOI
node .\skill-src\scripts\browser_dl.js --site cnki --search "深度学习 目标检测" --out papers
node .\skill-src\scripts\browser_dl.js --site wanfang --search "深度学习" --out papers
node .\skill-src\scripts\browser_dl.js --url "10.1016/j.cell.2012.10.006" --doi "10.1016/j.cell.2012.10.006" --out papers

# URL → WebVPN URL（纯 Python，含算法自检）
python .\skill-src\scripts\webvpn_url.py "https://kns.cnki.net/kcms2/article/abstract?v=xxx"
python .\skill-src\scripts\webvpn_url.py --selftest

# OA 检索 / 解析 / 下载 / 归档
python .\skill-src\scripts\paper_oa.py search "graph neural network drug discovery" --limit 8
python .\skill-src\scripts\paper_oa.py oa 10.1371/journal.pone.0000308
python .\skill-src\scripts\paper_oa.py fetch 10.48550/arXiv.1706.03762 --out papers
python .\skill-src\scripts\paper_oa.py organize papers
```

> Windows PowerShell 5.1 执行策略默认禁止运行 `.ps1`：命令里带 `-ExecutionPolicy Bypass` 即可；`campus_net.ps1` 已保存为 **UTF-8 with BOM**（5.1 识别中文所必需），另存时请保持。

## 已验证内容（本机实测）

| 项 | 结果 |
|---|---|
| WebVPN token 算法 | 用用户提供的真实链接反向验证：`wisdom.chaoxing.com` → token 完全一致；整条重写 URL 逐字符一致（`--selftest` 通过） |
| OA 下载 | arXiv `10.48550/arXiv.1706.03762`、PLoS `10.1371/journal.pone.0000308` 下载成功，PDF 魔数校验通过，清单写入 |
| Unpaywall | 需真实 email；`example.com` 会被 422 拒绝（脚本已给出提示） |
| 网络检测 | 图书馆/数据库探测页、强制门户、公网 IP、代理、内网 IP 均正常输出；WebVPN 重写探测（302→/login）与 CARSI 门户可达性已纳入 `status` |
| 统一认证 | WHUT 为智慧理工大 tpass：图形验证码 + JS RSA 加密密码 → 脚本判定 `manual_required` 并引导浏览器人工登录 |
| 学校入口 | `lib.whut.edu.cn`（超星智慧门户）、`wisdom.chaoxing.com` 数据库列表、`zhlgd.whut.edu.cn/tpass`、`webvpn.whut.edu.cn`（网瑞达）、`carsi.whut.edu.cn`、`idp.whut.edu.cn` 均实测可达；`cas.whut.edu.cn` 不存在 |
| 风控识别 | 知网 `kns.cnki.net` → `/verify/home?captchaType=blockPuzzle`（滑块）；`browser_dl.js` 遇到验证页会暂停并置前浏览器窗口等用户点一次 |
| **知网订阅全文自动下载** | 实测：`--site cnki --search "深度学习 目标检测"` → 自动检索、进详情页、下载 PDF（1.05 MB、2.02 MB 两篇均 `%PDF` 校验通过，写入 manifest） |
| **万方订阅全文自动下载** | 实测：`--site wanfang --search "深度学习"` → 检索、拼详情页 URL、下载 PDF（1.94 MB，`%PDF` 校验通过） |
| **出版社订阅 PDF 自动下载** | 实测：Springer 订阅文章（2.67 MB）、Elsevier/Cell 订阅文章（0.17 MB 与 8.0 MB）经 `browser_dl.js` 自动下载（Cookie+Referer / 页面内 fetch 两条路径） |
| 离线自检 | `python -m py_compile` 通过；`webvpn_url.py --selftest` 通过；`.ps1` UTF-8 with BOM 校验通过 |

## 已知限制

- **知网/万方/维普首次访问可能弹滑块**：`browser_dl.js` 会暂停并置前浏览器窗口，用户点一次后 Cookie 持久化（`%USERPROFILE%\.dsh\campus-papers\browser-profile`），后续自动下载。
- **维普部分刊需机构登录**：`--site vip` 检索可用；若目标刊未在机构订阅范围，会返回 `no_pdf_found`，可先用 `--dump` 排查或走人工兜底。
- **WoS/Scopus 导出（RIS/CSV/BibTeX）仍是人工流程**：这两个平台不提供 PDF 下载，导出操作由用户在浏览器完成，skill 负责打开入口与后续归档。
- **统一认证有验证码/扫码的学校不能脚本登录**：skill 明确交还用户手动完成（这也是安全设计）。
- **CARSI 是逐库机构登录**：每个数据库首次使用都要走一次学校 IdP（脚本不自动化该流程）；CARSI 覆盖范围有限，未覆盖的库再走 WebVPN。
- **WebVPN 只重写 URL**：脚本不代替浏览器登录态；校外下载授权文献仍需用户在已登录 WebVPN 的浏览器里点击。匿名访问也会下发同名 Cookie，不能只凭 Cookie 判断登录态（脚本用「重写 URL 是否 302→/login」判定）。
- **校内探测是启发式**：`on_campus` 依赖配置的探测页与 IP 段，不同学校需按 `config.json` 调整；用户回答优先。
- **数据库列表页前端异步渲染**：超星数据库导航 HTML 里没有库名，不要臆造列表 API，需浏览器点击或直接打开目标库官网。

## 安全与合规

- 账号密码不写入 `config.json`；可选 `-Action save-cred` 用 Windows DPAPI 加密保存在 `%USERPROFILE%\.dsh\campus-papers\cred.xml`（仅当前 Windows 用户可解密）。
- 不在对话中收集/回显密码；验证码、短信码、扫码一律由用户在浏览器完成。
- 仅下载 OA 或学校已授权内容；尊重数据库使用条款，不批量抓取、不共享账号、不绕过付费墙。
