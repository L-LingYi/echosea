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

export async function reviewSuggestion(id, action) {
  const res = await api(`/api/persona/suggestions/${id}/review`, {
    method: 'POST',
    body: JSON.stringify({ action }),
  });
  toast(action === 'approve' ? `已审批通过 (${res.status})` : '已忽略/停用');
  loadPersona();
}

export async function loadPersona() {
  const [list, cfgs, suggestions] = await Promise.all([
    api('/api/persona/versions'),
    api('/api/persona/configs'),
    api('/api/persona/suggestions').catch(() => []),
  ]);
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

  if (!suggestions.length) {
    replaceChildren('suggestionList', el('div', { className: 'empty-state' }, '暂无待审演进建议或纠偏提示'));
  } else {
    const typeMap = {
      few_shot: '高赞 Few-Shot',
      banned_phrase: '待审口癖词',
      persona_advice: '性格建议（仅参考）',
      correction_hint: '48h 自纠偏提示',
    };
    replaceChildren(
      'suggestionList',
      suggestions.map(s => {
        const c = s.content_json || {};
        const detailText = s.suggestion_type === 'few_shot'
          ? `问：${c.user || ''} → 答：${c.assistant || ''}`
          : (c.phrase || c.advice || c.hint || '');
        const stCls = s.status === 'approved' || s.status === 'active' ? 'ok' : (s.status === 'pending' ? 'warn' : 'info');
        return el(
          'div',
          { style: 'padding:10px 0;border-bottom:1px solid var(--border-soft);display:flex;justify-content:space-between;gap:10px;align-items:flex-start;' },
          el(
            'div',
            { style: 'flex:1;min-width:0;' },
            el('div', { style: 'display:flex;gap:6px;align-items:center;flex-wrap:wrap;margin-bottom:4px;' },
              badge(typeMap[s.suggestion_type] || s.suggestion_type, 'purple'),
              badge(labelOf(STATUS_LABELS, s.status), stCls),
              el('strong', {}, s.title || '-'),
            ),
            detailText ? el('div', { style: 'font-size:13px;color:var(--ink2);margin-bottom:2px;' }, detailText) : null,
            s.reason ? el('div', { style: 'font-size:12px;color:var(--mute);' }, s.reason) : null,
          ),
          (s.status === 'pending' || s.status === 'active')
            ? el(
              'div',
              { className: 'row-actions' },
              s.status === 'pending'
                ? el('button', { className: 'btn sm', type: 'button', onClick: () => reviewSuggestion(Number(s.id), 'approve') }, '通过')
                : null,
              el('button', { className: 'btn sm secondary', type: 'button', onClick: () => reviewSuggestion(Number(s.id), 'reject') }, s.status === 'active' ? '关闭' : '忽略'),
            )
            : null,
        );
      }),
    );
  }
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
