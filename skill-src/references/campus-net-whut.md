# 校园网与学校入口

需要判断校内直连、CARSI、WebVPN 或配置诊断时读此文件。以下入口来自用户原包，本轮仅离线审查，未重新联网核实可用性、订阅范围或学校政策。

| 用途 | 原包入口 |
|---|---|
| 图书馆 | `https://lib.whut.edu.cn/` |
| WebVPN | `https://webvpn.whut.edu.cn/login` |
| 统一认证候选入口 | `https://webvpn.whut.edu.cn/login?cas_login=true` |
| 学校 CARSI 门户 | `http://carsi.whut.edu.cn/` |
| 学校 IdP 标识 | `https://idp.whut.edu.cn/idp/shibboleth` |
| 数据库导航候选 | `https://wisdom.chaoxing.com/newwisdom/doordatabase/database.html?pageId=527321&wfwfid=25279&websiteId=285586&dataType=100001` |

优先学校门户上当前显示的入口，不凭旧 DNS、旧 IP、旧导航参数判断网络。原包链接中的 `uid` 含义未知，已从默认配置移除；如果导航需要其他参数，应从当前图书馆入口重新取得。

## 诊断

```powershell
powershell -NoProfile -File scripts/campus_net.ps1 -Action status -Config config/config.example.json -Json
powershell -NoProfile -File scripts/campus_net.ps1 -Action open -Url "https://webvpn.whut.edu.cn/login?cas_login=true"
```

- `probe_scope=independent_http_session` 表示命令行请求的结果；`browser_session=not_observed`，不能据此要求已登录浏览器重登。
- `vpn_connected` 为 `true/false/null`，同时看 `vpn_state` 文本（如 `campus_direct`、`webvpn_reachable_login_required`）；`null` 只表示脚本无法判断浏览器会话，`webvpn.http_probe_logged_in=false` 也只代表独立探测请求。
- 判定看 `on_campus` 与 `on_campus_reason`：内置 WHUT 网段（202.114.48.0/20、202.114.88.0/21、218.197.96.0/19）与校园 LAN 私网（10.82/16、10.83/16）命中为 `campus_cidr`/`campus_lan_private`，公网 IP 在 WHUT 网段为 `public_ip_cidr`，CERNET AS4538 只作 `cernet_as` 证据。CIDR/AS 只说明网络位置，不证明目标论文有订阅权限。
- 默认 `on_campus_probes` 均为 `reachability_only`，不证明在校园网。若有经用户确认确实只对校内授权开放的探测页，可设置 `proves_campus_access:true` 并配置明确的正向 `must_contain`；公开首页不能用作这类探测。
- `campus_cidrs` 支持 `campus_cidrs_mode=append|replace` 覆盖/追加；家庭/宿舍私网网段重叠很常见，所以校园 LAN 私网只作弱证据（`campus_lan_private`），仍需文章页权限或 `rqIp` 等佐证。
- `status` 的校园网/公网 IP 探测默认直连（`HttpWebRequest.Proxy=$null`），绕过 Clash 等系统代理；需要临时走系统代理时加 `-UseSystemProxy`。代理出口会改变机构 IP 授权，下载时同样要对独立浏览器加 `--no-proxy`。
- 403、重定向、机构标志和用户名等都不是目标全文访问成功；以文章按钮和 PDF 核验结果确认。
- 强制门户检测是启发式提示，可能与网络屏蔽/重定向混淆，不自动证明网络异常原因。

## 认证

网络诊断脚本默认 `auth.mode=manual`；它的通用 POST 登录不等于学校浏览器自动登录。用户新增授权允许通过独立 `登录配置.private.dpapi`（首选，DPAPI 加密）或 `登录配置.private.json`（回退）和 `登录图书馆.ps1` 入口填写账号密码，见 [login-handoff.md](login-handoff.md)。验证码、扫码和二次验证仍由用户操作，随后访问同一浏览器中的目标论文。CARSI 通常形成数据库侧机构会话，WebVPN 是代理页面，两者的登录结果不能互相推定。

保留原包的 `save-cred`（Windows DPAPI）与通用表单登录代码，作为其他已验证环境的可选能力；本轮没有验证自动表单登录适配学校当前认证流程。DPAPI 只降低磁盘明文风险，当前 Windows 用户仍能解密，不是永久安全保管库；无需为浏览器人工登录启用它。

不要为了升级 skill 覆盖用户的 `config.json`、删除浏览器 profile 或移动已有论文。
