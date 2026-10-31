'use strict';
const fs = require('fs');

function publicUrl(value) {
  try {
    const u = new URL(value);
    if (!/^https?:$/.test(u.protocol)) return value;
    u.username = ''; u.password = ''; u.hash = '';
    // Retain document identifiers needed by CNKI and search pages; omit auth/signatures.
    const allowed = new Set(['dbcode', 'dbname', 'filename', 'id', 'doi', 'articleid', 'fileid', 'kw', 'q', 'v', 'pageid', 'websiteid', 'datatype', 'wfwfid']);
    for (const key of [...u.searchParams.keys()]) if (!allowed.has(key.toLowerCase())) u.searchParams.delete(key);
    return u.href;
  } catch { return value; }
}

function sanitize(value) {
  if (typeof value === 'string') return value.replace(/https?:\/\/[^\s<>"']+/g, publicUrl);
  if (Array.isArray(value)) return value.map(sanitize);
  if (value && typeof value === 'object') return Object.fromEntries(Object.entries(value).map(([k, v]) => [k, sanitize(v)]));
  return value;
}

function requiresUser(p) {
  if (!p) return true;
  return Boolean(p.captchaEl || p.passwordEl ||
    /\/(?:login|signin)(?:[/?#]|$)|verify\/home|captchaType=|captchaId=|cdn-cgi\/challenge/i.test(p.url || '') ||
    /请完成安全验证|向右滑动完成验证|拖动滑块|滑块验证|are you a robot|confirm you are a human|captcha challenge|请验证您是真人|verify (?:that )?you are human|checking your browser|just a moment|请稍候…?$/i.test(p.bodyText || '') ||
    /are you a robot|just a moment|请稍候…?$/i.test(p.title || ''));
}

function basicPdf(path, maxBytes = 60 * 1024 * 1024) {
  try {
    const st = fs.statSync(path);
    if (!st.isFile() || st.size < 8 || st.size > maxBytes) return false;
    const fd = fs.openSync(path, 'r');
    try {
      const head = Buffer.alloc(5), tail = Buffer.alloc(Math.min(4096, st.size));
      fs.readSync(fd, head, 0, 5, 0);
      fs.readSync(fd, tail, 0, tail.length, st.size - tail.length);
      return head.toString() === '%PDF-' && tail.includes(Buffer.from('%%EOF'));
    } finally { fs.closeSync(fd); }
  } catch { return false; }
}

class Cdp {
  constructor(ws, timeoutMs = 30000) {
    this.ws = ws; this.timeoutMs = timeoutMs; this.nextId = 1;
    this.pending = new Map(); this.listeners = [];
    const fail = () => {
      for (const p of this.pending.values()) { clearTimeout(p.timer); p.reject(new Error('CDP disconnected')); }
      this.pending.clear();
    };
    ws.addEventListener('close', fail); ws.addEventListener('error', fail);
    ws.addEventListener('message', (ev) => {
      let msg;
      try { msg = JSON.parse(ev.data); } catch { return; }
      if (msg.id && this.pending.has(msg.id)) {
        const p = this.pending.get(msg.id); this.pending.delete(msg.id); clearTimeout(p.timer);
        if (msg.error) p.reject(new Error(msg.error.message || 'CDP error'));
        else p.resolve(msg.result);
      } else this.listeners.slice().forEach((fn) => fn(msg));
    });
  }
  send(method, params = {}, sessionId) {
    const id = this.nextId++;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => { this.pending.delete(id); reject(new Error('CDP timeout: ' + method)); }, this.timeoutMs);
      this.pending.set(id, {resolve, reject, timer});
      try { this.ws.send(JSON.stringify({id, method, params, ...(sessionId ? {sessionId} : {})})); }
      catch (e) { clearTimeout(timer); this.pending.delete(id); reject(e); }
    });
  }
  close() { try { this.ws.close(); } catch {} }
}

module.exports = {publicUrl, sanitize, requiresUser, basicPdf, Cdp};
