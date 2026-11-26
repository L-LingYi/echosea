export const TABS = ['dashboard', 'sea', 'traces', 'providers', 'stickers', 'persona', 'perception', 'ops', 'alerts'];

export const SLOT_LABELS = {
  main_brain: '主脑对话',
  gatekeeper: '门卫决策',
  reflection: '离线反思',
  embedding: '向量检索',
  tts: '语音合成',
  web_search: '联网搜索',
};
export const ROLE_LABELS = { primary: '主路由', fallback: '备用路由' };
export const PROTO_LABELS = { openai_compatible: 'OpenAI 兼容', typesafe_systemone: 'Jev 原生协议' };
export const HEALTH_LABELS = { healthy: '健康', error: '异常', unknown: '未测活', cooldown: '冷却保护' };
export const STAGE_LABELS = {
  inbound: '入站接收',
  gatekeeper: '门卫决策',
  main_brain: '主脑生成',
  web_search: '联网搜索',
  tts: '语音合成',
  onebot: 'OneBot 投递',
  reflection: '离线反思',
  recall: '撤回拦截',
};
export const STATUS_LABELS = {
  ok: '成功',
  accepted: '已受理',
  submitted: '已提交',
  synthesized: '已合成',
  direct_pass: '直接放行',
  soft_entry: '主动软切入',
  silent: '静默旁听',
  blocked_recall: '撤回拦截',
  approved: '已启用',
  pending: '待审核',
  rejected: '已拒绝',
  succeeded: '成功',
  running: '进行中',
  failed: '失败',
  rolled_back: '已回滚',
  success: '成功',
  error: '失败',
};
export const SEVERITY_LABELS = { critical: '严重', warning: '警告', info: '通知' };
export const ALERT_TYPE_LABELS = { webhook: 'Webhook 推送', smtp: 'SMTP 邮件', qq_admin: 'QQ 管理员私聊' };
export const STORE_LABELS = { redis: 'Redis 状态库', memory: '内存状态库' };
export const GATEWAY_MODE_LABELS = { reverse_ws: '反向 WS', forward_ws: '正向 WS', webhook: 'HTTP 回调' };

let onUnauthorizedHandler = () => {};
export function setUnauthorizedHandler(fn) {
  onUnauthorizedHandler = fn;
}

export function labelOf(map, key) {
  return map[key] || key || '-';
}

export function formatCompactNum(n) {
  const num = Number(n || 0);
  if (num >= 1000000) return (num / 1000000).toFixed(1).replace(/\.0$/, '') + 'M';
  if (num >= 1000) return (num / 1000).toFixed(1).replace(/\.0$/, '') + 'k';
  return String(num);
}

export function esc(val) {
  return String(val ?? '').replace(/[&<>"']/g, ch => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch]));
}

export function formatLocalTime(iso, includeYear = false) {
  if (!iso) return '-';
  const raw = String(iso).trim();
  const hasTz = /(?:Z|[+-]\d{2}:?\d{2})$/i.test(raw);
  const d = new Date(hasTz ? raw : `${raw}Z`);
  if (Number.isNaN(d.getTime())) return raw.slice(includeYear ? 0 : 5, 19).replace('T', ' ');
  const pad = n => String(n).padStart(2, '0');
  const md = `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
  return includeYear ? `${d.getFullYear()}-${md}` : md;
}

export function toast(msg) {
  const box = document.getElementById('toast');
  if (!box) return;
  box.textContent = String(msg);
  box.classList.remove('hidden');
  setTimeout(() => box.classList.add('hidden'), 2600);
}

export function toggleDrawer(force) {
  const sb = document.getElementById('sidebar');
  const bd = document.getElementById('drawerBackdrop');
  const open = typeof force === 'boolean' ? force : !sb.classList.contains('open');
  sb.classList.toggle('open', open);
  bd.classList.toggle('open', open);
}

export function applyTheme(theme) {
  document.documentElement.setAttribute('data-theme', theme);
  localStorage.setItem('echosea_theme', theme);
  const btn = document.getElementById('themeToggleBtn');
  if (btn) btn.textContent = theme === 'dark' ? '☀️ 浅色模式' : '🌙 深色模式';
}

export function toggleTheme() {
  const cur = document.documentElement.getAttribute('data-theme') || 'light';
  applyTheme(cur === 'dark' ? 'light' : 'dark');
}

export function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === undefined || v === null) continue;
    if (k === 'className') node.className = v;
    else if (k === 'textContent') node.textContent = String(v);
    else if (k === 'style' && typeof v === 'string') node.setAttribute('style', v);
    else if (k === 'dataset' && typeof v === 'object') {
      for (const [dk, dv] of Object.entries(v)) node.dataset[dk] = String(dv);
    } else if (k.startsWith('on') && typeof v === 'function') {
      node.addEventListener(k.slice(2).toLowerCase(), v);
    } else {
      node.setAttribute(k, String(v));
    }
  }
  for (const child of children.flat()) {
    if (child === undefined || child === null || child === false) continue;
    node.appendChild(typeof child === 'string' || typeof child === 'number' ? document.createTextNode(String(child)) : child);
  }
  return node;
}

export function replaceChildren(target, ...children) {
  const container = typeof target === 'string' ? document.getElementById(target) : target;
  if (!container) return;
  container.replaceChildren(...children.flat().filter(Boolean).map(c => (
    typeof c === 'string' || typeof c === 'number' ? document.createTextNode(String(c)) : c
  )));
}

export function badge(text, variant = 'info') {
  return el('span', { className: `badge ${variant}` }, text);
}

export function safeExternalUrl(raw) {
  try {
    const u = new URL(String(raw || ''), window.location.origin);
    if (u.protocol === 'http:' || u.protocol === 'https:') return u.href;
  } catch {
    return null;
  }
  return null;
}

export async function api(path, opts = {}) {
  const isFormData = typeof FormData !== 'undefined' && opts.body instanceof FormData;
  const headers = Object.assign(
    isFormData ? {} : { 'Content-Type': 'application/json' },
    opts.headers || {},
  );
  const res = await fetch(path, Object.assign({ credentials: 'same-origin' }, opts, { headers }));
  if (res.status === 401) {
    if (!opts.silent401) onUnauthorizedHandler();
    throw new Error('Unauthorized');
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || '请求失败');
  return data;
}
