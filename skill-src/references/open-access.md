# OA 全文获取参考（open-access.md）

**何时打开本文档**：当需要判断一篇论文是否开放获取（OA）、把 DOI/标题解析成可下载的 PDF 直链、或执行「DOI → PDF」自动下载与校验时，打开本文档。校园网/WebVPN 与数据库入口另见 `library-databases.md`。

> 运行环境假设：Windows + `pwsh`，可用 `curl.exe`、`Invoke-WebRequest`、Python 3 stdlib。本文所有端点均在 2026-10-30 实测（响应码/字段以实测为准）。
> 脚本对应实现：`scripts/paper_oa.py`（`search` / `oa` / `fetch` / `batch` / `organize`），邮箱取自 `config/config.json` 的 `email`；本文只讲端点、字段与失败降级。

## 0. 合规红线（先读，不满足就停）

1. **只下载 OA 或学校已授权的全文**。付费墙后的正文一律不碰。
2. **禁止** Sci-Hub、Sci-Net、LibGen 等盗版镜像；禁止绕过付费墙、破解验证码、伪造机构 IP。
3. **ResearchGate 明确禁止抓取**（robots 全站 Disallow），只能把链接交给用户手动点。
4. 脚本遇到 `403`、`1015`、Cloudflare「Just a moment...」挑战页，一律判定「脚本无权限」→ 转人工浏览器，**不要换 UA 硬试**。
5. 批量请求间隔 ≥1 s（arXiv 要求 ≥3 s），带可识别 UA 和真实 `mailto`。机构 IP 下的异常高频下载会被图书馆/出版商封禁，责任在账号本人。

## 1. DOI → PDF 决策流程（按顺序执行，命中即停）

1. **归一化 DOI**：去掉 `https://doi.org/` 前缀，形如 `10.1038/nature12373`；标题检索出的 DOI 要先走 Crossref 校验。
2. **查 OA 状态（最快）**：`GET https://api.unpaywall.org/v2/{DOI}?email={真实邮箱}`，读 `is_oa`、`best_oa_location.url_for_pdf`。
3. `is_oa=true` 且 `url_for_pdf` 非空 → 下载该 URL。
4. `is_oa=false` 或 PDF 直链失效 → 查 OpenAlex `best_oa_location.pdf_url` + `locations[]`（找 `source.type=repository` 的机构库落地页）。
5. 生物医学 → Europe PMC `search?query=DOI:"{DOI}"&resultType=core`，看 `pmcid`、`isOpenAccess`、`fullTextUrlList`。
6. 物理/数学/CS → arXiv：`export.arxiv.org/api/query?id_list=...`；有 `<arxiv:doi>` 与目标 DOI 相同则直接取 PDF。
7. 中文刊 → 走 `library-databases.md` 的知网/万方/维普人工或机构下载，不要用 OA API 硬找。
8. 预印本（10.1101/*, 10.21203/*）→ bioRxiv/medRxiv `details` API 取 `version`，再拼 PDF URL；被 Cloudflare 拦就转浏览器。
9. 全都没有 → 用 OpenAlex `locations[].landing_page_url` 找作者主页/机构知识库，或走文献传递（见 §5）。
10. **下载后必须校验**（§4）：`%PDF` 魔数 + Content-Type + 体积 + sha256 去重，校验不过就当没下到。
11. 记录结果（CSV/清单）：DOI、标题、来源 URL、是否 OA、`oa_status`、文件路径、失败原因。

## 2. 端点速查表（含降级顺序）

| 顺序 | 用途 | 端点 | 认证 | 失败后 |
|---|---|---|---|---|
| 1 | OA 全文定位 | `https://api.unpaywall.org/v2/{doi}?email={email}` | 必须真实 email | →2 |
| 2 | 元数据+OA | `https://api.openalex.org/works/https://doi.org/{doi}` | 可无（额度极小） | →3 |
| 3 | DOI/标题解析 | `https://api.crossref.org/works/{doi}` | 可无，建议 `mailto` | →4 |
| 4 | 备用 OA 字段 | `https://api.semanticscholar.org/graph/v1/paper/DOI:{doi}?fields=openAccessPdf` | 可无（常 429） | →5 |
| 5 | 生物医学 | `https://www.ebi.ac.uk/europepmc/webservices/rest/search?query=DOI:"{doi}"&format=json&resultType=core` | 无 | →6 |
| 6 | 预印本 | `https://export.arxiv.org/api/query?id_list={id}` | 无 | →7 |
| 7 | 期刊目录 | `https://doaj.org/api/search/articles/doi:{doi}` | 无 | →8 |
| 8 | 机构库聚合 | `https://api.core.ac.uk/v3/search/works?q={doi}` | 免注册（严格限流） | →9 |
| 9 | 数据/预印本 DOI | `https://api.datacite.org/dois/{doi}` | 无 | →人工 |

## 3. 各 API 实测要点（字段名照抄即可）

### 3.1 Crossref（解析 DOI / 标题 → DOI）

```powershell
# DOI → 元数据
curl.exe -s "https://api.crossref.org/works/10.1038/nature12373?mailto=你的邮箱"
# 标题 → DOI（select 可裁剪返回体积，实测有效）
curl.exe -s "https://api.crossref.org/works?query.bibliographic=Nanometre-scale+thermometry+in+a+living+cell&rows=2&select=DOI,title,container-title,issued&mailto=你的邮箱"
```

- 关键字段：`message.DOI`、`message.title[0]`、`message.container-title[0]`、`message.issued.date-parts[0]`、`message.link[].URL`（`content-type` 为 `application/pdf` 时可能是出版方 PDF，但**多为付费墙**，必须再走 §4 校验）。
- 实测限流头：带 `mailto` 时 `x-rate-limit-limit: 10`、`x-rate-limit-interval: 1s`、`x-concurrency-limit: 3`、`x-api-pool: polite-single`（官方文档：public 5/s、polite 10/s、plus 150/s）。超限返回 **429**，退避后重试。

### 3.2 OpenAlex（OA 状态最全，含机构库位置）

```powershell
curl.exe -s "https://api.openalex.org/works/https://doi.org/10.1038/nature12373"
curl.exe -s "https://api.openalex.org/works?filter=doi:10.1038/nature12373&select=id,doi,title,open_access,best_oa_location"
curl.exe -s "https://api.openalex.org/works?search=nanometre-scale%20thermometry&per-page=1"
```

- 关键字段：`open_access.is_oa`、`open_access.oa_status`（`gold|green|hybrid|bronze|closed`）、`open_access.oa_url`、`best_oa_location.pdf_url`、`best_oa_location.version`、`locations[].source.type`（`repository` 即机构库）、`locations[].landing_page_url`、`ids.pmcid`、`has_content.pdf`、`content_urls.pdf`。
- **额度（2026-10 实测，重要）**：响应 `meta.notice` 明确「无 API key 时与你所在 IP 的其他用户共享 $0.10/天免费预算」，单次约 `$0.001`，即无 key 约百次级；无 key 请求头 `X-RateLimit-Limit: 1000`、`X-RateLimit-Reset`（秒）。建议申请免费 key：`POST https://api.openalex.org/api-keys`，body `{"email":"<用户邮箱>"}`（邮件点确认后生效，独立 $1/天），之后请求加 `api_key=...`。Agent 用法文档：`https://help.openalex.org/llms.txt`。
- **免费边界**：官方明确「单条记录查询免费」（`GET /works/{id|doi:...}`），搜索/列表/过滤按 credits 计费（列表调用 `meta.cost_usd` 会返回本次花费）。
- **不要用它下全文**：`content_urls.pdf`（`https://content.openalex.org/works/{WID}.pdf`）实测返回 `401 {"error":"API key required"}`，需 key；纯 OA 场景优先拿原始 OA 链接。

### 3.3 Unpaywall（OA 判定权威，email 必填）

```powershell
curl.exe -s "https://api.unpaywall.org/v2/10.1038/nature12373?email=你的真实邮箱"
```

- **实测两条 422**：不带 email → `{"message":"Email address required in API call"}`；用 `example.com` 占位邮箱 → `{"message":"Please use your own email address in API calls."}`。所以必须填真实可达邮箱（可让用户提供，或复用其机构邮箱）。
- 关键字段：`is_oa`、`oa_status`、`best_oa_location.url_for_pdf`、`best_oa_location.url_for_landing_page`、`best_oa_location.host_type`（`publisher|repository`）、`best_oa_location.version`、`oa_locations[]`（每个含 `host_type`/`url_for_pdf`/`repository_institution`）、`journal_is_in_doaj`、`has_repository_copy`、`oa_locations_embargoed[]`。
- `url_for_pdf` 可能指向 **bronze（出版社开放阅读但无开放许可）** 页面：实测 `https://www.nature.com/articles/nature12373.pdf` 返回的是 HTML 而非 PDF。**必须做 §4 校验，不能盲信该字段**。

### 3.4 Semantic Scholar（备用，公共池易 429）

```powershell
curl.exe -s "https://api.semanticscholar.org/graph/v1/paper/DOI:10.1038/nature12373?fields=title,isOpenAccess,openAccessPdf,externalIds,year,venue"
```

- 关键字段：`isOpenAccess`、`openAccessPdf.url`、`openAccessPdf.status`（如 `BRONZE`）、`openAccessPdf.license`、`externalIds.ArXiv/PubMedCentral/PubMed`。
- 实测：单篇查询 200；`/paper/search` 立即返回 `{"message":"Too Many Requests...","code":"429"}`。=> 该接口只做兜底，带 `x-api-key` 才稳定；遇 429 直接降级，不要重试超过 2 次。

### 3.5 Europe PMC / PMC（生物医学首选，无需 key）

```powershell
# 检索：拿 pmcid / 全文可用性 / 全文链接列表
curl.exe -s 'https://www.ebi.ac.uk/europepmc/webservices/rest/search?query=DOI:%2210.1038/nature12373%22&format=json&resultType=core'
# DOI → PMCID 转换（NCBI 官方；建议带 email+tool）
curl.exe -s 'https://pmc.ncbi.nlm.nih.gov/tools/idconv/api/v1/articles/?ids=10.1038/nature12373&format=json&email=你的邮箱&tool=campus-paper-skill'
```

- 检索关键字段：`result[].pmcid`、`result[].isOpenAccess`、`result[].inEPMC`、`result[].inPMC`、`result[].hasPDF`、`result[].fullTextUrlList.fullTextUrl[].{availabilityCode,documentStyle,site,url}`（`availabilityCode=F` 为免费，`documentStyle=pdf` 的那条即 PDF 入口）。
- `idconv` 关键字段：`records[].doi`、`records[].pmcid`、`records[].pmid`、`records[].requested-id`；缺 email/tool 会返回 `request.warnings`（实测出现两条 warning）。
- 全文 XML：`https://www.ebi.ac.uk/europepmc/webservices/rest/{PMCID}/fullTextXML`。实测在 OA 子集内返回 `200 application/xml`（如 `PMC5334499`，74 KB），不在 OA 子集返回 `500`（如 `PMC4221854`）。用 `curl.exe` 默认请求头即可；用 web_fetch 之类带 `Accept` 协商的工具取 XML 可能返回 406。附件：`.../{PMCID}/supplementaryFiles`（`200 application/zip`）。
- 站点 `europepmc.org/articles/{PMCID}?pdf=render` 有 Cloudflare 防护（实测返回「Just a moment...」HTML），**只适合人工浏览器点开**。

### 3.6 arXiv（物理/数学/CS 最稳，实测可直下）

```powershell
curl.exe -s "https://export.arxiv.org/api/query?id_list=1304.1068&max_results=1"
```

- 返回 Atom：`entry/id`（`http://arxiv.org/abs/1304.1068v1`）、`entry/title`、`entry/summary`、`entry/published`、`arxiv:doi`、`arxiv:journal_ref`、`entry/link[@rel='related'][@type='application/pdf']/@href`（形如 `https://arxiv.org/pdf/1304.1068v1`）。
- PDF 直链规则：`https://arxiv.org/pdf/{id}`（可带版本号，如 `/pdf/1304.1068v1`）；实测返回 `%PDF-1.5`，2.4 MB，可直接脚本下载。
- `id_list` 需要 arXiv 编号：先用 OpenAlex `ids`/`locations[]` 里 `source.display_name=arXiv` 的落地页拿到编号；也可用标题检索 `search_query=ti:"nanometre-scale thermometry"`（实测可用，注意 arXiv 标题常与期刊标题不一致，`totalResults` 可能为 0），**没有 `doi:` 检索字段**。
- **限流**：官方 TOU 规定「每 3 秒最多 1 次请求，且同一时间只用 1 个连接」，违反可能被封。

### 3.7 DOAJ / CORE / bioRxiv / medRxiv / DataCite

| 服务 | 端点与关键字段 | 实测/限制 |
|---|---|---|
| DOAJ | `https://doaj.org/api/search/articles/doi:{doi}`；结果 `results[].bibjson.{title,doi,year,link[].url}`（字段名按 DOAJ 文档，本次查询无命中，未逐字段验证） | 实测 200；查非 DOAJ 刊返回 `total:0` 属正常。官方限流 **2 req/s**（允许 5 个突发） |
| CORE | `https://api.core.ac.uk/v3/search/works?q={doi}&limit=10`；`results[].{id,doi,title,downloadUrl,fullText}`（字段名按 v3 文档，实测被 429 未取到样本） | 免注册可用，但官方限流 **1 批或 5 次单请求 / 10 s**；实测无 key 直接 **429**，需退避，key 到 `https://core.ac.uk/services/api` 注册 |
| bioRxiv/medRxiv | `https://api.biorxiv.org/details/{server}/{doi}`，server=`biorxiv` 或 `medrxiv`；`collection[].{doi,version,date,license,type,jatsxml,title,authors}` | 实测 medRxiv DOI 用 `biorxiv` 查到 `no posts found`，**server 必须对**；`jatsxml` 为全文 XML 地址 |
| 预印本 PDF | 规则 `https://www.{server}.org/content/{doi}v{version}.full.pdf` | 实测 bioRxiv/medRxiv 站点有 Cloudflare（返回 `error code: 1015`），脚本下载不可靠 → 转浏览器 |
| DataCite | `https://api.datacite.org/dois/{doi}`；`data.attributes.{doi,url,titles[0].title,creators[].name,types.resourceTypeGeneral,relatedIdentifiers[]}` | 实测 200；arXiv DOI（`10.48550/arXiv.*`）走这里 |

## 4. 下载与校验（可直接复制的 PowerShell）

```powershell
$ua   = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) campus-paper-skill/0.1'
$url  = 'https://arxiv.org/pdf/1304.1068'
$out  = Join-Path $env:TEMP 'oa-test.pdf'
Invoke-WebRequest -Uri $url -OutFile $out -UserAgent $ua -TimeoutSec 60 -UseBasicParsing
$fs = [System.IO.File]::OpenRead($out); $buf = New-Object byte[] 5; [void]$fs.Read($buf,0,5); $fs.Close()
$magic = [System.Text.Encoding]::ASCII.GetString($buf)      # 期望 "%PDF-"
$size  = (Get-Item $out).Length                              # 期望 >100KB
$hash  = (Get-FileHash $out -Algorithm SHA256).Hash          # 用于去重
```

校验规则（全部满足才算成功）：
- 文件头 5 字节 = `%PDF-`（实测 arXiv 为 `%PDF-1.5`）；HTTP 头 `Content-Type` 含 `application/pdf` 为强证据，但不是必须（CDN 常返回 `octet-stream`）。
- 体积 >100 KB；明显偏小（<20 KB）多半是错误页。
- 重定向后 `BaseResponse.RequestMessage.RequestUri` 落在登录页/出版社首页 → 失败。
- **坏味道**：正文以 `<!DOCTYPE html>` / `<title>Just a moment...` / `error code: 1015` 开头，或 `Content-Type: text/html` → 当场判失败并记录原因。
- 去重：`sha256` 全量哈希 + `DOI` 双键；同名不同 DOI 不算重复，同 DOI 不同版本（v1/v2）保留最新并在清单标注。
- 命名：`{年份}_{第一作者姓}_{标题前40字}_{DOI后缀}.pdf`，例 `2013_Kucsko_Nanometre-scale-thermometry_10.1038-nature12373.pdf`；文件名过滤 Windows 非法字符 `\ / : * ? " < > |`，标题截断到 60 字符内。

## 5. 没有 OA 时的合规替代路径

1. **机构库/作者主页**：OpenAlex `locations[]` 里 `source.type=repository` 的条目，取 `landing_page_url`；常见有 DASH、eScholarship、各校 IR。`version=acceptedVersion/submittedVersion` 通常可直接下载。
2. **预印本**：arXiv、bioRxiv、medRxiv、SSRN、ChinaXiv（`https://www.chinaxiv.org/`）。
3. **文献传递 / 馆际互借**：武汉理工大学图书馆「文献传递」页 `http://lib.whut.edu.cn/engine2/general/more?appId=2738427&websiteId=285586&wfwfid=25279&pageId=527321&typeId=8210857&currentBranch=0`；CALIS/CASHL 体系。适合单篇、非批量。
4. **邮件索取模板**（3 行，替换占位符）：

```text
Subject: Request for a copy of your paper "{TITLE}" ({YEAR}, {JOURNAL})
Dear Dr. {LASTNAME}, I am a student at Wuhan University of Technology working on {TOPIC}.
I could not access the full text of your paper (DOI: {DOI}) through my library.
Would you be willing to share a preprint or author-accepted manuscript for my personal reading? Thank you.
```

5. **让用户自己在校园网/WebVPN 内点**：付费墙内全文只能由用户在授权网络内人工下载，见 `library-databases.md` §6。ResearchGate 只能由用户手动点「Request full-text」。

## 6. 失败矩阵（照表降级）

| 现象 | 判定 | 下一步 |
|---|---|---|
| Unpaywall 422 `Please use your own email address` | 邮箱被拒 | 换成用户真实邮箱（学校邮箱最佳） |
| Unpaywall `is_oa:false` | 无 OA | 走 §5，勿找镜像 |
| `url_for_pdf` 返回 HTML/付费墙 | 假直链（bronze） | 检查 `oa_locations[]` 的 repository 分支 |
| OpenAlex 429 / `X-RateLimit-Remaining: 0` | 共享额度耗尽 | 换 Crossref/Europe PMC；或申请 key |
| S2 429 | 公共池限流 | 跳过 S2，≥10 s 后只重试 1 次 |
| Europe PMC fullTextXML `500` | 不在 OA 子集 | 用 `fullTextUrlList` 找免费入口或转人工 |
| arXiv 无匹配 | 该文未上 arXiv | 回 Crossref/出版方 |
| 403 / 1015 / Cloudflare | 反爬 | **停止脚本**，把 URL 交给用户浏览器下载 |
| 下载后校验失败 | 下到错误页 | 删除临时文件，记录失败，转人工 |

## 7. 来源（均为本次实测访问）

- Crossref REST API 访问与限流：https://www.crossref.org/documentation/retrieve-metadata/rest-api/access-and-authentication/
- OpenAlex Work 字段字典：https://help.openalex.org/data/works/attributes/ ；OA 状态判定：https://help.openalex.org/data/works/open-access/
- OpenAlex 认证与额度（免费 key、每日 $1）：https://help.openalex.org/api/authentication/ ；Agent 入口：https://help.openalex.org/llms.txt
- Unpaywall API：https://unpaywall.org/products/api （邮箱要求以实测 422 报文为准）
- Semantic Scholar Graph API（429 报文给出的 key 申请页）：https://www.semanticscholar.org/product/api#api-key-form
- Europe PMC REST：`https://www.ebi.ac.uk/europepmc/webservices/rest/search`（实测 200；官网文档站 `europepmc.org` 有 Cloudflare，脚本直接调 EBI 域名）
- PMC ID Converter：`https://pmc.ncbi.nlm.nih.gov/tools/idconv/api/v1/articles/?ids=...&format=json`（实测 200；NCBI 网页站点在本机会被 reset，接口域名可用）
- arXiv API 条款（3 秒/次）：https://info.arxiv.org/help/api/tou.html ；查询端点 `https://export.arxiv.org/api/query`
- DOAJ API v4（2 req/s）：https://doaj.org/api/v4/docs
- CORE API（免注册与限流 5 次/10 s）：https://core.ac.uk/services/api
- bioRxiv/medRxiv API：https://api.biorxiv.org/
- DataCite REST API：https://api.datacite.org/dois/{doi}
