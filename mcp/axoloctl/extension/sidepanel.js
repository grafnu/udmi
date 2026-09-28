document.addEventListener('DOMContentLoaded', async () => {
  const uiSelector = document.getElementById('ui-selector');
  const viewport = document.getElementById('agent-viewport');
  const refreshBtn = document.getElementById('refresh-btn');
  const correlationStatus = document.getElementById('correlation-status');
  const correlationNonce = document.getElementById('correlation-nonce');

  const DEFAULT_HOST_URL = 'http://localhost:9290';
  let currentCorrelation = { correlated: false, tag: '', nonce: '', hostUrl: '' };
  let panelWindowId = null;
  let lastViewportUrl = '';
  let isUpdatingBadge = false;
  let pendingBadgeUpdate = false;

  try {
    if (typeof chrome !== 'undefined' && chrome.windows && chrome.windows.getCurrent) {
      const win = await chrome.windows.getCurrent();
      if (win && win.id != null) {
        panelWindowId = win.id;
      }
    }
  } catch (_e) {
    panelWindowId = null;
  }

  async function getHostUrl() {
    if (currentCorrelation && currentCorrelation.hostUrl) {
      return currentCorrelation.hostUrl;
    }
    try {
      const stored = await chrome.storage.local.get(['axoloctlHostUrl']);
      if (stored && stored.axoloctlHostUrl) {
        return stored.axoloctlHostUrl.replace(/\/+$/, '');
      }
    } catch (_e) {
      // Fallback to default
    }
    return DEFAULT_HOST_URL;
  }

  function buildViewportUrl(baseUrl) {
    if (!baseUrl) return 'about:blank';
    try {
      const urlObj = new URL(baseUrl);
      if (currentCorrelation.correlated && currentCorrelation.tag) {
        urlObj.searchParams.set('tag', currentCorrelation.tag);
        urlObj.searchParams.set('nonce', currentCorrelation.nonce || '');
      }
      return urlObj.toString();
    } catch (_e) {
      return baseUrl;
    }
  }

  function setViewportUrl(targetUrl, forceReload = false) {
    if (!targetUrl) return;
    if (!forceReload && lastViewportUrl === targetUrl) {
      return;
    }
    lastViewportUrl = targetUrl;
    viewport.src = targetUrl;
  }

  async function updateCorrelationBadge() {
    if (isUpdatingBadge) {
      pendingBadgeUpdate = true;
      return;
    }
    isUpdatingBadge = true;
    try {
      const res = await chrome.runtime.sendMessage({
        type: 'AXOLOCTL_GET_ACTIVE_CORRELATION',
        windowId: panelWindowId,
      });
      const prevTag = currentCorrelation.tag;
      if (res && res.correlated) {
        currentCorrelation = res;
        const shortCommit = (res.commit || '').slice(0, 8);
        correlationStatus.className = 'status-pill correlated';
        correlationStatus.textContent = `Session: ${res.tag} (${shortCommit})`;
        correlationNonce.textContent = `nonce:${(res.nonce || '').slice(0, 8)}`;
      } else {
        currentCorrelation = {
          correlated: false,
          tag: '',
          nonce: '',
          hostUrl: (res && res.hostUrl) || '',
        };
        correlationStatus.className = 'status-pill uncorrelated';
        correlationStatus.textContent = 'No correlated viewer in active tab';
        correlationNonce.textContent = '';
      }
      if (prevTag !== currentCorrelation.tag && uiSelector.value) {
        setViewportUrl(buildViewportUrl(uiSelector.value));
      }
    } catch (_err) {
      correlationStatus.className = 'status-pill uncorrelated';
      correlationStatus.textContent = 'Correlation service unavailable';
      correlationNonce.textContent = '';
    } finally {
      isUpdatingBadge = false;
      if (pendingBadgeUpdate) {
        pendingBadgeUpdate = false;
        updateCorrelationBadge();
      }
    }
  }

  async function loadUIs(forceReload = false) {
    await updateCorrelationBadge();
    const hostUrl = await getHostUrl();
    try {
      const resp = await fetch(`${hostUrl}/api/uis`);
      if (!resp.ok) throw new Error('UI fetch failed');
      const data = await resp.json();

      uiSelector.innerHTML = '';

      if (data.uis && data.uis.length > 0) {
        data.uis.forEach((ui) => {
          const opt = document.createElement('option');
          opt.value = ui.url;
          opt.textContent = ui.label;
          if (ui.id === data.default_ui) {
            opt.selected = true;
          }
          uiSelector.appendChild(opt);
        });

        if (data.uis.length > 1) {
          uiSelector.classList.remove('hidden');
        } else {
          uiSelector.classList.add('hidden');
        }

        setViewportUrl(buildViewportUrl(uiSelector.value), forceReload);
      }
    } catch (e) {
      console.warn('Failed to load /api/uis, showing placeholder', e);
      uiSelector.classList.add('hidden');
      lastViewportUrl = '';
      viewport.src =
        'data:text/html,<h3 style="font-family:sans-serif;color:#f8fafc;background:#0f172a;padding:16px;">Agent UI not reachable at ' +
        hostUrl +
        '</h3><p style="font-family:sans-serif;color:#cbd5e1;padding:0 16px;">Run <code>bin/tmux_axoloctl start &lt;config&gt;</code></p>';
    }
  }

  uiSelector.addEventListener('change', () => {
    setViewportUrl(buildViewportUrl(uiSelector.value), true);
  });

  refreshBtn.addEventListener('click', () => {
    loadUIs(true);
  });

  if (typeof chrome !== 'undefined' && chrome.runtime && chrome.runtime.onMessage) {
    chrome.runtime.onMessage.addListener((msg) => {
      if (msg && msg.type === 'AXOLOCTL_CORRELATION_UPDATED') {
        updateCorrelationBadge();
      }
    });
  }

  loadUIs();
});
