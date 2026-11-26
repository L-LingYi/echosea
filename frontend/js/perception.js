import { api, badge, el, replaceChildren, toast } from './api.js';

export function fillHotPreset(name, cat, url) {
  document.getElementById('hsName').value = name;
  document.getElementById('hsCat').value = cat;
  document.getElementById('hsUrl').value = url;
}

export async function toggleHotSource(id, en) {
  await api(`/api/ops/hot-sources/${id}`, { method: 'PATCH', body: JSON.stringify({ is_enabled: en }) });
  loadPerception();
}

export async function delHotSource(id) {
  await api(`/api/ops/hot-sources/${id}`, { method: 'DELETE' });
  loadPerception();
}

export async function refreshBriefing() {
  const r = await api('/api/ops/briefings/refresh', { method: 'POST' });
  toast(r.ok ? '简报已更新' : r.message);
  loadPerception();
}

export async function syncSchedule() {
  const r = await api('/api/ops/schedule/sync', { method: 'POST', body: '{}' });
  toast(`状态已更新：${r.current_activity} (${r.mood})`);
}

export async function loadPerception() {
  const [b, sched, sources, presets] = await Promise.all([
    api('/api/ops/briefings/latest'),
    api('/api/ops/schedule'),
    api('/api/ops/hot-sources'),
    api('/api/ops/hot-sources/presets'),
  ]);
  document.getElementById('briefingBox').textContent = b.digest_text || '暂无今日刷网简报';
  replaceChildren(
    'scheduleTable',
    sched.map(s => el(
      'tr',
      {},
      el('td', {}, el('code', {}, `${Number(s.start_hour)}:00 - ${Number(s.end_hour)}:00`)),
      el('td', {}, s.activity),
      el('td', {}, badge(s.default_mood, 'info')),
      el('td', {}, Number(s.energy_target)),
    )),
  );
  replaceChildren(
    'hotPresetPills',
    presets.map(p => el(
      'button',
      { type: 'button', className: 'btn sm secondary', onClick: () => fillHotPreset(p.name, p.category, p.feed_url) },
      `＋ ${p.name}`,
    )),
  );
  if (!sources.length) {
    replaceChildren('hotSourceTable', el('tr', {}, el('td', { colSpan: 5, className: 'empty-state' }, '尚未配置热点渠道，点击上方预设或填写 RSS 地址添加')));
  } else {
    replaceChildren(
      'hotSourceTable',
      sources.map(s => el(
        'tr',
        {},
        el('td', {}, el('strong', {}, s.name)),
        el('td', {}, badge(s.category, 'info')),
        el('td', { style: 'font-size:12px;word-break:break-all;' }, s.feed_url),
        el('td', {}, badge(s.is_enabled ? '启用' : '停用', s.is_enabled ? 'ok' : 'warn')),
        el(
          'td',
          { className: 'row-actions' },
          el('button', { className: 'btn sm secondary', type: 'button', onClick: () => toggleHotSource(Number(s.id), !s.is_enabled) }, s.is_enabled ? '停用' : '启用'),
          el('button', { className: 'btn sm danger', type: 'button', onClick: () => delHotSource(Number(s.id)) }, '删除'),
        ),
      )),
    );
  }
}

export function initHotSourceForm() {
  const form = document.getElementById('hotSourceForm');
  if (!form) return;
  form.addEventListener('submit', async e => {
    e.preventDefault();
    await api('/api/ops/hot-sources', {
      method: 'POST',
      body: JSON.stringify({
        name: document.getElementById('hsName').value.trim(),
        category: document.getElementById('hsCat').value.trim() || 'general',
        feed_url: document.getElementById('hsUrl').value.trim(),
        is_enabled: true,
      }),
    });
    toast('热点渠道已添加');
    form.reset();
    loadPerception();
  });
}
