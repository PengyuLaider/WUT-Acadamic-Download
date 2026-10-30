# 图书馆数据库与访问入口参考（library-databases.md）

**何时打开本文档**：当用户要在中文库/外文库检索并下载论文、需要判断当前是「校内直连」还是「校外 WebVPN」、或需要把某个数据库链接交给用户人工点击下载时，打开本文档。OA 直链与 API 下载见 `open-access.md`。

> 脚本对应实现：`scripts/campus_net.ps1`（`status` / `open` / `vpn-url`）、`scripts/webvpn_url.py`；本文讲各库入口、认证判定与常见坑。

## 0. 三句话结论

1. **校内直连**：学校出口 IP 已自动认证，直接打开数据库官网即可，通常不出现登录墙，页面会显示机构名。
2. **校外**：优先 CARSI（学校 IdP `https://idp.whut.edu.cn/`、知网 `https://fsso.cnki.net/`；学校指南说明 CARSI 无需 VPN、链路更短、更快）；CARSI 未覆盖的库再登录 WebVPN（`https://webvpn.whut.edu.cn/login`），在门户里打开目标网站。
3. **能脚本直下的只有 OA 直链**；知网/万方/维普/WoS 的付费全文**必须用户浏览器人工点**（见 §6）。

## 1. 第一步：判定访问方式（必做，不要猜）

> 取证细节与双轨判定（用户自述 + 探测证据）见 `campus-net-whut.md` §2；本文只列**数据库侧的观察信号**。

```powershell
# 图书馆门户服务端写入的出口 IP（弱判据，仅提示；202.114.x.x ≠ 已认证）
curl.exe -s --max-time 15 http://lib.whut.edu.cn/ | Select-String -Pattern "rqIp"
# 数据库聚合页是否跳登录（超星发现）
curl.exe -s -o NUL -w "%{http_code} %{url_effective}`n" -L --ssl-no-revoke --max-time 15 https://ss.zhizhen.com/
```

**校内直连判定标准（强判据：能直接拿到正文/PDF 且页面显示机构名）**

| 信号 | 校内直连 | 校外/未认证 |
|---|---|---|
| CNKI | 右上角显示「机构账户为：武汉理工大学」，下载按钮直接给 CAJ/PDF | 出现登录框，或跳 `zhlgd.whut.edu.cn` 统一认证 |
| Web of Science | 右上角/页脚出现机构名（Wuhan University of Technology），可直接检索 | 要求 Sign In / 机构登录 |
| ScienceDirect | 文章页标题上方显示机构名，有可用 `Download PDF` | 只有 `Get Access`/`Purchase PDF` |
| IEEE Xplore | 文章页可直接点 `PDF` | 要求 Sign in 或提示 institutional access |
| 图书馆门户 | `lib.whut.edu.cn` 直接显示门户，数据库列表可点开 | 门户可开但点库后要求登录 |

> 校外时，**不要**尝试伪造 IP、Cookie 或破解机构认证。正确做法是 WebVPN / CARSI / 让用户连校园网。
> PDF 请求返回 401/403 或返回 HTML 登录页（而非 `%PDF`）→ 判未授权，见 `campus-net-whut.md` §2.2。

## 2. 学校入口（2026-10-30 实测，可直接用）

| 用途 | URL | 实测 |
|---|---|---|
| 图书馆门户 | `https://lib.whut.edu.cn/` | 200（超星智慧门户，页面 JS 渲染） |
| 门户备选入口 | `https://whutlib.mh.chaoxing.com/page/527321/show` | 超星托管等价页，门户异常时用 |
| 数据库列表（期刊） | `https://wisdom.chaoxing.com/newwisdom/doordatabase/database.html?pageId=527321&wfwfid=25279&websiteId=285586&sw=&uid=46691481&dataType=100001` | 200（由门户「电子期刊→更多」链接取得） |
| 数据库列表（图书/学位论文/多媒体） | 同上，`dataType=100002` / `100004` / `100292` | 同上 |
| 数据库校外访问说明 | `http://lib.whut.edu.cn/engine2/general/more?appId=2738427&websiteId=285586&wfwfid=25279&pageId=527321&typeId=8210903&currentBranch=0` | 200 |
| 文献传递 | `http://lib.whut.edu.cn/engine2/general/more?appId=2738427&websiteId=285586&wfwfid=25279&pageId=527321&typeId=8210857&currentBranch=0` | 200 |
| WebVPN 登录 | `https://webvpn.whut.edu.cn/login` | 200（标题「资源访问控制系统」，统一认证/账号登录） |
| CARSI 学校 IdP | `https://idp.whut.edu.cn/` | 200；CARSI 门户 `http://carsi.whut.edu.cn/` |
| 馆藏书目 OPAC | `http://202.114.89.11/opac/index` | 校园内网地址 |
| 科技查新/查收查引 | `http://chaxin.whut.edu.cn/` | 200 |

- 门户里的链接会自动追加 `pageId/wfwfid/websiteId` 参数，**能去参数就用裸官网 URL**（更稳、可复用）。
- `https://lib.whut.edu.cn/database.html` **不存在**（实测 404）。数据库列表实际在超星智慧图书馆 `wisdom.chaoxing.com` 的 `database.html` 页，参数照抄上表。

## 3. 中文库

| 库 | 官网入口 | 认证方式 | 全文格式 | 批量导出 | 脚本直下 |
|---|---|---|---|---|---|
| 中国知网 CNKI | `https://kns.cnki.net/kns8s/`（检索页）；`https://www.cnki.net/`（首页）；`https://login.cnki.net/`（登录） | 校内 IP 自动认证；校外用 WebVPN 或 CARSI `https://fsso.cnki.net/` | CAJ（需 CAJViewer）或 PDF；**部分硕博论文仅 CAJ** | 勾选文献 →「导出/参考文献」，支持 EndNote/NoteExpress/RefWorks/CNKI E-Study | ❌（见下） |
| 万方 | `https://www.wanfangdata.com.cn/`；学位论文 `https://c.wanfangdata.com.cn/thesis` | 校内 IP 自动认证；首页右上「机构登录」 | PDF | 文献 →「引用/导出」，支持 NoteExpress/EndNote/RefWorks/查新格式 | ❌ |
| 维普 | `https://www.cqvip.com/`；期刊检索 `https://qikan.cqvip.com/` | 校内 IP 自动认证 | PDF | 勾选 →「导出题录」（NoteExpress/EndNote 等） | ❌ |
| 超星发现 | `https://ss.zhizhen.com/` | 校内 IP 自动认证；实测校外跳 `login.zhizhen.com` | 仅**调度**，无自有全文 | 导出题录 | ❌（调度跳转） |
| 读秀 | `https://www.duxiu.com/` | 校内 IP；实测校外跳 `login.jsp` | 图书/章节，需**文献传递**（邮件收全文） | 题录 | ❌ |
| 百度学术 | `https://xueshu.baidu.com/` | 无需认证 | 仅调度（跳到出版商/知网等） | 引用导出 | ❌ |
| ChinaXiv | `https://www.chinaxiv.org/` | 无需认证，中文预印本 OA | PDF 可直下 | — | ✅（OA） |

**CNKI 常见坑（实测）**
- 直接访问 `https://kns.cnki.net/kns8s/` 会先跳到 `https://kns.cnki.net/verify/home?captchaType=blockPuzzle...`（滑块验证），**脚本无法通过**，必须浏览器人工。
- 校外只能走 WebVPN 或 `fsso.cnki.net`（CARSI）；仅靠学校账号密码登录知网个人账号**不会**获得机构全文权限。
- 下载的 CAJ 需要 CAJViewer；能选 PDF 时优先 PDF，便于后续脚本处理。
- 请用户下载时留意：同一篇可能同时提供「CAJ 下载 / PDF 下载」，只让用户点 PDF。

## 4. 外文库

| 库 | 官网入口 | 全文获取 | 导出/批量 | 脚本直下 |
|---|---|---|---|---|
| Web of Science | `https://www.webofscience.com/wos/woscc/basic-search` | 不提供全文，只做检索/引文，全文靠链接解析（Find it） | Marked List → Export：RIS/BibTeX/Plain text/Excel | ❌ |
| Scopus | `https://www.scopus.com/search/form.uri?display=basic` | 同上，全文跳 ScienceDirect/出版方 | 勾选 → Export：CSV/RIS/BibTeX | ❌ |
| IEEE Xplore | `https://ieeexplore.ieee.org/` | 订阅全文 + **部分 OA**（文章标 Open Access，有 PDF 直链） | Export → RIS/BibTeX/CSV | ⚠️ OA 可试，站点常 reset |
| ACM DL | `https://dl.acm.org/` | 订阅 + **OA 文章**（标 Open Access） | Export → RIS/BibTeX | ❌（实测 403） |
| Elsevier ScienceDirect | `https://www.sciencedirect.com/` | 订阅 + **OA 文章**（标 Open access） | Export → RIS/BibTeX/EndNote | ❌（需会话 cookie） |
| SpringerLink | `https://link.springer.com/` | 订阅 + **OA**（标 Open Access，PDF 可下） | Export → RIS/BibTeX/EndNote | ⚠️ OA 可试 |
| Wiley Online Library | `https://onlinelibrary.wiley.com/` | 订阅 + **OA**（标 Open Access） | Export → RIS/BibTeX | ❌（实测 403） |
| Taylor & Francis | `https://www.tandfonline.com/` | 订阅 + **OA**（标 Open Access） | Export → RIS/BibTeX | ❌（实测 403） |
| PubMed | `https://pubmed.ncbi.nlm.nih.gov/` | 免费全文看 `Free PMC article`/`Free article` 标识 | Send to → Citation manager（.nbib）/ CSV | ⚠️ 走 Europe PMC API 更稳 |
| Google Scholar | `https://scholar.google.com/` | 仅索引；右侧 `[PDF]` 才是可下全文 | 引用 → EndNote/RefWorks/BibTeX | ❌（本机不可达） |

**外文库常见坑**
- 本机实测：IEEE / PubMed / Semantic Scholar 网页出现 `Connection reset`，Google Scholar 连接超时。**打不开不等于论文没有**——先用 `open-access.md` 的 API（Crossref/OpenAlex/Europe PMC/arXiv API 均实测可用），或让用户开 WebVPN/换网络。
- ACM/Wiley/T&F/ScienceDirect 有 Cloudflare 或会话校验（403/400），脚本**不要**硬爬；OA 文章把 URL 给用户浏览器即可。
- WoS 会话有时效（长时间不操作会掉登录），导出前先确认仍显示机构名；大批量导出建议分页多次导。
- ScienceDirect 需要 cookie 会话，直接用 `curl` 打开会落 `user/error/ATN-20`（实测 400）。
- Google Scholar 无全文时：点「所有版本」找机构库/作者主页，或用 OpenAlex `locations[]`、Unpaywall（见 `open-access.md` §5），**不要抓取 Scholar**。

## 5. 校外 WebVPN 访问规则

1. 打开 `https://webvpn.whut.edu.cn/login`，用统一认证（校园卡号 + 智慧理工大密码）或账号登录。
2. 登录后在门户地址栏**粘贴原始 URL**（如 `https://www.sciencedirect.com/`），门户按
   `https://webvpn.whut.edu.cn/{http|https}/{token}/{path}?{query}` 规则重写并打开。
   - `token` 是 host 的 AES-128-CFB 加密串（固定前缀 `77726476706e69737468656265737421` + `hex(AES-128-CFB(host))`）。
   - **构造/校验代码与已验证示例见 `vpn-webvpn.md` §2–§3**；不要凭记忆手写 token，也不要对 query 二次编码。
   - 不想算 token：直接在门户资源列表里搜数据库名点进去。
3. 会话 Cookie 为 `wengine_vpn_ticketwebvpn_whut_edu_cn`（会话级、HttpOnly），**不要写进清单/分享**；超时重新登录即可，已下载文件不受影响。
4. WebVPN 不可用时的优先级：**先 CARSI**（`https://idp.whut.edu.cn/`、知网 `https://fsso.cnki.net/`），再让用户连校园网；学校指南建议文献访问优先 CARSI（更快、避免与门户跳转冲突），不要与 WebVPN 同时开。
5. WebVPN 重写后的 URL 只对当前会话有效，清单里记**原始官网 URL**。

## 6. 下载方式分级与人工引导

### 6.1 分级

| 级别 | 典型来源 | agent 可做什么 |
|---|---|---|
| A 全自动（浏览器） | **知网、万方**（检索→详情→PDF）、Springer/Elsevier 等订阅 PDF、arXiv/PLOS/PMC OA | `scripts/browser_dl.js` 自动找 PDF 并下载；首次遇到滑块时用户点一次，Cookie 持久化后全自动（实测：知网 2 篇、万方 1 篇、Springer、Cell 均成功） |
| A 脚本直下 | arXiv、PLOS、PMC OA XML、出版社 OA PDF、机构库 | `paper_oa.py fetch` 下载 + 校验（`open-access.md` §4） |
| B 半自动 | WoS/Scopus/知网题录导出；维普部分刊 | 用户点导出/登录后重试 `browser_dl.js --site vip`，agent 解析、改名、汇总 |
| C 必须人工 | 读秀文献传递、部分需机构登录的库、WoS 题录导出 | 打开浏览器给确切链接，等用户确认，再校验与归档 |

### 6.2 agent 引导人工下载的标准流程

1. **先确认身份**：问「现在在校园网内，还是校外？」→ 决定直连还是 WebVPN。也可以直接跑 `campus_net.ps1 -Action status` 取证。
2. **优先自动下载**：中文库/出版社先试 `node scripts/browser_dl.js --site cnki|wanfang|vip --search "关键词"` 或 `--url <文章页/DOI>`；返回 `downloaded` 就已完成（自动写 manifest）；返回 `need_user` 时让用户在弹出窗口点一次滑块/登录后重跑。
3. **再人工兜底**：`browser_dl.js` 不支持的站点，用默认浏览器直达文章页或检索页，不要只给网站首页：

```powershell
Start-Process 'https://doi.org/10.1038/nature12373'          # 出版社文章页（浏览器会按需跳转）
Start-Process 'https://kns.cnki.net/kns8s/'                   # 中文库检索页（需用户已完成机构认证）
```

3. **说清点哪里**：例如「页面顶部 `Download PDF` 按钮」「知网文章页右侧 `PDF 下载`（不要点 CAJ）」「WoS 先勾选再进 Marked List → Export → RIS」。
4. **等用户确认**：明确请用户回复「已下载」+ 文件名或路径；不要假设成功。
5. **校验与归档**：检查 `%PDF` 魔数、体积 >100 KB、sha256 去重，重命名为 `{年份}_{第一作者}_{短标题}_{DOI}.pdf`，存到约定目录（如 `papers/`）。
6. **更新清单**（Markdown/CSV，字段固定）：

```text
序号 | 标题 | 第一作者 | 年份 | 期刊/会议 | DOI | 来源(库/URL) | 文件名 | 状态(已下载/待人工/无权限)
```

## 7. 本机网络实测矩阵（2026-10-30，判断「打不开是否正常」用）

| 站点 | 实测结果 | 解读 |
|---|---|---|
| `lib.whut.edu.cn` | 200 | 门户可达 |
| `www.cnki.net` / `login.cnki.net` / `fsso.cnki.net` | 200 | 入口可达；`kns.cnki.net` 会跳滑块验证 |
| `www.wanfangdata.com.cn` | 200 | 首页含「机构登录」 |
| `www.cqvip.com` | 200；`qikan.cqvip.com` 412 | 期刊检索页反爬 |
| `ss.zhizhen.com` | 302→`login.zhizhen.com` | 校外需认证 |
| `www.duxiu.com` | 302→`login.jsp` | 需认证 |
| `xueshu.baidu.com` / `www.carsi.edu.cn` / `www.chinaxiv.org` | 200 | 可用 |
| `www.webofscience.com` | 200 | 页面可达，全文权限看机构 |
| `www.scopus.com` | 307→`id.elsevier.com` | 需登录/OAuth |
| `ieeexplore.ieee.org` / `pubmed.ncbi.nlm.nih.gov` / `www.semanticscholar.org` | Connection reset | 本机网络问题，换网络/WebVPN 再试 |
| `dl.acm.org` / `onlinelibrary.wiley.com` / `www.tandfonline.com` | 403 | Cloudflare 防护 |
| `www.sciencedirect.com` | 400→`user/error/ATN-20` | 缺会话 cookie，需浏览器 |
| `link.springer.com` | 200 | 可访问 |
| `scholar.google.com` | 超时 | 本机不可达，用替代源 |
| `api.crossref.org` / `api.openalex.org` / `www.ebi.ac.uk` / `export.arxiv.org` | 200 | **OA API 全部可用，优先走 API** |

## 8. 来源（均为本次实测访问的页面）

- 武汉理工大学图书馆门户：https://lib.whut.edu.cn/ （导航链接与实测 URL 见 §2）
- 超星智慧图书馆数据库列表：https://wisdom.chaoxing.com/newwisdom/doordatabase/database.html
- WebVPN 登录：https://webvpn.whut.edu.cn/login
- CARSI：https://www.carsi.edu.cn/ ；学校 IdP：https://idp.whut.edu.cn/
- 中国知网：https://www.cnki.net/ ；https://kns.cnki.net/kns8s/ ；https://fsso.cnki.net/
- 万方：https://www.wanfangdata.com.cn/ ；维普：https://www.cqvip.com/
- 超星发现：https://ss.zhizhen.com/ ；读秀：https://www.duxiu.com/ ；百度学术：https://xueshu.baidu.com/
- Web of Science：https://www.webofscience.com/wos/woscc/basic-search ；Scopus：https://www.scopus.com/
- IEEE：https://ieeexplore.ieee.org/ ；ACM：https://dl.acm.org/ ；ScienceDirect：https://www.sciencedirect.com/
- SpringerLink：https://link.springer.com/ ；Wiley：https://onlinelibrary.wiley.com/ ；T&F：https://www.tandfonline.com/
- PubMed：https://pubmed.ncbi.nlm.nih.gov/ ；Google Scholar：https://scholar.google.com/
