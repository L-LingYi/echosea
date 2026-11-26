import {
  HEALTH_LABELS,
  PROTO_LABELS,
  ROLE_LABELS,
  SLOT_LABELS,
  api,
  badge,
  el,
  labelOf,
  replaceChildren,
  toast,
} from './api.js';

let PROVIDERS_CACHE = [];

export function resetProviderForm() {
  document.getElementById('pEditId').value = '';
  document.getElementById('providerForm').reset();
  document.getElementById('providerFormTitle').textContent = '新增 / 配置模型槽位';
}

export function openProviderModal() {
  resetProviderForm();
  document.getElementById('providerModal').classList.remove('hidden');
}

export function closeProviderModal() {
  document.getElementById('providerModal').classList.add('hidden');
}

export function editProvider(id) {
  const p = PROVIDERS_CACHE.find(x => x.id === id);
  if (!p) return;
  document.getElementById('pEditId').value = String(p.id);
  document.getElementById('pSlot').value = p.slot;
  document.getElementById('pProto').value = p.protocol;
  document.getElementById('pRole').value = p.role;
  document.getElementById('pName').value = p.name;
  document.getElementById('pUrl').value = p.base_url;
  document.getElementById('pModel').value = p.model_name;
  document.getElementById('pKey').value = '';
  document.getElementById('pExtra').value = p.extra_config && Object.keys(p.extra_config).length
    ? JSON.stringify(p.extra_config, null, 2)
    : '';
  document.getElementById('providerFormTitle').textContent = `编辑模型路由：${p.name}`;
  document.getElementById('providerModal').classList.remove('hidden');
}

export async function resetProviderCooldown(id) {
  await api(`/api/providers/${id}/reset-cooldown`, { method: 'POST' });
  toast('已解除冷却，下一次请求将优先使用该路由');
  await loadProviders();
}

export async function testProvider(id) {
  const r = await api(`/api/providers/${id}/test`, { method: 'POST' });
  toast(r.ok ? `测活成功 (${r.latency_ms}ms)` : `测活失败: ${r.error}`);
  await loadProviders();
}

export async function delProvider(id) {
  if (!confirm('确认删除该模型路由？')) return;
  await api(`/api/providers/${id}`, { method: 'DELETE' });
  toast('已删除');
  await loadProviders();
}

export async function loadProviders() {
  const data = await api('/api/providers');
  PROVIDERS_CACHE = data.items || [];
  if (!PROVIDERS_CACHE.length) {
    replaceChildren(
      'providerGrid',
      el('div', { className: 'card empty-state', style: 'grid-column:1/-1;' }, '尚未配置任何模型槽位，点击右上角「＋ 新增 / 配置槽位」开始'),
    );
    return;
  }

  replaceChildren(
    'providerGrid',
    PROVIDERS_CACHE.map(p => {
      const healthVariant = p.cooldown_active
        ? 'warn'
        : (p.last_health_status === 'healthy' ? 'ok' : (p.last_health_status === 'error' ? 'err' : 'warn'));
      const healthLabel = p.cooldown_active
        ? `冷却中 ${Math.max(1, Math.ceil(Number(p.cooldown_remaining_seconds || 0) / 60))}m`
        : `${labelOf(HEALTH_LABELS, p.last_health_status)}${p.last_health_ms ? ` ${Number(p.last_health_ms)}ms` : ''}`;

      const actions = [
        el('button', { className: 'btn sm secondary', type: 'button', onClick: () => editProvider(Number(p.id)) }, '编辑'),
        el('button', { className: 'btn sm secondary', type: 'button', onClick: () => testProvider(Number(p.id)) }, '测活'),
      ];
      if (p.cooldown_active) {
        actions.push(el('button', { className: 'btn sm', type: 'button', onClick: () => resetProviderCooldown(Number(p.id)) }, '恢复主路'));
      }
      actions.push(el('button', { className: 'btn sm danger', type: 'button', onClick: () => delProvider(Number(p.id)) }, '删除'));

      return el(
        'div',
        { className: 'provider-card' },
        el(
          'div',
          { style: 'display:flex;justify-content:space-between;align-items:center;gap:6px;' },
          badge(`${labelOf(SLOT_LABELS, p.slot)} · ${labelOf(ROLE_LABELS, p.role)}`, 'info'),
          badge(`● ${healthLabel}`, healthVariant),
        ),
        el(
          'div',
          {},
          el('div', { style: 'font-weight:700;font-size:15px;' }, p.name),
          el('div', { style: 'font-size:12.5px;color:var(--mute);margin-top:2px;' }, p.model_name),
        ),
        el('div', { style: 'font-size:12px;color:var(--ink2);word-break:break-all;' }, p.base_url),
        el(
          'div',
          { className: 'pill-list' },
          badge(labelOf(PROTO_LABELS, p.protocol), 'purple'),
          el('code', {}, p.api_key_masked || '(空)'),
        ),
        p.cooldown_active
          ? el(
            'div',
            { style: 'font-size:11.5px;color:var(--warn-ink);background:var(--warn-bg);border:1px solid rgba(245,158,11,.35);border-radius:8px;padding:6px 8px;' },
            `已切备路：${p.cooldown_reason || '冷却保护中'}，到期自动试探回切`,
          )
          : null,
        el('div', { className: 'row-actions', style: 'margin-top:auto;padding-top:8px;border-top:1px solid var(--border-soft);' }, ...actions),
      );
    }),
  );
}

export function initProvidersForm() {
  const form = document.getElementById('providerForm');
  if (!form) return;
  form.addEventListener('submit', async e => {
    e.preventDefault();
    const editId = document.getElementById('pEditId').value.trim();
    const extraRaw = document.getElementById('pExtra').value.trim();
    let extraObj = null;
    if (extraRaw) {
      try {
        extraObj = JSON.parse(extraRaw);
      } catch {
        toast('扩展配置 JSON 格式有误');
        return;
      }
    }
    const body = {
      slot: document.getElementById('pSlot').value,
      protocol: document.getElementById('pProto').value,
      role: document.getElementById('pRole').value,
      name: document.getElementById('pName').value.trim(),
      base_url: document.getElementById('pUrl').value.trim(),
      model_name: document.getElementById('pModel').value.trim(),
    };
    const keyVal = document.getElementById('pKey').value.trim();
    if (keyVal) body.api_key = keyVal;
    if (extraObj !== null) body.extra_config = extraObj;
    if (editId) {
      await api(`/api/providers/${editId}`, { method: 'PATCH', body: JSON.stringify(body) });
      toast('已安全更新模型配置');
    } else {
      body.api_key = keyVal;
      await api('/api/providers', { method: 'PUT', body: JSON.stringify(body) });
      toast('模型路由已保存并热生效');
    }
    closeProviderModal();
    loadProviders();
  });
}
