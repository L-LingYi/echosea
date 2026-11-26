import {
  TABS,
  api,
  applyTheme,
  setUnauthorizedHandler,
  toast,
  toggleDrawer,
  toggleTheme,
} from './api.js';
import { loadDashboard, switchUsageGranularity } from './dashboard.js';
import {
  closeIdentityModal,
  initIdentityModal,
  initSeaControls,
  loadConsciousnessSea,
  loadUnifiedMemory,
  seaResetView,
  seaSetViewMode,
  seaToggleArchived,
  seaToggleAutoRotate,
  seaToggleDimension,
  seaToggleInspector,
  seaZoom,
  switchMemoryView,
} from './sea.js';
import { loadTraces } from './traces.js';
import { closeProviderModal, initProvidersForm, loadProviders, openProviderModal } from './providers.js';
import { importStickers, loadStickers, uploadStickerFile } from './stickers.js';
import { initPersonaForm, loadPersona, runSandbox, saveRuntimeConfig } from './persona.js';
import { initHotSourceForm, loadPerception, refreshBriefing, syncSchedule } from './perception.js';
import {
  checkSystemUpdate,
  closeUpdateConfirmModal,
  exchangeAliyunCode,
  initOpsForms,
  loadOps,
  openAliyunOAuth,
  restoreSnapshot,
  runReflection,
  setupTotp,
  testBackupConn,
  triggerBackup,
  triggerSystemUpdate,
} from './ops.js';
import { initAlertForm, loadAlerts, sendTestAlert } from './alerts.js';

let CURRENT_TAB = 'dashboard';

function showLoginScreen() {
  document.getElementById('appView').classList.add('hidden');
  document.getElementById('loginView').classList.remove('hidden');
}

async function logout() {
  try {
    await fetch('/api/auth/logout', { method: 'POST', credentials: 'same-origin' });
  } catch {
    // Ignore network failures during client state reset
  }
  showLoginScreen();
}

function enterApp(username) {
  document.getElementById('loginView').classList.add('hidden');
  document.getElementById('appView').classList.remove('hidden');
  document.getElementById('userInfo').textContent = `管理员：${username}`;
  switchTab('dashboard');
}

function refreshCurrentTab() {
  if (CURRENT_TAB === 'dashboard') loadDashboard().catch(e => toast(e.message));
  else if (CURRENT_TAB === 'sea') loadUnifiedMemory();
  else if (CURRENT_TAB === 'traces') loadTraces().catch(e => toast(e.message));
  else if (CURRENT_TAB === 'providers') loadProviders().catch(e => toast(e.message));
  else if (CURRENT_TAB === 'stickers') loadStickers().catch(e => toast(e.message));
  else if (CURRENT_TAB === 'persona') loadPersona().catch(e => toast(e.message));
  else if (CURRENT_TAB === 'perception') loadPerception().catch(e => toast(e.message));
  else if (CURRENT_TAB === 'ops') loadOps().catch(e => toast(e.message));
  else if (CURRENT_TAB === 'alerts') loadAlerts().catch(e => toast(e.message));
}

function switchTab(tab) {
  if (!TABS.includes(tab)) return;
  CURRENT_TAB = tab;
  toggleDrawer(false);
  document.querySelectorAll('.nav-btn').forEach(b => b.classList.toggle('active', b.dataset.tab === tab));
  document.querySelectorAll('.mb-btn[data-tab]').forEach(b => b.classList.toggle('active', b.dataset.tab === tab));
  TABS.forEach(t => document.getElementById('tab-' + t).classList.toggle('hidden', t !== tab));
  refreshCurrentTab();
}

const ACTION_MAP = {
  'toggle-drawer': () => toggleDrawer(),
  'close-drawer': () => toggleDrawer(false),
  'open-drawer': () => toggleDrawer(true),
  'refresh-tab': () => refreshCurrentTab(),
  'toggle-theme': () => toggleTheme(),
  'logout': () => logout(),
  'load-dashboard': () => loadDashboard(),
  'usage-daily': () => switchUsageGranularity('daily'),
  'usage-hourly': () => switchUsageGranularity('hourly'),
  'sea-demo': () => loadConsciousnessSea(true),
  'sea-refresh': () => loadUnifiedMemory(),
  'mview-graph': () => switchMemoryView('graph'),
  'mview-identities': () => switchMemoryView('identities'),
  'mview-ambient': () => switchMemoryView('ambient'),
  'sea-zoom-in': () => seaZoom(1.2),
  'sea-zoom-out': () => seaZoom(0.83),
  'sea-reset': () => seaResetView(),
  'sea-mode-people': () => seaSetViewMode('people'),
  'sea-mode-all': () => seaSetViewMode('all'),
  'sea-toggle-dim': () => seaToggleDimension(),
  'sea-toggle-rotate': () => seaToggleAutoRotate(),
  'sea-toggle-inspector': () => seaToggleInspector(),
  'sea-toggle-archived': () => seaToggleArchived(),
  'load-traces': () => loadTraces(),
  'load-providers': () => loadProviders(),
  'open-provider-modal': () => openProviderModal(),
  'close-provider-modal': () => closeProviderModal(),
  'close-identity-modal': () => closeIdentityModal(),
  'load-persona': () => loadPersona(),
  'toggle-import-box': () => document.getElementById('importBox').classList.toggle('hidden'),
  'load-stickers': () => loadStickers(),
  'upload-sticker-file': () => uploadStickerFile(),
  'import-stickers': () => importStickers(),
  'toggle-new-persona': () => document.getElementById('newPersonaForm').classList.toggle('hidden'),
  'run-sandbox': () => runSandbox(),
  'save-runtime-config': () => saveRuntimeConfig(),
  'refresh-briefing': () => refreshBriefing(),
  'sync-schedule': () => syncSchedule(),
  'run-reflection': () => runReflection(),
  'trigger-backup': () => triggerBackup(),
  'check-update': () => checkSystemUpdate(),
  'trigger-update': () => triggerSystemUpdate(),
  'close-update-confirm-modal': () => closeUpdateConfirmModal(),
  'test-backup-conn': () => testBackupConn(),
  'oauth-aliyun': () => openAliyunOAuth(),
  'exchange-aliyun-code': () => exchangeAliyunCode(),
  'dry-run-restore': () => restoreSnapshot(true),
  'execute-restore': () => restoreSnapshot(false),
  'setup-totp': () => setupTotp(),
  'send-test-alert': () => sendTestAlert(),
};

function bindUI() {
  setUnauthorizedHandler(showLoginScreen);

  const savedTheme = localStorage.getItem('echosea_theme');
  const initialTheme = savedTheme || (window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
  applyTheme(initialTheme);

  const loginForm = document.getElementById('loginForm');
  if (loginForm) {
    loginForm.addEventListener('submit', async e => {
      e.preventDefault();
      document.getElementById('loginErr').textContent = '';
      try {
        const data = await api('/api/auth/login', {
          method: 'POST',
          body: JSON.stringify({
            username: document.getElementById('loginUser').value.trim(),
            password: document.getElementById('loginPass').value,
            totp_code: document.getElementById('loginTotp').value.trim() || null,
          }),
        });
        enterApp(data.username);
      } catch (err) {
        document.getElementById('loginErr').textContent = err.message;
      }
    });
  }

  document.addEventListener('click', e => {
    const target = e.target instanceof Element ? e.target.closest('[data-action], [data-tab]') : null;
    if (!target) return;
    if (target.dataset.tab) {
      switchTab(target.dataset.tab);
      return;
    }
    const act = target.dataset.action;
    if (act && ACTION_MAP[act]) {
      Promise.resolve(ACTION_MAP[act]()).catch(err => toast(err.message || '操作失败'));
    }
  });

  const modal = document.getElementById('providerModal');
  if (modal) {
    modal.addEventListener('click', e => {
      if (e.target === modal) closeProviderModal();
    });
  }

  const idModal = document.getElementById('identityModal');
  if (idModal) {
    idModal.addEventListener('click', e => {
      if (e.target === idModal) closeIdentityModal();
    });
  }

  const updModal = document.getElementById('updateConfirmModal');
  if (updModal) {
    updModal.addEventListener('click', e => {
      if (e.target === updModal) closeUpdateConfirmModal();
    });
  }

  initSeaControls();
  initIdentityModal();
  initProvidersForm();
  initPersonaForm();
  initHotSourceForm();
  initOpsForms(showLoginScreen);
  initAlertForm();
}

bindUI();
api('/api/auth/me', { silent401: true })
  .then(me => {
    if (me && me.username) enterApp(me.username);
  })
  .catch(() => {
    showLoginScreen();
  });
