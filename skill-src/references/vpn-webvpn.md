# WebVPN 地址与会话

入口来自用户原包：`https://webvpn.whut.edu.cn/`。本轮没有联网验证登录页面、账号规则、订阅范围或学校政策。

## 地址转换

原包附带网瑞达 AES-128-CFB 地址转换器，离线样本可用 `python scripts/webvpn_url.py --selftest` 检查。静态地址 token 不是登录凭证，也不能生成机构访问权限。

```powershell
python scripts/webvpn_url.py "https://www.sciencedirect.com/science/article/pii/文章编号"
python scripts/webvpn_url.py --base "https://webvpn.whut.edu.cn" "https://lib.whut.edu.cn/"
```

默认端口 HTTP/HTTPS 使用 `网关/协议/token/原路径?原查询#锚点`。query 保持原始编码，fragment 保留为浏览器锚点。已处于同一网关的 URL 原样返回，避免二次重写。

非默认端口和 IPv6 的学校路由规则尚未验证，脚本明确拒绝自动生成；应从学校资源页提供的入口打开。不同学校的代理实现可能不同，不能只换域名就断言算法适配。

## 续接会话

1. 在已获授权的浏览器中打开学校入口；已有会话时先直接访问目标资源。
2. 如页面实际要求认证，让用户在该浏览器完成，然后沿用原标签继续。
3. 检查文章全文权限。机构名称出现或重定向回资源页不代表该期刊/年份已订阅。
4. 下载后本地核验并登记。下载按钮一直加载时记录当前步骤，再决定换数据库官方入口、CARSI 或合法 OA。

不要让用户导出 Cookie 到聊天、环境变量或配置。登录 Cookie、会话 ticket、签名 CDN URL 不应写进日志或交接卡；浏览器自身管理 Cookie。`browser_dl.js` 已取消原包导出全部 Cookie 再用普通 HTTP 重放的分支。

CLI 独立 profile 默认在 `%USERPROFILE%\.dsh\campus-papers\browser-profile`，不与 Codex 内置页、日常 Edge/Chrome 或其他 profile 自动共享登录。持久化不保证免重登，Cookie 仍可能过期。不要删除 profile 解决普通权限问题；首次使用修订版时若 9333 端口被旧会话占用，关闭那个独立浏览器再运行，或换 `CAMPUS_BROWSER_PORT`。

命令行 `curl`/PowerShell 未携带浏览器会话时返回 `/login`，不能据此否定用户已经登录。只在实际使用的浏览器中判断。
