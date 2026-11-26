import { STATUS_LABELS, api, badge, el, labelOf, replaceChildren, toast } from './api.js';

const LAYER_COLORS = {
  L5_self: '#facc15',
  group: '#38bdf8',
  L4_identity: '#34d399',
  L2_ambient: '#22d3ee',
  L3_public_episode: '#818cf8',
  L3_private_episode: '#f43f5e',
  L4_slang: '#fb923c',
};
const LAYER_NAMES = {
  L5_self: 'L5 自我核心',
  group: '群聊记忆域',
  L4_identity: 'L4 熟人卡',
  L2_ambient: 'L2 边缘旁听',
  L3_public_episode: 'L3 群情景/纪念',
  L3_private_episode: 'L3 私聊半隔离',
  L4_slang: 'L4 群黑话',
};

const SEA = {
  nodes: [],
  edges: [],
  visibleLayers: new Set(Object.keys(LAYER_COLORS)),
  camX: 0,
  camY: 0,
  zoom: 1,
  selected: null,
  dragNode: null,
  panning: false,
  lastX: 0,
  lastY: 0,
  raf: null,
  tick: 0,
};

function makeDemoSea() {
  return {
    nodes: [
      { id: 'self_core', label: 'EchoSea · 意识核心', layer: 'L5_self', size: 28, summary: '精力 88 · 心情 excited · 晚间吹水看群', detail: { energy_level: 88, mood: 'excited', current_activity: '晚间吹水看群' } },
      { id: 'group_101', label: '群 101 · 摸鱼主群', layer: 'group', size: 21, summary: '活跃群聊记忆域', detail: { group_id: 101 } },
      { id: 'qq_1', label: '阿明', layer: 'L4_identity', size: 18, summary: 'QQ 10086 · 亲密度 82', detail: { qq_id: 10086, known_names: ['阿明', '摸鱼明'], relationship_level: 82, public_profile: { interests: ['原神', '烧烤'] }, has_private_isolation: true } },
      { id: 'qq_2', label: '红姐', layer: 'L4_identity', size: 16, summary: 'QQ 20001 · 亲密度 65', detail: { qq_id: 20001, known_names: ['小红', '红姐'], relationship_level: 65, public_profile: { interests: ['露营', '吸猫'] } } },
      { id: 'amb_1', label: '边缘旁听 #12', layer: 'L2_ambient', size: 13, summary: '刚才大家在争晚饭吃麻辣烫还是烧烤', detail: { message_count: 24, summary_text: '近2小时群内热议晚饭选麻辣烫还是烧烤。' } },
      { id: 'ep_1', label: '五排连胜之夜', layer: 'L3_public_episode', size: 16, summary: '群内共同经历 · 纪念日', detail: { scope: 'public_group', is_anniversary: true, content: '昨晚和群友五排连胜五把。' } },
      { id: 'ep_2', label: '私聊吐露换工作', layer: 'L3_private_episode', size: 15, summary: '仅私聊可见 · SQL 级严格半隔离', detail: { scope: 'private_dm', content: '私聊时聊到想换工作，群聊绝不提及。' } },
      { id: 'slang_1', label: '梗·寄了', layer: 'L4_slang', size: 11, summary: '事情办砸了 (approved)', detail: { phrase: '寄了', meaning: '事情办砸了', status: 'approved', usage_count: 19 } },
    ],
    edges: [
      { source: 'self_core', target: 'group_101', relation: '常驻群域' }, { source: 'self_core', target: 'qq_1', relation: '熟人羁绊' }, { source: 'self_core', target: 'qq_2', relation: '熟人羁绊' },
      { source: 'qq_1', target: 'group_101', relation: '群名片:摸鱼明' }, { source: 'qq_2', target: 'group_101', relation: '群名片:红姐' }, { source: 'group_101', target: 'amb_1', relation: '旁听沉淀' },
      { source: 'qq_1', target: 'amb_1', relation: '参与讨论' }, { source: 'qq_2', target: 'amb_1', relation: '参与讨论' }, { source: 'group_101', target: 'ep_1', relation: '群内情景' },
      { source: 'qq_1', target: 'ep_1', relation: '共同经历' }, { source: 'qq_1', target: 'ep_2', relation: '私密记忆(半隔离)' }, { source: 'group_101', target: 'slang_1', relation: '群黑话' },
    ],
  };
}

export async function approveSlang(id) {
  await api(`/api/memory/expressions/${id}`, { method: 'PATCH', body: JSON.stringify({ status: 'approved' }) });
  toast('已通过黑话');
  loadMemoryTables();
}

export async function forgetUser(qq) {
  if (!confirm(`确认连根清理 QQ ${qq} 的所有记忆与画像？`)) return;
  const r = await api('/api/memory/forget', { method: 'POST', body: JSON.stringify({ qq_id: qq, wipe_profile: true }) });
  toast(`已清理消息 ${r.messages_deleted} 条`);
  loadUnifiedMemory();
}

const _splitCsv = val => String(val || '').split(/[,，]/).map(x => x.trim()).filter(Boolean);
const _joinCsv = arr => (Array.isArray(arr) ? arr.join(', ') : '');

export function closeIdentityModal() {
  const modal = document.getElementById('identityModal');
  if (modal) modal.classList.add('hidden');
}

export function openIdentityModal(ident) {
  const modal = document.getElementById('identityModal');
  if (!modal || !ident) return;
  const pub = ident.public_profile || {};
  const priv = ident.private_profile || {};
  const pubRel = pub.relationship || {};
  const pubTraits = pub.stable_traits || {};
  const privRel = priv.relationship || {};
  const privTraits = priv.stable_traits || {};

  document.getElementById('identityFormTitle').textContent = `编辑四维画像 · QQ ${ident.qq_id}`;
  document.getElementById('idEditQq').value = String(ident.qq_id);
  document.getElementById('idNickname').value = ident.nickname || '';
  document.getElementById('idRelLevel').value = String(Number(ident.relationship_level || 0));

  document.getElementById('idPubAddr').value = pubRel.preferred_address || '';
  document.getElementById('idPubStyle').value = pubRel.interaction_style || '';
  document.getElementById('idPubBound').value = _joinCsv(pubRel.boundaries);
  document.getElementById('idPubInterests').value = _joinCsv(pubTraits.interests || pub.interests);
  document.getElementById('idPubExpertise').value = _joinCsv(pubTraits.expertise);
  document.getElementById('idPubSchedule').value = _joinCsv(pubTraits.schedule_habits);
  document.getElementById('idPubStates').value = _joinCsv((pub.recent_states || []).map(s => s.text));
  document.getElementById('idPubCandidates').value = _joinCsv((pub.candidate_clues || []).map(c => c.value));

  document.getElementById('idPrivAddr').value = privRel.preferred_address || '';
  document.getElementById('idPrivStyle').value = privRel.interaction_style || '';
  document.getElementById('idPrivBound').value = _joinCsv(privRel.boundaries);
  document.getElementById('idPrivInterests').value = _joinCsv(privTraits.interests || priv.interests);
  document.getElementById('idPrivExpertise').value = _joinCsv(privTraits.expertise);
  document.getElementById('idPrivSchedule').value = _joinCsv(privTraits.schedule_habits);
  document.getElementById('idPrivStates').value = _joinCsv((priv.recent_states || []).map(s => s.text));
  modal.dataset.rawPub = JSON.stringify(pub);
  modal.dataset.rawPriv = JSON.stringify(priv);
  modal.classList.remove('hidden');
}

function _buildEditedStates(rawList, inputTexts) {
  const now = new Date();
  const expDefault = new Date(now.getTime() + 10 * 86400 * 1000).toISOString();
  const byText = new Map((rawList || []).map(s => [s.text, s]));
  return inputTexts.map(txt => byText.get(txt) || { text: txt, created_at: now.toISOString(), expires_at: expDefault });
}

export function initIdentityModal() {
  const form = document.getElementById('identityForm');
  if (!form) return;
  form.addEventListener('submit', async e => {
    e.preventDefault();
    const modal = document.getElementById('identityModal');
    const qq = Number(document.getElementById('idEditQq').value);
    const oldPub = JSON.parse(modal.dataset.rawPub || '{}');
    const oldPriv = JSON.parse(modal.dataset.rawPriv || '{}');

    const candAllow = new Set(_splitCsv(document.getElementById('idPubCandidates').value));
    const keptCands = (oldPub.candidate_clues || []).filter(c => candAllow.has(c.value));

    const publicProfile = {
      relationship: {
        preferred_address: document.getElementById('idPubAddr').value.trim(),
        interaction_style: document.getElementById('idPubStyle').value.trim(),
        boundaries: _splitCsv(document.getElementById('idPubBound').value),
      },
      stable_traits: {
        interests: _splitCsv(document.getElementById('idPubInterests').value),
        expertise: _splitCsv(document.getElementById('idPubExpertise').value),
        schedule_habits: _splitCsv(document.getElementById('idPubSchedule').value),
      },
      candidate_clues: keptCands,
      recent_states: _buildEditedStates(oldPub.recent_states, _splitCsv(document.getElementById('idPubStates').value)),
    };
    const privateProfile = {
      relationship: {
        preferred_address: document.getElementById('idPrivAddr').value.trim(),
        interaction_style: document.getElementById('idPrivStyle').value.trim(),
        boundaries: _splitCsv(document.getElementById('idPrivBound').value),
      },
      stable_traits: {
        interests: _splitCsv(document.getElementById('idPrivInterests').value),
        expertise: _splitCsv(document.getElementById('idPrivExpertise').value),
        schedule_habits: _splitCsv(document.getElementById('idPrivSchedule').value),
      },
      candidate_clues: oldPriv.candidate_clues || [],
      recent_states: _buildEditedStates(oldPriv.recent_states, _splitCsv(document.getElementById('idPrivStates').value)),
    };

    await api(`/api/memory/identities/${qq}`, {
      method: 'PATCH',
      body: JSON.stringify({
        nickname: document.getElementById('idNickname').value.trim(),
        relationship_level: parseInt(document.getElementById('idRelLevel').value || '0', 10),
        public_profile: publicProfile,
        private_profile: privateProfile,
      }),
    });
    toast(`已更新 QQ ${qq} 的四维画像`);
    closeIdentityModal();
    loadUnifiedMemory();
  });
}

export async function loadMemoryTables() {
  const [ids, amb, slangs] = await Promise.all([
    api('/api/memory/identities'),
    api('/api/memory/ambient'),
    api('/api/memory/expressions'),
  ]);
  if (!ids.length) {
    replaceChildren('identityTable', el('tr', {}, el('td', { colSpan: 5, className: 'empty-state' }, '暂无 QQ 身份记录')));
  } else {
    replaceChildren(
      'identityTable',
      ids.map(i => el(
        'tr',
        {},
        el('td', {}, el('code', {}, i.qq_id)),
        el('td', {}, el('strong', {}, i.nickname || '-')),
        el('td', {}, el('div', { className: 'pill-list' }, ...(i.known_names && i.known_names.length ? i.known_names.map(n => badge(n, 'info')) : ['-']))),
        el('td', {}, `${Number(i.relationship_level)} `, badge(i.relationship_tier_label || '初识', i.relationship_tier === 'close_pal' ? 'ok' : (i.relationship_tier === 'acquaintance' ? 'info' : 'warn'))),
        el(
          'td',
          {},
          el(
            'div',
            { className: 'row-actions' },
            el('button', { className: 'btn sm secondary', type: 'button', onClick: () => openIdentityModal(i) }, '编辑画像'),
            el('button', { className: 'btn sm danger', type: 'button', onClick: () => forgetUser(Number(i.qq_id)) }, '一键忘却'),
          ),
        ),
      )),
    );
  }

  if (!amb.length) {
    replaceChildren('ambientBox', el('div', { className: 'empty-state' }, '暂无边缘旁听摘要'));
  } else {
    replaceChildren(
      'ambientBox',
      amb.map(a => el(
        'div',
        { style: 'padding:10px 0;border-bottom:1px solid var(--border-soft);' },
        el('div', { style: 'display:flex;justify-content:space-between;margin-bottom:4px;' }, el('strong', {}, `群 ${a.group_id}`), badge(`${Number(a.message_count)} 条沉淀`, 'info')),
        el('div', { style: 'font-size:13px;color:var(--ink2);' }, a.summary_text),
      )),
    );
  }

  if (!slangs.length) {
    replaceChildren('slangBox', el('div', { className: 'empty-state' }, '暂无群黑话'));
  } else {
    replaceChildren(
      'slangBox',
      slangs.map(s => el(
        'div',
        { style: 'padding:10px 0;border-bottom:1px solid var(--border-soft);display:flex;justify-content:space-between;gap:8px;align-items:center;' },
        el(
          'div',
          {},
          el('strong', {}, s.phrase),
          ' ',
          badge(labelOf(STATUS_LABELS, s.status), s.status === 'approved' ? 'ok' : 'warn'),
          el('div', { style: 'font-size:12px;color:var(--mute);' }, s.meaning),
        ),
        s.status === 'pending'
          ? el('button', { className: 'btn sm secondary', type: 'button', onClick: () => approveSlang(Number(s.id)) }, '通过')
          : null,
      )),
    );
  }
}

export function switchMemoryView(view) {
  document.querySelectorAll('.seg-btn[data-mview]').forEach(b => b.classList.toggle('active', b.dataset.mview === view));
  ['graph', 'identities', 'ambient'].forEach(v => document.getElementById('mview-' + v).classList.toggle('hidden', v !== view));
  if (view === 'graph') loadConsciousnessSea(false);
  if (view === 'identities' || view === 'ambient') loadMemoryTables();
}

export function loadUnifiedMemory() {
  loadConsciousnessSea(false);
  loadMemoryTables();
}

export function seaZoom(factor) {
  SEA.zoom = Math.max(0.4, Math.min(2.6, SEA.zoom * factor));
}

export function seaResetView() {
  SEA.camX = 0;
  SEA.camY = 0;
  SEA.zoom = window.innerWidth < 768 ? 0.78 : 1.0;
}

function renderInspector(node) {
  const d = node.detail || {};
  const relEdges = SEA.edges.filter(e => e.source.id === node.id || e.target.id === node.id);
  const relNodes = relEdges.length
    ? relEdges.map(e => el(
      'div',
      { className: 'sea-kv' },
      el('span', {}, e.source.id === node.id ? `→ ${e.target.label}` : `← ${e.source.label}`),
      el('span', { style: 'color:#818cf8;' }, e.relation),
    ))
    : [el('div', { style: 'color:#64748b;' }, '暂无关联')];

  const extraNodes = [];
  if (node.layer === 'L4_identity') {
    const names = (d.known_names || []).map(x => badge(x, 'info'));
    extraNodes.push(
      el(
        'div',
        { style: 'margin:10px 0;' },
        el('div', { style: 'color:#94a3b8;font-size:11px;margin-bottom:4px;' }, '历史名片'),
        el('div', { className: 'pill-list' }, ...(names.length ? names : [el('span', { style: 'color:#64748b;' }, '无')])),
      ),
    );
    const pub = d.public_profile || {};
    const pubTraits = pub.stable_traits || {};
    const pubRel = pub.relationship || {};
    const traitList = [
      ...(pubTraits.interests || pub.interests || []),
      ...(pubTraits.expertise || []),
      ...(pubTraits.schedule_habits || []),
    ];
    if (pubRel.interaction_style || pubRel.preferred_address) {
      extraNodes.push(
        el(
          'div',
          { style: 'margin:8px 0;font-size:12px;color:#cbd5e1;' },
          `互动分寸：${[pubRel.preferred_address && `称呼:${pubRel.preferred_address}`, pubRel.interaction_style].filter(Boolean).join(' · ')}`,
        ),
      );
    }
    if (traitList.length) {
      extraNodes.push(
        el(
          'div',
          { style: 'margin:8px 0;' },
          el('div', { style: 'color:#94a3b8;font-size:11px;margin-bottom:4px;' }, '公开稳定特征'),
          el('div', { className: 'pill-list' }, ...traitList.map(x => badge(x, 'ok'))),
        ),
      );
    }
    if ((pub.recent_states || []).length) {
      extraNodes.push(
        el(
          'div',
          { style: 'margin:8px 0;' },
          el('div', { style: 'color:#94a3b8;font-size:11px;margin-bottom:4px;' }, '近期状态（带TTL）'),
          el('div', { className: 'pill-list' }, ...pub.recent_states.map(s => badge(s.text, 'warn'))),
        ),
      );
    }
    if (d.has_private_isolation) {
      extraNodes.push(el('div', { style: 'margin:8px 0;' }, badge('🔒 含私聊半隔离档案（群聊不可见）', 'err')));
    }
    if (d.qq_id) {
      extraNodes.push(
        el(
          'div',
          { style: 'margin:10px 0;display:grid;grid-template-columns:1fr 1fr;gap:8px;' },
          el('button', { className: 'btn sm secondary', type: 'button', onClick: () => openIdentityModal(d) }, '编辑四维画像'),
          el('button', { className: 'btn sm danger', type: 'button', onClick: () => forgetUser(Number(d.qq_id)) }, '一键忘却'),
        ),
      );
    }
  } else if (d.content || d.summary_text) {
    extraNodes.push(
      el('div', { style: 'background:rgba(255,255,255,.06);padding:10px;border-radius:9px;margin:10px 0;color:#e2e8f0;line-height:1.5;' }, d.content || d.summary_text),
    );
  }

  replaceChildren(
    'seaInspector',
    el('div', { style: 'margin-bottom:6px;' }, badge(LAYER_NAMES[node.layer] || node.layer, 'info')),
    el('h4', {}, node.label),
    el('p', { style: 'color:#cbd5e1;margin:0 0 8px;' }, node.summary || ''),
    ...extraNodes,
    el('div', { style: 'font-size:11.5px;color:#94a3b8;margin:10px 0 4px;font-weight:600;' }, '关联脉络'),
    el('div', {}, ...relNodes),
  );
}

function startSeaLoop() {
  if (SEA.raf) cancelAnimationFrame(SEA.raf);
  const canvas = document.getElementById('seaCanvas');
  if (!canvas) return;
  const ctx = canvas.getContext('2d');
  const stars = Array.from({ length: 75 }, (_, i) => ({ x: ((i * 137.5) % 1000) / 1000, y: ((i * 293.3) % 1000) / 1000, r: 0.5 + (i % 3) * 0.55 }));

  function step() {
    const rect = canvas.getBoundingClientRect();
    if (rect.width > 0 && rect.height > 0) {
      const dpr = window.devicePixelRatio || 1;
      if (canvas.width !== Math.round(rect.width * dpr) || canvas.height !== Math.round(rect.height * dpr)) {
        canvas.width = Math.round(rect.width * dpr);
        canvas.height = Math.round(rect.height * dpr);
      }
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      const W = rect.width;
      const H = rect.height;
      ctx.clearRect(0, 0, W, H);
      SEA.tick += 0.014;
      for (const s of stars) {
        ctx.fillStyle = `rgba(199,210,254,${0.14 + 0.16 * Math.sin(SEA.tick + s.x * 11)})`;
        ctx.beginPath();
        ctx.arc(s.x * W, s.y * H, s.r, 0, Math.PI * 2);
        ctx.fill();
      }
      const vis = SEA.nodes.filter(n => SEA.visibleLayers.has(n.layer));
      for (let i = 0; i < vis.length; i++) {
        const a = vis[i];
        if (a === SEA.dragNode || a.id === 'self_core') continue;
        a.vx += -a.x * 0.0008;
        a.vy += -a.y * 0.0008;
        for (let j = i + 1; j < vis.length; j++) {
          const b = vis[j];
          const dx = a.x - b.x;
          const dy = a.y - b.y;
          const dist = Math.hypot(dx, dy) || 1;
          if (dist < 135) {
            const f = (135 - dist) * 0.0035;
            const fx = (dx / dist) * f;
            const fy = (dy / dist) * f;
            a.vx += fx;
            a.vy += fy;
            if (b !== SEA.dragNode && b.id !== 'self_core') {
              b.vx -= fx;
              b.vy -= fy;
            }
          }
        }
      }
      for (const e of SEA.edges) {
        if (!SEA.visibleLayers.has(e.source.layer) || !SEA.visibleLayers.has(e.target.layer)) continue;
        const dx = e.target.x - e.source.x;
        const dy = e.target.y - e.source.y;
        const dist = Math.hypot(dx, dy) || 1;
        const f = (dist - 120) * 0.0022;
        const fx = (dx / dist) * f;
        const fy = (dy / dist) * f;
        if (e.source !== SEA.dragNode && e.source.id !== 'self_core') {
          e.source.vx += fx;
          e.source.vy += fy;
        }
        if (e.target !== SEA.dragNode && e.target.id !== 'self_core') {
          e.target.vx -= fx;
          e.target.vy -= fy;
        }
      }
      for (const n of vis) {
        if (n === SEA.dragNode || n.id === 'self_core') continue;
        n.vx *= 0.86;
        n.vy *= 0.86;
        n.x += n.vx;
        n.y += n.vy;
      }
      ctx.save();
      ctx.translate(W / 2 + SEA.camX, H / 2 + SEA.camY);
      ctx.scale(SEA.zoom, SEA.zoom);
      for (const e of SEA.edges) {
        if (!SEA.visibleLayers.has(e.source.layer) || !SEA.visibleLayers.has(e.target.layer)) continue;
        const isPrivate = (e.relation || '').includes('私密');
        const hi = SEA.selected && (SEA.selected.id === e.source.id || SEA.selected.id === e.target.id);
        ctx.strokeStyle = isPrivate ? 'rgba(244,63,94,0.6)' : (hi ? 'rgba(129,140,248,0.82)' : 'rgba(148,163,184,0.24)');
        ctx.lineWidth = hi ? 1.9 : 1.1;
        ctx.setLineDash(isPrivate ? [4, 4] : []);
        ctx.beginPath();
        ctx.moveTo(e.source.x, e.source.y);
        ctx.lineTo(e.target.x, e.target.y);
        ctx.stroke();
        ctx.setLineDash([]);
        const t = (SEA.tick * 0.35 + (e.source.x + e.target.y) * 0.002) % 1;
        const tt = t < 0 ? t + 1 : t;
        ctx.fillStyle = isPrivate ? '#fb7185' : '#a5b4fc';
        ctx.beginPath();
        ctx.arc(e.source.x + (e.target.x - e.source.x) * tt, e.source.y + (e.target.y - e.source.y) * tt, 2.1, 0, Math.PI * 2);
        ctx.fill();
      }
      for (const n of vis) {
        const col = LAYER_COLORS[n.layer] || '#60a5fa';
        const sel = SEA.selected && SEA.selected.id === n.id;
        const grad = ctx.createRadialGradient(n.x, n.y, 1, n.x, n.y, n.size * 2.1);
        grad.addColorStop(0, col);
        grad.addColorStop(1, 'rgba(0,0,0,0)');
        ctx.fillStyle = grad;
        ctx.beginPath();
        ctx.arc(n.x, n.y, n.size * 2.1, 0, Math.PI * 2);
        ctx.fill();
        ctx.fillStyle = col;
        ctx.beginPath();
        ctx.arc(n.x, n.y, n.size * 0.56, 0, Math.PI * 2);
        ctx.fill();
        if (sel) {
          ctx.strokeStyle = '#fff';
          ctx.lineWidth = 2.4;
          ctx.beginPath();
          ctx.arc(n.x, n.y, n.size * 0.82, 0, Math.PI * 2);
          ctx.stroke();
        }
        ctx.fillStyle = '#f8fafc';
        ctx.font = `${n.id === 'self_core' ? '700 12.5px' : '500 11.5px'} -apple-system, sans-serif`;
        ctx.textAlign = 'center';
        ctx.fillText(n.label, n.x, n.y + n.size * 0.56 + 15);
      }
      ctx.restore();
    }
    SEA.raf = requestAnimationFrame(step);
  }
  step();
}

export async function loadConsciousnessSea(forceDemo = false) {
  let data = forceDemo ? makeDemoSea() : await api('/api/memory/graph');
  if (!forceDemo && (!data.nodes || data.nodes.length <= 1)) {
    data = makeDemoSea();
    document.getElementById('seaStats').textContent = '当前库内记忆较少，已展示演示星图 · 有真实对话后自动切换';
  } else {
    document.getElementById('seaStats').textContent = `节点 ${data.nodes.length} · 关联 ${data.edges.length} · 点击任意星点探索`;
  }
  const n = data.nodes.length;
  SEA.nodes = data.nodes.map((node, i) => {
    const angle = (i / Math.max(1, n)) * Math.PI * 2;
    const radius = node.id === 'self_core' ? 0 : (node.layer === 'group' ? 115 : 190 + (i % 3) * 42);
    return Object.assign({}, node, { x: Math.cos(angle) * radius, y: Math.sin(angle) * radius, vx: 0, vy: 0 });
  });
  const byId = Object.fromEntries(SEA.nodes.map(x => [x.id, x]));
  SEA.edges = data.edges.map(e => ({ source: byId[e.source], target: byId[e.target], relation: e.relation })).filter(e => e.source && e.target);
  SEA.selected = SEA.nodes[0] || null;
  if (SEA.selected) renderInspector(SEA.selected);
  seaResetView();
  startSeaLoop();
}

export function initSeaControls() {
  document.querySelectorAll('#seaFilters .chip').forEach(btn => {
    btn.addEventListener('click', () => {
      const l = btn.dataset.layer;
      if (SEA.visibleLayers.has(l)) {
        SEA.visibleLayers.delete(l);
        btn.classList.remove('on');
      } else {
        SEA.visibleLayers.add(l);
        btn.classList.add('on');
      }
    });
  });

  const canvas = document.getElementById('seaCanvas');
  if (!canvas) return;
  const toWorld = (cx, cy) => {
    const r = canvas.getBoundingClientRect();
    return { x: (cx - r.left - r.width / 2 - SEA.camX) / SEA.zoom, y: (cy - r.top - r.height / 2 - SEA.camY) / SEA.zoom };
  };
  const hitTest = (wx, wy) => SEA.nodes.find(n => SEA.visibleLayers.has(n.layer) && Math.hypot(n.x - wx, n.y - wy) <= Math.max(18, n.size * 0.9));

  canvas.addEventListener('pointerdown', e => {
    canvas.setPointerCapture(e.pointerId);
    const w = toWorld(e.clientX, e.clientY);
    const hit = hitTest(w.x, w.y);
    SEA.lastX = e.clientX;
    SEA.lastY = e.clientY;
    if (hit) {
      SEA.dragNode = hit;
      SEA.selected = hit;
      renderInspector(hit);
    } else {
      SEA.panning = true;
    }
  });
  canvas.addEventListener('pointermove', e => {
    if (SEA.dragNode) {
      const w = toWorld(e.clientX, e.clientY);
      SEA.dragNode.x = w.x;
      SEA.dragNode.y = w.y;
      SEA.dragNode.vx = 0;
      SEA.dragNode.vy = 0;
    } else if (SEA.panning) {
      SEA.camX += e.clientX - SEA.lastX;
      SEA.camY += e.clientY - SEA.lastY;
      SEA.lastX = e.clientX;
      SEA.lastY = e.clientY;
    }
  });
  canvas.addEventListener('pointerup', () => {
    SEA.dragNode = null;
    SEA.panning = false;
  });
  canvas.addEventListener('wheel', e => {
    e.preventDefault();
    seaZoom(e.deltaY < 0 ? 1.1 : 0.91);
  }, { passive: false });
}
