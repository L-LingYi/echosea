import { STATUS_LABELS, api, badge, el, labelOf, replaceChildren, safeExternalUrl, toast } from './api.js';

export async function autoTagSticker(id) {
  toast('正在结合群聊语境理解打标...');
  const r = await api(`/api/stickers/${id}/auto-tag`, { method: 'POST', body: '{}' });
  toast(`已打标：${(r.emotion_tags || []).join(' / ')}`);
  loadStickers();
}

export async function importStickers() {
  const lines = document.getElementById('stickerImportInput').value.trim().split('\n').filter(Boolean);
  const items = lines.map(l => {
    const [url, tags, hint] = l.split('|');
    return {
      source_url: (url || '').trim(),
      emotion_tags: (tags || 'fun').split(',').map(x => x.trim()).filter(Boolean),
      context_hint: (hint || '').trim(),
    };
  });
  const r = await api('/api/stickers/import', { method: 'POST', body: JSON.stringify({ items }) });
  toast(`已导入 ${r.imported_count} 张表情包`);
  document.getElementById('importBox').classList.add('hidden');
  loadStickers();
}

export async function reviewSticker(id, status) {
  await api(`/api/stickers/${id}`, { method: 'PATCH', body: JSON.stringify({ status }) });
  loadStickers();
}

export async function delSticker(id) {
  await api(`/api/stickers/${id}`, { method: 'DELETE' });
  loadStickers();
}

export async function uploadStickerFile() {
  const input = document.getElementById('stickerFileInput');
  const file = input.files && input.files[0];
  if (!file) {
    toast('请先选择本地 JSON 或 JSONL 文件');
    return;
  }
  const limit = parseInt(document.getElementById('presetLimit').value || '80', 10);
  toast('正在解析并导入本地表情包文件...');
  const content = await file.text();
  const r = await api('/api/stickers/presets/upload', {
    method: 'POST',
    body: JSON.stringify({ filename: file.name, content, limit }),
  });
  toast(`导入完成：新增 ${r.imported_count} 张，跳过重复 ${r.skipped_duplicates} 张`);
  input.value = '';
  loadStickers();
}

export async function loadStickers() {
  const [list, presets] = await Promise.all([api('/api/stickers'), api('/api/stickers/presets')]);
  replaceChildren(
    'stickerPresetGrid',
    presets.map(p => {
      const href = safeExternalUrl(p.repo_url);
      return el(
        'div',
        { style: 'border:1px solid var(--border);border-radius:10px;padding:12px;background:var(--surface-2);display:flex;flex-direction:column;gap:6px;' },
        el('div', { style: 'font-weight:700;font-size:13.5px;' }, p.name),
        el('div', { style: 'font-size:12px;color:var(--ink2);flex:1;' }, p.description),
        el('div', { style: 'font-size:11.5px;color:var(--mute);' }, p.format_hint || ''),
        href
          ? el(
            'div',
            { style: 'margin-top:4px;' },
            el('a', { href, target: '_blank', rel: 'noopener noreferrer', className: 'btn sm secondary', style: 'text-decoration:none;display:inline-block;' }, '前往仓库下载 ↗'),
          )
          : null,
      );
    }),
  );

  if (!list.length) {
    replaceChildren(
      'stickerGrid',
      el('div', { className: 'card empty-state', style: 'grid-column:1/-1;' }, '图库还是空的，群里斗图或点右上角「＋ 批量导入」后即可在这里看到'),
    );
    return;
  }

  replaceChildren(
    'stickerGrid',
    list.map(s => {
      const thumbBox = el('div', { className: 'sticker-thumb' });
      const proxyUrl = `/api/stickers/${Number(s.id)}/image`;
      const fallbackUrl = safeExternalUrl(s.source_url);
      const img = el('img', { src: proxyUrl, alt: 'sticker', loading: 'lazy', referrerpolicy: 'no-referrer' });
      let triedFallback = false;
      img.addEventListener('error', () => {
        if (!triedFallback && fallbackUrl) {
          triedFallback = true;
          img.src = fallbackUrl;
          return;
        }
        thumbBox.replaceChildren(el('span', { style: 'font-size:32px;' }, '🎭'));
      });
      thumbBox.appendChild(img);

      const tags = (s.emotion_tags || []).length
        ? s.emotion_tags.map(t => badge(t, 'info'))
        : [badge('未打标', 'warn')];
      const stCls = s.status === 'approved' ? 'ok' : (s.status === 'rejected' ? 'err' : 'warn');
      const actions = [
        el('button', { className: 'btn sm secondary', type: 'button', onClick: () => autoTagSticker(Number(s.id)) }, '✨ AI 理解打标'),
      ];
      if (s.status !== 'approved') {
        actions.push(el('button', { className: 'btn sm', type: 'button', onClick: () => reviewSticker(Number(s.id), 'approved') }, '通过'));
      }
      actions.push(el('button', { className: 'btn sm danger', type: 'button', onClick: () => delSticker(Number(s.id)) }, '删除'));

      return el(
        'div',
        { className: 'sticker-card' },
        thumbBox,
        el(
          'div',
          { className: 'sticker-body' },
          el(
            'div',
            { style: 'display:flex;justify-content:space-between;align-items:center;' },
            badge(labelOf(STATUS_LABELS, s.status), stCls),
            el('span', { style: 'font-size:11.5px;color:var(--mute);' }, `出现 ${Number(s.appearance_count)} · 反应 ${Number(s.positive_reactions)}`),
          ),
          el('div', { className: 'pill-list' }, ...tags),
          el('div', { className: 'sticker-hint' }, s.context_hint || '暂无语境理解，可点击下方「AI 理解打标」自动生成'),
          el('div', { className: 'row-actions', style: 'margin-top:auto;' }, ...actions),
        ),
      );
    }),
  );
}
