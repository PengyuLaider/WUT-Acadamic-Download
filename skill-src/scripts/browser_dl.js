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
 *   status: saved_pending_verification | selection_required | need_user | no_pdf_found | error
 */
'use strict';

const { spawn } = require('child_process');
const fs = require('fs');
const path = require('path');
const http = require('http');
const https = require('https');
const { Cdp, sanitize, publicUrl, requiresUser, basicPdf } = require('./browser_support');
const {DEFAULT_FILE, approvedOrigin, readCredentials, credentialStatus, fillLogin} = require('./login_credentials');

// ---------------------------------------------------------------------------
// 参数
// ---------------------------------------------------------------------------
function parseArgs(argv) {
  const args = { url: '', out: '', timeout: 90, waitUser: 180, json: false, headless: false, doi: '', title: '', site: '', search: '', select: null, login: false, credentials: '', noProxy: process.env.CAMPUS_BROWSER_NO_PROXY === '1' };
  for (let i = 2; i < argv.length; i++) {
    const a = argv[i];
    if (['--url','--out','--timeout','--wait-user','--doi','--title','--site','--search','--select','--credentials'].includes(a)
        && (!argv[i + 1] || argv[i + 1].startsWith('--'))) throw new Error(a + ' 缺少参数值');
    if (a === '--url') args.url = argv[++i];
    else if (a === '--out') args.out = argv[++i];
    else if (a === '--timeout') args.timeout = Number(argv[++i]);
    else if (a === '--wait-user') args.waitUser = Number(argv[++i]);
    else if (a === '--doi') args.doi = argv[++i];
    else if (a === '--title') args.title = argv[++i];
    else if (a === '--site') args.site = (argv[++i] || '').toLowerCase();
    else if (a === '--search') args.search = argv[++i];
    else if (a === '--select') args.select = Number(argv[++i]);
    else if (a === '--credentials') args.credentials = argv[++i];
    else if (a === '--login') args.login = true;
    else if (a === '--help' || a === '-h') args.help = true;
    else if (a === '--json') args.json = true;
    else if (a === '--headless') args.headless = true;
    else if (a === '--dump') args.dump = true;
    else if (a === '--no-proxy') args.noProxy = true;
    else if (a === '--use-proxy') args.noProxy = false;
    else if (!a.startsWith('-') && !args.url) args.url = a;
    else throw new Error('未知参数: ' + a);
  }
  if (args.help) return args;
  if (args.login && (args.url || args.search || args.doi || args.dump)) throw new Error('--login 为独立登录动作，请勿与文章/搜索/诊断参数合用');
  if (args.login) args.url = 'https://webvpn.whut.edu.cn/login?cas_login=true';
  if (args.doi && !/^10\.\d{4,9}\/\S+$/.test(args.doi)) throw new Error('--doi 格式无效');
  for (const n of ['timeout', 'waitUser']) if (!Number.isFinite(args[n]) || args[n] < 1 || args[n] > 1800) throw new Error(n + ' 必须为 1..1800 秒');
  if (args.select !== null && (!Number.isInteger(args.select) || args.select < 1)) throw new Error('--select 必须为正整数');
  if (args.select !== null && !args.search) throw new Error('--select 仅可与 --search 一起使用');
  const SITE_SEARCH = {
    cnki: (kw) => `https://kns.cnki.net/kns8s/defaultresult/index?kw=${encodeURIComponent(kw)}`,
    wanfang: (kw) => `https://s.wanfangdata.com.cn/paper?q=${encodeURIComponent(kw)}`,
    vip: (kw) => `https://qikan.cqvip.com/Qikan/Search/Index?key=${encodeURIComponent(kw)}`,
  };
  if (args.search && !args.url) {
    const build = SITE_SEARCH[args.site || 'cnki'];
    if (!build) {
      throw new Error('--site 只支持: ' + Object.keys(SITE_SEARCH).join(' / '));
    }
    args.url = build(args.search);
  }
  if (!args.url && args.doi) args.url = 'https://doi.org/' + args.doi;
  if (!args.url) throw new Error('缺少 --url / --doi / --search；使用 --help 查看用法');
  if (!/^https?:\/\//i.test(args.url)) {
    const doi = args.url.replace(/^doi:\s*/i, '');
    if (!/^10\.\d{4,9}\/\S+$/.test(doi)) throw new Error('需要 HTTP(S) URL 或 DOI');
    args.url = 'https://doi.org/' + doi;
  }
  const parsed = new URL(args.url);
  if (parsed.username || parsed.password) throw new Error('URL 不得含账号密码');
  return args;
}

async function main() {
const args = parseArgs(process.argv);
if (args.help) {
  console.log('node browser_dl.js --url <文章URL或DOI> [--doi DOI] [--title 题名] [--out papers] [--no-proxy]\nnode browser_dl.js --site cnki|wanfang|vip --search 关键词 [--select 1]\n检索默认只列候选；--select 为用户确认后选定的序号。--dump 只返回精简诊断。\n--no-proxy：独立浏览器绕过系统代理直连校园网（Clash/VPN 开着时机构 IP 授权会失效，必须加）');
  console.log('node browser_dl.js --login [--credentials 本地配置路径] [--wait-user 240]');
  return;
}
let loginCredentials = null;
const credentialFile = args.credentials || (args.login ? DEFAULT_FILE : '');
if (credentialFile) {
  const check = credentialStatus(credentialFile);
  if (check.status !== 'ready') { console.log(JSON.stringify(check)); process.exitCode = 2; return; }
  loginCredentials = readCredentials(credentialFile);
  if (args.login) args.url = loginCredentials.login_url;
}
const overall = setTimeout(() => fail('本次操作总时限已到；可在原浏览器完成验证后重跑'), (args.timeout + args.waitUser + 120) * 1000);
overall.unref();
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
const port = Number(process.env.CAMPUS_BROWSER_PORT || '9333');
if (!Number.isInteger(port) || port < 1024 || port > 65535) throw new Error('CAMPUS_BROWSER_PORT 无效');
const downloadEvents = new Map();
const ownFrames = new Set();

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function fail(message, extra = {}) {
  const payload = { status: 'error', message, ...extra };
  console.log(JSON.stringify(sanitize(payload)));
  process.exit(1);
}
function log(...parts) {
  console.error('[browser_dl]', ...parts.map(sanitize));
}

// ---------------------------------------------------------------------------
// 浏览器 + CDP
// ---------------------------------------------------------------------------
function httpJson(urlName) {
  return new Promise((resolve, reject) => {
    const req = http.get(urlName, (res) => {
      let data = '';
      res.on('data', (c) => { data += c; if (data.length > 1024 * 1024) req.destroy(new Error('CDP response too large')); });
      res.on('error', reject);
      res.on('end', () => { try { if (res.statusCode !== 200) throw new Error('CDP HTTP status'); resolve(JSON.parse(data)); } catch (e) { reject(e); } });
    });
    const timer = setTimeout(() => req.destroy(new Error('CDP HTTP timeout')), 3000);
    req.on('close', () => clearTimeout(timer)); req.on('error', reject);
  });
}

async function ensureBrowser() {
  const marker = path.join(profileDir, 'campus-browser-session.json');
  let existing;
  try { existing = await httpJson(`http://127.0.0.1:${port}/json/version`); } catch { /* not running */ }
  if (existing) {
    let owned;
    try { owned = JSON.parse(fs.readFileSync(marker, 'utf8')); } catch {}
    if (!owned || owned.endpoint !== existing.webSocketDebuggerUrl || owned.profile !== path.resolve(profileDir)) {
      throw new Error('调试端口已被其他或旧版会话占用；请关闭该独立浏览器后重跑，或设置另一个 CAMPUS_BROWSER_PORT');
    }
    // 已有浏览器若代理模式与本次要求不一致，无法在运行中改变；提示关闭后重跑
    if (Boolean(owned.noProxy) !== Boolean(args.noProxy)) {
      throw new Error('现有独立浏览器的代理模式与本次不同（' + (owned.noProxy ? '直连' : '系统代理') + '）；请关闭该独立浏览器后重跑，或用 --no-proxy 保持一致');
    }
    return existing;
  }
  const flags = [
    '--remote-debugging-address=127.0.0.1',
    `--remote-debugging-port=${port}`,
    `--user-data-dir=${profileDir}`,
    '--no-first-run',
    '--no-default-browser-check',
    '--disable-features=msEdgeFirstRunExperience',
    '--window-size=1280,900',
    '--start-maximized',
    '--lang=zh-CN',
  ];
  // 校园网直连（绕过 Clash/VPN 系统代理）：机构 IP 授权只对校园出口有效
  if (args.noProxy) flags.push('--no-proxy-server');
  // 让 PDF 直接下载而不是内置预览（否则自动化拿不到文件）
  flags.push('--disable-pdf-extension');
  try {
    const defDir = path.join(profileDir, 'Default');
    fs.mkdirSync(defDir, { recursive: true });
    const prefFile = path.join(defDir, 'Preferences');
    let prefs = {};
    try { prefs = JSON.parse(fs.readFileSync(prefFile, 'utf8')); } catch { /* 首次启动无文件 */ }
    prefs.plugins = Object.assign({}, prefs.plugins, { always_open_pdf_externally: true });
    prefs.download = Object.assign({}, prefs.download, { prompt_for_download: false, default_directory: outDir });
    fs.writeFileSync(prefFile, JSON.stringify(prefs));
  } catch { /* 偏好写入失败不影响下载捕获 */ }
  if (args.headless) flags.push('--headless=new');
  flags.push('about:blank');
  const child = spawn(browserExe, flags, { stdio: 'ignore', detached: true });
  child.on('error', (e) => fail('浏览器启动失败: ' + e.message));
  child.unref();
  const deadline = Date.now() + 25000;
  while (Date.now() < deadline) {
    try {
      const version = await httpJson(`http://127.0.0.1:${port}/json/version`);
      fs.writeFileSync(marker, JSON.stringify({endpoint:version.webSocketDebuggerUrl, profile:path.resolve(profileDir), noProxy:Boolean(args.noProxy)}));
      return version;
    }
    catch { await sleep(300); }
  }
  fail('浏览器启动超时（CDP 端口未就绪）');
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
    passwordEl: Array.from(document.querySelectorAll('input[type=password]')).some(el => el.getBoundingClientRect().width > 0),
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
    needUser: /(安全验证|滑块|拖动滑块|验证码|请完成验证|verify you are human|captcha)/i.test(bodyText)
      || /(verify|captcha|login)/i.test(location.href),
  };
})()`;

// 在页面内 fetch PDF（带 Cookie/机构 IP 授权），返回 base64
async function fetchPdfInPage(cdp, sessionId, url, maxBytes = 60 * 1024 * 1024) {
  const expr = `(async () => {
    try {
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), 20000);
      let r;
      try { r = await fetch(${JSON.stringify(url)}, { credentials: 'include', redirect: 'follow', signal: controller.signal }); } catch (e) { clearTimeout(timer); throw e; }
      const ctype = r.headers.get('content-type') || '';
      if (!r.ok) { clearTimeout(timer); return { status: r.status, ctype }; }
      const reader = r.body.getReader(), chunks = []; let length = 0;
      try {
        while (true) {
          const {done, value} = await reader.read(); if (done) break;
          length += value.length;
          if (length > ${maxBytes}) { controller.abort(); return {tooBig:true}; }
          chunks.push(value);
        }
      } finally { clearTimeout(timer); }
      const u8 = new Uint8Array(length); let pos = 0;
      for (const chunk of chunks) { u8.set(chunk, pos); pos += chunk.length; }
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
  if (buf.length > 60 * 1024 * 1024 || buf.slice(0, 5).toString() !== '%PDF-' || !buf.subarray(-4096).includes(Buffer.from('%%EOF'))) return null;
  const safe = ('paper_' + (suggestedName || 'paper')).replace(/[\\\\/:*?"<>|]+/g, ' ').replace(/\s+/g, ' ').trim().slice(0, 90) || 'paper';
  let target = path.join(outDir, safe.endsWith('.pdf') ? safe : safe + '.pdf');
  let n = 2;
  while (fs.existsSync(target)) {
    target = path.join(outDir, path.basename(safe, path.extname(safe)) + '_' + n + '.pdf');
    n++;
  }
  fs.writeFileSync(target, buf, {flag:'wx'});
  return { path: target, bytes: buf.length };
}

async function waitForNewFile(before, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    await sleep(500);
    for (const item of downloadEvents.values()) {
      if (item.state !== 'completed' || !item.suggestedFilename) continue;
      const full = path.join(outDir, path.basename(item.suggestedFilename));
      if (before.includes(full) || !basicPdf(full)) continue;
      return { path: full, bytes: fs.statSync(full).size };
    }
  }
  return null;
}

// 用浏览器 Cookie + Referer 走普通 HTTP 下载（绕过 ScienceDirect pdfft / CNKI bar 等防外链逻辑）
function cookieHeaderFor(cookies, hostname) {
  return (cookies || [])
    .filter((c) => {
      const d = (c.domain || '').replace(/^\./, '');
      return hostname === d || hostname.endsWith('.' + d);
    })
    .map((c) => `${c.name}=${c.value}`)
    .join('; ');
}

function httpDownload(target, headers, redirects = 6) {
  return new Promise((resolve) => {
    const attempt = (url, left) => {
      if (left < 0) { resolve({ error: 'too many redirects' }); return; }
      const mod = url.startsWith('https:') ? https : http;
      let req;
      try {
        req = mod.get(url, { headers }, (res) => {
          if ([301, 302, 303, 307, 308].includes(res.statusCode) && res.headers.location) {
            res.resume();
            const next = new URL(res.headers.location, url);
            if (url.startsWith('https:') && next.protocol !== 'https:') { resolve({error:'HTTPS downgrade refused'}); return; }
            headers = Object.fromEntries(Object.entries(headers).filter(([key]) => !/^(cookie|authorization|proxy-authorization)$/i.test(key)));
            attempt(next.href, left - 1);
            return;
          }
          const chunks = [];
          let bytes = 0;
          res.on('data', (c) => { bytes += c.length; if (bytes > 60 * 1024 * 1024) { req.destroy(); resolve({error:'PDF exceeds size limit'}); } else chunks.push(c); });
          res.on('error', () => resolve({error:'response interrupted'}));
          res.on('end', () => resolve({ status: res.statusCode, headers: res.headers, body: Buffer.concat(chunks) }));
        });
      } catch (e) { resolve({ error: e.message }); return; }
      req.on('error', (e) => resolve({ error: e.message }));
      req.setTimeout(60000, () => { req.destroy(); resolve({ error: 'timeout' }); });
    };
    attempt(target, redirects);
  });
}

// ---------------------------------------------------------------------------
// 主流程
// ---------------------------------------------------------------------------
  const version = await ensureBrowser();
  log('浏览器:', version.Browser);
  const ws = new WebSocket(version.webSocketDebuggerUrl);
  await new Promise((res, rej) => {
    const timer = setTimeout(() => { ws.close(); rej(new Error('CDP WebSocket 超时')); }, 10000);
    ws.addEventListener('open', () => { clearTimeout(timer); res(); });
    ws.addEventListener('error', () => { clearTimeout(timer); rej(new Error('CDP WebSocket 连接失败')); });
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
  const frameTree = await cdp.send('Page.getFrameTree', {}, sessionId);
  const addFrames = (tree) => { if (!tree) return; ownFrames.add(tree.frame.id); (tree.childFrames || []).forEach(addFrames); };
  addFrames(frameTree.frameTree);
  cdp.listeners.push((msg) => {
    if (msg.sessionId === sessionId && msg.method === 'Page.frameNavigated') ownFrames.add(msg.params.frame.id);
    if (msg.method === 'Browser.downloadWillBegin' && ownFrames.has(msg.params.frameId)) downloadEvents.set(msg.params.guid, {...msg.params, state:'inProgress'});
    if (msg.method === 'Browser.downloadProgress' && downloadEvents.has(msg.params.guid)) Object.assign(downloadEvents.get(msg.params.guid), msg.params);
  });
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
  if (!probe || !/^https?:/i.test(probe.url || '')) throw new Error('文章页导航失败');
  log('页面:', probe.title, '|', probe.url);

  let credentialFill = null;
  let credentialFillAttempted = false;
  async function attemptCredentialFill() {
    if (credentialFillAttempted || !loginCredentials || !probe || !probe.passwordEl || !approvedOrigin(probe.url)) return;
    // At most one fill/submit attempt, including after a user clicks the CAS entry.
    credentialFillAttempted = true;
    credentialFill = await fillLogin(cdp, sessionId, loginCredentials);
    log('本机凭据处理:', credentialFill.status);
    if (credentialFill.status === 'credentials_filled') log('账号密码已填入学校页面；请完成验证码/扫码并点击登录。');
  }
  await attemptCredentialFill();

  // 验证码/滑块/登录墙：先给页面 15 秒自愈（Elsevier 等有 "正在验证您的浏览器…" 中间页），
  // 仍不通过才抛 NEED_USER 并等待用户在可见浏览器窗口里操作。
  // 注意：只认真实的滑块/安全验证（URL 或可见滑块元素），普通登录框里的 "验证码" 字样不算。
  const hardCheck = requiresUser;
  // Cloudflare Turnstile / "请稍候" 挑战页：必须等页面真正进入文章页，不能因 captchaEl 抖动提前放行
  const isChallenge = (p) => Boolean(p && (
    /are you a robot|confirm you are a human|captcha challenge|请验证您是真人|verify (?:that )?you are human|checking your browser|just a moment|请稍候/i
      .test(`${p.title || ''} ${p.bodyText || ''}`)
    || /cdn-cgi\/challenge|challenge-platform/i.test(p.url || '')));
  const needHuman = (p) => Boolean(p && (isChallenge(p) || hardCheck(p)));
  const hasPdfEntry = (p) => Boolean(p && (
    (p.pdfAnchors || []).length ||
    (p.textLinks || []).some((a) => /view\s*pdf|download\s*pdf|pdf\s*下载|下载\s*pdf/i.test(a.text || '')) ||
    /view\s*pdf/i.test(p.bodyText || '')));

  let needUser = false;
  async function waitHumanHere(where) {
    if (!needHuman(await evaluate(cdp, sessionId, DOM_PROBE))) return true;
    log('NEED_USER(' + where + '): 需要人机验证/登录，窗口已置前；完成后自动继续（最长 ' + args.waitUser + ' 秒）');
    try { await cdp.send('Page.bringToFront', {}, sessionId); } catch { /* noop */ }
    log('若窗口仍被遮挡，请在任务栏打开独立浏览器。');
    const deadline = Date.now() + args.waitUser * 1000;
    while (Date.now() < deadline) {
      await sleep(2000);
      const p = await evaluate(cdp, sessionId, DOM_PROBE);
      probe = p;
      await attemptCredentialFill();
      if (!needHuman(p)) return true;
    }
    return false;
  }

  // 统一等待：ScienceDirect 会先显示“请稍候”再渲染文章页；期间出现人机验证就让用户点一次
  const contentStart = Date.now();
  const contentDeadline = contentStart + args.timeout * 1000;
  while (Date.now() < contentDeadline) {
    if (!needHuman(probe) && hasPdfEntry(probe)) break;
    if (needHuman(probe)) {
      needUser = true;
      const ok = await waitHumanHere('文章页');
      needUser = false;
      if (!ok) {
        console.log(JSON.stringify(sanitize({ status: 'need_user', url: probe.url, title: probe.title,
          note: '验证未完成；请在浏览器窗口处理后重跑同一命令' })));
        cdp.close(); process.exit(4);
      }
      probe = await evaluate(cdp, sessionId, DOM_PROBE);
      continue;
    }
    const settled = /^https?:/i.test(probe.url || '') && probe.readyState === 'complete'
      && !/请稍候|are you a robot/i.test(`${probe.title || ''} ${probe.bodyText || ''}`);
    if (settled && Date.now() - contentStart > 10000) break;
    await sleep(2000);
    probe = await evaluate(cdp, sessionId, DOM_PROBE);
    await attemptCredentialFill();
  }
  if (needHuman(probe)) {
    console.log(JSON.stringify(sanitize({ status: 'need_user', url: probe.url, title: probe.title,
      note: '页面仍需人机验证/登录；请在浏览器窗口处理后重跑同一命令' })));
    cdp.close(); process.exit(4);
  }

  if (args.login) {
    console.log(JSON.stringify(sanitize({status:'login_unverified', url:probe.url,
      credential_fill_status:credentialFill ? credentialFill.status : 'not_needed_or_unrecognized_origin',
      credential_values_returned:false,
      note:'登录页面阶段已结束或未识别为登录墙；需在同一浏览器打开目标文章核验全文权限，不能据此宣称学校登录和订阅已全部验证'})));
    cdp.close(); process.exit(3);
  }

  if (args.dump) {
    console.log(JSON.stringify(sanitize({status:'dump', title:probe.title, url:probe.url,
      captcha:requiresUser(probe), links:[...(probe.pdfAnchors || []), ...(probe.textLinks || [])].map(a => ({title:a.text, url:a.href}))}), null, 2));
    cdp.close(); process.exit(0);
  }
  if (args.search) {
    const results = await evaluate(cdp, sessionId, `(() => {
      const seen = new Set();
      return Array.from(document.querySelectorAll('a[href]'))
        .filter(a => /(kcms2\\/article\\/abstract|\\/periodical\\/|\\/thesis\\/|\\/conference\\/|Qikan\\/Article\\/Detail)/i.test(a.href))
        .map(a => ({url:a.href, title:(a.textContent || '').replace(/\\s+/g,' ').trim()}))
        .filter(a => a.title.length > 4 && !seen.has(a.url) && seen.add(a.url)).slice(0,30);
    })()`);
    if (!args.select) {
      console.log(JSON.stringify(sanitize({status:results.length ? 'selection_required' : 'no_results', results,
        note:'请核对题名，用户选定后用该文章 URL 下载；列表序号可能随页面排序变化。'})));
      cdp.close(); process.exit(results.length ? 0 : 3);
    }
    const selected = results[args.select - 1];
    if (!selected) throw new Error('--select 超出候选范围；请改用已确认的文章 URL');
    if (args.title && args.title !== selected.title) throw new Error('搜索排序已变化，选中题名与 --title 不一致；请使用文章 URL');
    args.title = args.title || selected.title;
    await cdp.send('Page.navigate', {url:selected.url}, sessionId);
    await sleep(4000); probe = await evaluate(cdp, sessionId, DOM_PROBE);
    if (requiresUser(probe)) { console.log(JSON.stringify(sanitize({status:'need_user', url:probe.url, note:'请在原浏览器完成认证，再以文章 URL 重跑'}))); cdp.close(); process.exit(4); }
  }

  // 收集候选 PDF 地址：citation_pdf_url > 直接 pdf 链接 > 文本含 PDF/下载 的链接
  const isSiteAsset = (u) => /\/www\/file\/|\/static\/|wfdatazs|logo|about[-_]?us|introduction/i.test(u || '');
  const candidates = [];
  const push = (u) => {
    if (!u || isSiteAsset(u)) return;
    if (!/^https?:/i.test(new URL(u, probe.url).href)) return;
    const abs = new URL(u, probe.url).href;
    if (!candidates.includes(abs)) candidates.push(abs);
  };
  (probe.metas || []).forEach(push);
  (probe.pdfAnchors || []).forEach((a) => push(a.href));
  (probe.downloadAnchors || []).forEach((a) => push(a.href));
  (probe.textLinks || []).forEach((a) => { if (/https?:/.test(a.href)) push(a.href); });
  // 过滤干扰项：图片/静态资源，以及不属于本文 PII 的 pdfft（参考文献错链）
  const pagePii = (probe.url.match(/\/pii\/([A-Za-z0-9]+)/) || [])[1] || '';
  const filtered = candidates.filter((u) => {
    if (/\.(?:jpe?g|png|gif|svg|css|js)(?:\?|$)/i.test(u)) return false;
    if (/\/pdfft(?:\?|$)/i.test(u) && pagePii && !u.toUpperCase().includes(pagePii.toUpperCase())) return false;
    return true;
  });
  filtered.sort((a, b) =>
    Number(/(\/pdfft|\/pdf\/|\.pdf(?:\?|$))/i.test(b)) - Number(/(\/pdfft|\/pdf\/|\.pdf(?:\?|$))/i.test(a)));
  candidates.length = 0; candidates.push(...filtered);
  log('候选 PDF 链接:', candidates.length, candidates.slice(0, 2));

  let saved = null;
  const cleanTitle = (t) => (t || '')
    .replace(/\s*-\s*中国知网\s*$/, '')
    .replace(/\s*[-|:]\s*(中国知网|CNKI)\s*$/i, '')
    .trim();
  const nameHint = (cleanTitle(args.title || probe.title) || 'paper') + '.pdf';

  // 0) 直接打开文章 PDF 链接：浏览器已设为“PDF 外部打开”，通常会直接落盘
  if (!saved && candidates.length) {
    const direct = candidates.find((u) => /\/pdfft(?:\?|$)/i.test(u)) || candidates.find((u) => /\.pdf(?:\?|$)/i.test(u));
    if (direct) {
      const before = fs.readdirSync(outDir).filter((f) => f.toLowerCase().endsWith('.pdf'))
        .map((f) => path.join(outDir, f));
      log('尝试直接打开 PDF:', direct.slice(0, 160));
      await cdp.send('Page.navigate', { url: direct }, sessionId);
      saved = await waitForNewFile(before, Math.min(90, args.timeout) * 1000);
      if (!saved) {
        // PDF 域名（pdf.sciencedirectassets.com 等）可能单独弹 Cloudflare 验证
        const ok = await waitHumanHere('PDF下载页');
        if (ok) saved = await waitForNewFile(before, 90000);
      }
      if (saved) log('直接导航 PDF 下载成功:', saved.path);
      else log('直接导航未产生下载（可能被预览/拦截），继续其它方式');
    }
  }

  // 1) 页面内 fetch（保留登录态/机构 IP 授权，最稳）——只 fetch 同源地址，
  //    跨域下载域（如 bar.cnki.net）会被 CORS 拦住且可能一次作废，交给点击流程。
  //    注意：已通过上一步落盘时跳过，避免用非 PDF 响应覆盖已成功的 saved。
  if (!saved) {
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
        (el) => /^(View\s*PDF|查看\s*PDF)$/i.test(norm(el.textContent)),
        (el) => /^(PDF\s*下载|下载\s*PDF|PDF下载|PDF)$/i.test(norm(el.textContent)),
        (el) => /^(下载全文|全文下载)$/i.test(norm(el.textContent)),
        (el) => /^下载$/.test(norm(el.textContent)) && inResult(el),
        (el) => /下载|download|view\s*pdf/i.test(norm(el.textContent)) && /btn|button|download|dl|wf-list|pdf/i.test(el.className || ''),
      ];
      let cand = null;
      for (const pick of pickers) {
        cand = all.find((el) => leaf(el) && !/full issue|整期/i.test(norm(el.textContent)) && pick(el));
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
      saved = await waitForNewFile(before, Math.min(45, args.timeout) * 1000);
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

  // 2c) 用浏览器 Cookie + Referer 走 HTTP 下载（ScienceDirect pdfft、CNKI bar 等防外链下载器）
  if (!saved && candidates.length) {
    const ua = version.userAgent || 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)';
    for (const u of candidates.slice(0, 6)) {
      if (!/^https?:/i.test(u)) continue;
      let hostname = '';
      try { hostname = new URL(u).hostname; } catch { continue; }
      if (!u.startsWith('https:')) continue;
      const allCookies = await cdp.send('Network.getCookies', {urls:[u]}, sessionId).then(r => r.cookies || []).catch(() => []);
      const res = await httpDownload(u, {
        'User-Agent': ua,
        'Referer': probe.url,
        'Accept': 'application/pdf,text/html;q=0.9,*/*;q=0.8',
        'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
        'Cookie': cookieHeaderFor(allCookies, hostname),
      });
      const body = res.body;
      if (body && body.length > 1024 && body.slice(0, 5).toString() === '%PDF-' && body.subarray(-4096).includes(Buffer.from('%%EOF'))) {
        saved = writePdfFromBase64(body.toString('base64'), nameHint);
        if (saved) { log('Cookie+Referer 下载成功:', saved.path); break; }
      } else {
        log('HTTP 直连失败:', u.slice(0, 120), res.status || res.error, (res.headers && res.headers['content-type']) || '');
      }
    }
  }

  if (!saved) {
    const current = await evaluate(cdp, sessionId, DOM_PROBE);
    if (requiresUser(current)) {
      console.log(JSON.stringify(sanitize({status:'need_user', url:current.url, title:current.title,
        note:'下载触发了新的验证或登录；请在原浏览器处理，再重跑同一文章命令'})));
      cdp.close(); process.exit(4);
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

  // 3) 兜底：直接打开候选 PDF（下载行为已 allow，attachment 会直接落盘）
  if (!saved && candidates.length) {
    const before = fs.readdirSync(outDir).filter((f) => f.toLowerCase().endsWith('.pdf'))
      .map((f) => path.join(outDir, f));
    await cdp.send('Page.navigate', { url: candidates[0] }, sessionId);
    saved = await waitForNewFile(before, 30000);
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
        identifier: args.doi || publicUrl(probe.url),
        status: 'saved_pending_verification',
        source: 'browser',
        license: null,
        version: null,
        url: publicUrl(probe.url),
        path: saved.path,
        bytes: saved.bytes,
        sha256,
        retrieved_at: new Date().toISOString(),
      };
      fs.appendFileSync(path.join(outDir, 'manifest.jsonl'), JSON.stringify(sanitize(record)) + '\n');
      saved.record = record;
      log('manifest 已追加');
    } catch (e) {
      log('写 manifest 失败:', e.message);
    }
  }

  const payload = {
    status: saved ? 'saved_pending_verification' : (needUser ? 'need_user' : 'no_pdf_found'),
    url: probe.url,
    title: args.title || probe.title,
    saved: saved ? saved.path : null,
    bytes: saved ? saved.bytes : null,
    doi: args.doi || null,
    candidates,
    need_user: needUser,
    note: saved ? (saved.record ? '已保存并登记；请用 pdf_check.py 核验完整性和题名，未核验不算下载完成' : '已保存但清单写入失败，请先补登记并核验')
      : '未拿到 PDF：可能未订阅该刊、页面需要登录/验证，或下载按钮在 iframe 内；可重跑 --wait-user',
  };
  console.log(JSON.stringify(sanitize(payload)));
  cdp.close();
  process.exit(saved ? (saved.record ? 3 : 1) : (needUser ? 4 : 3));
}
if (require.main === module) main().catch(e => { console.log(JSON.stringify(sanitize({status:'error', message:e.message || String(e)}))); process.exit(1); });
module.exports = {parseArgs};
