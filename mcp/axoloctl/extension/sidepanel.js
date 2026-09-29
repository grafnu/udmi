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

  function extractVhostTag(viewerUrl) {
    if (!viewerUrl) return null;
    try {
      const parsed = new URL(viewerUrl);
      if (parsed.pathname.startsWith('/ui/')) return null;
      const host = (parsed.hostname || '').toLowerCase();
      if (host.endsWith('.localhost')) {
        const prefix = host.slice(0, -'.localhost'.length);
        const tag = prefix.split('.')[0];
        return tag || null;
      }
    } catch (_e) {
      // Ignore invalid URL
    }
    return null;
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

  async function directTabCorrelationFallback(hostUrl) {
    try {
      if (typeof chrome === 'undefined' || !chrome.tabs || !chrome.tabs.query) {
        return null;
      }
      let tabs = [];
      if (panelWindowId != null) {
        tabs = await chrome.tabs.query({ active: true, windowId: panelWindowId });
      }
      if (!tabs || tabs.length === 0) {
        tabs = await chrome.tabs.query({ active: true, lastFocusedWindow: true, windowType: 'normal' });
      }
      if (!tabs || tabs.length === 0) {
        tabs = await chrome.tabs.query({ active: true, windowType: 'normal' });
      }
      const activeTab = tabs && tabs[0];
      if (!activeTab || !activeTab.url) {
        return null;
      }
      const vtag = extractVhostTag(activeTab.url);
      if (!vtag) {
        return null;
      }
      const statusResp = await fetch(`${hostUrl}/api/status`);
      if (!statusResp.ok) {
        return null;
      }
      const statusData = await statusResp.json();
      const sessions = (statusData && statusData.sessions) || {};
      const sInfo = sessions[vtag];
      if (!sInfo) {
        return null;
      }
      const nonces = Object.keys(sInfo.nonces || {});
      const latestNonce = nonces.length > 0 ? nonces[nonces.length - 1] : `vhost:${vtag}`;
      return {
        correlated: true,
        tabId: activeTab.id,
        tag: vtag,
        commit: sInfo.commit || '',
        description: sInfo.description || '',
        nonce: latestNonce,
        viewerUrl: activeTab.url,
        backendUrl: sInfo.url || '',
        hostUrl,
      };
    } catch (_e) {
      return null;
    }
  }

  function buildViewportUrl(baseUrl) {
    if (!baseUrl) return 'about:blank';
    try {
      const urlObj = new URL(baseUrl);
      if (currentCorrelation.correlated && currentCorrelation.tag) {
        urlObj.searchParams.set('tag', currentCorrelation.tag);
        urlObj.searchParams.set('nonce', currentCorrelation.nonce || '');
      } else {
        urlObj.searchParams.delete('tag');
        urlObj.searchParams.delete('nonce');
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
      let res = await chrome.runtime.sendMessage({
        type: 'AXOLOCTL_GET_ACTIVE_CORRELATION',
        windowId: panelWindowId,
      });
      if (!res || !res.correlated) {
        const hostUrl = (res && res.hostUrl) || (await getHostUrl());
        const fallback = await directTabCorrelationFallback(hostUrl);
        if (fallback && fallback.correlated) {
          res = fallback;
        }
      }
      if (res && res.correlated && res.tag) {
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
      if (uiSelector.value) {
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

      const prevSelected = uiSelector.value;
      uiSelector.innerHTML = '';

      if (data.uis && data.uis.length > 0) {
        data.uis.forEach((ui) => {
          const opt = document.createElement('option');
          opt.value = ui.url;
          opt.textContent = ui.label;
          if (prevSelected ? ui.url === prevSelected : ui.id === data.default_ui) {
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
    window.location.reload();
  });

  if (typeof chrome !== 'undefined') {
    if (chrome.runtime && chrome.runtime.onMessage) {
      chrome.runtime.onMessage.addListener((msg) => {
        if (msg && msg.type === 'AXOLOCTL_CORRELATION_UPDATED') {
          updateCorrelationBadge();
        }
      });
    }
    if (chrome.tabs && chrome.tabs.onActivated) {
      chrome.tabs.onActivated.addListener(() => {
        updateCorrelationBadge();
      });
    }
    if (chrome.tabs && chrome.tabs.onUpdated) {
      chrome.tabs.onUpdated.addListener((_tabId, changeInfo) => {
        if (changeInfo.status === 'complete' || changeInfo.url) {
          updateCorrelationBadge();
        }
      });
    }
    if (chrome.windows && chrome.windows.onFocusChanged) {
      chrome.windows.onFocusChanged.addListener(() => {
        updateCorrelationBadge();
      });
    }
  }

  loadUIs();
  setInterval(updateCorrelationBadge, 1500);
});
