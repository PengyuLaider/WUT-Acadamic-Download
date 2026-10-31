# 本机登录配置与接力

更新日期：2026-10-31。用户明确要求在本机文件中填写自己的账号密码，并允许后续受其委托的智能体使用来登录学校图书馆/WebVPN、继续下载指定论文。该授权不涵盖其他网站、修改账号、付费、分享凭据或绕过验证码。

## 文件与入口

原包根目录 `%USERPROFILE%\Desktop\校园网论文skill` 下：

- `登录配置.private.json`：由用户本机填写 `username`、`password`。这是明文私密文件，不属于可分享的 skill 内容，不复制到工作副本/全局 skill/备份包，不显示在聊天或日志。
- `填写登录配置.ps1` + `登录配置界面.py`：本机 127.0.0.1 网页界面，用户在浏览器里填写账号密码并保存（密码不回显、不写日志、不上传；保存后尽量收紧文件 ACL）。只有需要登录 WebVPN/CARSI 时才需要；如果校园网 IP 直连已能下载全文就不必填。
- `登录图书馆.ps1 -CheckOnly`：子进程读取配置，返回 `ready`、`credentials_missing` 或 `config_error`，不回显账号密码，不联网。
- `登录图书馆.ps1`：独立 Edge/Chrome 登录入口。仅在宿主允许直接 CDP 自动化时运行；宿主限制此方式时，不以脚本规避限制。可以由用户本机运行，再让支持同一浏览器的智能体接续。
- 模块 `scripts/login_credentials.js` 由本地进程读取，使用浏览器原生输入事件填表，让学校页面自身处理密码加密。不会把明文密码作为通用 HTTP POST 发出，也不把值拼到页面脚本、命令行参数或输出中。

从安装后的其他 skill 目录使用，应显式传 `--credentials` 为用户指定的私密文件绝对路径；不要自动搜索磁盘或浏览器密码库。

```powershell
# 在包根目录执行
powershell -NoProfile -ExecutionPolicy Bypass -File .\登录图书馆.ps1 -CheckOnly
powershell -NoProfile -ExecutionPolicy Bypass -File .\登录图书馆.ps1 -WaitUser 240
# 直接 CLI（Node 22+）
node skill-src/scripts/browser_dl.js --login --credentials "%USERPROFILE%\Desktop\校园网论文skill\登录配置.private.json" --wait-user 240
```

## 行为边界

只向 `https://webvpn.whut.edu.cn` 和 `https://zhlgd.whut.edu.cn` 的可见登录框输入；表单提交到其他域名、HTTP、未知统一认证域名或嵌套跨域页面时不填，返回待处理，不擅自扩大白名单。

默认 `auto_submit:false`，填完后由用户完成验证码并点击登录。可由用户在本机改为 true：无可见验证码时尝试点击一次识别出的“登录”；出现验证码、扫码、二次验证仍交给用户。没有识别到按钮不猜测提交接口。每次运行只填/提交一次；错误后停止，先确认密码、账号或学校页面变化。

配置可读和字段填入都不等于认证成功。登录脚本即使离开登录墙也仅返回 `login_unverified`（退出码 3）；`need_user` 为退出码 4；配置未填/损坏为 2。下一步需在同一浏览器访问实际文章验证学校会话、机构订阅和 PDF；不要重跑独立 HTTP 探测来否定浏览器登录。

独立 profile 默认 `%USERPROFILE%\.dsh\campus-papers\browser-profile`，默认 CDP 端口 9333；同一机器同一 profile 可续用，仍可能过期。不把这个会话等同于 Codex 内置浏览器或用户普通 Edge。不要为了续接导出 Cookie。

## 已做与待做

已完成本地配置读取、脱敏状态、域名与表单来源限制、原生填表、一次提交、验证码暂停等离线模拟测试。尚未在学校当前页面上验证选择器、密码加密响应、认证跳转及全文访问。

接力时先运行 `-CheckOnly`。只有 ready 才进入登录测试；看实际可见页面确认字段和动作。不要 `Get-Content` 私密文件后输出全文。登录失败只记录固定状态及脱敏页面，不回显输入值，不截取含密码的配置/浏览器页面。

人工接手时记录目标 DOI、浏览器/profile、步骤和输出目录；下载沿用原 skill 的 `import-file` 与 PDF 核验流程。


## 2026-10-31 界面升级

完整工具包的配置界面入口为 `python campus_portal.py`，终端前台运行；`--demo` 不读写真实配置。旧的后台 UI 启动脚本已停用，不要求关闭防护或执行策略绕过。界面只保存账号，学校登录与 PDF 核验分别进行。
