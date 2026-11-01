'use strict';
// Local credential consumer. Never print credentials or pass them as CLI arguments.
const fs = require('fs');
const path = require('path');
const ALLOWED_ORIGINS = new Set(['https://webvpn.whut.edu.cn', 'https://zhlgd.whut.edu.cn']);
const packageRoot=path.resolve(__dirname,'../..');
const DEFAULT_FILE=process.env.CAMPUS_CREDENTIALS || (fs.existsSync(path.join(packageRoot,'campus_portal.py'))?path.join(packageRoot,'登录配置.private.json'):'');
const SELECTORS = {
  username: 'input[name="un"],input#un,input[name="username"],input#username,input[autocomplete="username"]',
  password: 'input[type="password"]',
};

function approvedOrigin(value) {
  try {
    const url = new URL(value);
    return !url.username && !url.password && ALLOWED_ORIGINS.has(url.origin);
  } catch { return false; }
}

function readCredentials(file = DEFAULT_FILE) {
  if(!file)throw new Error('credential_path_required_use_explicit_config');
  let value;
  try { value = JSON.parse(fs.readFileSync(file, 'utf8').replace(/^\uFEFF/, '')); }
  catch { throw new Error('credential_file_unreadable_or_invalid_json'); }
  if (!value || Array.isArray(value) || typeof value !== 'object') throw new Error('credential_config_must_be_object');
  if (typeof value.enabled !== 'boolean' || typeof value.auto_submit !== 'boolean') throw new Error('credential_options_must_be_boolean');
  if (!approvedOrigin(value.login_url)) throw new Error('credential_login_origin_not_allowed');
  if (typeof value.username !== 'string' || typeof value.password !== 'string') throw new Error('credential_fields_must_be_strings');
  const username = value.username.trim();
  if (/\r|\n|\0/.test(username) || /\0/.test(value.password)) throw new Error('credential_fields_contain_invalid_characters');
  // Do not trim passwords: leading/trailing spaces can be part of a real password.
  return {enabled:value.enabled, username, password:value.password, login_url:value.login_url, auto_submit:value.auto_submit};
}

function credentialStatus(file = DEFAULT_FILE) {
  try {
    const c = readCredentials(file);
    const missing = [];
    if (!c.username) missing.push('username');
    if (!c.password) missing.push('password');
    return {status:!c.enabled ? 'disabled' : missing.length ? 'credentials_missing' : 'ready',
      missing_fields:missing, auto_submit:c.auto_submit, credential_values_returned:false,
      live_login_tested:false};
  } catch (error) {
    return {status:'config_error', reason:error.message, credential_values_returned:false, live_login_tested:false};
  }
}

async function evaluate(cdp, session, expression) {
  const response = await cdp.send('Runtime.evaluate', {expression, returnByValue:true}, session);
  if (response.exceptionDetails || !response.result) throw new Error('login_page_inspection_failed');
  return response.result.value;
}

async function fillLogin(cdp, session, credentials) {
  if (!credentials.enabled) return {status:'disabled', submitted:false};
  if (!credentials.username || !credentials.password) return {status:'credentials_missing', submitted:false};
  try {
    // Check location before each field; values never appear in Runtime.evaluate source.
    for (const field of ['username','password']) {
      const origin = await evaluate(cdp, session, 'location.origin');
      if (!approvedOrigin(origin)) return {status:'origin_not_allowed', submitted:false};
      const selector = JSON.stringify(SELECTORS[field]);
      const context = await evaluate(cdp, session, `(() => {
        const visible = el => !el.disabled && !el.readOnly && el.getBoundingClientRect().width > 0;
        const el = Array.from(document.querySelectorAll(${selector})).find(visible);
        return {origin:location.origin, found:Boolean(el), action:el && el.form ? el.form.action : location.href};
      })()`);
      if (!context || !approvedOrigin(context.origin) || !approvedOrigin(context.action)) return {status:'form_origin_not_allowed', submitted:false};
      if (!context.found) return {status:'login_fields_not_found', submitted:false};
      // Guard and focus in one page call. Select existing content before native input.
      const focused = await evaluate(cdp, session, `(() => {
        if (!${JSON.stringify([...ALLOWED_ORIGINS])}.includes(location.origin)) return false;
        const el = Array.from(document.querySelectorAll(${selector})).find(el => !el.disabled && !el.readOnly && el.getBoundingClientRect().width > 0);
        if (!el) return false; el.focus(); el.select(); return true;
      })()`);
      if (!focused) return {status:'login_focus_failed', submitted:false};
      await cdp.send('Input.insertText', {text:credentials[field]}, session);
    }
    if (!credentials.auto_submit) return {status:'credentials_filled', submitted:false, need_user:true};
    const origin = await evaluate(cdp, session, 'location.origin');
    if (!approvedOrigin(origin)) return {status:'origin_not_allowed', submitted:false};
    const submission = await evaluate(cdp, session, `(() => {
      const allowed = ${JSON.stringify([...ALLOWED_ORIGINS])};
      if (!allowed.includes(location.origin)) return 'origin_not_allowed';
      const visible = el => el.getBoundingClientRect().width > 0 && el.getBoundingClientRect().height > 0;
      // CAPTCHA and MFA are handled by the user; never try to solve or submit them empty.
      if (Array.from(document.querySelectorAll('input[name="code"],input[name*="captcha"],input[id*="captcha"],input[name*="sms"],[class*="captcha"],[id*="captcha"],[class*="verify-slider"]')).some(visible)) return 'need_user';
      const pw = Array.from(document.querySelectorAll('input[type="password"]')).find(visible);
      if (!pw || (pw.form && !allowed.includes(new URL(pw.form.action, location.href).origin))) return 'form_origin_not_allowed';
      const scope = pw.form || document;
      const button = Array.from(scope.querySelectorAll('button,input[type="submit"],a'))
        .find(el => visible(el) && !el.disabled && /^(登录|登 录|立即登录|login|log in|sign in)$/i.test((el.innerText || el.value || '').trim()));
      if (!button) return 'submit_button_not_found';
      button.click(); return 'submitted_once';
    })()`);
    return {status:submission, submitted:submission === 'submitted_once', need_user:submission !== 'submitted_once'};
  } catch {
    // CDP errors can include context; replace with a constant error before returning.
    return {status:'login_fill_failed', submitted:false, need_user:true};
  }
}

if (require.main === module) {
  const argv = process.argv.slice(2);
  let file = DEFAULT_FILE;
  if (argv.length === 3 && argv[0] === '--check' && argv[1] === '--config') file = argv[2];
  else if (argv.length !== 1 || argv[0] !== '--check') {
    console.log(JSON.stringify({status:'invalid_arguments',usage:'node login_credentials.js --check [--config file]'}));
    process.exit(2);
  }
  const result = credentialStatus(file);
  console.log(JSON.stringify(result));
  process.exit(result.status === 'ready' ? 0 : 2);
}
module.exports = {DEFAULT_FILE, approvedOrigin, readCredentials, credentialStatus, fillLogin};
