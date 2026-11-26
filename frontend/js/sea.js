import { STATUS_LABELS, api, badge, el, formatLocalTime, labelOf, replaceChildren, toast } from './api.js';

const LAYER_COLORS = {
  L5_self: '#38bdf8',
  group: '#60a5fa',
  L4_identity: '#34d399',
  L2_ambient: '#22d3ee',
  L3_public_episode: '#818cf8',
  L3_private_episode: '#fb7185',
  L4_slang: '#fb923c',
};

const LAYER_NAMES = {
  L5_self: 'L5 自我核心',
  group: '群聊记忆域',
  L4_identity: 'L4 熟人名片',
  L2_ambient: 'L2 边缘旁听',
  L3_public_episode: 'L3 群情景记忆',
  L3_private_episode: 'L3 私聊半隔离',
  L4_slang: 'L4 群黑话',
};

const PEOPLE_VIEW_LAYERS = new Set(['L5_self', 'L4_identity', 'L3_public_episode', 'L3_private_episode']);
const ALL_VIEW_LAYERS = new Set(Object.keys(LAYER_COLORS));

const SEA = {
  nodes: [],
  edges: [],
  byId: {},
  visibleLayers: new Set(PEOPLE_VIEW_LAYERS),
  viewMode: 'people', // 'people' | 'all'
  mode3d: true,
  autoRotate: true,
  includeArchived: false,
  groupFilter: 'all',
  searchQuery: '',
  isDemo: false,
  // 3D Orbit Camera state (smoothly damped toward goal*)
  yaw: 0.35,
  pitch: 0.28,
  distance: 620,
  targetX: 0,
  targetY: 0,
  targetZ: 0,
  goalYaw: 0.35,
  goalPitch: 0.28,
  goalDistance: 620,
  goalTargetX: 0,
  goalTargetY: 0,
  goalTargetZ: 0,
  fov: 680,
  // Interaction state
  selected: null,
  focusedPersonId: null,
  hoveredId: null,
  pointerMode: null, // 'orbit' | 'pan'
  lastX: 0,
  lastY: 0,
  moved: false,
  raf: null,
  tick: 0,
  inspectorOpen: true,
};

function makeDemoSea() {
  return {
    nodes: [
      {
        id: 'self_core',
        label: 'EchoSea · 意识核心',
        layer: 'L5_self',
        size: 28,
        summary: '精力 88 · 心情 relaxed · 晚间吹水看群',
        detail: {
          energy_level: 88,
          mood: 'relaxed',
          current_activity: '晚间吹水看群',
          diary_entry: '今晚群里大家聊得很热闹，阿明又在安利烧烤摊。',
        },
      },
      {
        id: 'group_101',
        label: '群 101 · 摸鱼主群',
        layer: 'group',
        size: 21,
        summary: '活跃群聊记忆域 · 4 位熟人',
        detail: { group_id: 101 },
      },
      {
        id: 'group_202',
        label: '群 202 · 深夜游戏房',
        layer: 'group',
        size: 19,
        summary: '开黑与技术闲聊群',
        detail: { group_id: 202 },
      },
      {
        id: 'qq_10086',
        label: '阿明',
        layer: 'L4_identity',
        size: 20,
        summary: 'QQ 10086 · 老熟人(82)',
        detail: {
          qq_id: 10086,
          nickname: '阿明',
          group_cards: { '101': '摸鱼明', '202': '打野阿明' },
          known_names: ['阿明', '摸鱼明', '打野阿明'],
          relationship_level: 82,
          relationship_tier: 'close_pal',
          relationship_tier_label: '老熟人',
          public_profile: {
            relationship: { preferred_address: '阿明', interaction_style: '爱互损吐槽', boundaries: ['不聊周末加班'] },
            stable_traits: { interests: ['原神', '炭火烧烤', '机械键盘'], expertise: ['Go后端'], schedule_habits: ['夜猫子'] },
            recent_states: [{ text: '这周在赶发版' }],
          },
          private_profile: {
            relationship: { preferred_address: '明哥', interaction_style: '私下聊得更走心', boundaries: [] },
            stable_traits: { interests: ['胶片摄影'], expertise: [], schedule_habits: [] },
            recent_states: [{ text: '考虑年底换工作' }],
          },
          has_private_isolation: true,
          episode_count: 3,
          private_episode_count: 1,
          ambient_count: 1,
        },
      },
      {
        id: 'qq_20001',
        label: '红姐',
        layer: 'L4_identity',
        size: 18,
        summary: 'QQ 20001 · 老熟人(64)',
        detail: {
          qq_id: 20001,
          nickname: '红姐',
          group_cards: { '101': '露营红姐' },
          known_names: ['小红', '红姐'],
          relationship_level: 64,
          relationship_tier: 'close_pal',
          relationship_tier_label: '老熟人',
          public_profile: {
            relationship: { preferred_address: '红姐', interaction_style: '温暖接梗', boundaries: [] },
            stable_traits: { interests: ['露营', '吸猫', '手冲咖啡'], expertise: ['UI设计'], schedule_habits: ['早起党'] },
            recent_states: [{ text: '周末准备去安吉露营' }],
          },
          private_profile: {},
          has_private_isolation: false,
          episode_count: 2,
          private_episode_count: 0,
          ambient_count: 1,
        },
      },
      {
        id: 'qq_30005',
        label: '林深',
        layer: 'L4_identity',
        size: 16,
        summary: 'QQ 30005 · 熟脸(28)',
        detail: {
          qq_id: 30005,
          nickname: '林深',
          group_cards: { '202': 'LinShen' },
          known_names: ['林深', 'LinShen'],
          relationship_level: 28,
          relationship_tier: 'acquaintance',
          relationship_tier_label: '熟脸',
          public_profile: {
            relationship: { preferred_address: '林深', interaction_style: '偏好正经聊技术', boundaries: ['不喜欢被催更'] },
            stable_traits: { interests: ['独立游戏', 'Synthwave'], expertise: ['Rust', '图形学'], schedule_habits: ['凌晨出没'] },
            recent_states: [{ text: '在做个人小游戏Demo' }],
          },
          private_profile: {},
          has_private_isolation: false,
          episode_count: 1,
          private_episode_count: 0,
          ambient_count: 0,
        },
      },
      {
        id: 'qq_40012',
        label: '柚子',
        layer: 'L4_identity',
        size: 14,
        summary: 'QQ 40012 · 初识(8)',
        detail: {
          qq_id: 40012,
          nickname: '柚子',
          group_cards: { '101': '冰镇柚子' },
          known_names: ['柚子'],
          relationship_level: 8,
          relationship_tier: 'stranger',
          relationship_tier_label: '初识',
          public_profile: {
            relationship: { preferred_address: '柚子', interaction_style: '轻松礼貌', boundaries: [] },
            stable_traits: { interests: ['二次元', '奶茶测评'], expertise: [], schedule_habits: [] },
            recent_states: [],
          },
          private_profile: {},
          has_private_isolation: false,
          episode_count: 1,
          private_episode_count: 0,
          ambient_count: 0,
        },
      },
      {
        id: 'ep_1',
        label: '五排连胜之夜',
        layer: 'L3_public_episode',
        size: 17,
        summary: '昨晚和阿明、林深五排连胜五把，最后一把逆风翻盘全群刷屏庆祝。',
        detail: {
          id: 1,
          scope: 'public_group',
          group_id: 202,
          qq_id: 10086,
          title: '五排连胜之夜',
          content: '昨晚和阿明、林深五排连胜五把，最后一把逆风翻盘全群刷屏庆祝，阿明立誓下次请大家吃烧烤。',
          importance: 0.92,
          is_anniversary: true,
          is_archived: false,
          recall_count: 7,
          created_at: new Date(Date.now() - 86400 * 1000 * 2).toISOString(),
        },
      },
      {
        id: 'ep_2',
        label: '私聊吐露换工作焦虑',
        layer: 'L3_private_episode',
        size: 16,
        summary: '私聊时阿明提到年底想换工作但担心大环境，此记忆仅私聊可见、群聊严格隔离。',
        detail: {
          id: 2,
          scope: 'private_dm',
          group_id: null,
          qq_id: 10086,
          title: '私聊吐露换工作焦虑',
          content: '私聊时阿明提到年底想换工作，目前在悄悄刷题准备面试，希望在群里绝口不提这件事。',
          importance: 0.95,
          is_anniversary: false,
          is_archived: false,
          recall_count: 4,
          created_at: new Date(Date.now() - 86400 * 1000 * 4).toISOString(),
        },
      },
      {
        id: 'ep_3',
        label: '安利巷口炭火烧烤摊',
        layer: 'L3_public_episode',
        size: 14,
        summary: '阿明强烈推荐公司楼下新开的东北炭火烤串，说牛油小串和烤冷面一绝。',
        detail: {
          id: 3,
          scope: 'public_group',
          group_id: 101,
          qq_id: 10086,
          title: '安利巷口炭火烧烤摊',
          content: '阿明强烈推荐公司楼下新开的东北炭火烤串，说牛油小串和烤冷面一绝，约好发版后一起去。',
          importance: 0.72,
          is_anniversary: false,
          is_archived: false,
          recall_count: 2,
          created_at: new Date(Date.now() - 86400 * 1000 * 6).toISOString(),
        },
      },
      {
        id: 'ep_4',
        label: '晒家里橘猫拆家现场',
        layer: 'L3_public_episode',
        size: 15,
        summary: '红姐在群里发了三张橘猫把猫抓板咬成雪花的照片，群友笑称“装修队长”。',
        detail: {
          id: 4,
          scope: 'public_group',
          group_id: 101,
          qq_id: 20001,
          title: '晒家里橘猫拆家现场',
          content: '红姐在群里发了三张橘猫把猫抓板咬成雪花的照片，大家笑称这是“金牌拆迁队长”，红姐还顺手分享了新买的咖啡豆。',
          importance: 0.81,
          is_anniversary: false,
          is_archived: false,
          recall_count: 5,
          created_at: new Date(Date.now() - 86400 * 1000 * 3).toISOString(),
        },
      },
      {
        id: 'ep_5',
        label: '安吉秋日露营攻略',
        layer: 'L3_public_episode',
        size: 14,
        summary: '红姐整理了安吉营地清单与夜拍机位，准备这周末带天幕和手冲壶出发。',
        detail: {
          id: 5,
          scope: 'public_group',
          group_id: 101,
          qq_id: 20001,
          title: '安吉秋日露营攻略',
          content: '红姐整理了安吉营地清单与夜拍机位，准备这周末带天幕和手冲壶出发。',
          importance: 0.76,
          is_anniversary: false,
          is_archived: false,
          recall_count: 3,
          created_at: new Date(Date.now() - 86400 * 1000 * 1).toISOString(),
        },
      },
      {
        id: 'ep_6',
        label: '独立游戏着色器讨论',
        layer: 'L3_public_episode',
        size: 15,
        summary: '林深分享了用 Rust + WGPU 写的复古水波纹 Shader，效果非常惊艳。',
        detail: {
          id: 6,
          scope: 'public_group',
          group_id: 202,
          qq_id: 30005,
          title: '独立游戏着色器讨论',
          content: '林深分享了用 Rust + WGPU 写的复古水波纹 Shader，并贴出了帧率对比截图，大家都在催他发试玩版。',
          importance: 0.84,
          is_anniversary: false,
          is_archived: false,
          recall_count: 3,
          created_at: new Date(Date.now() - 86400 * 1000 * 5).toISOString(),
        },
      },
      {
        id: 'ep_7',
        label: '新入群奶茶红黑榜',
        layer: 'L3_public_episode',
        size: 13,
        summary: '柚子刚进群就分享了五家秋季新品奶茶测评，推荐三分糖去冰桂花乌龙。',
        detail: {
          id: 7,
          scope: 'public_group',
          group_id: 101,
          qq_id: 40012,
          title: '新入群奶茶红黑榜',
          content: '柚子刚进群就分享了五家秋季新品奶茶测评，力推三分糖去冰桂花乌龙。',
          importance: 0.65,
          is_anniversary: false,
          is_archived: false,
          recall_count: 1,
          created_at: new Date(Date.now() - 86400 * 1000 * 1).toISOString(),
        },
      },
      {
        id: 'amb_1',
        label: '边缘旁听 #12',
        layer: 'L2_ambient',
        size: 13,
        summary: '近两小时群内热议晚饭选麻辣烫还是炭火烧烤，最终烧烤微弱领先。',
        detail: {
          id: 12,
          group_id: 101,
          message_count: 24,
          summary_text: '近两小时群内热议晚饭选麻辣烫还是炭火烧烤，最终烧烤微弱领先。',
          window_end: new Date().toISOString(),
        },
      },
      {
        id: 'slang_1',
        label: '梗·寄了',
        layer: 'L4_slang',
        size: 11,
        summary: '事情办砸了或发版翻车 (approved)',
        detail: { id: 1, group_id: 101, phrase: '寄了', meaning: '事情办砸了或发版翻车', status: 'approved', usage_count: 19 },
      },
    ],
    edges: [
      { source: 'self_core', target: 'group_101', relation: '常驻群域' },
      { source: 'self_core', target: 'group_202', relation: '常驻群域' },
      { source: 'self_core', target: 'qq_10086', relation: '熟人羁绊' },
      { source: 'self_core', target: 'qq_20001', relation: '熟人羁绊' },
      { source: 'self_core', target: 'qq_30005', relation: '熟人羁绊' },
      { source: 'self_core', target: 'qq_40012', relation: '初识' },
      { source: 'qq_10086', target: 'group_101', relation: '群名片:摸鱼明' },
      { source: 'qq_10086', target: 'group_202', relation: '群名片:打野阿明' },
      { source: 'qq_20001', target: 'group_101', relation: '群名片:露营红姐' },
      { source: 'qq_30005', target: 'group_202', relation: '群名片:LinShen' },
      { source: 'qq_40012', target: 'group_101', relation: '群名片:冰镇柚子' },
      { source: 'qq_10086', target: 'ep_1', relation: '共同经历' },
      { source: 'qq_10086', target: 'ep_2', relation: '私密记忆(半隔离)' },
      { source: 'qq_10086', target: 'ep_3', relation: '共同经历' },
      { source: 'qq_20001', target: 'ep_4', relation: '共同经历' },
      { source: 'qq_20001', target: 'ep_5', relation: '共同经历' },
      { source: 'qq_30005', target: 'ep_6', relation: '共同经历' },
      { source: 'qq_40012', target: 'ep_7', relation: '共同经历' },
      { source: 'group_202', target: 'ep_1', relation: '群内情景' },
      { source: 'group_101', target: 'ep_3', relation: '群内情景' },
      { source: 'group_101', target: 'ep_4', relation: '群内情景' },
      { source: 'group_101', target: 'ep_5', relation: '群内情景' },
      { source: 'group_202', target: 'ep_6', relation: '群内情景' },
      { source: 'group_101', target: 'ep_7', relation: '群内情景' },
      { source: 'group_101', target: 'amb_1', relation: '旁听沉淀' },
      { source: 'qq_10086', target: 'amb_1', relation: '参与讨论' },
      { source: 'qq_20001', target: 'amb_1', relation: '参与讨论' },
      { source: 'group_101', target: 'slang_1', relation: '群黑话' },
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

async function patchEpisode(epId, patch, successMsg) {
  if (SEA.isDemo) {
    toast('当前为演示星图，真实记忆可保存修改');
    return;
  }
  await api(`/api/memory/episodes/${epId}`, { method: 'PATCH', body: JSON.stringify(patch) });
  toast(successMsg);
  await loadConsciousnessSea(false);
}

async function deleteEpisode(epId, title) {
  if (SEA.isDemo) {
    toast('当前为演示星图，真实记忆可删除');
    return;
  }
  if (!confirm(`确认永久删除情景记忆「${title || epId}」？`)) return;
  await api(`/api/memory/episodes/${epId}`, { method: 'DELETE' });
  toast('已删除该条记忆');
  await loadConsciousnessSea(false);
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
  // factor > 1 zooms in (reduces camera distance)
  SEA.goalDistance = Math.max(240, Math.min(1350, SEA.goalDistance / factor));
}

export function seaResetView() {
  SEA.focusedPersonId = null;
  SEA.goalTargetX = 0;
  SEA.goalTargetY = 0;
  SEA.goalTargetZ = 0;
  SEA.goalYaw = SEA.mode3d ? 0.35 : 0;
  SEA.goalPitch = SEA.mode3d ? 0.28 : 0;
  SEA.goalDistance = window.innerWidth < 768 ? 760 : 600;
  SEA.autoRotate = SEA.mode3d;
  syncToolbarState();
}

export function seaToggleDimension() {
  SEA.mode3d = !SEA.mode3d;
  if (!SEA.mode3d) {
    SEA.goalYaw = 0;
    SEA.goalPitch = 0;
    SEA.autoRotate = false;
  } else {
    SEA.goalYaw = 0.35;
    SEA.goalPitch = 0.28;
    SEA.autoRotate = !SEA.focusedPersonId;
  }
  syncToolbarState();
}

export function seaToggleAutoRotate() {
  if (!SEA.mode3d) {
    SEA.mode3d = true;
    SEA.goalPitch = 0.26;
  }
  SEA.autoRotate = !SEA.autoRotate;
  syncToolbarState();
}

export function seaSetViewMode(mode) {
  SEA.viewMode = mode === 'all' ? 'all' : 'people';
  SEA.visibleLayers = new Set(SEA.viewMode === 'all' ? ALL_VIEW_LAYERS : PEOPLE_VIEW_LAYERS);
  syncToolbarState();
}

export function seaToggleInspector() {
  SEA.inspectorOpen = !SEA.inspectorOpen;
  const panel = document.getElementById('seaInspector');
  if (panel) panel.classList.toggle('collapsed', !SEA.inspectorOpen);
  const btn = document.getElementById('seaInspectorToggle');
  if (btn) btn.textContent = SEA.inspectorOpen ? '收起详情' : '展开详情';
}

export function seaToggleArchived() {
  SEA.includeArchived = !SEA.includeArchived;
  syncToolbarState();
  loadConsciousnessSea(SEA.isDemo);
}

function syncToolbarState() {
  document.querySelectorAll('#seaFilters .chip[data-layer]').forEach(btn => {
    btn.classList.toggle('on', SEA.visibleLayers.has(btn.dataset.layer));
  });
  document.querySelectorAll('[data-sea-mode]').forEach(btn => {
    btn.classList.toggle('on', btn.dataset.seaMode === SEA.viewMode);
  });
  const dimBtn = document.getElementById('seaDimBtn');
  if (dimBtn) {
    dimBtn.textContent = SEA.mode3d ? '3D 星海' : '2D 平面';
    dimBtn.classList.toggle('on', SEA.mode3d);
  }
  const rotBtn = document.getElementById('seaRotateBtn');
  if (rotBtn) {
    rotBtn.textContent = SEA.autoRotate ? '自转中' : '已暂停';
    rotBtn.classList.toggle('on', SEA.autoRotate);
  }
  const archBtn = document.getElementById('seaArchivedBtn');
  if (archBtn) {
    archBtn.classList.toggle('on', SEA.includeArchived);
  }
}

function isNodeVisible(node) {
  if (!SEA.visibleLayers.has(node.layer)) return false;
  if (SEA.groupFilter !== 'all') {
    const gid = Number(SEA.groupFilter);
    if (node.id === 'self_core') return true;
    if (node.layer === 'group') return Number(node.detail?.group_id) === gid;
    if (node.layer === 'L4_identity') {
      const cards = node.detail?.group_cards || {};
      const inCards = Object.prototype.hasOwnProperty.call(cards, String(gid));
      const linked = SEA.edges.some(e => (e.source.id === node.id && e.target.id === `group_${gid}`) || (e.target.id === node.id && e.source.id === `group_${gid}`));
      return inCards || linked;
    }
    if (node.detail?.group_id != null) return Number(node.detail.group_id) === gid;
    if (node.detail?.qq_id) {
      const person = SEA.byId[`qq_${node.detail.qq_id}`];
      return person ? isNodeVisible(person) : false;
    }
    return false;
  }
  return true;
}

function getNeighborSet(nodeId) {
  const set = new Set([nodeId]);
  for (const e of SEA.edges) {
    if (e.source.id === nodeId) set.add(e.target.id);
    if (e.target.id === nodeId) set.add(e.source.id);
  }
  return set;
}

function focusNode(node, flyCamera = true) {
  if (!node) return;
  SEA.selected = node;
  if (node.layer === 'L4_identity') {
    SEA.focusedPersonId = node.id;
  } else if (node.detail?.qq_id && SEA.byId[`qq_${node.detail.qq_id}`]) {
    SEA.focusedPersonId = `qq_${node.detail.qq_id}`;
  } else if (node.id === 'self_core') {
    SEA.focusedPersonId = null;
  }
  if (flyCamera) {
    SEA.autoRotate = false;
    SEA.goalTargetX = node.x;
    SEA.goalTargetY = node.y;
    SEA.goalTargetZ = node.z;
    SEA.goalDistance = node.id === 'self_core' ? 580 : (node.layer === 'L4_identity' ? 370 : 340);
    if (SEA.mode3d && node.id !== 'self_core') {
      const angle = Math.atan2(node.x, node.z);
      SEA.goalYaw = angle;
      SEA.goalPitch = 0.22;
    }
    syncToolbarState();
  }
  if (!SEA.inspectorOpen) seaToggleInspector();
  renderInspector(node);
}

function layoutGalaxy3D(nodes, edges) {
  const byId = Object.fromEntries(nodes.map(n => [n.id, n]));
  const people = nodes.filter(n => n.layer === 'L4_identity');
  const groups = nodes.filter(n => n.layer === 'group');

  // 1. Self core at origin
  const core = byId.self_core;
  if (core) {
    core.x = 0;
    core.y = 0;
    core.z = 0;
  }

  // 2. People arranged in 3D golden-angle orbital shell (higher familiarity -> closer to core)
  const goldenAngle = Math.PI * (3 - Math.sqrt(5));
  people.sort((a, b) => Number(b.detail?.relationship_level || 0) - Number(a.detail?.relationship_level || 0));
  people.forEach((p, idx) => {
    const rel = Number(p.detail?.relationship_level || 0);
    const radius = Math.max(155, 285 - Math.min(110, rel * 1.15)) + (idx % 3) * 24;
    const theta = people.length <= 6
      ? (idx / Math.max(1, people.length)) * Math.PI * 2 + 0.4
      : idx * goldenAngle;
    const yWave = Math.sin(idx * 1.65 + 0.5) * 68;
    p.x = Math.cos(theta) * radius;
    p.z = Math.sin(theta) * radius;
    p.y = yWave;
    p._orbitRadius = radius;
  });

  // 3. Groups on a gently elevated/lowered 3D ring
  groups.forEach((g, idx) => {
    const theta = (idx / Math.max(1, groups.length)) * Math.PI * 2 + Math.PI / 4;
    const r = 215;
    g.x = Math.cos(theta) * r;
    g.z = Math.sin(theta) * r;
    g.y = idx % 2 === 0 ? -110 : 110;
  });

  // 4. Attach memory episodes as 3D satellites orbiting their linked person (or group)
  const personSatBuckets = new Map();
  const groupSatBuckets = new Map();
  const orphanMems = [];

  for (const n of nodes) {
    if (n.layer === 'L3_public_episode' || n.layer === 'L3_private_episode') {
      const pKey = n.detail?.qq_id ? `qq_${n.detail.qq_id}` : null;
      const gKey = n.detail?.group_id ? `group_${n.detail.group_id}` : null;
      if (pKey && byId[pKey]) {
        if (!personSatBuckets.has(pKey)) personSatBuckets.set(pKey, []);
        personSatBuckets.get(pKey).push(n);
      } else if (gKey && byId[gKey]) {
        if (!groupSatBuckets.has(gKey)) groupSatBuckets.set(gKey, []);
        groupSatBuckets.get(gKey).push(n);
      } else {
        orphanMems.push(n);
      }
    } else if (n.layer === 'L2_ambient' || n.layer === 'L4_slang') {
      const gKey = n.detail?.group_id ? `group_${n.detail.group_id}` : null;
      if (gKey && byId[gKey]) {
        if (!groupSatBuckets.has(gKey)) groupSatBuckets.set(gKey, []);
        groupSatBuckets.get(gKey).push(n);
      } else {
        orphanMems.push(n);
      }
    }
  }

  for (const [pKey, sats] of personSatBuckets.entries()) {
    const parent = byId[pKey];
    const baseAngle = Math.atan2(parent.z, parent.x);
    sats.forEach((sat, idx) => {
      const isPrivate = sat.layer === 'L3_private_episode';
      const spread = sats.length === 1 ? 0 : ((idx / (sats.length - 1)) - 0.5) * Math.min(2.6, sats.length * 0.78);
      const ang = baseAngle + spread;
      const r = 102 + (idx % 2) * 26;
      sat.x = parent.x + Math.cos(ang) * r;
      sat.z = parent.z + Math.sin(ang) * r;
      sat.y = parent.y + (isPrivate ? -56 : ((idx % 2 === 0 ? 1 : -1) * 34 + (idx - sats.length / 2) * 14));
      sat._ownerId = pKey;
    });
  }

  for (const [gKey, sats] of groupSatBuckets.entries()) {
    const parent = byId[gKey];
    sats.forEach((sat, idx) => {
      const ang = (idx / Math.max(1, sats.length)) * Math.PI * 2 + 0.3;
      const r = 88 + (idx % 2) * 20;
      sat.x = parent.x + Math.cos(ang) * r;
      sat.z = parent.z + Math.sin(ang) * r;
      sat.y = parent.y + (idx % 2 === 0 ? 30 : -30);
      sat._ownerId = gKey;
    });
  }

  orphanMems.forEach((sat, idx) => {
    const ang = (idx / Math.max(1, orphanMems.length)) * Math.PI * 2;
    sat.x = Math.cos(ang) * 180;
    sat.z = Math.sin(ang) * 180;
    sat.y = 85;
  });

  // Ensure every node has numeric coords
  for (const n of nodes) {
    if (typeof n.x !== 'number') { n.x = 0; n.y = 0; n.z = 0; }
  }
  return { byId, edges };
}

function buildPersonCardEl(node) {
  const d = node.detail || {};
  const pub = d.public_profile || {};
  const pubTraits = pub.stable_traits || {};
  const pubRel = pub.relationship || {};
  const tier = d.relationship_tier || 'stranger';
  const tierLabel = d.relationship_tier_label || '初识';
  const relScore = Number(d.relationship_level || 0);
  const initial = (node.label || '?').trim().slice(0, 1).toUpperCase();

  const tags = [
    ...(pubTraits.interests || pub.interests || []),
    ...(pubTraits.expertise || []),
    ...((pub.recent_states || []).map(s => s.text)),
  ].filter(Boolean).slice(0, 3);

  const epCount = Number(d.episode_count || 0);
  const privCount = Number(d.private_episode_count || 0);
  const subtitle = [pubRel.preferred_address && `称呼:${pubRel.preferred_address}`, pubRel.interaction_style].filter(Boolean).join(' · ') || `QQ ${d.qq_id || '-'}`;

  const card = el(
    'div',
    {
      className: `sea-card sea-card-person tier-${tier}`,
      dataset: { nodeId: node.id },
      onClick: e => {
        e.stopPropagation();
        focusNode(node, true);
      },
      onMouseenter: () => { SEA.hoveredId = node.id; },
      onMouseleave: () => { if (SEA.hoveredId === node.id) SEA.hoveredId = null; },
    },
    el(
      'div',
      { className: 'sea-card-head' },
      el('div', { className: `sea-avatar tier-${tier}` }, initial),
      el(
        'div',
        { className: 'sea-card-title-wrap' },
        el('div', { className: 'sea-card-name' }, node.label),
        el('div', { className: 'sea-card-sub' }, subtitle),
      ),
      el('span', { className: `sea-tier-pill tier-${tier}` }, `${tierLabel} ${relScore}`),
    ),
    tags.length
      ? el('div', { className: 'sea-card-tags' }, ...tags.map(t => el('span', { className: 'sea-tag' }, t)))
      : null,
    el(
      'div',
      { className: 'sea-card-foot' },
      el('span', { className: 'sea-mem-count' }, `记忆 ${epCount} 条`),
      privCount > 0 ? el('span', { className: 'sea-priv-pill' }, `🔒 ${privCount} 私密`) : null,
    ),
  );
  return card;
}

function buildMemoryCardEl(node) {
  const d = node.detail || {};
  const isPrivate = node.layer === 'L3_private_episode';
  const isAnniv = Boolean(d.is_anniversary);
  const isAmbient = node.layer === 'L2_ambient';
  const scopeText = isPrivate ? '🔒 私密半隔离' : (isAnniv ? '★ 纪念日' : (isAmbient ? '旁听摘要' : '群情景'));
  const summary = d.content || d.summary_text || node.summary || '';
  const imp = typeof d.importance === 'number' ? Math.round(d.importance * 100) : null;
  const recalls = Number(d.recall_count || 0);

  return el(
    'div',
    {
      className: `sea-card sea-card-memory ${isPrivate ? 'is-private' : ''} ${isAnniv ? 'is-anniv' : ''} ${isAmbient ? 'is-ambient' : ''}`,
      dataset: { nodeId: node.id },
      onClick: e => {
        e.stopPropagation();
        focusNode(node, true);
      },
      onMouseenter: () => { SEA.hoveredId = node.id; },
      onMouseleave: () => { if (SEA.hoveredId === node.id) SEA.hoveredId = null; },
    },
    el(
      'div',
      { className: 'sea-mem-head' },
      el('span', { className: `sea-scope-dot ${isPrivate ? 'priv' : (isAnniv ? 'anniv' : (isAmbient ? 'amb' : 'pub'))}` }, scopeText),
      imp !== null ? el('span', { className: 'sea-mem-score' }, `权重 ${imp}`) : null,
    ),
    el('div', { className: 'sea-mem-title' }, node.label),
    el('div', { className: 'sea-mem-excerpt' }, summary),
    (recalls > 0 || d.is_archived)
      ? el(
        'div',
        { className: 'sea-mem-meta' },
        recalls > 0 ? el('span', {}, `回忆 ${recalls} 次`) : null,
        d.is_archived ? el('span', { className: 'sea-arch-tag' }, '已归档') : null,
      )
      : null,
  );
}

function buildCoreCardEl(node) {
  const d = node.detail || {};
  return el(
    'div',
    {
      className: 'sea-card sea-card-core',
      dataset: { nodeId: node.id },
      onClick: e => {
        e.stopPropagation();
        focusNode(node, true);
      },
      onMouseenter: () => { SEA.hoveredId = node.id; },
      onMouseleave: () => { if (SEA.hoveredId === node.id) SEA.hoveredId = null; },
    },
    el('div', { className: 'sea-core-title' }, 'EchoSea · 意识核心'),
    el('div', { className: 'sea-core-sub' }, `精力 ${d.energy_level ?? 80} · ${d.current_activity || '摸鱼看群'}`),
  );
}

function buildCompactChipEl(node) {
  const isGroup = node.layer === 'group';
  return el(
    'div',
    {
      className: `sea-card sea-card-chip ${isGroup ? 'is-group' : 'is-slang'}`,
      dataset: { nodeId: node.id },
      onClick: e => {
        e.stopPropagation();
        focusNode(node, true);
      },
      onMouseenter: () => { SEA.hoveredId = node.id; },
      onMouseleave: () => { if (SEA.hoveredId === node.id) SEA.hoveredId = null; },
    },
    el('span', { className: 'sea-chip-dot', style: `background:${LAYER_COLORS[node.layer] || '#60a5fa'}` }),
    el('span', {}, node.label),
  );
}

function mountSeaCards() {
  const layer = document.getElementById('seaCardLayer');
  if (!layer) return;
  const frag = document.createDocumentFragment();
  for (const node of SEA.nodes) {
    let cardEl;
    if (node.layer === 'L4_identity') cardEl = buildPersonCardEl(node);
    else if (node.layer === 'L3_public_episode' || node.layer === 'L3_private_episode' || node.layer === 'L2_ambient') cardEl = buildMemoryCardEl(node);
    else if (node.layer === 'L5_self') cardEl = buildCoreCardEl(node);
    else cardEl = buildCompactChipEl(node);
    node._cardEl = cardEl;
    frag.appendChild(cardEl);
  }
  layer.replaceChildren(frag);
}

function renderInspector(node) {
  const d = node.detail || {};
  const relEdges = SEA.edges.filter(e => e.source.id === node.id || e.target.id === node.id);
  const relNodes = relEdges.length
    ? relEdges.map(e => {
      const other = e.source.id === node.id ? e.target : e.source;
      const dir = e.source.id === node.id ? '→' : '←';
      return el(
        'button',
        {
          className: 'sea-kv-btn',
          type: 'button',
          onClick: () => focusNode(other, true),
        },
        el('span', { className: 'sea-kv-name' }, `${dir} ${other.label}`),
        el('span', { className: 'sea-kv-rel' }, e.relation),
      );
    })
    : [el('div', { style: 'color:#64748b;font-size:12px;' }, '暂无关联')];

  const extraNodes = [];
  if (node.layer === 'L4_identity') {
    const names = (d.known_names || []).map(x => badge(x, 'info'));
    extraNodes.push(
      el(
        'div',
        { className: 'sea-ins-block' },
        el('div', { className: 'sea-ins-label' }, '历史名片'),
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
          { className: 'sea-ins-block' },
          el('div', { className: 'sea-ins-label' }, '互动分寸'),
          el('div', { style: 'font-size:12.5px;color:#e2e8f0;' }, [pubRel.preferred_address && `习惯称呼：${pubRel.preferred_address}`, pubRel.interaction_style].filter(Boolean).join(' · ')),
        ),
      );
    }
    if ((pubRel.boundaries || []).length) {
      extraNodes.push(
        el(
          'div',
          { className: 'sea-ins-block' },
          el('div', { className: 'sea-ins-label' }, '明确雷区'),
          el('div', { className: 'pill-list' }, ...pubRel.boundaries.map(x => badge(x, 'err'))),
        ),
      );
    }
    if (traitList.length) {
      extraNodes.push(
        el(
          'div',
          { className: 'sea-ins-block' },
          el('div', { className: 'sea-ins-label' }, '稳定特征'),
          el('div', { className: 'pill-list' }, ...traitList.map(x => badge(x, 'ok'))),
        ),
      );
    }
    if ((pub.recent_states || []).length) {
      extraNodes.push(
        el(
          'div',
          { className: 'sea-ins-block' },
          el('div', { className: 'sea-ins-label' }, '近期状态（带 TTL）'),
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
          { style: 'margin:12px 0;display:grid;grid-template-columns:1fr 1fr;gap:8px;' },
          el('button', { className: 'btn sm secondary', type: 'button', onClick: () => openIdentityModal(d) }, '编辑四维画像'),
          el('button', { className: 'btn sm danger', type: 'button', onClick: () => forgetUser(Number(d.qq_id)) }, '一键忘却'),
        ),
      );
    }
  } else if (node.layer === 'L3_public_episode' || node.layer === 'L3_private_episode') {
    extraNodes.push(
      el(
        'div',
        { className: 'pill-list', style: 'margin:8px 0;' },
        badge(d.scope === 'private_dm' ? '🔒 私聊半隔离' : '🌐 群聊共享', d.scope === 'private_dm' ? 'err' : 'info'),
        d.is_anniversary ? badge('★ 纪念日', 'warn') : null,
        typeof d.importance === 'number' ? badge(`重要度 ${Math.round(d.importance * 100)}%`, 'purple') : null,
        typeof d.recall_count === 'number' ? badge(`回忆 ${d.recall_count} 次`, 'ok') : null,
      ),
      el('div', { className: 'sea-ins-quote' }, d.content || node.summary || ''),
      d.created_at ? el('div', { style: 'font-size:11.5px;color:#94a3b8;margin-bottom:10px;' }, `沉淀时间：${formatLocalTime(d.created_at, true)}`) : null,
    );
    if (d.id) {
      extraNodes.push(
        el(
          'div',
          { style: 'margin:10px 0;display:flex;flex-wrap:wrap;gap:6px;' },
          el(
            'button',
            {
              className: 'btn sm secondary',
              type: 'button',
              onClick: () => patchEpisode(d.id, { is_anniversary: !d.is_anniversary }, d.is_anniversary ? '已取消纪念日标记' : '已设为纪念日'),
            },
            d.is_anniversary ? '取消纪念日' : '★ 设为纪念日',
          ),
          el(
            'button',
            {
              className: 'btn sm secondary',
              type: 'button',
              onClick: () => patchEpisode(d.id, { is_archived: !d.is_archived }, d.is_archived ? '已恢复记忆' : '已归档记忆'),
            },
            d.is_archived ? '取消归档' : '归档',
          ),
          el(
            'button',
            {
              className: 'btn sm danger',
              type: 'button',
              onClick: () => deleteEpisode(d.id, d.title || node.label),
            },
            '删除',
          ),
        ),
      );
    }
  } else if (d.content || d.summary_text || d.diary_entry) {
    extraNodes.push(
      el('div', { className: 'sea-ins-quote' }, d.content || d.summary_text || d.diary_entry),
    );
  }

  replaceChildren(
    'seaInspectorBody',
    el('div', { style: 'margin-bottom:6px;' }, badge(LAYER_NAMES[node.layer] || node.layer, node.layer === 'L3_private_episode' ? 'err' : 'info')),
    el('h4', {}, node.label),
    el('p', { style: 'color:#94a3b8;margin:0 0 8px;font-size:12px;' }, node.summary || ''),
    ...extraNodes,
    el('div', { className: 'sea-ins-label', style: 'margin-top:12px;' }, `关联脉络 (${relEdges.length})`),
    el('div', { className: 'sea-kv-list' }, ...relNodes),
  );
}

function projectPoint(wx, wy, wz, W, H, cosY, sinY, cosP, sinP) {
  const dx = wx - SEA.targetX;
  const dy = wy - SEA.targetY;
  const dz = wz - SEA.targetZ;
  // Rotate around Y (yaw)
  const rx = dx * cosY - dz * sinY;
  const rz1 = dx * sinY + dz * cosY;
  // Rotate around X (pitch)
  const ry = dy * cosP - rz1 * sinP;
  const rz = dy * sinP + rz1 * cosP;
  const depth = SEA.distance + rz;
  if (depth < 40) return null;
  const scale = SEA.fov / depth;
  return {
    sx: W / 2 + rx * scale,
    sy: H / 2 + ry * scale,
    scale,
    depth,
  };
}

function startSeaLoop() {
  if (SEA.raf) cancelAnimationFrame(SEA.raf);
  const canvas = document.getElementById('seaCanvas');
  if (!canvas) return;
  const ctx = canvas.getContext('2d');

  // 3D Parallax Starfield
  const stars = Array.from({ length: 160 }, (_, i) => {
    const phi = Math.acos(1 - 2 * ((i + 0.5) / 160));
    const theta = Math.PI * (1 + Math.sqrt(5)) * i;
    const r = 720 + (i % 5) * 90;
    return {
      x: r * Math.sin(phi) * Math.cos(theta),
      y: r * Math.cos(phi),
      z: r * Math.sin(phi) * Math.sin(theta),
      size: 0.7 + (i % 3) * 0.55,
      phase: i * 0.7,
    };
  });

  function step() {
    const rect = canvas.getBoundingClientRect();
    if (rect.width > 0 && rect.height > 0) {
      const dpr = Math.min(window.devicePixelRatio || 1, 2);
      if (canvas.width !== Math.round(rect.width * dpr) || canvas.height !== Math.round(rect.height * dpr)) {
        canvas.width = Math.round(rect.width * dpr);
        canvas.height = Math.round(rect.height * dpr);
      }
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      const W = rect.width;
      const H = rect.height;
      ctx.clearRect(0, 0, W, H);
      SEA.tick += 0.016;

      if (SEA.autoRotate && SEA.mode3d && !SEA.pointerMode) {
        SEA.goalYaw += 0.0018;
      }

      // Smooth camera damping
      const damp = 0.1;
      SEA.yaw += (SEA.goalYaw - SEA.yaw) * damp;
      SEA.pitch += (SEA.goalPitch - SEA.pitch) * damp;
      SEA.distance += (SEA.goalDistance - SEA.distance) * damp;
      SEA.targetX += (SEA.goalTargetX - SEA.targetX) * damp;
      SEA.targetY += (SEA.goalTargetY - SEA.targetY) * damp;
      SEA.targetZ += (SEA.goalTargetZ - SEA.targetZ) * damp;

      const cosY = Math.cos(SEA.yaw);
      const sinY = Math.sin(SEA.yaw);
      const cosP = Math.cos(SEA.pitch);
      const sinP = Math.sin(SEA.pitch);

      // 1. Subtle 3D Starfield
      for (const s of stars) {
        const p = projectPoint(s.x, s.y, s.z, W, H, cosY, sinY, cosP, sinP);
        if (!p || p.sx < -10 || p.sx > W + 10 || p.sy < -10 || p.sy > H + 10) continue;
        const alpha = (0.14 + 0.18 * Math.sin(SEA.tick * 1.4 + s.phase)) * Math.min(1, p.scale * 1.4);
        ctx.fillStyle = `rgba(186,230,253,${alpha.toFixed(3)})`;
        ctx.beginPath();
        ctx.arc(p.sx, p.sy, Math.max(0.6, s.size * p.scale * 1.1), 0, Math.PI * 2);
        ctx.fill();
      }

      // 2. Subtle 3D Sonar Reference Rings on the XZ plane (y = 0)
      for (const ringR of [165, 285]) {
        ctx.beginPath();
        let started = false;
        for (let i = 0; i <= 64; i++) {
          const a = (i / 64) * Math.PI * 2;
          const p = projectPoint(Math.cos(a) * ringR, 0, Math.sin(a) * ringR, W, H, cosY, sinY, cosP, sinP);
          if (!p) { started = false; continue; }
          if (!started) { ctx.moveTo(p.sx, p.sy); started = true; }
          else ctx.lineTo(p.sx, p.sy);
        }
        ctx.strokeStyle = ringR === 165 ? 'rgba(56,189,248,0.11)' : 'rgba(129,140,248,0.08)';
        ctx.lineWidth = 1;
        ctx.stroke();
      }

      // 3. Project visible nodes
      const activeId = SEA.hoveredId || (SEA.selected ? SEA.selected.id : null);
      const activeNeighbors = activeId ? getNeighborSet(activeId) : null;
      const focusedNeighbors = SEA.focusedPersonId ? getNeighborSet(SEA.focusedPersonId) : null;
      const q = SEA.searchQuery.trim().toLowerCase();

      const projected = new Map();
      const sortedVis = [];
      for (const n of SEA.nodes) {
        const vis = isNodeVisible(n);
        if (!vis) {
          if (n._cardEl) n._cardEl.style.display = 'none';
          continue;
        }
        const p = projectPoint(n.x, n.y, n.z, W, H, cosY, sinY, cosP, sinP);
        if (!p) {
          if (n._cardEl) n._cardEl.style.display = 'none';
          continue;
        }
        projected.set(n.id, p);
        sortedVis.push({ node: n, proj: p });
      }
      // Painter's algorithm: farthest first
      sortedVis.sort((a, b) => b.proj.depth - a.proj.depth);

      // 4. Draw 3D Curved Energy Arcs (Edges)
      for (const e of SEA.edges) {
        const ps = projected.get(e.source.id);
        const pt = projected.get(e.target.id);
        if (!ps || !pt) continue;
        const isPrivate = (e.relation || '').includes('私密');
        const isPersonMem = (e.source.layer === 'L4_identity' && e.target.layer.startsWith('L3_'))
          || (e.target.layer === 'L4_identity' && e.source.layer.startsWith('L3_'));
        const isHighlighted = activeId && (e.source.id === activeId || e.target.id === activeId);
        const isFocusLink = SEA.focusedPersonId && (e.source.id === SEA.focusedPersonId || e.target.id === SEA.focusedPersonId);
        const dimmed = (SEA.focusedPersonId && !isFocusLink && !isHighlighted);

        // Midpoint lifted slightly in 3D for graceful curved arc
        const mx = (e.source.x + e.target.x) / 2;
        const my = (e.source.y + e.target.y) / 2 - (isPersonMem ? 18 : 10);
        const mz = (e.source.z + e.target.z) / 2;
        const pm = projectPoint(mx, my, mz, W, H, cosY, sinY, cosP, sinP);

        ctx.save();
        if (isPrivate) {
          ctx.strokeStyle = dimmed ? 'rgba(251,113,133,0.16)' : (isHighlighted || isFocusLink ? 'rgba(251,113,133,0.88)' : 'rgba(251,113,133,0.48)');
          ctx.setLineDash([5, 5]);
        } else if (isHighlighted || isFocusLink) {
          ctx.strokeStyle = isPersonMem ? 'rgba(129,140,248,0.85)' : 'rgba(56,189,248,0.72)';
        } else {
          ctx.strokeStyle = dimmed ? 'rgba(148,163,184,0.07)' : (isPersonMem ? 'rgba(129,140,248,0.34)' : 'rgba(148,163,184,0.18)');
        }
        ctx.lineWidth = (isHighlighted || isFocusLink) ? 1.9 : (isPersonMem ? 1.3 : 0.9);
        ctx.beginPath();
        ctx.moveTo(ps.sx, ps.sy);
        if (pm) ctx.quadraticCurveTo(pm.sx, pm.sy, pt.sx, pt.sy);
        else ctx.lineTo(pt.sx, pt.sy);
        ctx.stroke();
        ctx.setLineDash([]);

        // Animated light pulse along highlighted or person-memory edges
        if (!dimmed && (isHighlighted || isFocusLink || isPersonMem)) {
          const t = (SEA.tick * 0.32 + (e.source.x * 0.001 + e.target.z * 0.001)) % 1;
          const tt = t < 0 ? t + 1 : t;
          const u = 1 - tt;
          const px = pm ? (u * u * ps.sx + 2 * u * tt * pm.sx + tt * tt * pt.sx) : (ps.sx + (pt.sx - ps.sx) * tt);
          const py = pm ? (u * u * ps.sy + 2 * u * tt * pm.sy + tt * tt * pt.sy) : (ps.sy + (pt.sy - ps.sy) * tt);
          ctx.fillStyle = isPrivate ? '#fda4af' : '#a5b4fc';
          ctx.beginPath();
          ctx.arc(px, py, isHighlighted || isFocusLink ? 2.6 : 1.9, 0, Math.PI * 2);
          ctx.fill();
        }
        ctx.restore();
      }

      // 5. Draw glowing 3D node anchors + sync 3D-projected HTML cards
      for (const { node: n, proj: p } of sortedVis) {
        const col = LAYER_COLORS[n.layer] || '#60a5fa';
        const isSel = SEA.selected && SEA.selected.id === n.id;
        const isMatch = q && (
          (n.label || '').toLowerCase().includes(q)
          || (n.summary || '').toLowerCase().includes(q)
          || String(n.detail?.qq_id || '').includes(q)
        );
        const isFocusRelevant = !SEA.focusedPersonId
          || n.id === SEA.focusedPersonId
          || (focusedNeighbors && focusedNeighbors.has(n.id));
        const isHoverRelevant = !activeNeighbors || activeNeighbors.has(n.id);
        const dimmed = (SEA.focusedPersonId && !isFocusRelevant) || (q && !isMatch);

        // Glowing 3D orb behind card anchor
        const r = Math.max(4, n.size * 0.42 * p.scale);
        const glowR = r * (n.id === 'self_core' ? 4.2 : 2.8);
        const grad = ctx.createRadialGradient(p.sx, p.sy, 0.5, p.sx, p.sy, glowR);
        grad.addColorStop(0, col);
        grad.addColorStop(1, 'rgba(0,0,0,0)');
        ctx.save();
        ctx.globalAlpha = dimmed ? 0.2 : 0.85;
        ctx.fillStyle = grad;
        ctx.beginPath();
        ctx.arc(p.sx, p.sy, glowR, 0, Math.PI * 2);
        ctx.fill();

        ctx.fillStyle = col;
        ctx.beginPath();
        ctx.arc(p.sx, p.sy, r, 0, Math.PI * 2);
        ctx.fill();

        if (isSel || isMatch) {
          ctx.strokeStyle = '#ffffff';
          ctx.lineWidth = 2;
          ctx.beginPath();
          ctx.arc(p.sx, p.sy, r + 4 + Math.sin(SEA.tick * 4) * 1.5, 0, Math.PI * 2);
          ctx.stroke();
        }
        ctx.restore();

        // Sync projected HTML card
        const card = n._cardEl;
        if (card) {
          const outOfBounds = p.sx < -160 || p.sx > W + 160 || p.sy < -120 || p.sy > H + 120;
          if (outOfBounds) {
            card.style.display = 'none';
          } else {
            card.style.display = '';
            const cardScale = Math.max(0.62, Math.min(1.12, p.scale * 0.92));
            const yOffset = n.layer === 'L5_self' ? 28 : 16;
            card.style.transform = `translate3d(${p.sx.toFixed(1)}px, ${(p.sy + yOffset * cardScale).toFixed(1)}px, 0) translate(-50%, 0) scale(${cardScale.toFixed(3)})`;
            card.style.zIndex = String(Math.max(1, Math.round(2000 - p.depth)));

            // LOD: expand memory summary cards when their owner is focused, selected, hovered, matched, or zoomed close
            const isMem = n.layer === 'L3_public_episode' || n.layer === 'L3_private_episode' || n.layer === 'L2_ambient';
            const expandMem = isMem && (
              isSel
              || SEA.hoveredId === n.id
              || isMatch
              || (SEA.focusedPersonId && n._ownerId === SEA.focusedPersonId)
              || p.scale > 1.22
            );
            card.classList.toggle('is-expanded', Boolean(expandMem));
            card.classList.toggle('is-selected', Boolean(isSel));
            card.classList.toggle('is-match', Boolean(isMatch));
            card.classList.toggle('is-dimmed', Boolean(dimmed || (!isHoverRelevant && SEA.hoveredId)));
          }
        }
      }
    }
    SEA.raf = requestAnimationFrame(step);
  }
  step();
}

function populateGroupFilter(nodes) {
  const sel = document.getElementById('seaGroupFilter');
  if (!sel) return;
  const groups = nodes.filter(n => n.layer === 'group');
  const prev = SEA.groupFilter;
  replaceChildren(
    sel,
    el('option', { value: 'all' }, `全部群域 (${groups.length})`),
    ...groups.map(g => el('option', { value: String(g.detail?.group_id || '') }, g.label)),
  );
  sel.value = groups.some(g => String(g.detail?.group_id) === prev) ? prev : 'all';
  SEA.groupFilter = sel.value;
}

export async function loadConsciousnessSea(forceDemo = false) {
  let data = forceDemo
    ? makeDemoSea()
    : await api(`/api/memory/graph?include_archived=${SEA.includeArchived ? 'true' : 'false'}`);
  const statEl = document.getElementById('seaStats');
  if (!forceDemo && (!data.nodes || data.nodes.length <= 1)) {
    data = makeDemoSea();
    SEA.isDemo = true;
    if (statEl) statEl.textContent = '演示模式 · 拖拽旋转 3D 星海 · 点击人物名片聚焦 TA 的记忆';
  } else {
    SEA.isDemo = forceDemo;
    const peopleCnt = data.nodes.filter(n => n.layer === 'L4_identity').length;
    const memCnt = data.nodes.filter(n => n.layer === 'L3_public_episode' || n.layer === 'L3_private_episode').length;
    if (statEl) {
      statEl.textContent = `${forceDemo ? '演示模式 · ' : ''}人物 ${peopleCnt} · 记忆 ${memCnt} · 关联 ${data.edges.length} · 拖拽旋转 / 滚轮缩放`;
    }
  }

  SEA.nodes = data.nodes.map(node => Object.assign({}, node));
  SEA.byId = Object.fromEntries(SEA.nodes.map(x => [x.id, x]));
  SEA.edges = data.edges
    .map(e => ({ source: SEA.byId[e.source], target: SEA.byId[e.target], relation: e.relation }))
    .filter(e => e.source && e.target);

  layoutGalaxy3D(SEA.nodes, SEA.edges);
  populateGroupFilter(SEA.nodes);
  mountSeaCards();
  syncToolbarState();

  const firstPerson = SEA.nodes.find(n => n.layer === 'L4_identity') || SEA.nodes[0] || null;
  SEA.selected = firstPerson;
  SEA.focusedPersonId = null;
  if (SEA.selected) renderInspector(SEA.selected);
  seaResetView();
  startSeaLoop();
}

export function initSeaControls() {
  document.querySelectorAll('#seaFilters .chip[data-layer]').forEach(btn => {
    btn.addEventListener('click', () => {
      const l = btn.dataset.layer;
      if (SEA.visibleLayers.has(l)) SEA.visibleLayers.delete(l);
      else SEA.visibleLayers.add(l);
      syncToolbarState();
    });
  });

  const searchInput = document.getElementById('seaSearchInput');
  if (searchInput) {
    searchInput.addEventListener('input', () => {
      SEA.searchQuery = searchInput.value;
    });
    searchInput.addEventListener('keydown', e => {
      if (e.key === 'Enter') {
        e.preventDefault();
        const q = SEA.searchQuery.trim().toLowerCase();
        if (!q) {
          seaResetView();
          return;
        }
        const hit = SEA.nodes.find(n => isNodeVisible(n) && (
          (n.label || '').toLowerCase().includes(q)
          || (n.summary || '').toLowerCase().includes(q)
          || String(n.detail?.qq_id || '').includes(q)
        ));
        if (hit) focusNode(hit, true);
        else toast('未找到匹配的人物或记忆');
      }
    });
  }

  const groupSel = document.getElementById('seaGroupFilter');
  if (groupSel) {
    groupSel.addEventListener('change', () => {
      SEA.groupFilter = groupSel.value;
    });
  }

  const canvas = document.getElementById('seaCanvas');
  if (!canvas) return;

  canvas.addEventListener('contextmenu', e => e.preventDefault());

  canvas.addEventListener('pointerdown', e => {
    canvas.setPointerCapture(e.pointerId);
    SEA.lastX = e.clientX;
    SEA.lastY = e.clientY;
    SEA.moved = false;
    SEA.pointerMode = (!SEA.mode3d || e.button === 2 || e.shiftKey) ? 'pan' : 'orbit';
    SEA.autoRotate = false;
    syncToolbarState();
  });

  canvas.addEventListener('pointermove', e => {
    if (!SEA.pointerMode) return;
    const dx = e.clientX - SEA.lastX;
    const dy = e.clientY - SEA.lastY;
    if (Math.hypot(dx, dy) > 3) SEA.moved = true;
    SEA.lastX = e.clientX;
    SEA.lastY = e.clientY;

    if (SEA.pointerMode === 'orbit') {
      SEA.goalYaw -= dx * 0.0065;
      SEA.goalPitch = Math.max(-1.22, Math.min(1.22, SEA.goalPitch + dy * 0.0055));
    } else {
      const panSpeed = SEA.distance / 560;
      const cosY = Math.cos(SEA.yaw);
      const sinY = Math.sin(SEA.yaw);
      SEA.goalTargetX -= (dx * cosY) * panSpeed;
      SEA.goalTargetZ += (dx * sinY) * panSpeed;
      SEA.goalTargetY -= dy * panSpeed;
    }
  });

  canvas.addEventListener('pointerup', () => {
    if (!SEA.moved && SEA.focusedPersonId) {
      // Clicking empty space clears spotlight focus gently
      SEA.focusedPersonId = null;
    }
    SEA.pointerMode = null;
  });

  canvas.addEventListener('wheel', e => {
    e.preventDefault();
    seaZoom(e.deltaY < 0 ? 1.12 : 0.89);
  }, { passive: false });
}
