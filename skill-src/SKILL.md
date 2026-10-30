---
name: campus-library-papers
description: 校园网/数字图书馆论文检索与下载。先询问用户是否在校园网环境，校内直连或按 CARSI/WebVPN 登录数字图书馆，检索、选择并下载论文；纯 OA 论文可校外直连下载。含网络检测、WebVPN 地址重写、OA 解析、PDF 下载与归档脚本。
whenToUse: 用户要登录校园网/图书馆、检索或下载论文、批量获取 PDF、判断某篇论文能否免费下载，或问"校外怎么下知网/SCI 论文"时使用。
---

# 校园网数字图书馆论文下载

目标：在**合法授权**范围内，把"登录校园网 → 进数字图书馆 → 检索 → 选中论文 → 下载 PDF → 归档"这一流程稳定跑完。纯 OA（开放获取）论文不需要校园网，可校外直连下载。

## 硬规则（违反即停）

1. **先问网络环境，再动手**。每次任务开始都必须执行 Step 0 的询问；检测脚本只是佐证，不能代替用户回答。
2. **凭据只由用户提供，绝不在对话里收集或回显密码**。用 `-Action save-cred`（Windows DPAPI 加密）或临时环境变量 `CAMPUS_USERNAME` / `CAMPUS_PASSWORD`。用户把密码贴进聊天时，提醒其撤回并改用上述方式。
3. **不代填验证码/短信码/扫码/人脸**。遇到统一认证的人工验证环节：打开浏览器，让用户自己完成，等用户确认后再继续。
4. **只下载 OA 论文或学校已授权资源**。不使用 Sci-Hub 等盗版镜像，不绕过付费墙，不批量抓取需登录的数据库页面，不共享账号。
5. **校外文献访问优先 CARSI**（学校指南：CARSI 无需 VPN、链路更短，可减少门户跳转引发的会话冲突）；CARSI 与 WebVPN 不要同时开，避免会话互相干扰。
6. **下载前让用户看到并选择**：先用标题/年份/作者/DOI/OA 状态列出候选；一次下载超过 5 篇或不确定时先确认。
7. **每次交付都要给出**：PDF 本地路径、来源（OA 渠道或出版社）、许可/版本（若已知）、失败原因与替代路径。

## Step 0：必须询问网络环境（不要跳过）

用 `ask_user_question` 一次问清（示例选项，可按用户上下文裁剪）：

```json
{
  "questions": [
    {
      "id": "network",
      "header": "网络环境",
      "question": "你现在在校园网环境吗？这决定走校内直连、CARSI/WebVPN，还是只下载 OA 论文。",
      "options": [
        {"label": "校内网络直连（图书馆已自动认证）"},
        {"label": "已连校园 WebVPN / VPN 客户端"},
        {"label": "校外网络，尚未连 VPN"},
        {"label": "不确定，请先帮我检测"}
      ]
    }
  ]
}
```

同时（或紧随其后）运行检测，拿到客观证据：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File "<SKILL_DIR>\scripts\campus_net.ps1" -Action status -Json -Config "<SKILL_DIR>\config\config.json"
```

> 首次使用：先按文末「首次使用配置」把 `config.example.json` 复制成 `config.json` 并填 `email`；不复制时脚本自动回退到 example，但 email 为空会跳过 Unpaywall，OA 命中率降低。

判读 `status` 输出：

| 字段 | 含义 | 对应动作 |
|---|---|---|
| `on_campus: true` | 探测页显示已在校内 | 走「校内直连」分支 |
| `vpn_connected: true` | WebVPN 网关已登录（或 Cookie 有效） | 走「WebVPN 分支」，所有库地址用 WebVPN 重写 |
| `on_campus: false` / `null` 且 `vpn_connected: false` | 校外未连 VPN | 先建议 CARSI（见路线 B1）；需要 WebVPN 再引导用户登录；否则走 OA 通道 |
| `captive_portal_detected: true` | 被强制门户拦截（未认证） | 先打开门户完成认证，再回到本流程 |
| `recommendation` | `direct` / `vpn` / `oa_or_vpn` / `unknown` | 作为建议，与用户回答交叉验证 |

**冲突处理**：用户说"在校内"但 `on_campus: false` 时，先确认是否开了代理/其他网络，再按检测结果走，并把原因告诉用户。用户明确说"就在校园网"时以用户为准，但记录一次复查。

> 出口 IP 只能当弱证据：宿舍 `WHUT-ISP` 走运营商出口仍可能有校园网权限。真正的强判据是"直接访问目标数据库不出现登录墙"（`references/campus-net-whut.md` §2.2）。

## Step 1：路线选择（A 校内直连 / B1 CARSI / B2 WebVPN / C 纯 OA）

### 路线 A：校内直连（已认证）

1. 打开数字图书馆入口（见 Step 2），确认数据库页不出现统一认证登录墙。
2. 检索：中文库用浏览器人工检索（知网/万方/维普），英文库优先用脚本 `search` 拿 DOI 与 OA 状态，再按座位/权限下载。
3. 下载：OA 走脚本 `fetch`；授权库用 `-Action open` 打开出版社页面由用户点击下载（浏览器已带校园网授权）。
4. 归档：脚本下载会自动写 `manifest.jsonl`；人工下载把文件移动到 `papers/` 后运行 `organize` 建索引。

### 路线 B1：校外 + CARSI（官方推荐，无需 VPN）

学校已接入 CARSI（`http://carsi.whut.edu.cn/`，IdP `https://idp.whut.edu.cn/idp/shibboleth`）。校外访问知网/万方/维普/WoS/Elsevier/IEEE/Springer 等优先走 CARSI：学校指南说明 CARSI 无需使用 VPN、连接速度更快且链路更短，可减少门户跳转引发的会话冲突。逐库步骤（以知网为例）见 `references/campus-net-whut.md` §3.4。

1. 打开数据库官网/检索页，找「机构登录 / 校外访问 / Institutional login」。
2. 选「武汉理工大学」，跳转统一身份认证；**用户本人**输入校园卡号 + 智慧理工大密码（+ 验证码），agent 不代填。
3. 成功后页面显示机构名（如知网右上角「机构账户为：武汉理工大学」）即为授权。
4. 后续下载与路线 A 相同（浏览器人工点击 + OA 脚本）。

### 路线 B2：校外 + WebVPN（网页版）

1. 先跑 `status`，确认 `vpn_connected`。
2. 未登录：打开 WebVPN 统一认证入口，请用户**手动**完成（验证码/密码由页面 JS 加密，agent 不代填）：

   ```powershell
   powershell -NoProfile -ExecutionPolicy Bypass -File "<SKILL_DIR>\scripts\campus_net.ps1" -Action open -Url "https://webvpn.whut.edu.cn/login?cas_login=true"
   ```

   用户确认"已登录"后重跑 `status` 验证 `vpn_connected: true`。
3. 把数据库地址重写成 WebVPN 地址再访问（浏览器端已登录即可用）：

   ```powershell
   powershell -NoProfile -ExecutionPolicy Bypass -File "<SKILL_DIR>\scripts\campus_net.ps1" -Action vpn-url -Url "https://kns.cnki.net/..." -Json
   ```

   或直接用 Python（等价，输出可复制的完整 URL）：

   ```text
   python "<SKILL_DIR>\scripts\webvpn_url.py" "https://kns.cnki.net/..."
   ```

4. 之后 Step 2–4 的链接一律先重写再打开。WebVPN 只保证"能访问"，下载仍需用户在浏览器里点击；脚本直链下载只用于 OA。Cookie 名称与有效性探测见 `references/vpn-webvpn.md` §5。
5. 提醒用户用完在 WebVPN 右上角「注销」，不要同时开 CARSI 与 WebVPN。

### 路线 C：校外无 VPN（只拿 OA）

无需校园网，直接跑 OA 通道（Step 3/4）。这不是降级，很多论文本来就有 OA 版本。

## Step 2：进入数字图书馆

用户学校入口（武汉理工大学，可在 `config.json` 换成自己的）：

- 图书馆首页：`https://lib.whut.edu.cn/`（备用：`https://whutlib.mh.chaoxing.com/page/527321/show`）
- 超星智慧图书馆 · 数据库列表（用户提供的入口，校外需 WebVPN 重写）：
  `https://wisdom.chaoxing.com/newwisdom/doordatabase/database.html?pageId=527321&wfwfid=25279&websiteId=285586&sw=&uid=46691481&dataType=100001`
- 统一身份认证（智慧理工大 tpass，账号=校园卡号）：`https://zhlgd.whut.edu.cn/tpass/login?service=<回跳地址>`
- CARSI 校外访问：门户 `http://carsi.whut.edu.cn/`，IdP `https://idp.whut.edu.cn/idp/shibboleth`（知网入口 `https://fsso.cnki.net/`）

操作要点：
- 从数据库列表点进入目标库（知网/万方/维普/WoS/Scopus/IEEE/ScienceDirect/Springer/Wiley/T&F 等），校内 IP 下通常直接可用，不出现"登录/机构登录"页。
- 校外优先在数据库官网做 CARSI 机构登录（路线 B1）；CARSI 不覆盖的库再用 `vpn-url` 重写后打开（路线 B2）。
- 判定"已授权"：页面出现学校机构名/数据库正常检索结果，且不跳统一认证；出现登录墙就先解决认证（路线 A/B）。
- 数据库列表页是前端异步渲染，**不要编造列表 API**；需要具体库就直接打开该库官网，或让用户在浏览器里从列表点击。

各库入口、下载/导出差异、常见坑见 `references/library-databases.md`。

## Step 3：检索与选择

优先用脚本获得结构化候选（DOI + OA 状态 + 引用量），比直接翻网页快：

```text
python "<SKILL_DIR>\scripts\paper_oa.py" --config "<SKILL_DIR>\config\config.json" search "关键词或论文标题" --limit 8
```

输出 `results[]` 字段：`title / year / authors / venue / doi / oa / oa_url / citations / source`。

- 把结果整理成**简短表格**给用户选（最多 8–10 条），标注 `OA` 与"可能需校内/WebVPN"。
- 用户已给 DOI/标题时跳过检索，直接 `oa` 或 `fetch`。
- 中文库（知网/万方/维普）用 `scripts/browser_dl.js` 直接在结果页检索并下载（见 Step 4 通道 1）；也可 `-Action open` 打开检索页人工挑选。

## Step 4：获取全文（三条通道）

> 命令里的 `<NODE>`：优先用 `node`；没有就调用 `load_workspace_dependencies` 拿 DSH 自带 node 绝对路径。

### 通道 1：浏览器自动下载（订阅全文 / 知网万方维普 / 出版社，推荐）

`browser_dl.js` 会打开真实 Edge/Chrome（独立 profile，首次登录后 Cookie 持久化），在校园网/机构授权下**自动找到 PDF 并下载**，遇到滑块/验证码才暂停等用户点一次。

```powershell
# 知网：按关键词检索并下载第一篇（最常用）
node "<SKILL_DIR>\scripts\browser_dl.js" --site cnki --search "深度学习 目标检测" --out "papers"

# 万方：按关键词检索并下载第一篇
node "<SKILL_DIR>\scripts\browser_dl.js" --site wanfang --search "深度学习" --out "papers"

# 维普：按关键词检索并下载第一篇（部分刊需机构登录，失败见下方兜底）
node "<SKILL_DIR>\scripts\browser_dl.js" --site vip --search "深度学习" --out "papers"

# 已知文章页/DOI：知网文章页、万方详情页、出版社 DOI 都可
node "<SKILL_DIR>\scripts\browser_dl.js" --url "https://kns.cnki.net/kcms2/article/abstract?v=..." --out "papers"
node "<SKILL_DIR>\scripts\browser_dl.js" --url "10.1016/j.cell.2012.10.006" --doi "10.1016/j.cell.2012.10.006" --out "papers"

# 想先看页面结构/排查：加 --dump
node "<SKILL_DIR>\scripts\browser_dl.js" --url "<URL>" --dump
```

返回 JSON：`status`（`downloaded` / `need_user` / `no_pdf_found` / `error`）、`saved`、`bytes`、`title`、`candidates`。

- **首次使用某站点**若返回 `need_user`：浏览器窗口已弹出并置前，请用户在窗口里完成滑块/统一认证（agent 不代填），完成后重跑同一命令；Cookie 会保存在 `%USERPROFILE%\.dsh\campus-papers\browser-profile`，之后**无需再登录**。
- 下载成功会自动写 `manifest.jsonl`，最后用 `organize` 生成索引。
- 已实测：知网（检索→下载 PDF）、万方（检索→下载 PDF）、Springer、Elsevier/Cell 订阅文章；IEEE/ACS 是否可下取决于学校订阅范围。
- 站点结构会变；失败先 `--dump` 看页面，再退回通道 3。

### 通道 2：OA 直链（脚本自动，校外可用）

```text
# 先看有哪些合法 OA 位置（不下载）
python "<SKILL_DIR>\scripts\paper_oa.py" --config "<SKILL_DIR>\config\config.json" oa "10.xxxx/xxxx"

# 下载最佳 OA PDF 到 papers/ 并写清单
python "<SKILL_DIR>\scripts\paper_oa.py" --config "<SKILL_DIR>\config\config.json" fetch "10.xxxx/xxxx" --out "papers"

# 批量：每行一个 DOI / arXiv ID / PMID
python "<SKILL_DIR>\scripts\paper_oa.py" --config "<SKILL_DIR>\config\config.json" batch dois.txt --out "papers"
```

- 成功：`status: downloaded`，返回 `path/bytes/sha256/license`；已下载过会返回 `exists`。
- `status: invalid_identifier`：输入不是 DOI/arXiv ID/PMID/PMCID/URL；按返回的 `examples` 纠正后重试。
- `status: no_oa`：不要硬来，按返回的 `suggestions` 走——校内/WebVPN 打开出版社页面人工下载、文献传递、邮件作者。
- `status: download_failed`：多为出版社防盗链或链接过期；换候选位置或浏览器人工下载。
- OA 渠道细节与降级顺序见 `references/open-access.md`。

### 通道 3：人工兜底（浏览器打开页面）

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File "<SKILL_DIR>\scripts\campus_net.ps1" -Action open -Url "https://doi.org/10.xxxx/xxxx"
# 校外：追加 -Vpn
```

- 用于 `browser_dl.js` 不支持的站点（WoS 题录导出、部分维普刊、需机构登录的库）：打开确切页面，请用户点导出/下载，等确认后移入 `papers/` 并 `organize`。
- WoS/Scopus 的导出（RIS/CSV/BibTeX）仍按人工流程走，skill 负责打开与后续归档。

## Step 5：归档与汇报

```text
python "<SKILL_DIR>\scripts\paper_oa.py" organize "papers"
```

输出 `index.csv`（doi/year/authors/title/venue/source/license/path/sha256），并检查 `missing_files`。

最终答复模板（简短）：

```markdown
已完成 N 篇下载：
1. <标题>（<年份>，<期刊>）→ `<绝对路径>`，来源：OA(Unpaywall/arXiv/…)，许可：CC BY
2. ...
未获取：<标题> —— 原因：无 OA；替代方案：校内/WebVPN 打开 <链接> 人工下载，或文献传递。
清单：`<download_dir>/manifest.jsonl`、`<download_dir>/index.csv`
```

## 首次使用配置

1. 复制配置模板并填写（`email` 必填真实邮箱，Unpaywall/Crossref 拒绝 example.com）：

   ```text
   copy "<SKILL_DIR>\config\config.example.json" "<SKILL_DIR>\config\config.json"
   ```

2. 换学校时改 `config.json` 的 `on_campus_probes`、`campus_cidrs`、`webvpn.base`、`carsi.*`、`auth.*`。
3. 需要自动登录时（部分学校无验证码）才设置 `auth.mode: "cas"`；有验证码/扫码的学校保持 `manual`，由用户浏览器登录。
4. 密码保存（可选，仅本机加密）：`campus_net.ps1 -Action save-cred`。

## 常见问题速查

| 现象 | 先检查 | 处理 |
|---|---|---|
| 数据库页跳统一认证 | `status.on_campus` / `vpn_connected` | 校内重连校园网；校外优先 CARSI（路线 B1），CARSI 不覆盖再走 WebVPN（B2） |
| 校外直连打不开出版社 | 是否需要 CARSI/WebVPN | 先在数据库官网做 CARSI 机构登录；仍不行再用 `-Action vpn-url` 重写 |
| CARSI 登录后仍无权限 | 库是否在 CARSI 覆盖清单 | 换 WebVPN；或确认登录成功（页面显示机构名） |
| WebVPN 重写后 404 | 原始 URL 是否带端口/查询串 | 用 `webvpn_url.py --json` 检查 token 与 path |
| OA 解析一直 `no_oa` | `config.json` 的 email 是否真实 | 填真实邮箱重试；换 arXiv/PMC/机构库 |
| 知网/万方下载返回 `no_pdf_found` | 是否订阅该刊、页面结构是否变化 | 先 `--dump` 看在哪个页面；换 `--url` 直接给文章页；维普部分刊需机构登录 |
| 浏览器脚本返回 `need_user` | 是否首次访问该站 | 在弹出的浏览器窗口完成滑块/登录后重跑同一命令（Cookie 会记住） |
| `browser_dl.js` 报找不到浏览器 | Edge/Chrome 是否安装 | 设置 `CAMPUS_BROWSER_EXE` 指向 msedge.exe/chrome.exe |
| 脚本中文乱码/解析报错 | `.ps1` 是否 UTF-8 with BOM | 用 VS Code「UTF-8 with BOM」另存（Windows PowerShell 5.1 要求） |
| 批量下载被限流 | 请求间隔 | 调大 `request_interval_seconds`，减少批量数 |

更多：`references/campus-net-whut.md`（校园网/图书馆/认证排障）、`references/vpn-webvpn.md`（WebVPN 原理与 Cookie）、`references/open-access.md`（OA API）、`references/library-databases.md`（各库入口）。

## 资源地图

- `scripts/campus_net.ps1`：网络状态检测、WebVPN 登录引导、URL 重写、浏览器打开、DPAPI 保存凭据。
- `scripts/browser_dl.js`：真实浏览器自动下载（知网/万方/维普/出版社），自带滑块暂停、Cookie 持久化、manifest 写入；零依赖（Node 22+）。
- `scripts/webvpn_url.py`：任意 URL → 学校 WebVPN URL（网瑞达 token 算法，纯标准库）。
- `scripts/paper_oa.py`：检索、OA 定位、PDF 下载、批量、归档（纯标准库）。
- `config/config.example.json`：学校/探测/认证/WebVPN 配置模板。
- `references/`：按需加载的细节文档（见上）。
