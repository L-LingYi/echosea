import { STAGE_LABELS, STATUS_LABELS, api, badge, el, formatLocalTime, labelOf, replaceChildren } from './api.js';

export async function loadTraces() {
  const rows = await api('/api/dashboard/traces?limit=80');
  if (!rows.length) {
    replaceChildren('traceTable', el('tr', {}, el('td', { colSpan: 7, className: 'empty-state' }, '暂无追踪记录')));
    return;
  }
  replaceChildren(
    'traceTable',
    rows.map(r => {
      const okSet = ['ok', 'accepted', 'submitted', 'synthesized', 'direct_pass', 'soft_entry'];
      const stCls = okSet.includes(r.status) ? 'ok' : (r.status === 'silent' ? 'warn' : 'err');
      const meta = r.meta_json || r.meta || {};
      const errExtras = [r.error_type || '-'];
      if (meta.recalled_episodes !== undefined) errExtras.push(`L3:${meta.recalled_episodes}`);
      return el(
        'tr',
        {},
        el('td', {}, el('code', {}, r.request_id)),
        el('td', {}, `${r.scene}${r.group_id ? ` / ${r.group_id}` : ''}`),
        el('td', {}, badge(labelOf(STAGE_LABELS, r.stage), 'info')),
        el('td', {}, badge(labelOf(STATUS_LABELS, r.status), stCls)),
        el('td', {}, `${Number(r.latency_ms)}ms`),
        el('td', {}, errExtras.join(' · ')),
        el('td', {}, formatLocalTime(r.created_at)),
      );
    }),
  );
}
