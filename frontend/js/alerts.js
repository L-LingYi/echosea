import { ALERT_TYPE_LABELS, SEVERITY_LABELS, api, badge, el, formatLocalTime, labelOf, replaceChildren, toast } from './api.js';

export async function loadAlerts() {
  const [chs, evs] = await Promise.all([api('/api/alerts/channels'), api('/api/alerts/events')]);
  const channelNodes = chs.length
    ? chs.map(c => badge(`${labelOf(ALERT_TYPE_LABELS, c.channel_type)} · ${c.name}`, 'info'))
    : [el('span', { style: 'color:var(--mute);' }, '尚未配置告警通道')];

  const eventRows = evs.length
    ? evs.map(e => {
      const cls = e.severity === 'critical' ? 'err' : (e.severity === 'warning' ? 'warn' : 'info');
      return el(
        'tr',
        {},
        el('td', {}, badge(labelOf(SEVERITY_LABELS, e.severity), cls)),
        el('td', {}, e.category),
        el('td', {}, e.title),
        el('td', {}, formatLocalTime(e.created_at)),
      );
    })
    : [el('tr', {}, el('td', { colSpan: 4, className: 'empty-state' }, '暂无告警记录'))];

  replaceChildren(
    'alertList',
    el('div', { className: 'pill-list', style: 'margin-bottom:14px;' }, ...channelNodes),
    el(
      'div',
      { className: 'table-wrap' },
      el(
        'table',
        {},
        el('thead', {}, el('tr', {}, el('th', {}, '级别'), el('th', {}, '分类'), el('th', {}, '标题'), el('th', {}, '时间'))),
        el('tbody', {}, ...eventRows),
      ),
    ),
  );
}

export async function sendTestAlert() {
  await api('/api/alerts/test', { method: 'POST' });
  toast('测试告警已发送');
  loadAlerts();
}

export function initAlertForm() {
  const form = document.getElementById('alertForm');
  if (!form) return;
  form.addEventListener('submit', async e => {
    e.preventDefault();
    let cfgObj;
    try {
      cfgObj = JSON.parse(document.getElementById('aConfig').value);
    } catch {
      toast('通道配置 JSON 格式有误');
      return;
    }
    await api('/api/alerts/channels', {
      method: 'POST',
      body: JSON.stringify({
        channel_type: document.getElementById('aType').value,
        name: document.getElementById('aName').value.trim(),
        config: cfgObj,
      }),
    });
    toast('告警通道已加密保存');
    loadAlerts();
  });
}
