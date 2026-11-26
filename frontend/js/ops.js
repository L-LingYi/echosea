import { STATUS_LABELS, api, badge, el, formatLocalTime, labelOf, replaceChildren, toast } from './api.js';

let LAST_CHECKED_REF = '';
let LAST_INSPECTION = null;
let UPD_TOTP_ENABLED = false;
let PENDING_CONFIRM_CTX = null;
const ACTIVE_UPDATE_STORAGE_KEY = 'echosea_active_update_job';
let UPDATE_POLL_TIMER = null;
let UPDATE_RELOAD_SCHEDULED = false;

function _getSavedUpdateJob() {
  try {
    return sessionStorage.getItem(ACTIVE_UPDATE_STORAGE_KEY) || '';
  } catch {
    return '';
  }
}

function _setSavedUpdateJob(jobUid) {
  try {
    if (jobUid) sessionStorage.setItem(ACTIVE_UPDATE_STORAGE_KEY, jobUid);
    else sessionStorage.removeItem(ACTIVE_UPDATE_STORAGE_KEY);
  } catch {
    // Ignore storage restriction
  }
}

function _setUpdateBanner(text) {
  const msg = document.getElementById('updateCheckMessage');
  if (msg) msg.textContent = text;
}

export function stopUpdatePolling(clearStorage = false) {
  if (UPDATE_POLL_TIMER) {
    clearTimeout(UPDATE_POLL_TIMER);
    UPDATE_POLL_TIMER = null;
  }
  if (clearStorage) _setSavedUpdateJob('');
}

export function startUpdatePolling(jobUid) {
  if (!jobUid) return;
  _setSavedUpdateJob(jobUid);
  stopUpdatePolling(false);
  _setUpdateBanner(`⏳ 更新任务 ${jobUid} 正在后台执行（刷新或关闭页面均不会中断更新），完成后将自动刷新页面...`);

  const tick = async () => {
    try {
      const upd = await api('/api/system/update/status');
      const job = (upd.jobs || []).find(j => j.job_uid === jobUid);
      if (!job || job.status === 'running' || job.status === 'pending') {
        _setUpdateBanner(`⏳ 更新任务 ${jobUid} 正在后台构建与重启（刷新页面不会中断），完成后将自动刷新...`);
        UPDATE_POLL_TIMER = setTimeout(tick, 3000);
        return;
      }
      stopUpdatePolling(true);
      if (job.status === 'succeeded') {
        if (!UPDATE_RELOAD_SCHEDULED) {
          UPDATE_RELOAD_SCHEDULED = true;
          const ver = (job.to_commit || '').slice(0, 12) || '最新版本';
          _setUpdateBanner(`✅ 更新任务 ${jobUid} 已完成（${ver}），即将自动刷新页面...`);
          toast('系统更新已完成，即将自动刷新页面加载新版本...');
          setTimeout(() => window.location.reload(), 1800);
        }
        return;
      }
      const summary = job.log_summary || labelOf(STATUS_LABELS, job.status);
      _setUpdateBanner(`⚠️ 更新任务 ${jobUid} 状态：${labelOf(STATUS_LABELS, job.status)}（${summary}）`);
      toast(`更新结束：${labelOf(STATUS_LABELS, job.status)}`);
      loadOps().catch(() => {});
    } catch (err) {
      if (err && err.message === 'Unauthorized') {
        stopUpdatePolling(false);
        return;
      }
      // During container restart, 502/503/network errors are expected — keep polling silently
      _setUpdateBanner(`⏳ 更新任务 ${jobUid} 进行中：Core 正在重启切换新版本，等待恢复后自动刷新页面...`);
      UPDATE_POLL_TIMER = setTimeout(tick, 3000);
    }
  };

  UPDATE_POLL_TIMER = setTimeout(tick, 2500);
}

export function resumeUpdatePollingIfNeeded() {
  const saved = _getSavedUpdateJob();
  if (saved && !UPDATE_POLL_TIMER) {
    startUpdatePolling(saved);
  }
}

export function closeUpdateConfirmModal() {
  const modal = document.getElementById('updateConfirmModal');
  if (modal) modal.classList.add('hidden');
  const code = document.getElementById('updateConfirmCode');
  if (code) code.value = '';
  PENDING_CONFIRM_CTX = null;
}

function renderDiffBox(diffText) {
  const box = document.getElementById('updateDiffBox');
  if (!box) return;
  const lines = String(diffText || '（无详细文本 Diff）').split('\n');
  box.replaceChildren(
    ...lines.map(line => {
      let cls = 'diff-line-ctx';
      if (line.startsWith('+') && !line.startsWith('+++')) cls = 'diff-line-add';
      else if (line.startsWith('-') && !line.startsWith('---')) cls = 'diff-line-del';
      else if (line.startsWith('@@') || line.startsWith('diff --git')) cls = 'diff-line-hunk';
      return el('span', { className: cls }, line || ' ');
    }),
  );
}

export function openUpdateConfirmModal(ctx) {
  PENDING_CONFIRM_CTX = ctx;
  const meta = document.getElementById('updateConfirmMeta');
  if (meta) {
    const reasons = (ctx.reasons || []).join('；');
    meta.textContent = `目标提交 ${(ctx.to_commit || '').slice(0, 12)}：${reasons || '包含受保护配置变更，请核对下方精确 Diff 后再放行。'}`;
  }
  replaceChildren(
    'updateConfirmFiles',
    (ctx.protected_files || []).map(f => badge(f, 'warn')),
  );
  renderDiffBox(ctx.diff || '');
  const label = document.getElementById('updateConfirmLabel');
  const input = document.getElementById('updateConfirmCode');
  if (UPD_TOTP_ENABLED) {
    if (label) label.textContent = '输入 6 位 TOTP 动态验证码（签发一次性更新令牌）';
    if (input) {
      input.type = 'text';
      input.setAttribute('inputmode', 'numeric');
      input.placeholder = '6 位动态码';
    }
  } else {
    if (label) label.textContent = '当前尚未开启 TOTP，请输入管理员密码确认（签发一次性更新令牌）';
    if (input) {
      input.type = 'password';
      input.removeAttribute('inputmode');
      input.placeholder = '管理员登录密码';
    }
  }
  const modal = document.getElementById('updateConfirmModal');
  if (modal) modal.classList.remove('hidden');
}

export function toggleBackupFields() {
  const mode = document.getElementById('bkProvider').value;
  document.getElementById('bkAliyunFields').classList.toggle('hidden', mode !== 'aliyun_openapi');
  document.getElementById('bkWebdavFields').classList.toggle('hidden', mode !== 'aliyun_webdav');
  if (mode === 'aliyun_webdav') {
    replaceChildren(
      'bkGuideBox',
      el('strong', {}, '🧭 阿里云盘 WebDAV / AList 接入向导（最推荐 · 免申请开发者资质）：'),
      el('br'),
      '① 在宿主机或 1Panel 运行 ', el('code', {}, 'AList'), ' 或 ', el('code', {}, 'aliyundrive-webdav'), ' 并用阿里云盘 App 扫码挂载；',
      el('br'),
      '② 在下方填入 WebDAV 地址（如 ', el('code', {}, 'http://127.0.0.1:5244/dav'), '）、账密与自定义目录（如 ', el('code', {}, '/EchoSea/backups'), '）；',
      el('br'),
      '③ 点击「保存配置并生效」→「测试连接」→ 勾选「开启后台定时自动备份」即可全自动托管。',
    );
  } else if (mode === 'aliyun_openapi') {
    replaceChildren(
      'bkGuideBox',
      el('strong', {}, '🧭 阿里云盘 OpenAPI 授权向导：'),
      el('br'),
      '① 若已有 ', el('code', {}, 'Refresh Token'), '，直接粘贴到左侧保存即可；若使用自建开放平台应用，先填右侧 Client ID / Secret 并保存；',
      el('br'),
      '② 点击「获取授权链接」登录阿里云盘，将回调页面中的 ', el('code', {}, 'code'), ' 粘贴并点「换取 Token」；',
      el('br'),
      '③ 填好自定义远程路径（如 ', el('code', {}, '/EchoSea/backups'), '），点「测试连接」通过后勾选「开启后台定时自动备份」。',
    );
  } else {
    replaceChildren(
      'bkGuideBox',
      el('strong', {}, '🧭 本地快照模式：'),
      '仅在服务器 ', el('code', {}, './backups'), ' 目录生成压缩快照并按保留份数自动清理，适合离线测试。',
    );
  }
}

export async function loadOps() {
  const [pl, bk, upd, bkCfg] = await Promise.all([
    api('/api/ops/plugins'),
    api('/api/ops/backups'),
    api('/api/system/update/status'),
    api('/api/ops/backup-config'),
  ]);
  document.getElementById('bkProvider').value = bkCfg.provider || 'aliyun_openapi';
  document.getElementById('bkPath').value = bkCfg.remote_path || '/EchoSea/backups';
  document.getElementById('bkDriveType').value = bkCfg.aliyun_drive_type || 'backup';
  document.getElementById('bkClientId').value = bkCfg.aliyun_client_id || '';
  document.getElementById('bkWdUrl').value = bkCfg.webdav_url || '';
  document.getElementById('bkWdUser').value = bkCfg.webdav_username || '';
  document.getElementById('bkAutoEnabled').checked = Boolean(bkCfg.auto_enabled);
  document.getElementById('bkInterval').value = String(bkCfg.schedule_interval_hours || 24);
  document.getElementById('bkRetention').value = String(bkCfg.retention_count || 14);
  document.getElementById('bkLastAuto').textContent = bkCfg.auto_enabled
    ? (bkCfg.last_auto_backup_at ? `定时备份已开启 · 上次执行：${formatLocalTime(bkCfg.last_auto_backup_at, true)}` : '定时备份已开启 · 等待首次自动执行')
    : '定时自动备份未开启';

  replaceChildren(
    'bkAuthBadge',
    bkCfg.aliyun_authorized
      ? badge(`已授权 (${bkCfg.aliyun_refresh_token_masked})`, 'ok')
      : badge('未授权：请填入 Refresh Token 或改用 WebDAV 模式', 'warn'),
  );
  toggleBackupFields();

  replaceChildren(
    'pluginList',
    (pl.loaded_plugins || []).map(p => el(
      'div',
      { style: 'padding:10px 0;border-bottom:1px solid var(--border-soft);' },
      el('strong', {}, p.name),
      ' ',
      badge(`v${p.version}`, 'info'),
      ' ',
      el('span', { style: 'font-size:12px;color:var(--mute);' }, `超时 ${Number(p.timeout_seconds)}s`),
      el('div', { style: 'font-size:12.5px;color:var(--ink2);margin-top:3px;' }, p.description),
    )),
  );

  if (!bk.length) {
    replaceChildren('backupList', el('div', { className: 'empty-state' }, '暂无备份记录'));
  } else {
    replaceChildren(
      'backupList',
      bk.map(b => el(
        'div',
        { style: 'padding:10px 0;border-bottom:1px solid var(--border-soft);display:flex;justify-content:space-between;' },
        el('span', {}, el('strong', {}, b.filename), ` → ${b.remote_target}`),
        badge(labelOf(STATUS_LABELS, b.status), b.status === 'success' ? 'ok' : 'err'),
      )),
    );
  }

  const modeLabel = upd.mode === 'github_public' ? `GitHub (${upd.github_repo})` : '未配置 GitHub 仓库';
  UPD_TOTP_ENABLED = Boolean(upd.totp_enabled);
  const autoCfg = upd.auto_update || {};
  const autoEl = document.getElementById('updAutoEnabled');
  if (autoEl) autoEl.checked = Boolean(autoCfg.auto_update_enabled);
  const hourEl = document.getElementById('updAutoHour');
  if (hourEl && autoCfg.auto_update_hour !== undefined) hourEl.value = String(autoCfg.auto_update_hour);
  const autoStatusEl = document.getElementById('updAutoStatus');
  if (autoStatusEl) {
    const hh = String(autoCfg.auto_update_hour ?? 4).padStart(2, '0');
    const mm = String(autoCfg.auto_update_minute ?? 0).padStart(2, '0');
    autoStatusEl.textContent = autoCfg.auto_update_enabled
      ? `自动更新已开启（每日 ${hh}:${mm} 检查第一级补丁）${autoCfg.last_auto_update_message ? ' · 最近：' + autoCfg.last_auto_update_message : ''}`
      : '定时自动更新未开启';
  }
  replaceChildren(
    'updateStatusBox',
    el(
      'div',
      { className: 'pill-list' },
      badge(`当前版本 v${upd.current_version}`, 'info'),
      badge(modeLabel, 'purple'),
      badge(upd.updater_configured ? '宿主机更新代理就绪' : '未配置宿主机更新代理', upd.updater_configured ? 'ok' : 'warn'),
    ),
  );

  const fmtUpdMode = m => (m === 'github_public' ? 'GitHub' : (m === 'local_repo' ? '受控更新源' : m));
  const jobs = upd.jobs || [];
  if (!jobs.length) {
    replaceChildren('updateJobTable', el('tr', {}, el('td', { colSpan: 6, className: 'empty-state' }, '暂无在线更新记录')));
  } else {
    replaceChildren(
      'updateJobTable',
      jobs.map(j => {
        const cls = j.status === 'succeeded'
          ? 'ok'
          : ((j.status === 'running' || j.status === 'pending') ? 'info' : (j.status === 'confirmation_required' ? 'warn' : 'err'));
        const actionCell = j.confirmation
          ? el(
            'div',
            { style: 'display:flex;gap:8px;align-items:center;flex-wrap:wrap;' },
            el('span', {}, j.log_summary),
            el(
              'button',
              {
                className: 'btn sm secondary',
                type: 'button',
                onClick: () => openUpdateConfirmModal(j.confirmation),
              },
              '查看 Diff 并确认',
            ),
          )
          : j.log_summary;
        return el(
          'tr',
          {},
          el('td', {}, el('code', {}, j.job_uid)),
          el('td', {}, `${fmtUpdMode(j.mode)} / ${j.target_ref}`),
          el('td', {}, el('code', {}, j.from_commit || j.from_version), ' → ', el('code', {}, j.to_commit || '-')),
          el('td', {}, badge(labelOf(STATUS_LABELS, j.status), cls)),
          el('td', {}, actionCell),
          el('td', {}, formatLocalTime(j.created_at)),
        );
      }),
    );
  }

  const runningJob = jobs.find(j => j.status === 'running' || j.status === 'pending');
  if (runningJob) {
    if (!UPDATE_POLL_TIMER) startUpdatePolling(runningJob.job_uid);
  } else {
    const savedUid = _getSavedUpdateJob();
    if (savedUid) {
      const doneJob = jobs.find(j => j.job_uid === savedUid);
      if (doneJob && doneJob.status === 'succeeded' && !UPDATE_RELOAD_SCHEDULED) {
        stopUpdatePolling(true);
        UPDATE_RELOAD_SCHEDULED = true;
        toast('系统更新已完成，即将自动刷新页面加载新版本...');
        setTimeout(() => window.location.reload(), 1500);
      } else if (doneJob) {
        stopUpdatePolling(true);
      }
    }
  }
}

export async function checkSystemUpdate() {
  const message = document.getElementById('updateCheckMessage');
  const trigger = document.getElementById('updateTrigger');
  LAST_CHECKED_REF = '';
  LAST_INSPECTION = null;
  replaceChildren('updateInspectBadge');
  trigger.disabled = true;
  message.textContent = '正在检查更新…';
  try {
    const result = await api('/api/system/update/check');
    if (result.update_available && result.updater_configured && !result.error) {
      LAST_CHECKED_REF = result.target_ref || '';
      trigger.disabled = !LAST_CHECKED_REF;
      if (LAST_CHECKED_REF) {
        try {
          const insp = await api('/api/system/update/inspect', {
            method: 'POST',
            body: JSON.stringify({ target_ref: LAST_CHECKED_REF }),
          });
          LAST_INSPECTION = { ...insp, target_ref: LAST_CHECKED_REF };
          if (insp.requires_confirmation) {
            replaceChildren(
              'updateInspectBadge',
              badge('第二级：含受保护变更，需弹窗审查 Diff + 二次验证', 'warn'),
              ' ',
              ...(insp.protected_files || []).map(f => badge(f, 'purple')),
            );
          } else {
            const note = (insp.safe_notes || []).join('；');
            replaceChildren(
              'updateInspectBadge',
              badge('第一级：安全变更，可直接自动安装', 'ok'),
              note ? el('span', { style: 'font-size:12px;color:var(--mute);margin-left:8px;' }, note) : null,
            );
          }
        } catch {
          // Non-fatal: trigger endpoint still enforces policy on host side
        }
      }
    }
    message.textContent = result.error || (result.update_available
      ? `发现新版本 ${result.latest_version} ${result.release_notes || ''}`
      : (result.message || '当前已是最新版本。'));
  } catch (err) {
    message.textContent = err.message;
  }
}

export async function triggerSystemUpdate() {
  const target = LAST_CHECKED_REF;
  if (!target) return;
  if (LAST_INSPECTION && LAST_INSPECTION.requires_confirmation) {
    openUpdateConfirmModal(LAST_INSPECTION);
    return;
  }
  if (!confirm('确认安装更新？Core 会短暂重启。')) return;
  LAST_CHECKED_REF = '';
  LAST_INSPECTION = null;
  document.getElementById('updateTrigger').disabled = true;
  try {
    const result = await api('/api/system/update/trigger', { method: 'POST', body: JSON.stringify({ target_ref: target }) });
    toast(`更新任务 ${result.job_uid} 已在后台启动，完成后将自动刷新页面`);
    if (result.status === 'running' || result.status === 'pending') {
      startUpdatePolling(result.job_uid);
    }
    await loadOps();
  } catch (err) {
    toast(err.message);
  }
}

export async function runReflection() {
  const r = await api('/api/ops/reflection/run', { method: 'POST', body: '{}' });
  toast(`反思完成：处理 ${r.processed_messages} 条，更新画像 ${r.profiles_updated}`);
}

export async function openAliyunOAuth() {
  const r = await api('/api/ops/backup-config/oauth-url', { method: 'POST', body: '{}' });
  if (!r.ok) {
    toast(r.error || '请先保存 Client ID');
    return;
  }
  window.open(r.authorize_url, '_blank', 'noopener');
}

export async function exchangeAliyunCode() {
  const code = document.getElementById('bkOAuthCode').value.trim();
  if (!code) {
    toast('请先粘贴回调 code');
    return;
  }
  const r = await api('/api/ops/backup-config/exchange-code', { method: 'POST', body: JSON.stringify({ code }) });
  toast(r.ok ? '阿里云盘授权成功，已加密保存 Refresh Token' : (r.error || '换取失败'));
  loadOps();
}

export async function testBackupConn() {
  toast('正在测试远程备份连接...');
  const r = await api('/api/ops/backup-config/test', { method: 'POST' });
  toast(r.ok ? r.message : (`连接失败：${r.error || '未知错误'}`));
}

export async function triggerBackup() {
  toast('正在打包并上传异地备份...');
  const r = await api('/api/ops/backups/trigger', { method: 'POST', body: '{}' });
  toast(r.ok ? `备份成功：${r.filename} (${Math.max(1, Math.round((r.size_bytes || 0) / 1024))} KB)` : `备份失败：${r.error_message || '请检查配置'}`);
  loadOps();
}

export async function restoreSnapshot(dryRun) {
  const input = document.getElementById('backupRestoreFile');
  const file = input && input.files && input.files[0];
  if (!file) {
    toast('请先选择 .json.gz 或 .json 快照文件');
    return;
  }
  if (!dryRun && !confirm('确认将快照中的数据合入当前数据库？建议先执行一次预检（Dry-Run）。')) return;
  const fd = new FormData();
  fd.append('file', file);
  const res = await api(`/api/ops/backups/restore?dry_run=${dryRun ? 'true' : 'false'}`, { method: 'POST', body: fd });
  const c = res.counts || {};
  const msg = dryRun
    ? `预检通过：熟人卡 ${c.identities || 0} · 情景 ${c.episodes || 0} · 黑话 ${c.expressions || 0} · 表情包 ${c.stickers || 0} · 人设 ${c.personas || 0}`
    : `恢复完成：导入熟人卡 ${(res.restored || {}).identities || 0}，新增情景 ${(res.restored || {}).episodes || 0}`;
  document.getElementById('backupRestoreResult').textContent = msg;
  toast(msg);
}

export async function setupTotp() {
  const r = await api('/api/auth/totp/setup', { method: 'POST' });
  replaceChildren(
    'totpSetupBox',
    '密钥：',
    el('code', {}, r.secret),
    el(
      'div',
      { style: 'display:flex;gap:6px;margin-top:6px;' },
      el('input', { id: 'totpVerifyCode', placeholder: '输入 6 位验证码', style: 'width:150px;' }),
      el('button', { className: 'btn sm', type: 'button', onClick: enableTotp }, '确认开启'),
    ),
  );
}

export async function enableTotp() {
  const input = document.getElementById('totpVerifyCode');
  await api('/api/auth/totp/enable', { method: 'POST', body: JSON.stringify({ code: (input ? input.value : '').trim() }) });
  toast('TOTP 二步验证已开启');
  replaceChildren('totpSetupBox', badge('已开启二步验证', 'ok'));
}

export function initOpsForms(onLogout) {
  const bkForm = document.getElementById('backupConfigForm');
  if (bkForm) {
    bkForm.addEventListener('submit', async e => {
      e.preventDefault();
      await api('/api/ops/backup-config', {
        method: 'PUT',
        body: JSON.stringify({
          provider: document.getElementById('bkProvider').value,
          remote_path: document.getElementById('bkPath').value.trim() || '/EchoSea/backups',
          aliyun_drive_type: document.getElementById('bkDriveType').value,
          aliyun_client_id: document.getElementById('bkClientId').value.trim(),
          aliyun_client_secret: document.getElementById('bkClientSecret').value.trim() || undefined,
          aliyun_refresh_token: document.getElementById('bkRefreshToken').value.trim() || undefined,
          webdav_url: document.getElementById('bkWdUrl').value.trim(),
          webdav_username: document.getElementById('bkWdUser').value.trim(),
          webdav_password: document.getElementById('bkWdPass').value.trim() || undefined,
          auto_enabled: document.getElementById('bkAutoEnabled').checked,
          schedule_interval_hours: parseInt(document.getElementById('bkInterval').value || '24', 10),
          retention_count: parseInt(document.getElementById('bkRetention').value || '14', 10),
        }),
      });
      document.getElementById('bkRefreshToken').value = '';
      document.getElementById('bkClientSecret').value = '';
      document.getElementById('bkWdPass').value = '';
      toast('异地备份配置已加密保存');
      loadOps();
    });
  }

  const bkProv = document.getElementById('bkProvider');
  if (bkProv) bkProv.addEventListener('change', toggleBackupFields);

  const pwdForm = document.getElementById('pwdForm');
  if (pwdForm) {
    pwdForm.addEventListener('submit', async e => {
      e.preventDefault();
      await api('/api/auth/password', {
        method: 'POST',
        body: JSON.stringify({
          old_password: document.getElementById('oldPwd').value,
          new_password: document.getElementById('newPwd').value,
        }),
      });
      toast('管理员密码已更新，请使用新密码重新登录');
      pwdForm.reset();
      onLogout();
    });
  }

  const autoUpdForm = document.getElementById('autoUpdateForm');
  if (autoUpdForm) {
    autoUpdForm.addEventListener('submit', async e => {
      e.preventDefault();
      await api('/api/system/update/auto-config', {
        method: 'PUT',
        body: JSON.stringify({
          auto_update_enabled: document.getElementById('updAutoEnabled').checked,
          auto_update_hour: parseInt(document.getElementById('updAutoHour').value || '4', 10),
          auto_update_minute: 0,
        }),
      });
      toast('无人值守自动更新策略已保存');
      await loadOps();
    });
  }

  const updConfirmForm = document.getElementById('updateConfirmForm');
  if (updConfirmForm) {
    updConfirmForm.addEventListener('submit', async e => {
      e.preventDefault();
      if (!PENDING_CONFIRM_CTX) return;
      const ctx = PENDING_CONFIRM_CTX;
      const secretInput = document.getElementById('updateConfirmCode').value.trim();
      const payload = {
        target_ref: ctx.target_ref,
        to_commit: ctx.to_commit,
        diff_sha256: ctx.diff_sha256,
      };
      if (UPD_TOTP_ENABLED) payload.totp_code = secretInput;
      else payload.password = secretInput;
      try {
        const tok = await api('/api/system/update/confirm-token', {
          method: 'POST',
          body: JSON.stringify(payload),
        });
        const trig = await api('/api/system/update/trigger', {
          method: 'POST',
          body: JSON.stringify({
            target_ref: ctx.target_ref,
            confirm_token: tok.confirm_token,
          }),
        });
        closeUpdateConfirmModal();
        LAST_CHECKED_REF = '';
        LAST_INSPECTION = null;
        const trigBtn = document.getElementById('updateTrigger');
        if (trigBtn) trigBtn.disabled = true;
        toast(`已通过二次验证，更新任务 ${trig.job_uid} 已在后台启动，完成后将自动刷新页面`);
        if (trig.status === 'running' || trig.status === 'pending') {
          startUpdatePolling(trig.job_uid);
        }
        await loadOps();
      } catch (err) {
        toast(err.message);
      }
    });
  }
}
