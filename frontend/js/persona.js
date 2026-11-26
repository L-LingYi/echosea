import { STATUS_LABELS, api, badge, el, labelOf, replaceChildren, toast } from './api.js';

export async function activatePersona(id) {
  await api(`/api/persona/versions/${id}/activate`, { method: 'POST' });
  toast('已切换生效人设');
  loadPersona();
}

export async function saveRuntimeConfig() {
  await api('/api/persona/configs', {
    method: 'PUT',
    body: JSON.stringify({
      scope_type: document.getElementById('cfgScopeType').value,
      scope_id: document.getElementById('cfgScopeId').value.trim() || 'default',
      config: { soft_entry_threshold: parseFloat(document.getElementById('cfgSoftThr').value || '0.78') },
    }),
  });
  toast('运行参数已热生效');
  loadPersona();
}

export async function runSandbox() {
  const lines = document.getElementById('simInput').value.trim().split('\n').filter(Boolean);
  const messages = lines.map(l => {
    const [qq, ...rest] = l.split('|');
    return { sender_qq: parseInt(qq, 10) || 10001, text: rest.join('|') };
  });
  const res = await api('/api/persona/sandbox/replay', {
    method: 'POST',
    body: JSON.stringify({
      messages,
      override_soft_entry_threshold: parseFloat(document.getElementById('simThreshold').value || '0.78'),
    }),
  });
  replaceChildren(
    'simResult',
    (res.results || []).map(r => {
      const act = r.decision.action;
      const cls = act === 'direct_pass' ? 'ok' : (act === 'soft_entry' ? 'info' : 'warn');
      return el(
        'div',
        { style: 'padding:10px 12px;border:1px solid var(--border);border-radius:10px;margin-bottom:8px;background:var(--surface-2);' },
        el(
          'div',
          { style: 'display:flex;justify-content:space-between;gap:8px;align-items:center;' },
          el('strong', {}, `${r.sender}: ${r.text}`),
          badge(`${labelOf(STATUS_LABELS, act)} (${r.decision.score})`, cls),
        ),
        el('div', { style: 'font-size:12px;color:var(--mute);margin-top:4px;' }, r.decision.reason),
      );
    }),
  );
}

export async function loadPersona() {
  const [list, cfgs] = await Promise.all([api('/api/persona/versions'), api('/api/persona/configs')]);
  replaceChildren(
    'personaList',
    list.map(p => el(
      'div',
      { style: 'padding:12px 0;border-bottom:1px solid var(--border-soft);' },
      el(
        'div',
        { style: 'display:flex;justify-content:space-between;align-items:center;' },
        el('strong', {}, `${p.version_tag} · ${p.bot_name}`),
        p.is_active
          ? badge('生效中', 'ok')
          : el('button', { className: 'btn sm secondary', type: 'button', onClick: () => activatePersona(Number(p.id)) }, '激活'),
      ),
      el('div', { className: 'pill-list', style: 'margin:6px 0;' }, ...(p.aliases || []).map(a => badge(a, 'info'))),
      el('div', { style: 'font-size:13px;color:var(--ink2);' }, p.system_prompt),
    )),
  );
  document.getElementById('cfgSavedList').textContent = cfgs.length
    ? `已保存配置：${cfgs.map(c => `${c.scope_type}:${c.scope_id}`).join(' / ')}`
    : '当前使用默认参数';
}

export function initPersonaForm() {
  const form = document.getElementById('newPersonaForm');
  if (!form) return;
  form.addEventListener('submit', async e => {
    e.preventDefault();
    await api('/api/persona/versions', {
      method: 'POST',
      body: JSON.stringify({
        version_tag: document.getElementById('npTag').value.trim(),
        bot_name: document.getElementById('npName').value.trim(),
        aliases: document.getElementById('npAliases').value.split(',').map(x => x.trim()).filter(Boolean),
        system_prompt: document.getElementById('npPrompt').value.trim(),
        banned_phrases: document.getElementById('npBanned').value.split(',').map(x => x.trim()).filter(Boolean),
        activate: true,
      }),
    });
    toast('新人设已创建并激活');
    form.reset();
    form.classList.add('hidden');
    loadPersona();
  });
}
