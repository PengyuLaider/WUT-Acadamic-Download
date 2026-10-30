#!/usr/bin/env node
/*
 * browser_dl.js —— 用真实浏览器（Edge/Chrome）在校园网/机构授权下自动下载论文 PDF。
 *
 * 为什么需要它：知网/万方/维普、Web of Science、Elsevier/IEEE/Nature 等站点有
 * 会话校验、JS 渲染、Cloudflare/滑块风控，普通 HTTP 脚本拿不到订阅全文。
 * 本脚本通过 Chrome DevTools Protocol（CDP）驱动你本机已安装的浏览器：
 *   - 使用独立浏览器 profile（首次登录后 Cookie 持久化，下次免登录）；
 *   - 打开文章页 → 自动找 "PDF/下载" 链接 → 在页面内 fetch（带登录态）或点击下载 → 落盘；
 *   - 遇到验证码/滑块时暂停并打印 NEED_USER，你在弹出的浏览器窗口里点一下，
 *     脚本检测到页面变化后自动继续（不代填验证码）。
 *
 * 零第三方依赖：Node 22+ 自带 WebSocket / fetch；浏览器用本机已安装的 Edge 或 Chrome。
 *
 * 用法：
 *   node browser_dl.js --url "https://doi.org/10.1016/j.cell.2020.09.018" --out papers
 *   node browser_dl.js --url "https://kns.cnki.net/kcms2/..." --out papers --wait-user 240
 *   node browser_dl.js --url "https://www.sciencedirect.com/science/article/pii/S009286742031004X" \
 *        --out papers --json
 *
 * 输出（stdout，一行 JSON）：{status, saved, bytes, title, url, need_user, candidates, note}
 *   status: downloaded | need_user | no_pdf_found | error
 */
'use strict';

const { spawn } = require('child_process');
const fs = require('fs');
const path = require('path');
const http = require('http');
const https = require('https');

// ---------------------------------------------------------------------------
// 参数
// ---------------------------------------------------------------------------
function parseArgs(argv) {
  const args = { url: '', out: '', timeout: 90, waitUser: 180, json: false, headless: false, doi: '', title: '', site: '', search: '' };
  for (let i = 2; i < argv.length; i++) {
    const a = argv[i];
    if (a === '--url') args.url = argv[++i];
    else if (a === '--out') args.out = argv[++i];
    else if (a === '--timeout') args.timeout = parseInt(argv[++i], 10);
    else if (a === '--wait-user') args.waitUser = parseInt(argv[++i], 10);
    else if (a === '--doi') args.doi = argv[++i];
    else if (a === '--title') args.title = argv[++i];
    else if (a === '--site') args.site = (argv[++i] || '').toLowerCase();
    else if (a === '--search') args.search = argv[++i];
    else if (a === '--json') args.json = true;
    else if (a === '--headless') args.headless = true;
    else if (a === '--dump') args.dump = true;
    else if (!args.url) args.url = a;
  }
  const SITE_SEARCH = {
    cnki: (kw) => `https://kns.cnki.net/kns8s/defaultresult/index?kw=${encodeURIComponent(kw)}`,
    wanfang: (kw) => `https://s.wanfangdata.com.cn/paper?q=${encodeURIComponent(kw)}`,
    vip: (kw) => `https://qikan.cqvip.com/Qikan/Search/Index?key=${encodeURIComponent(kw)}`,
  };
  if (args.search && !args.url) {
    const build = SITE_SEARCH[args.site || 'cnki'];
    if (!build) {
      console.error('--site 只支持: ' + Object.keys(SITE_SEARCH).join(' / '));
      process.exit(2);
    }
    args.url = build(args.search);
  }
  if (!args.url && args.doi) args.url = 'https://doi.org/' + args.doi;
  if (!args.url) {
    console.error('用法: node browser_dl.js --url <文章URL或DOI> [--out 目录] [--wait-user 240] [--json]');
    console.error('      node browser_dl.js --site cnki --search "深度学习" [--out 目录]   # 知网检索后自动下载第一篇');
    process.exit(2);
  }
  if (!/^https?:\/\//i.test(args.url)) {
    args.url = 'https://doi.org/' + args.url.replace(/^doi:\s*/i, '');
  }
  return args;
}

const args = parseArgs(process.argv);
const outDir = path.resolve(args.out || path.join(process.cwd(), 'papers'));
fs.mkdirSync(outDir, { recursive: true });

const BROWSER_CANDIDATES = [
  process.env.CAMPUS_BROWSER_EXE,
  'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
  'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe',
  'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe',
  'C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe',
].filter(Boolean);
const browserExe = BROWSER_CANDIDATES.find((p) => fs.existsSync(p));
if (!browserExe) {
  fail('找不到 Edge/Chrome，请设置 CAMPUS_BROWSER_EXE 环境变量指向浏览器 exe');
}
const profileDir = process.env.CAMPUS_BROWSER_PROFILE
  || path.join(process.env.USERPROFILE || process.env.HOME || '.', '.dsh', 'campus-papers', 'browser-profile');
fs.mkdirSync(profileDir, { recursive: true });
const port = parseInt(process.env.CAMPUS_BROWSER_PORT || '9333', 10);

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function fail(message, extra = {}) {
  const payload = { status: 'error', message, ...extra };
  console.log(JSON.stringify(payload));
  process.exit(1);
}
function log(...parts) {
  console.error('[browser_dl]', ...parts);
}

// ---------------------------------------------------------------------------
// 浏览器 + CDP
// ---------------------------------------------------------------------------
function httpJson(urlName) {
  return new Promise((resolve, reject) => {
    http.get(urlName, (res) => {
      let data = '';
      res.on('data', (c) => (data += c));
      res.on('end', () => {
        try { resolve(JSON.parse(data)); } catch (e) { reject(e); }
      });
    }).on('error', reject);
  });
}

async function ensureBrowser() {
  // 已有我们的调试端口就直接连（保留登录态，避免重复开窗口）
  try { return await httpJson(`http://127.0.0.1:${port}/json/version`); } catch { /* not running */ }
  const flags = [
    `--remote-debugging-port=${port}`,
    `--user-data-dir=${profileDir}`,
    '--no-first-run',
    '--no-default-browser-check',
    '--disable-features=msEdgeFirstRunExperience',
    '--window-size=1280,900',
    '--lang=zh-CN',
  ];
  if (args.headless) flags.push('--headless=new');
  flags.push('about:blank');
  const child = spawn(browserExe, flags, { stdio: 'ignore', detached: false });
  child.unref();
  const deadline = Date.now() + 25000;
  while (Date.now() < deadline) {
    try { return await httpJson(`http://127.0.0.1:${port}/json/version`); }
    catch { await sleep(300); }
  }
  fail('浏览器启动超时（CDP 端口未就绪）');
}

class Cdp {
  constructor(ws) {
    this.ws = ws;
    this.nextId = 1;
    this.pending = new Map();
    this.listeners = [];
    ws.addEventListener('message', (ev) => {
      const msg = JSON.parse(ev.data);
      if (msg.id && this.pending.has(msg.id)) {
        const { resolve, reject } = this.pending.get(msg.id);
        this.pending.delete(msg.id);
        if (msg.error) reject(new Error(JSON.stringify(msg.error)));
        else resolve(msg.result);
      } else {
        this.listeners.slice().forEach((fn) => fn(msg));
      }
    });
  }
  send(method, params = {}, sessionId) {
    const id = this.nextId++;
    const msg = { id, method, params };
    if (sessionId) msg.sessionId = sessionId;
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      this.ws.send(JSON.stringify(msg));
    });
  }
  close() { try { this.ws.close(); } catch { /* noop */ } }
}

// 在页面里执行表达式
async function evaluate(cdp, sessionId, expression, timeoutMs = 120000) {
  const r = await cdp.send('Runtime.evaluate', {
    expression, returnByValue: true, awaitPromise: true, timeout: timeoutMs,
  }, sessionId);
  if (r.exceptionDetails) {
    throw new Error('页面脚本异常: ' + JSON.stringify(r.exceptionDetails).slice(0, 500));
  }
  return r.result ? r.result.value : undefined;
}

const DOM_PROBE = `(() => {
  const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim();
  const anchors = Array.from(document.querySelectorAll('a[href]')).map((a) => ({
    text: norm(a.textContent).slice(0, 80),
    href: a.href,
    cls: (a.className || '').toString(),
    dl: a.hasAttribute('download'),
  }));
  const pdfAnchors = anchors.filter((a) =>
    /\\.pdf(\\?|$)/i.test(a.href) || /pdfft|pdfdownload|download.*pdf/i.test(a.href));
  const downloadAnchors = anchors.filter((a) =>
    /download/i.test(a.href) || /download/i.test(a.cls) || a.dl ||
    /^(下载|下载全文|PDF下载)$/.test(a.text));
  const textLinks = anchors.filter((a) =>
    /(download\\s*pdf|pdf\\s*下载|下载\\s*pdf|下载全文|全文下载|获取全文|在线全文|^下载$)/i.test(a.text));
  const metas = Array.from(document.querySelectorAll('meta[name="citation_pdf_url"]')).map((m) => m.content);
  const bodyText = norm(document.body ? document.body.innerText : '').slice(0, 3000);
  return {
    title: document.title,
    url: location.href,
    readyState: document.readyState,
    pdfAnchors: pdfAnchors.slice(0, 20),
    downloadAnchors: downloadAnchors.slice(0, 10),
    textLinks: textLinks.slice(0, 20),
    metas: metas.slice(0, 5),
    bodyText,
    captchaEl: (() => {
      const nodes = Array.from(document.querySelectorAll(
        '[class*="captcha"],[id*="captcha"],[class*="puzzle"],[class*="slide-verify"],[class*="verify-slider"]'));
      return nodes.some((el) => {
        const r = el.getBoundingClientRect();
        const st = window.getComputedStyle(el);
        const txt = (el.textContent || '').replace(/\s+/g, '');
        return r.width > 40 && r.height > 20 && st.visibility !== 'hidden' && st.display !== 'none'
          && !/广告|advert/i.test(el.className || '');
      }) || Array.from(document.querySelectorAll('div,span,p')).some((el) => {
        const t = (el.textContent || '').replace(/\s+/g, '');
        return (t === '向右滑动完成验证' || t === '请完成安全验证' || t === '拖动滑块完成验证');
      });
    })(),
    needUser: /(安全验证|滑块|拖动滑块|验证码|请完成验证|verify|captcha|sign in|登录后|institution)/i.test(bodyText)
      || /(verify|captcha|login)/i.test(location.href),
  };
})()`;

// 在页面内 fetch PDF（带 Cookie/机构 IP 授权），返回 base64
async function fetchPdfInPage(cdp, sessionId, url, maxBytes = 60 * 1024 * 1024) {
  const expr = `(async () => {
    try {
      const r = await fetch(${JSON.stringify(url)}, { credentials: 'include', redirect: 'follow' });
      const ctype = r.headers.get('content-type') || '';
      if (!r.ok) return { status: r.status, ctype };
      const buf = await r.arrayBuffer();
      if (buf.byteLength > ${maxBytes}) return { status: r.status, ctype, tooBig: true, len: buf.byteLength };
      const u8 = new Uint8Array(buf);
      let bin = '';
      const chunk = 0x8000;
      for (let i = 0; i < u8.length; i += chunk) {
        bin += String.fromCharCode.apply(null, u8.subarray(i, i + chunk));
      }
      return { status: r.status, ctype, len: u8.length, b64: btoa(bin) };
    } catch (e) {
      return { error: String(e && e.message || e) };
    }
  })()`;
  return evaluate(cdp, sessionId, expr);
}

function writePdfFromBase64(b64, suggestedName) {
  const buf = Buffer.from(b64, 'base64');
  if (buf.length < 1024 || buf.slice(0, 5).toString('latin1') !== '%PDF-') return null;
  const safe = (suggestedName || 'paper').replace(/[\\\\/:*?"<>|]+/g, ' ').replace(/\s+/g, ' ').trim().slice(0, 90) || 'paper';
  let target = path.join(outDir, safe.endsWith('.pdf') ? safe : safe + '.pdf');
  let n = 2;
  while (fs.existsSync(target)) {
    target = path.join(outDir, path.basename(safe, path.extname(safe)) + '_' + n + '.pdf');
    n++;
  }
  fs.writeFileSync(target, buf);
  return { path: target, bytes: buf.length };
}

async function waitForNewFile(before, timeoutMs) {
  // 下载可能先出现 .crdownload 再改名，给足时间（至少 90 秒）
  const deadline = Date.now() + Math.max(timeoutMs, 90000);
  while (Date.now() < deadline) {
    await sleep(1000);
    const files = fs.readdirSync(outDir);
    const pdfs = files.filter((f) => f.toLowerCase().endsWith('.pdf'));
    for (const f of pdfs) {
      const full = path.join(outDir, f);
      if (before.includes(full)) continue;
      const st = fs.statSync(full);
      if (st.size > 1024) return { path: full, bytes: st.size };
    }
  }
  return null;
}

// 用浏览器 Cookie + Referer 走普通 HTTP 下载（绕过新标签页 Referer 丢失 / 防外链）
function cookieHeaderFor(cookies, hostname) {
  return (cookies || [])
    .filter((c) => {
      const d = (c.domain || '').replace(/^\./, '');
      return hostname === d || hostname.endsWith('.' + d);
    })
    .map((c) => `${c.name}=${c.value}`)
    .join('; ');
}

function httpDownload(url, headers, redirects = 6) {
  return new Promise((resolve) => {
    const attempt = (target, left) => {
      if (left < 0) { resolve({ error: 'too many redirects' }); return; }
      const mod = target.startsWith('https:') ? https : http;
      let req;
      try {
        req = mod.get(target, { headers }, (res) => {
          if ([301, 302, 303, 307, 308].includes(res.statusCode) && res.headers.location) {
            res.resume();
            attempt(new URL(res.headers.location, target).href, left - 1);
            return;
          }
          const chunks = [];
          res.on('data', (c) => chunks.push(c));
          res.on('end', () => resolve({ status: res.statusCode, headers: res.headers, body: Buffer.concat(chunks) }));
        });
      } catch (e) {
        resolve({ error: e.message });
        return;
      }
      req.on('error', (e) => resolve({ error: e.message }));
      req.setTimeout(60000, () => { req.destroy(); resolve({ error: 'timeout' }); });
    };
    attempt(url, redirects);
  });
}

// ---------------------------------------------------------------------------
// 主流程
// ---------------------------------------------------------------------------
(async () => {
  const version = await ensureBrowser();
  log('浏览器:', version.Browser);
  const ws = new WebSocket(version.webSocketDebuggerUrl);
  await new Promise((res, rej) => {
    ws.addEventListener('open', res);
    ws.addEventListener('error', () => rej(new Error('CDP WebSocket 连接失败')));
  });
  const cdp = new Cdp(ws);
  await cdp.send('Browser.setDownloadBehavior', {
    behavior: 'allow', downloadPath: outDir, eventsEnabled: true,
  });

  const { targetId } = await cdp.send('Target.createTarget', { url: 'about:blank' });
  const { sessionId } = await cdp.send('Target.attachToTarget', { targetId, flatten: true });
  await cdp.send('Page.enable', {}, sessionId);
  await cdp.send('Runtime.enable', {}, sessionId);
  await cdp.send('Network.enable', {}, sessionId);
  // 记录点击后网络层出现的 PDF/下载类请求（不少站点的下载按钮是 JS 触发的）
  const netUrls = [];
  cdp.listeners.push((msg) => {
    if (msg.sessionId === sessionId && msg.method === 'Network.requestWillBeSent') {
      const u = (msg.params && msg.params.request && msg.params.request.url) || '';
      if (/(\.pdf(\?|$)|stampPDF|getPDF|download|pdfft)/i.test(u) && !netUrls.includes(u)) netUrls.push(u);
    }
  });
  await cdp.send('Page.navigate', { url: args.url }, sessionId);

  // 等页面稳定（必须真的导航到 http(s)，不能停在 about:blank）
  const loadDeadline = Date.now() + args.timeout * 1000;
  let probe = null;
  while (Date.now() < loadDeadline) {
    await sleep(1500);
    probe = await evaluate(cdp, sessionId, DOM_PROBE);
    if (probe && /^https?:/i.test(probe.url || '') && probe.readyState === 'complete') break;
  }
  // 导航失败（chrome-error）时重试一次
  if (probe && !/^https?:/i.test(probe.url || '')) {
    log('导航失败，重试一次:', probe.url);
    await sleep(1500);
    await cdp.send('Page.navigate', { url: args.url }, sessionId);
    await sleep(4000);
    probe = await evaluate(cdp, sessionId, DOM_PROBE);
  }
  log('页面:', probe.title, '|', probe.url);

  // 检索模式：先在结果页取第一条文献，再进入详情页下载
  if (args.search) {
    const found = await evaluate(cdp, sessionId, `(() => {
      const links = Array.from(document.querySelectorAll('a[href]')).filter((a) =>
        /(kcms2\\/article\\/abstract|\\/periodical\\/|Qikan\\/Article\\/Detail)/i.test(a.href));
      const a = links.find((x) => (x.textContent || '').replace(/\\s+/g, '').length > 4);
      return a ? { href: a.href, title: (a.textContent || '').replace(/\\s+/g, ' ').trim() } : null;
    })()`);
    let detailUrl = found && found.href ? found.href : '';
    let detailTitle = found && found.title ? found.title : '';

    // 万方：结果项里带 periodical_<id> 文本，拼详情页 URL
    if (!detailUrl && /wanfang/i.test(probe.url)) {
      const item = await evaluate(cdp, sessionId, `(() => {
        const area = Array.from(document.querySelectorAll('[class*="title-area"]')).find((el) =>
          /(periodical|thesis|conference)_[A-Za-z0-9-]{5,}/.test(el.textContent || ''));
        if (!area) return null;
        const m = (area.textContent || '').match(/(periodical|thesis|conference)_([A-Za-z0-9-]{5,})/);
        if (!m) return null;
        const t = area.querySelector('[class*="title"]');
        return { kind: m[1], id: m[2], raw: m[0], title: t ? (t.textContent || '').trim() : '' };
      })()`);
      if (item) {
        detailUrl = `https://d.wanfangdata.com.cn/${item.kind}/${item.id}`;
        detailTitle = item.title || '';
        log('万方命中:', item.raw, '->', detailUrl);
      }
    }

    if (detailUrl) {
      args.url = detailUrl;
      args.title = args.title || detailTitle.replace(/\s*-\s*中国知网\s*$/, '');
      log('检索命中:', args.title, '|', detailUrl.slice(0, 120));
      await cdp.send('Page.navigate', { url: detailUrl }, sessionId);
      await sleep(4500);
      probe = await evaluate(cdp, sessionId, DOM_PROBE);
      log('详情页:', probe.title, '|', probe.url);
      args._found = true;
    } else {
      log('检索结果里没找到文献链接（可能需要登录/验证或页面结构变了）');
    }
  }
  if (args.search && !args._found) {
    const payload = { status: 'no_pdf_found', url: probe.url, title: probe.title,
                      note: '检索结果未解析出文献链接；可改用 --url 直接给文章页，或先用 --dump 排查页面结构' };
    console.log(JSON.stringify(payload));
    cdp.close();
    process.exit(3);
  }

  // --dump：只诊断页面（标题/正文/PDF 候选链接），不下载，便于排查站点结构
  if (args.dump) {
    const extra = await evaluate(cdp, sessionId, `(() => ({
      bodyText: (document.body ? document.body.innerText : '').slice(0, 2500),
      links: Array.from(document.querySelectorAll('a[href]')).slice(0, 80).map((a) => ({
        t: (a.textContent || '').replace(/\\s+/g, ' ').trim().slice(0, 60), h: a.href,
      })),
      inputs: Array.from(document.querySelectorAll('input')).slice(0, 20).map((i) => ({
        id: i.id, name: i.name, type: i.type, placeholder: i.placeholder,
      })),
      articles: Array.from(document.querySelectorAll('a[href]'))
        .filter((a) => /kcms|detail|abstract/i.test(a.href))
        .slice(0, 15)
        .map((a) => ({ t: (a.textContent || '').replace(/\\s+/g, ' ').trim().slice(0, 70), h: a.href })),
      tables: Array.from(document.querySelectorAll('table')).slice(0, 5).map((t) => ({
        cls: t.className, rows: t.querySelectorAll('tr').length,
      })),
      items: Array.from(document.querySelectorAll('[class*="wf-list-item"],[class*="list-item"],[class*="result-item"]'))
        .slice(0, 2)
        .map((el) => el.outerHTML.slice(0, 2500)),
      downloadSnippets: (() => {
        const els = Array.from(document.querySelectorAll('*'))
          .filter((el) => (el.textContent || '').replace(/\\s+/g, '') === '下载全文');
        return els.slice(0, 2).map((el) => {
          const chain = [];
          let cur = el;
          for (let i = 0; i < 5 && cur; i++) { chain.push(cur.outerHTML.slice(0, 800)); cur = cur.parentElement; }
          return chain;
        });
      })(),
      titleNodes: Array.from(document.querySelectorAll('[class*="title"],[class*="name"]'))
        .filter((el) => {
          const t = (el.textContent || '').replace(/\\s+/g, ' ').trim();
          return t.length >= 4 && t.length <= 80 && /深度|学习|算法|研究|基于/.test(t);
        })
        .slice(0, 10)
        .map((el) => ({ tag: el.tagName, cls: (el.className || '').toString().slice(0, 70),
          t: (el.textContent || '').replace(/\\s+/g, ' ').trim().slice(0, 60) })),
      idHints: (() => {
        const html = document.documentElement.innerHTML;
        const out = [];
        const re = /(?:periodical|thesis|conference|d\\.wanfangdata\\.com\\.cn)\\?[/:=]["']?([A-Za-z0-9_\\-]{6,})/g;
        let m;
        while ((m = re.exec(html)) && out.length < 12) out.push(m[0]);
        const attrs = Array.from(document.querySelectorAll('[data-id],[data-doi],[data-docid],[data-recordid]'))
          .slice(0, 8)
          .map((el) => ({ tag: el.tagName, cls: (el.className || '').toString().slice(0, 50),
            id: el.getAttribute('data-id') || el.getAttribute('data-doi') || el.getAttribute('data-docid') || el.getAttribute('data-recordid') }));
        return { urlMatches: out, attrs };
      })(),
      actions: Array.from(document.querySelectorAll('a,button,li,div,span'))
        .filter((el) => {
          const t = (el.textContent || '').replace(/\\s+/g, ' ').trim();
          return t.length > 0 && t.length < 30 && /(PDF|CAJ|下载|全文|HTML)/i.test(t);
        })
        .slice(0, 40)
        .map((el) => ({
          tag: el.tagName,
          cls: (el.className || '').toString().slice(0, 70),
          t: (el.textContent || '').replace(/\\s+/g, ' ').trim().slice(0, 30),
          href: el.href || el.getAttribute('data-href') || '',
          onclick: (el.getAttribute('onclick') || '').slice(0, 120),
        })),
    }))()`);
    const payload = { status: 'dump', url: probe.url, title: probe.title, ...extra };
    console.log(JSON.stringify(payload, null, 2));
    cdp.close();
    process.exit(0);
  }

  // 验证码/滑块/登录墙：先给页面 15 秒自愈（Elsevier 等有 "正在验证您的浏览器…" 中间页），
  // 仍不通过才抛 NEED_USER 并等待用户在可见浏览器窗口里操作。
  // 注意：只认真实的滑块/安全验证（URL 或可见滑块元素），普通登录框里的 "验证码" 字样不算。
  const hardCheck = (p) => {
    if (!p) return false;
    const urlHit = /(verify\/home|captchaType=|captchaId=)/i.test(p.url || '');
    const textHit = /(请完成安全验证|向右滑动完成验证|拖动滑块|滑块验证)/i.test(p.bodyText || '');
    // 只认明确的验证页/滑块文案；页面里普通的 "验证码" 登录框、广告位不算
    return Boolean(urlHit || textHit);
  };
  let needUser = false;
  if (probe && probe.needUser) {
    for (let i = 0; i < 10 && hardCheck(probe); i++) {
      await sleep(1500);
      probe = await evaluate(cdp, sessionId, DOM_PROBE);
    }
    if (hardCheck(probe)) {
      needUser = true;
      log('验证判定:', JSON.stringify({
        urlHit: /(verify\/home|captchaType=|captchaId=)/i.test(probe.url || ''),
        textHit: /(请完成安全验证|向右滑动完成验证|拖动滑块|滑块验证)/i.test(probe.bodyText || ''),
        captchaEl: probe.captchaEl,
        url: probe.url,
      }));
      try { await cdp.send('Page.bringToFront', {}, sessionId); } catch { /* noop */ }
      log('NEED_USER: 页面需要验证码/滑块/登录，请在弹出的浏览器窗口完成；完成后自动继续');
      const waitDeadline = Date.now() + args.waitUser * 1000;
      while (Date.now() < waitDeadline) {
        await sleep(2000);
        probe = await evaluate(cdp, sessionId, DOM_PROBE);
        if (!hardCheck(probe)) { needUser = false; break; }
      }
      if (needUser) {
        const payload = { status: 'need_user', url: probe.url, title: probe.title,
                          note: '验证码/登录未完成；请在浏览器窗口处理后重跑同一命令' };
        console.log(JSON.stringify(payload));
        cdp.close();
        process.exit(4);
      }
      log('用户已完成验证，继续');
      await sleep(2000);
      probe = await evaluate(cdp, sessionId, DOM_PROBE);
    }
  }

  // 收集候选 PDF 地址：citation_pdf_url > 直接 pdf 链接 > 文本含 PDF/下载 的链接
  const isSiteAsset = (u) => /\/www\/file\/|\/static\/|wfdatazs|logo|about[-_]?us|introduction/i.test(u || '');
  const candidates = [];
  const push = (u) => {
    if (!u || isSiteAsset(u)) return;
    const abs = new URL(u, probe.url).href;
    if (!candidates.includes(abs)) candidates.push(abs);
  };
  (probe.metas || []).forEach(push);
  (probe.pdfAnchors || []).forEach((a) => push(a.href));
  (probe.downloadAnchors || []).forEach((a) => push(a.href));
  (probe.textLinks || []).forEach((a) => { if (/https?:/.test(a.href)) push(a.href); });
  log('候选 PDF 链接:', candidates.length);

  let saved = null;
  const cleanTitle = (t) => (t || '')
    .replace(/\s*-\s*中国知网\s*$/, '')
    .replace(/\s*[-|:]\s*(中国知网|CNKI)\s*$/i, '')
    .trim();
  const nameHint = (cleanTitle(args.title || probe.title) || 'paper') + '.pdf';

  // 1) 页面内 fetch（保留登录态/机构 IP 授权，最稳）——只 fetch 同源地址，
  //    跨域下载域（如 bar.cnki.net）会被 CORS 拦住且可能一次作废，交给点击流程。
  const pageOrigin = (() => { try { return new URL(probe.url).origin; } catch { return ''; } })();
  for (const url of candidates.slice(0, 8)) {
    let sameOrigin = false;
    try { sameOrigin = new URL(url).origin === pageOrigin; } catch { /* ignore */ }
    if (!sameOrigin) { log('跳过跨域候选（改走点击）:', url.slice(0, 120)); continue; }
    const res = await fetchPdfInPage(cdp, sessionId, url);
    if (res && res.b64 && res.b64.length > 1000) {
      saved = writePdfFromBase64(res.b64, nameHint);
      if (saved) { log('页面内 fetch 成功:', saved.path); break; }
    } else {
      log('fetch 候选失败:', url, JSON.stringify(res).slice(0, 200));
    }
  }

  // 2) 点击下载（JS 按钮/POST 下载）：监听新文件，并记录按钮 href
  let clicked = null;
  if (!saved) {
    netUrls.length = 0;
    const before = fs.readdirSync(outDir).filter((f) => f.toLowerCase().endsWith('.pdf'))
      .map((f) => path.join(outDir, f));
    clicked = await evaluate(cdp, sessionId, `(() => {
      const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim();
      const all = Array.from(document.querySelectorAll('a,button,div,span,li'));
      const leaf = (el) => !Array.from(el.children).some((ch) => /下载|PDF|全文/i.test(norm(ch.textContent)));
      const inResult = (el) => Boolean(el.closest('[class*="item"],[class*="result"],[class*="list"],[class*="row"]'));
      const pickers = [
        (el) => /^(PDF\\s*下载|下载\\s*PDF|PDF下载|PDF)$/i.test(norm(el.textContent)),
        (el) => /^(下载全文|全文下载|CAJ\\s*下载|CAJ下载)$/i.test(norm(el.textContent)),
        (el) => /^下载$/.test(norm(el.textContent)) && inResult(el),
        (el) => /下载|download/i.test(norm(el.textContent)) && /btn|button|download|dl|wf-list/i.test(el.className || ''),
      ];
      let cand = null;
      for (const pick of pickers) {
        cand = all.find((el) => leaf(el) && pick(el));
        if (cand) break;
      }
      if (!cand) return null;
      const href = cand.href || (cand.closest('a') ? cand.closest('a').href : '') || '';
      cand.click();
      return {
        text: norm(cand.textContent).slice(0, 60),
        tag: cand.tagName,
        cls: (cand.className || '').toString().slice(0, 80),
        href,
      };
    })()`);
    log('点击候选:', JSON.stringify(clicked));
    if (clicked) {
      saved = await waitForNewFile(before, Math.min(45, args.timeout));
      if (saved) log('下载捕获成功:', saved.path);
    }

    // 2a) 点击触发的是 JS/接口请求：从网络事件里捞出 PDF 地址，再在页面内 fetch
    if (!saved) {
      await sleep(3000);
      log('网络层候选:', netUrls.slice(0, 6));
      for (const u of netUrls.slice(0, 6)) {
        if (!/^https?:/i.test(u) || /\.js(\?|$)/i.test(u)) continue;
        const res = await fetchPdfInPage(cdp, sessionId, u);
        if (res && res.b64 && res.b64.length > 1000) {
          saved = writePdfFromBase64(res.b64, nameHint);
          if (saved) { log('网络请求回放成功:', saved.path); break; }
        }
      }
    }
  }

  // 2b) 点击没落盘（新标签/内嵌阅读器）：跟着按钮 href 走，抽 iframe/embed 里的 PDF 地址
  if (!saved && clicked && clicked.href && /^https?:/i.test(clicked.href)) {
    await cdp.send('Page.navigate', { url: clicked.href }, sessionId);
    await sleep(3000);
    const inner = await evaluate(cdp, sessionId, `(() => {
      const out = [];
      document.querySelectorAll('iframe,embed,object').forEach((el) => {
        const u = el.src || el.data || '';
        if (/pdf|stamp|download/i.test(u)) out.push(u);
      });
      return { url: location.href, embeds: out.slice(0, 6), title: document.title };
    })()`);
    log('点击后页面:', JSON.stringify(inner).slice(0, 300));
    const urls = [];
    if (/\.pdf(\?|$)/i.test(inner.url)) urls.push(inner.url);
    (inner.embeds || []).forEach((u) => urls.push(u));
    for (const u of urls) {
      const res = await fetchPdfInPage(cdp, sessionId, u);
      if (res && res.b64) {
        saved = writePdfFromBase64(res.b64, nameHint);
        if (saved) { log('内嵌 PDF 抓取成功:', saved.path); break; }
      }
    }
  }

  // 2c) 用浏览器 Cookie + Referer 走 HTTP 直接下载（CNKI bar、出版社防外链下载器常需这一步）
  if (!saved && candidates.length) {
    const allCookies = await cdp.send('Network.getAllCookies').then((r) => r.cookies || []).catch(() => []);
    const ua = version.userAgent || 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)';
    for (const u of candidates.slice(0, 4)) {
      if (!/^https?:/i.test(u)) continue;
      let hostname = '';
      try { hostname = new URL(u).hostname; } catch { continue; }
      const res = await httpDownload(u, {
        'User-Agent': ua,
        'Referer': probe.url,
        'Accept': 'application/pdf,text/html;q=0.9,*/*;q=0.8',
        'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
        'Cookie': cookieHeaderFor(allCookies, hostname),
      });
      if (res.body && res.body.slice(0, 5).toString('latin1') === '%PDF-') {
        const buf = res.body;
        const safe = ((args.title || probe.title || 'paper') + '.pdf').replace(/[\\/:*?"<>|]+/g, ' ').slice(0, 90);
        let target = path.join(outDir, safe.endsWith('.pdf') ? safe : safe + '.pdf');
        let n = 2;
        while (fs.existsSync(target)) {
          target = path.join(outDir, path.basename(safe, path.extname(safe)) + '_' + n + '.pdf');
          n++;
        }
        fs.writeFileSync(target, buf);
        saved = { path: target, bytes: buf.length };
        log('Cookie+Referer HTTP 下载成功:', target);
        break;
      }
      log('HTTP 直连失败:', u.slice(0, 120), res.status || res.error,
        (res.headers && res.headers['content-type']) || '');
    }
  }

  // 3) 兜底：直接打开候选 PDF（下载行为已 allow，attachment 会直接落盘）
  if (!saved && candidates.length) {
    const before = fs.readdirSync(outDir).filter((f) => f.toLowerCase().endsWith('.pdf'))
      .map((f) => path.join(outDir, f));
    await cdp.send('Page.navigate', { url: candidates[0] }, sessionId);
    saved = await waitForNewFile(before, 30);
    if (saved) log('直接导航下载成功:', saved.path);
  }

  // 写入 manifest.jsonl（paper_oa.py organize 可直接归档）
  if (saved) {
    try {
      const crypto = require('crypto');
      const sha256 = crypto.createHash('sha256').update(fs.readFileSync(saved.path)).digest('hex');
      const record = {
        doi: args.doi || null,
        title: args.title || probe.title || '',
        authors: [],
        year: null,
        venue: '',
        identifier: args.doi || probe.url,
        status: 'downloaded',
        source: 'browser',
        license: null,
        version: null,
        url: probe.url,
        path: saved.path,
        bytes: saved.bytes,
        sha256,
        retrieved_at: new Date().toISOString(),
      };
      fs.appendFileSync(path.join(outDir, 'manifest.jsonl'), JSON.stringify(record) + '\n');
      saved.record = record;
      log('manifest 已追加');
    } catch (e) {
      log('写 manifest 失败:', e.message);
    }
  }

  const payload = {
    status: saved ? 'downloaded' : (needUser ? 'need_user' : 'no_pdf_found'),
    url: probe.url,
    title: args.title || probe.title,
    saved: saved ? saved.path : null,
    bytes: saved ? saved.bytes : null,
    doi: args.doi || null,
    candidates,
    need_user: needUser,
    note: saved ? 'PDF 已落盘并写入 manifest.jsonl，可用 paper_oa.py organize 生成索引'
      : '未拿到 PDF：可能未订阅该刊、页面需要登录/验证，或下载按钮在 iframe 内；可重跑 --wait-user',
  };
  console.log(JSON.stringify(payload));
  cdp.close();
  process.exit(saved ? 0 : (needUser ? 4 : 3));
})().catch((e) => fail('browser_dl 异常: ' + (e && e.message ? e.message : String(e))));
