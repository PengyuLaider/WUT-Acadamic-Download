> **何时打开本文档**：需要判断"当前网络能否直接下载论文"、"图书馆认证是否生效"、"要不要改用 WebVPN 或 CARSI"时打开。若已确定在校外并准备用 WebVPN 网页版，直接看 `vpn-webvpn.md`。

# 武汉理工大学（WHUT）校园网状态与图书馆认证参考

本文只讲**如何判定与验证**，不存放任何账号密码。文中标"实测"的结论采集于 2026-10-30（Windows 10 / PowerShell 5.1 / curl 8.13），换网络、换学期后应重新验证一次。

## 0. 一页速查

| 项目 | 值 |
|---|---|
| 图书馆门户 | `http://lib.whut.edu.cn/`（HTTPS 同样返回 200；标题「武汉理工大学图书馆」，为超星智慧门户 engine2 站点） |
| 统一身份认证（CAS） | `https://zhlgd.whut.edu.cn/tpass/login`（智慧理工大 tpass） |
| 账号 / 密码 | **校园卡号** / **智慧理工大密码**（网络信息中心官方通知明确） |
| CARSI（无需 VPN 的校外访问） | `http://carsi.whut.edu.cn/`；IdP entityID `https://idp.whut.edu.cn/idp/shibboleth` |
| 超星智慧图书馆·数据库导航 | 见 §3.3（用户提供的入口，实测 200，`<title>数据库导航</title>`） |
| 校外 WebVPN | `https://webvpn.whut.edu.cn/` → 细节见 `vpn-webvpn.md` |
| 支持渠道 | noc@whut.edu.cn；027-87297533；反馈 QQ 群 700 852 640 |

## 1. 关键域名与实测 A 记录

| 域名 | 实测 IPv4 | 说明 |
|---|---|---|
| `whut.edu.cn` | 202.114.50.2 | 学校主站（教育网） |
| `lib.whut.edu.cn` | 202.114.89.79 | 图书馆门户 |
| `webvpn.whut.edu.cn` | 202.114.49.228 | WebVPN（网瑞达） |
| `zhlgd.whut.edu.cn` | — | 智慧理工大统一身份认证（tpass） |
| `idp.whut.edu.cn` | — | CARSI / Shibboleth IdP |
| `whutlib.mh.chaoxing.com` | — | 超星托管的图书馆门户等价页：`https://whutlib.mh.chaoxing.com/page/527321/show`（`lib.whut.edu.cn` 异常时的备选入口） |
| `cas.whut.edu.cn` | **DNS 无解析** | 该域名不存在，不要尝试；统一认证在 `zhlgd.whut.edu.cn` |
| `wisdom.chaoxing.com` | 140.210.72.5/6、42.62.107.226/229 | 超星（图书馆智慧门户托管方，非校园域名） |

> 注意：`lib.whut.edu.cn`、`wisdom.chaoxing.com` 都可能返回 302/307 或有 CDN 差异，判断"能不能用"要以实际请求结果为准，不要只看解析地址。

## 2. 网络状态判定（双轨：用户自述 + 探测证据）

**判定规则**：① 先问用户"现在是否连校园网 / 能否直接下载论文"；② 再用下面四步取证；③ 两者冲突时以 §2.2/§2.3 的实测为准，并请用户确认。**不要把 IP 段写死，也不要把用户自述当唯一依据。**

### 2.1 第一步：出口 IP 粗判（弱判据）

图书馆门户首页 HTML 由服务端写入 `rqIp`，即**服务器看到的你的出口 IP**，可直接读取：

```powershell
$r = Invoke-WebRequest -Uri 'http://lib.whut.edu.cn/' -UseBasicParsing -TimeoutSec 20
[regex]::Match($r.Content, "rqIp\s*=\s*'([^']+)'").Groups[1].Value
```

```powershell
# 等价的 curl 写法（Windows 自带 curl.exe）
curl.exe -s http://lib.whut.edu.cn/ | Select-String -Pattern "rqIp"
```

```python
# Python（本机 bundled python 无 requests，用 urllib）
import re, urllib.request
html = urllib.request.urlopen('http://lib.whut.edu.cn/', timeout=20).read().decode('utf-8', 'ignore')
print(re.search(r"rqIp\s*=\s*'([^']+)'", html).group(1))
```

判据（只作提示，不作结论）：

- `202.114.x.x` → 典型 WHUT 教育网出口，**疑似**在校内；不能据此宣布已认证。
- 其他公网 IP → **不能直接判为校外**：`WHUT-ISP` / 宿舍网络的出口就是运营商公网地址，仍具校园网资源访问权限。
- 实测（2026-10-30）：`rqIp = 117.152.144.201`（中国移动段），而用户自述"校内直连、图书馆已自动认证" → 自述与 IP 冲突，按本节开头双轨规则，以 §2.2/§2.3 实测裁决并请用户确认。

### 2.2 第二步：目标数据库实测（强判据）

对**用户真正要下的那篇论文/那个库**做一次访问测试，这是最可靠的判据：

1. 直接请求目标资源（出版社页面、数据库检索页、PDF 链接）。
2. 出现下列任一情况 → 未获授权（需 CARSI 或 WebVPN 或登录）：
   - 跳转到 `https://zhlgd.whut.edu.cn/tpass/login...`（统一身份认证）；
   - 页面出现 "Sign in / Log in / 机构登录 / 购买 / 获取访问权限 / 付费" 等提示；
   - PDF 请求返回 401/403，或返回 HTML 登录页而不是 `%PDF`。
3. 能直接拿到正文/下载链接，且页面显示机构名称 → 已获授权，**不要多余登录**。

### 2.3 第三步：WebVPN 会话判定（强判据，校外场景）

请求任一条 WebVPN 重写 URL，跟随重定向看最终地址：

```powershell
curl.exe -s -o NUL -w "%{http_code} %{redirect_url}" "https://webvpn.whut.edu.cn/https/<token>/<path>"
```

- 返回 `302` 且 `redirect_url` 含 `/login` → **无 WebVPN 会话**（需用户浏览器登录，见 `vpn-webvpn.md` §4/§6）。
- 返回 `200` 且正文是目标站点内容 → 会话有效。
- Python 等价思路：`urllib.request` 跟随重定向后检查 `resp.geturl()` 是否含 `/login`（用默认 opener 时会自动跟 302，看最终 URL 即可）。

### 2.4 第四步：CARSI 判据（知网场景）

在知网通过机构登录后，**右上角显示「机构账户为：武汉理工大学」**即为成功（官方 CARSI 指南步骤 6 的原文判据）。详见 §3.4。

## 3. WHUT 图书馆与统一身份认证

### 3.1 图书馆门户（实测）

- `http://lib.whut.edu.cn/` 与 `https://lib.whut.edu.cn/` 均返回 200，标题「武汉理工大学图书馆」。
- 它是**超星智慧门户（engine2）**站点：首页 HTML 内嵌以下变量，可作探测锚点：
  - `var pageId = 527321;`
  - `var websiteId = 285586;`
  - `var p_wfwfid = 25279;`
  - `var isLogin = false;`（门户自身登录态，未登录时为 false；它**不等于**校园网 IP 授权状态）
- 门户子页 URL 形如（实测 200）：`http://lib.whut.edu.cn/engine2/general/more?appId=...&websiteId=285586&wfwfid=25279&pageId=527321&typeId=...&currentBranch=0`
- 超星托管等价页（`lib.whut.edu.cn` 异常时的备选）：`https://whutlib.mh.chaoxing.com/page/527321/show`
- 首页正文由 `/page/ck/peking_library` 组件动态渲染，**curl 抓不到导航文字**，需要浏览器（Playwright/DevTools）才能看到菜单。

### 3.2 统一身份认证（CAS / 智慧理工大）

- 登录页：`https://zhlgd.whut.edu.cn/tpass/login`（直接 GET 返回 200；页面含「统一身份认证 / 忘记密码 / 校园卡 / 验证码 / 智慧理工大」等字样，实测）。
- CAS 跳转链（实测，`curl -L` 1 跳）：
  `https://webvpn.whut.edu.cn/login?cas_login=true`
  → `302` → `https://zhlgd.whut.edu.cn/tpass/login?service=https%3A%2F%2Fwebvpn.whut.edu.cn%2Flogin%3Fcas_login%3Dtrue`
- 账号 = **校园卡号**，密码 = **智慧理工大密码**（网信中心 2022-12-29 通知、2023-03-16 通知一致）。
- tpass 登录表单（实测）：POST 到同一路径，字段 `un`（学号/用户名）、`pd`（密码）、隐藏 `lt` / `execution` / `_eventId=submit`，另有 JS 生成 `ua` / `visitorId` / `rsa` / `ul` / `pl`；**密码由前端 RSA 加密 + 图形验证码 `code`**（另提供手机验证码登录）→ **不存在稳定的脚本登录路径，只能浏览器人工登录**。
- 登录含验证码/短信二次验证，**必须由用户本人完成**；忘记密码可通过邮箱 / 企业微信 / 手机短信验证码重置（官方 CARSI 指南说明）。
- CARSI 的 IdP：`https://idp.whut.edu.cn/idp/shibboleth`（各数据库快捷访问 URL 中的 `entityID` 均为此值，实测页面可查）。

### 3.3 超星智慧图书馆·数据库导航（用户提供的入口）

原始入口（实测 HTTP 200，标题「数据库导航」，正文约 15 万字节、前端动态渲染）：

```
https://wisdom.chaoxing.com/newwisdom/doordatabase/database.html?pageId=527321&wfwfid=25279&websiteId=285586&sw=&uid=46691481&dataType=100001
```

要点（均已实测）：

- `pageId=527321`、`websiteId=285586`、`wfwfid=25279` 与图书馆门户首页内嵌变量**完全一致** → 这是图书馆智慧门户的数据库页，不是第三方野站点。
- **HTML 内没有任何数据库名称**：搜索 `知网 / CNKI / 万方 / 维普 / IEEE / Springer` 均 0 次命中；列表由前端 JS 异步加载。
  → 执行 agent **不要编造列表 API**；需要列表时用浏览器渲染页面，或在 DevTools → Network 里抓真实 XHR。
- `sw=` 是搜索词参数；实测把它改成"知网"后 HTML 长度仍约 15 万字节且内容不变 → 过滤发生在前端，**不能靠改 URL 拿结果**（页面动态渲染，字节数会变，不要当稳定判据）。
- 该页的 WebVPN 重写 URL 已逐字符验证可构造（见 `vpn-webvpn.md` §2/§3）；未登录状态访问重写 URL 会 302 到 `/login`（实测）。

### 3.4 CNKI（中国知网）等数据库怎么进

**校内（已认证 / 已连校园网）**：图书馆门户 → 数据库导航（§3.3 URL）→ 打开目标库入口。列表需浏览器渲染，优先让浏览器完成点击，再用脚本接管后续 URL。

**校外（推荐 CARSI，官方「方法二：先资源后认证」）**，知网实测流程：

1. 打开 `https://www.cnki.net/` → 右上「机构登录」→「校外访问」。
2. 机构列表输入并选中「武汉理工大学」→「前往」。
3. 跳转 `zhlgd.whut.edu.cn` 统一身份认证，**用户本人**输入校园卡号 + 密码（+ 验证码）。
4. 勾选「同意此使用条款」→ 提交；发布信息时长选「当提供给此服务的信息变化时再次提示我」→ 接受。
5. 回到知网首页，右上显示「**机构账户为：武汉理工大学**」= 成功。

**CARSI 官方资源清单**（`http://carsi.whut.edu.cn/` 实测，共 13 项）：万方、Web of Science、Elsevier(ScienceDirect)、IEEE/IET Electronic Library、SpringerLink、Springer eBooks、EBSCO、IOP、RSC、Emerald、ProQuest Research Library、ACS、PQDT。
中文库另见官方 CARSI 指南：中国知网 `https://www.cnki.net/`、万方 `https://www.wanfangdata.com.cn/`、维普 `http://qikan.cqvip.com/index.html`、超星 `https://fsso.zhizhen.com/index`。

**知网风控实测**：`https://kns.cnki.net/` 会 302 到 `https://kns.cnki.net/verify/home?captchaType=blockPuzzle&...`（滑块验证，标题「安全验证」）。
→ 脚本直连不可行；**禁止绕过验证**，必须用浏览器 + 用户本人交互。
**并发限制**：知网等库有并发数限制，用完退出（官方指南提示）。

## 4. 校内直连分支（无需登录时）

1. 确认接入方式：`WHUT-WLAN`（教学办公区常用，认证后 7 天免认证）/ `WHUT-1X`（增强型，30 天免认证）/ `WHUT-DORM`（宿舍）/ `WHUT-ISP`（运营商校园宽带，具备校园网资源访问权限）（来源：网信中心《校园网服务》）。
2. 看 Wi-Fi 状态：图标出现「!」或「无网络连接」= 连上信号但**未认证**。
3. 用 §2.1 读 `rqIp` 记录出口；用 §3.3 打开数据库导航，确认页面能正常渲染。
4. 用用户指定的论文做一次真实下载测试（§2.2）。
5. 通过 → 直接使用数据库原生 URL，不要额外登录、不要开 WebVPN；学校指南建议文献访问优先 CARSI（无需 VPN、更快、链路更短，可减少门户跳转的会话冲突）。
6. 某个库仍要求登录：区分是"库自身账号"（如知网个人账号、EndNote 账号）还是"IP 授权不覆盖该库"；记录下来交给用户确认，不要反复试密码。

## 5. 强制门户 / 认证拦截的识别

| 判据 | 命令 | 含义 |
|---|---|---|
| A. HTTP 被 302/307 到认证页 | `curl.exe -s -o NUL -w "%{http_code} %{redirect_url}" http://example.com/` | `redirect_url` 含 `login/auth/portal/srun/webauth` 等 → 被强制门户拦截 |
| B. 200 但正文是登录表单 | `curl.exe -s http://example.com/ \| Select-String -Pattern "<title>|认证|登录"` | captive portal 常见：拦截后返回登录页 HTML |
| C. HTTPS 被降级到 HTTP | 同上，观察 redirect_url 协议 | 可疑，不要输入任何凭据 |
| D. DNS 正常但所有 HTTP 被改写 | `Resolve-DnsName -Type A example.com` + A 判据 | 典型校园网未认证状态 |

- WHUT 具体认证门户 URL：**未核实**——官方《校园网服务》页只说明 SSID 与"需要认证后才能使用"，未公布门户地址。用判据 A 现场抓 `Location` 即可得到真实地址。
- 处理：让**用户本人**在浏览器完成认证（或改连 `WHUT-1X`），agent 不要自动化提交认证表单。

## 6. 凭据处理（红线）

- 账号 = 校园卡号，密码 = 智慧理工大密码，只允许**用户本人**输入。
- ❌ 禁止把密码写进 skill 文件、参考文档、脚本、日志、`.env` 提交仓库。
- ❌ 禁止把密码作为命令行参数（会进 PowerShell 历史、进程命令行、崩溃转储）。
- ✅ 交互式输入：PowerShell `Read-Host -AsSecureString`；Python `getpass.getpass()`。
- ✅ 需要网页登录时，把控制权交还用户：打开浏览器 → 用户输入账号密码/验证码 → agent 只接管登录后的页面。
- ❌ 不代填验证码、不自动提交登录表单（验证码本身就是反自动化设计，且违反学校账号管理要求）。
- ❌ 不在对话/日志里回显密码、Cookie、ticket；引用时写成 `wengine_vpn_ticket...=<redacted>`。
- ✅ 会话结束提醒用户在 WebVPN 右上角「注销」。

## 7. 常见故障排查

| 现象 | 检查命令 | 处理 |
|---|---|---|
| 域名解析失败 | `Resolve-DnsName lib.whut.edu.cn -Type A` | 换回自动获取 DNS；临时用 `223.5.5.5` 对比；不要手改 hosts |
| IPv6 不通 | `Resolve-DnsName lib.whut.edu.cn -Type AAAA`；`curl.exe -6 -I https://lib.whut.edu.cn/` | 部分资源只认 IPv4，可强制 IPv4（`curl -4`）或临时禁用 IPv6 网卡再试 |
| 代理冲突 | `netsh winhttp show proxy`；`$env:HTTP_PROXY` / `$env:HTTPS_PROXY` | 系统代理（Clash/V2Ray 等）会把校内请求送错出口；临时关闭或加"直连"规则 |
| 证书错误 | `curl.exe -v https://lib.whut.edu.cn/ 2>&1 \| Select-String "SSL|subject"` | 不要无脑 `curl -k`；先确认是否校内设备/网关证书；不可信就改走 CARSI |
| 登录后仍提示未授权 | 换无痕窗口；清 `webvpn.whut.edu.cn` / `lib.whut.edu.cn` 的 Cookie | 常见于多标签/多账号串会话；退出重登；不要同时开 CARSI 与 WebVPN |
| 页面能开但下载 0 字节 | `curl.exe -o out.pdf -w "%{http_code} %{content_type}" <pdf url>` | 多半拿到的是登录页 HTML；检查 `content_type` 是否 `application/pdf` |
| 数据库列表空白 | 浏览器 DevTools → Console/Network | 前端 JS 被拦或 CDN 加载失败；禁用广告拦截插件重试 |

## 8. 未核实项（不要当事实使用）

1. 图书馆「数据库导航」的真实列表 XHR API（页面 JS 异步渲染，HTML 无库名）。
2. 校外访问 `wisdom.chaoxing.com/.../database.html` 时是否额外要求超星登录（当前实测未触发登录页；`uid=46691481` 的含义未核实）。
3. WHUT 强制门户的具体认证 URL。
4. `zhlgd.whut.edu.cn` 与 `idp.whut.edu.cn` 是否同一套账号后端（官方文档均称"统一身份认证"，未验证实现）。
5. 图书馆"电子资源使用管理办法"的原文 URL（WebVPN 使用指南中被引用，未找到公开链接）。
6. 文献传递 / 馆际互借的在线申请入口 URL。
7. 用户当前网络实际归属：实测 `rqIp=117.152.144.201` 非教育网段，与"当前在校内网络直连"的自述不一致（可能为运营商校园宽带 / 热点 / NAT），需用户确认。

## 9. 来源

- 网信中心《关于启用 Web VPN 校外访问系统的通知》：http://nic.whut.edu.cn/notices/202609/t20260903_1415527.html
- 网信中心《关于调整师生用户在非校园网环境下访问校内资源方式的通知》（含账号规则、CARSI、客户端 VPN 现状）：http://nic.whut.edu.cn/notices/202609/t20260903_1415528.html
- 《武汉理工大学WebVPN使用指南》附件：http://nic.whut.edu.cn/notices/202609/P020260903599590750538.docx
- 《武汉理工大学校园网 VPN 服务管理规范》附件：http://nic.whut.edu.cn/notices/202609/P020260903599590875737.docx
- 网信中心《校园网服务》（SSID 与认证说明）：https://nic.whut.edu.cn/service_guides/network/202609/t20260904_1415662.html
- 网信中心《CARSI校外访问学术资源使用指南》：http://nic.whut.edu.cn/onboarding/202609/t20260916_1417787.html
- 武汉理工大学 CARSI 服务（资源清单）：http://carsi.whut.edu.cn/
- 武汉理工大学图书馆：http://lib.whut.edu.cn/
- 超星智慧图书馆数据库导航（用户提供入口）：https://wisdom.chaoxing.com/newwisdom/doordatabase/database.html?pageId=527321&wfwfid=25279&websiteId=285586&sw=&uid=46691481&dataType=100001
- 本机实测命令输出（curl/Invoke-WebRequest/Resolve-DnsName，2026-10-30）
