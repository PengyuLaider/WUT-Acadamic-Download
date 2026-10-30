> **何时打开本文档**：用户在校外（或校园网未认证）需要下载论文、确定走 WHUT WebVPN 网页版时打开。用于构造重写 URL、判断登录态、抓取会话 Cookie、选择降级方案。校内直连/认证判定见 `campus-net-whut.md`。

# WebVPN 校外访问参考（WHUT · 网瑞达 WebVPN）
## 0. 一页速查
| 项 | 结论（2026-10-30 实测） |
|---|---|
| 入口 | `https://webvpn.whut.edu.cn/`（`http://` 307→HTTPS；根路径 302→`/login`） |
| 系统 | 北京网瑞达「资源访问控制系统」，静态资源在 `/wengine-vpn/`；**不是**深信服/EasyConnect |
| 账号 | **校园卡号** + **智慧理工大密码**（官方通知） |
| 登录方式 | 「统一认证」CAS 或「账号登录」（一卡通号+密码+图形验证码），两种都有人工验证码 |
| 未登录访问资源 | `302 Location: https://webvpn.whut.edu.cn/login` |
| 重写 URL | `https://webvpn.whut.edu.cn/{http\|https}/{token}/{path}?{query}` |
| token | `hex("wrdvpnisthebest!") + hex(AES-128-CFB(host))`，**已断言通过**（§3） |
| 会话 Cookie | `wengine_vpn_ticketwebvpn_whut_edu_cn`（HttpOnly/Secure，会话级） |
| 协议范围 | 仅 http、https（官方）；SSH/远程桌面走客户端 VPN |
| 文献优先 | CARSI（学校指南：无需 VPN、连接更快、链路更短；与 WebVPN 不要同时开） |

## 1. 系统识别与官方依据
```powershell
curl.exe -s -I "https://webvpn.whut.edu.cn/"
# 302 -> Location: https://webvpn.whut.edu.cn/login
# Set-Cookie: wengine_vpn_ticketwebvpn_whut_edu_cn=wrdvpn1-<32hex>; Path=/; Secure; Domain=webvpn.whut.edu.cn; HttpOnly
```

页面实测特征：登录页含 `username` / `password` / `code`（图形验证码）字段、`cas_login` 入口、验证码接口 `/login/image`；两个登录 tab：`data-type="cas"`「统一认证」、`data-type="local"`「账号登录」。

官方一手文档：

- 《关于启用 Web VPN 校外访问系统的通知》`http://nic.whut.edu.cn/notices/202609/t20260903_1415527.html`
- 《武汉理工大学WebVPN使用指南》附件 `http://nic.whut.edu.cn/notices/202609/P020260903599590750538.docx`
- 指南要点：浏览器访问入口 → 校园卡号登录 → 进入资源页 → 若"常用站点"没有，在页面上方地址栏输入网址（左侧下拉选 http/https）→ 跳转；用完点右上角「注销」；推荐 Chrome/Firefox/IE11/Edge/Safari。

## 2. URL 结构
```
https://webvpn.whut.edu.cn/<proto>/<token>/<path>[?<query>]
```

- `proto`：原站协议 `http` / `https`；`https` 已用给定 URL 验证，`http` 变体官方支持但未登录态无法实测。
- `token`：host 的加密串，长度 = `32 + 2×len(host)`；只加密 host。
- `path` / `query`：原 URL 的 path 与 query，**原样拼接、不再加密**（已验证：给定 URL 重建后逐字符一致 `URL_MATCH=true`，包括空参数 `sw=`）。
- 示例（给定 URL，已验证）：`https://wisdom.chaoxing.com/newwisdom/doordatabase/database.html?pageId=527321&wfwfid=25279&websiteId=285586&sw=&uid=46691481&dataType=100001`
  → `https://webvpn.whut.edu.cn/https/77726476706e69737468656265737421e7fe5298283d2653760986b4915b247bfa5069/newwisdom/doordatabase/database.html?pageId=527321&wfwfid=25279&websiteId=285586&sw=&uid=46691481&dataType=100001`
### 2.1 拼接/编码注意点（易错，逐条核对）
1. token 只对应 **host**（小写、不含 `https://`、不含 path；非默认端口时本 skill 实现会把 `host:port` 一起加密，WHUT 未实测该变体）；不要把整条 URL 丢进 AES。
2. `path` 必须以 `/` 开头；token 后直接接 `/newwisdom/...`，中间不要多写 `//`。
3. **query 原样搬运，不要二次 `encodeURIComponent`**：`%25` 双编码会让目标站点收到错误参数（搜索词变成 `%E4%B8%AD` 字面量）。
4. 中文/空格等非 ASCII 字符保持原 URL 已有的 percent-encoding；用 `urllib.parse.urlsplit` 拆解，不要重新 quote。
5. `#fragment` 不发送给服务器，重写 URL 中通常丢弃；如需锚点由前端恢复（未核实）。
6. 不要对 token 做 URL 编码或大小写转换。
7. `sw=` 这类空值参数要保留；query 中原有的 `&` 不要写成 `&amp;`（从 HTML 复制常见）。

## 3. token 算法（已用真实 token 验证）
### 3.1 结论
```
token = hex("wrdvpnisthebest!") + hex( AES-128-CFB( plaintext=host, key=iv="wrdvpnisthebest!" ) )
  等价：token = hex( key_bytes || AES-CFB(host) )，key_bytes = b"wrdvpnisthebest!"（16 字节）
```

- 固定前缀 **32 个 hex 字符**：`77726476706e69737468656265737421`，即 `hex("wrdvpnisthebest!")`，末尾 `21` = ASCII `!`。
- ⚠️ 前缀若写成 `hex("wrdvpnisthebest")`（15 字节 / 30 字符）是**笔误**，会少掉 `21`，WebVPN 无法解析。本机断言：`MATCH(prefix15)=false`，`MATCH(prefix16)=true`。
- 同一 host 的 token 确定、可缓存；不同 host 必须重新生成。
### 3.2 实测验证（可复现）
被测 token（取自给定重写 URL）：`77726476706e69737468656265737421e7fe5298283d2653760986b4915b247bfa5069`

| 步骤 | 结果 |
|---|---|
| host = `wisdom.chaoxing.com`（19 字节） | — |
| `hex("wrdvpnisthebest!")` | `77726476706e69737468656265737421`（32） |
| AES-128-CFB 密文 hex | `e7fe5298283d2653760986b4915b247bfa5069`（38） |
| 拼接后与给定 token 比较 | **MATCH = true** |
| 反向解密 | `wisdom.chaoxing.com` |
| 整条重写 URL 重建比较 | **URL_MATCH = true** |

结论：与公开实现一致，**无出入**；唯一坑是前缀必须含 `!` 对应的 `21`。AES 自检向量：`aes-128-ecb(0^16)=66e94bd4ef8a2c3b884cfa59ca342b2e`。
### 3.3 实现细节
- 模式必须 **CFB-128**：Node 用 `aes-128-cfb`；pycryptodome 用 `MODE_CFB, segment_size=128`。CFB-8 结果是错的。
- key 与 IV **都**是 `wrdvpnisthebest!`；长度自检 `len(token) == 32 + 2*len(host)`（本例 70）。
### 3.4 生成与校验代码
Node（本机 Node 24 已验证可直接运行）：

```js
// webtoken.js —— node webtoken.js wisdom.chaoxing.com
const crypto = require('crypto');
const key = Buffer.from('wrdvpnisthebest!');           // 16 字节，同时作 key 与 IV
const ci = crypto.createCipheriv('aes-128-cfb', key, key);
const ct = Buffer.concat([ci.update(process.argv[2], 'utf8'), ci.final()]);
console.log(Buffer.concat([key, ct]).toString('hex')); // 期望 7772...fa5069
```

Python（需 `pip install pycryptodome`；本机 bundled python **无** `Crypto`/`cryptography`，实测）：

```python
from Crypto.Cipher import AES
K = b'wrdvpnisthebest!'
def token_for(host: str) -> str:
    ct = AES.new(K, AES.MODE_CFB, K, segment_size=128).encrypt(host.encode())
    return (K + ct).hex()
print(token_for('wisdom.chaoxing.com'))  # 77726476...fa5069
```

反解校验：`bytes.fromhex(token[32:])` 用同一 key/IV 解密应得回 host。

## 4. 登录
### 4.1 统一认证 CAS（推荐）
实测跳转链（`curl -L`，1 跳）：

```
https://webvpn.whut.edu.cn/login?cas_login=true
  --302--> https://zhlgd.whut.edu.cn/tpass/login?service=https%3A%2F%2Fwebvpn.whut.edu.cn%2Flogin%3Fcas_login%3Dtrue
```

- tpass 登录 POST 到同一路径，字段：`un`（学号/用户名）、`pd`（密码）、隐藏 `lt` / `execution` / `_eventId=submit`，另有 JS 生成 `ua` / `visitorId` / `rsa` / `ul` / `pl`，以及图形验证码 `code`、手机验证码登录。
- **密码由 JS RSA 加密 + 图形验证码** → 不存在稳定的脚本登录路径。**只能浏览器人工登录**：agent 打开浏览器 → 用户输入账号密码/验证码 → agent 只接管登录后的页面。
- 登录成功后回到 WebVPN，`/login` 被替换为资源页。
### 4.2 账号登录（本地账号）
- 一卡通号 + 密码 + 图形验证码（`/login/image`），请求带 `_csrf` / `auth_type`，可能触发短信二次验证（`sms_code`、`second_login_layer`）。
- 同样只能由用户操作；**不要**脚本化登录接口（`_csrf` 与图形验证码都是反自动化设计）。

## 5. Cookie
### 5.1 实测 Cookie
| 属性 | 值 |
|---|---|
| 名称 | `wengine_vpn_ticketwebvpn_whut_edu_cn` |
| 值 | `wrdvpn1-<32 位 hex>`（每次请求重新下发） |
| Domain / Path | `webvpn.whut.edu.cn` / `/` |
| Secure / HttpOnly | true / true |
| 有效期 | 无 `Expires`/`Max-Age` → 会话级，关浏览器失效 |

同域另有 `show_vpn` / `show_fast` / `show_faq` / `heartbeat` / `Language` / `route` 等偏好 Cookie，**不是凭据**。
⚠️ 未登录访问 `/login` 时也下发同名 Cookie（匿名 ticket）→ **有 Cookie ≠ 已登录**，必须用 §5.4 探测判定。
### 5.2 抓取方法（用户操作）
1. 浏览器登录 `https://webvpn.whut.edu.cn/`。
2. F12 → Application（应用）→ Storage → Cookies → `https://webvpn.whut.edu.cn` → 复制 `wengine_vpn_ticketwebvpn_whut_edu_cn` 的值；或 Network 面板右键请求 → Copy as cURL 后删多余头。
3. **Cookie 是 HttpOnly → `document.cookie` 读不到**，不能用页面脚本导出；需 DevTools Application 面板、浏览器扩展或 CDP（`Network.getAllCookies`）。
### 5.3 携带 Cookie 访问
```powershell
$s = New-Object Microsoft.PowerShell.Commands.WebRequestSession
$s.Cookies.Add((New-Object System.Net.Cookie('wengine_vpn_ticketwebvpn_whut_edu_cn', '<ticket>', '/', 'webvpn.whut.edu.cn')))
$u = "https://webvpn.whut.edu.cn/https/<token>/newwisdom/doordatabase/database.html?pageId=527321&wfwfid=25279&websiteId=285586"
$r = Invoke-WebRequest -Uri $u -WebSession $s -UseBasicParsing -MaximumRedirection 5
$r.StatusCode; $r.BaseResponse.ResponseUri.AbsoluteUri
```

```bash
curl.exe -s -L -b "wengine_vpn_ticketwebvpn_whut_edu_cn=wrdvpn1-xxxx" \
  "https://webvpn.whut.edu.cn/https/<token>/<path?query>" -o out.html
```

Python（本机无 requests）：`urllib.request.Request(url, headers={'Cookie': 'wengine_vpn_ticketwebvpn_whut_edu_cn=wrdvpn1-xxxx', 'User-Agent': 'Mozilla/5.0'})`。
### 5.4 有效性探测（判据）
```powershell
curl.exe -s -o NUL -w "%{http_code} %{redirect_url}" "https://webvpn.whut.edu.cn/https/<token>/<path>"
```

| 结果 | 判定 |
|---|---|
| `302` + redirect_url 含 `/login` | 未登录 / ticket 失效 → 按 §6 重新登录 |
| `200` + 目标页面正文 | 已登录 |
| `200` 但 `<title>` 是「资源访问控制系统」 | 伪 200 登录页，按未登录处理 |

## 6. 校外 WebVPN 分支操作序列
1. **探测**：按 §5.4 请求目标重写 URL；已登录 → 第 4 步。
2. **引导登录**：让用户浏览器打开 `https://webvpn.whut.edu.cn/` →「统一认证」→ 校园卡号 + 智慧理工大密码（验证码用户自填）。agent 不代填、不脚本化。
3. **验证连通**：用户确认后复测 §5.4；仍失败 → 换无痕窗口重登 / 确认已点「同意」授权。
4. **构造 URL**：§3.4 生成 token → 按 §2/§2.1 拼接重写 URL。
5. **访问/下载**：同一会话顺序请求；下载后校验 `Content-Type` 与文件头（PDF 应以 `%PDF` 开头），避免把登录页存成 `.pdf`。
6. **收尾**：提醒用户点右上角「注销」；丢弃 ticket，不落盘、不进日志。

补充：WebVPN 会重写页面内链接/资源，**不要**在 WebVPN 域下跑爬虫或设代理批量下载；大文件可能被限速中断，优先浏览器另存，脚本下载限并发（≤1~2）并支持重试。

## 7. 备选与降级
### 7.1 CARSI（首选替代，无需 VPN）
- 门户 `http://carsi.whut.edu.cn/`；IdP entityID `https://idp.whut.edu.cn/idp/shibboleth`。
- 覆盖：官方 12 个外文库（万方、WoS、Elsevier、IEEE/IET、SpringerLink、Springer eBooks、EBSCO、IOP、RSC、Emerald、ProQuest、ACS、PQDT）+ 中文库（知网、万方、维普、超星）。
- 优点：无客户端、官方称快于 WebVPN；缺点：覆盖不全、有并发限制、账号需激活。知网步骤见 `campus-net-whut.md` §3.4。
### 7.2 客户端 VPN 现状
- **山石 VPN（Hillstone）**：官方现用，面向教工服务器类服务，需在智慧理工大办事大厅申请开通。
- **深信服 SSL VPN（EasyConnect）**：官方 2023 通知写明**逐步关停**，不推荐。
- 客户端 VPN 无法替代 WebVPN 的网页代理能力；文献场景优先 CARSI。
### 7.3 WebVPN 不可用时的降级顺序
1. CARSI（§7.1）。
2. 换网络：`WHUT-WLAN` / `WHUT-1X` 或校内有线（见 `campus-net-whut.md` §4）。
3. 图书馆文献传递 / 馆际互借（入口 URL **未核实**，需向图书馆确认）。
4. 开放获取替代：出版社 OA、作者主页/机构知识库、arXiv、PMC、DOAJ、Unpaywall。
5. 仍拿不到 → 明确告知"授权范围不覆盖"，**不要**尝试非常规手段。

## 8. 合规红线（官方原文要点）
《武汉理工大学校园网 VPN 服务管理规范》（网信中心 2023-03-08）：

- VPN 账号**仅限本人**，不得转借；不得商用/牟利；不得恶意下载校内购买资源（含数据库）。
- **不得设置代理服务器**读取或下载电子资源。
- 违规后果：停 VPN、停校园网认证、通报批评；导致数据库被冻结需赔偿（原文举例"外文库 1 万元/月"）。
- 《武汉理工大学图书馆电子资源使用管理办法》同样适用（WebVPN 指南引用）。
- Agent 行为约束：不代填密码/验证码；不写死或回显凭据；不批量抓取；**不绕过付费墙与风控验证**（如知网滑块，实测 `kns.cnki.net` 会 302 到 `/verify/home?captchaType=blockPuzzle`）；用完提示注销。
- 支持渠道：noc@whut.edu.cn；027-87297533；QQ 群 700 852 640。

## 9. 未核实项（不要当事实）
1. 登录成功后 Cookie 是否换新值/新增字段（只观测到匿名态下发的同名 ticket）。
2. ticket 的服务端绝对 TTL（观测为会话级）。
3. `/http/<token>/` 在 WHUT 是否与 `/https/` 完全同构。
4. host 带端口时的 token 规则（推测对 `host:port` 加密，未验证）。
5. WebVPN 是否代理 JSON/XHR 接口与跨域资源。
6. 数据库导航页在 WebVPN 下是否被超星前端再次要求登录（`uid=46691481` 含义未知）；文献传递/馆际互借入口 URL；山石 VPN 客户端下载地址。

## 10. 来源
- 网信中心《关于启用 Web VPN 校外访问系统的通知》：http://nic.whut.edu.cn/notices/202609/t20260903_1415527.html
- 网信中心《关于调整师生用户在非校园网环境下访问校内资源方式的通知》：http://nic.whut.edu.cn/notices/202609/t20260903_1415528.html
- 《武汉理工大学WebVPN使用指南》附件：http://nic.whut.edu.cn/notices/202609/P020260903599590750538.docx
- 《武汉理工大学校园网 VPN 服务管理规范》附件：http://nic.whut.edu.cn/notices/202609/P020260903599590875737.docx
- 《武汉理工大学Hillstone VPN系统使用指南》附件：http://nic.whut.edu.cn/notices/202609/P020260903599590969561.docx
- 武汉理工大学 CARSI 服务：http://carsi.whut.edu.cn/
- 网信中心《CARSI校外访问学术资源使用指南》：http://nic.whut.edu.cn/onboarding/202609/t20260916_1417787.html
- WRD WebVPN 转换器（公开实现）：https://howerl.github.io/wrd-webvpn-converter/
- webVPN_mitm（公开实现，参考 URL/token 结构）：https://github.com/Metaphorme/webVPN_mitm
- 本机实测（curl/Node crypto 断言、Set-Cookie 头、CAS 302 链，2026-10-30）
