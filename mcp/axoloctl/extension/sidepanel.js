document.addEventListener('DOMContentLoaded', async () => {
  const sessionSelector = document.getElementById('session-selector');
  const uiSelector = document.getElementById('ui-selector');
  const viewport = document.getElementById('agent-viewport');
  const refreshBtn = document.getElementById('refresh-btn');
  const correlationStatus = document.getElementById('correlation-status');
  const correlationNonce = document.getElementById('correlation-nonce');

  const newSessionModal = document.getElementById('new-session-modal');
  const newSessionForm = document.getElementById('new-session-form');
  const newSessionInput = document.getElementById('new-session-input');
  const newSessionSubmit = document.getElementById('new-session-submit');
  const newSessionCancel = document.getElementById('new-session-cancel');
  const newSessionError = document.getElementById('new-session-error');

  const DEFAULT_HOST_URL = 'http://localhost:9290';
  const SELECTED_UI_KEY = 'axoloctlSelectedUiId';
  let currentCorrelation = {
    correlated: false,
    tag: '',
    nonce: '',
    hostUrl: '',
    pageLoadedOk: true,
    verifyingReload: false,
  };
  let definedSessions = {};
  let panelWindowId = null;
  let lastViewportUrl = '';
  let isUpdatingBadge = false;
  let pendingBadgeUpdate = false;
  let isSwitchingSession = false;

  async function getSavedUiId() {
    try {
      if (typeof chrome !== 'undefined' && chrome.storage && chrome.storage.local) {
        const stored = await chrome.storage.local.get([SELECTED_UI_KEY]);
        if (stored && stored[SELECTED_UI_KEY]) {
          try {
            localStorage.setItem(SELECTED_UI_KEY, stored[SELECTED_UI_KEY]);
          } catch (_e) {
            // Ignore localStorage errors
          }
          return stored[SELECTED_UI_KEY];
        }
      }
    } catch (_e) {
      // Fallback to localStorage
    }
    try {
      return localStorage.getItem(SELECTED_UI_KEY) || '';
    } catch (_e) {
      return '';
    }
  }

  async function saveSelectedUiId(uiId) {
    if (!uiId) return;
    try {
      localStorage.setItem(SELECTED_UI_KEY, uiId);
    } catch (_e) {
      // Ignore localStorage errors
    }
    try {
      if (typeof chrome !== 'undefined' && chrome.storage && chrome.storage.local) {
        await chrome.storage.local.set({ [SELECTED_UI_KEY]: uiId });
      }
    } catch (_e) {
      // Ignore chrome.storage errors
    }
  }

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
      if (typeof chrome !== 'undefined' && chrome.storage && chrome.storage.local) {
        const stored = await chrome.storage.local.get(['axoloctlHostUrl']);
        if (stored && stored.axoloctlHostUrl) {
          return stored.axoloctlHostUrl.replace(/\/+$/, '');
        }
      }
    } catch (_e) {
      // Fallback to default
    }
    return DEFAULT_HOST_URL;
  }

  async function fetchDefinedSessions(hostUrl) {
    try {
      const statusResp = await fetch(`${hostUrl}/api/status`);
      if (!statusResp.ok) {
        return null;
      }
      const statusData = await statusResp.json();
      const runningMap = (statusData && statusData.sessions) || {};
      const definedMap = (statusData && statusData.defined_sessions) || {};
      const merged = { ...definedMap };
      for (const [tag, info] of Object.entries(runningMap)) {
        merged[tag] = { ...(merged[tag] || {}), ...info, running: true };
      }
      definedSessions = merged;
      return { statusData, merged };
    } catch (_e) {
      return null;
    }
  }

  async function directTabCorrelationFallback(hostUrl, preloadedStatus = null) {
    try {
      if (typeof chrome === 'undefined' || !chrome.tabs || !chrome.tabs.query) {
        return null;
      }
      let tabs = [];
      if (panelWindowId != null) {
        tabs = await chrome.tabs.query({ active: true, windowId: panelWindowId });
      }
      if (!tabs || tabs.length === 0) {
        tabs = await chrome.tabs.query({
          active: true,
          lastFocusedWindow: true,
          windowType: 'normal',
        });
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
      let statusData = preloadedStatus;
      if (!statusData) {
        const fetched = await fetchDefinedSessions(hostUrl);
        statusData = fetched && fetched.statusData;
      }
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
        pageLoadedOk: true,
        verifyingReload: false,
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
      } else {
        urlObj.searchParams.delete('tag');
      }
      urlObj.searchParams.delete('nonce');
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

  function renderSessionDropdown() {
    if (!sessionSelector) return;
    if (document.activeElement === sessionSelector || isSwitchingSession) {
      return;
    }

    const activeTag = currentCorrelation.correlated ? currentCorrelation.tag : '';
    const verifying = Boolean(
      currentCorrelation.correlated &&
        (currentCorrelation.verifyingReload || currentCorrelation.pageLoadedOk === false)
    );

    if (!activeTag) {
      sessionSelector.className = 'uncorrelated';
      sessionSelector.title = 'No correlated viewer in active tab — select a session or create a new one';
    } else if (verifying) {
      sessionSelector.className = 'verifying';
      sessionSelector.title = `Verifying page load for session '${activeTag}'...`;
    } else {
      sessionSelector.className = 'correlated';
      const shortCommit = (currentCorrelation.commit || '').slice(0, 8);
      sessionSelector.title = `Session: ${activeTag} (${shortCommit}) — Page verified OK`;
    }

    sessionSelector.innerHTML = '';

    const placeholderOpt = document.createElement('option');
    placeholderOpt.value = '';
    placeholderOpt.textContent = activeTag ? '-- Switch session --' : '-- Select session --';
    if (!activeTag) {
      placeholderOpt.selected = true;
    }
    sessionSelector.appendChild(placeholderOpt);

    const allTags = new Set(Object.keys(definedSessions));
    if (activeTag) {
      allTags.add(activeTag);
    }

    Array.from(allTags)
      .sort()
      .forEach((tag) => {
        const info = definedSessions[tag] || {};
        const isRunning = info.running !== false;
        const commit =
          (tag === activeTag && currentCorrelation.commit) || info.commit || '';
        const shortCommit = commit ? commit.slice(0, 8) : '';
        const opt = document.createElement('option');
        opt.value = tag;

        if (tag === activeTag && verifying) {
          opt.textContent = `⟳ ${tag} (verifying...)`;
        } else {
          const prefix = isRunning ? '●' : '○';
          opt.textContent = shortCommit
            ? `${prefix} ${tag} (${shortCommit})`
            : `${prefix} ${tag}`;
        }

        if (tag === activeTag) {
          opt.selected = true;
        }
        sessionSelector.appendChild(opt);
      });

    const newOpt = document.createElement('option');
    newOpt.value = '__new__';
    newOpt.textContent = 'New...';
    sessionSelector.appendChild(newOpt);
  }

  async function updateCorrelationBadge() {
    if (isUpdatingBadge || isSwitchingSession) {
      pendingBadgeUpdate = true;
      return;
    }
    isUpdatingBadge = true;
    try {
      const hostUrl = await getHostUrl();
      const fetched = await fetchDefinedSessions(hostUrl);
      const statusData = fetched && fetched.statusData;

      let res = null;
      if (typeof chrome !== 'undefined' && chrome.runtime && chrome.runtime.sendMessage) {
        try {
          res = await chrome.runtime.sendMessage({
            type: 'AXOLOCTL_GET_ACTIVE_CORRELATION',
            windowId: panelWindowId,
          });
        } catch (_e) {
          res = null;
        }
      }
      if (!res || !res.correlated) {
        const effHostUrl = (res && res.hostUrl) || hostUrl;
        const fallback = await directTabCorrelationFallback(effHostUrl, statusData);
        if (fallback && fallback.correlated) {
          res = fallback;
        }
      }
      if (res && res.correlated && res.tag) {
        currentCorrelation = res;
        const shortCommit = (res.commit || '').slice(0, 8);
        if (correlationStatus) {
          correlationStatus.textContent = `Session: ${res.tag} (${shortCommit})`;
        }
        if (correlationNonce) {
          correlationNonce.textContent = `nonce:${(res.nonce || '').slice(0, 8)}`;
        }
      } else if (!currentCorrelation.manualPin) {
        currentCorrelation = {
          correlated: false,
          tag: '',
          nonce: '',
          hostUrl: (res && res.hostUrl) || hostUrl,
          pageLoadedOk: true,
          verifyingReload: false,
        };
        if (correlationStatus) {
          correlationStatus.textContent = 'No correlated viewer in active tab';
        }
        if (correlationNonce) {
          correlationNonce.textContent = '';
        }
      }
      renderSessionDropdown();
      if (uiSelector.value) {
        setViewportUrl(buildViewportUrl(uiSelector.value));
      }
    } catch (_err) {
      if (correlationStatus) {
        correlationStatus.textContent = 'Correlation service unavailable';
      }
      if (correlationNonce) {
        correlationNonce.textContent = '';
      }
    } finally {
      isUpdatingBadge = false;
      if (pendingBadgeUpdate) {
        pendingBadgeUpdate = false;
        updateCorrelationBadge();
      }
    }
  }

  async function activateSession(tag) {
    const cleanTag = (tag || '').trim();
    if (!cleanTag) return;
    isSwitchingSession = true;
    sessionSelector.disabled = true;
    try {
      const hostUrl = await getHostUrl();
      let res = null;
      if (typeof chrome !== 'undefined' && chrome.runtime && chrome.runtime.sendMessage) {
        try {
          res = await chrome.runtime.sendMessage({
            type: 'AXOLOCTL_SWITCH_SESSION',
            tag: cleanTag,
            windowId: panelWindowId,
          });
        } catch (_e) {
          res = null;
        }
      }
      if (!res || (!res.ok && !res.error)) {
        const httpResp = await fetch(`${hostUrl}/api/sessions`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ tag: cleanTag }),
        });
        const data = await httpResp.json();
        if (!httpResp.ok || data.error || data.running === false) {
          throw new Error(data.error || `Failed to start session '${cleanTag}'`);
        }
        res = {
          ok: true,
          correlated: true,
          tag: cleanTag,
          commit: data.commit || '',
          description: data.description || '',
          nonce: `vhost:${cleanTag}`,
          viewerUrl: data.url || `http://${cleanTag}.localhost:9290/`,
          backendUrl: data.url || `http://${cleanTag}.localhost:9290`,
          hostUrl,
          pageLoadedOk: true,
          verifyingReload: false,
          manualPin: true,
        };
      }
      if (!res.ok) {
        throw new Error(res.error || `Failed to switch to session '${cleanTag}'`);
      }

      await fetchDefinedSessions(hostUrl);
      currentCorrelation = {
        ...res,
        correlated: true,
        tag: cleanTag,
      };
      if (uiSelector.value) {
        setViewportUrl(buildViewportUrl(uiSelector.value), true);
      }
    } finally {
      isSwitchingSession = false;
      sessionSelector.disabled = false;
      renderSessionDropdown();
    }
  }

  function openNewSessionModal() {
    if (!newSessionModal) return;
    newSessionError.textContent = '';
    newSessionError.classList.add('hidden');
    newSessionInput.value = '';
    newSessionInput.disabled = false;
    newSessionSubmit.disabled = false;
    newSessionSubmit.textContent = 'Create';
    newSessionModal.classList.remove('hidden');
    setTimeout(() => newSessionInput.focus(), 10);
  }

  function closeNewSessionModal() {
    if (!newSessionModal) return;
    newSessionModal.classList.add('hidden');
    newSessionError.textContent = '';
    newSessionError.classList.add('hidden');
    renderSessionDropdown();
  }

  if (sessionSelector) {
    sessionSelector.addEventListener('change', async () => {
      const val = sessionSelector.value;
      if (val === '__new__') {
        sessionSelector.value = currentCorrelation.correlated ? currentCorrelation.tag : '';
        openNewSessionModal();
        return;
      }
      closeNewSessionModal();
      if (!val) {
        return;
      }
      try {
        await activateSession(val);
      } catch (err) {
        console.error('Failed to switch session:', err);
        renderSessionDropdown();
      }
    });
  }

  if (newSessionCancel) {
    newSessionCancel.addEventListener('click', () => {
      closeNewSessionModal();
    });
  }

  if (newSessionInput) {
    newSessionInput.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') {
        e.preventDefault();
        closeNewSessionModal();
      }
    });
  }

  if (newSessionForm) {
    newSessionForm.addEventListener('submit', async (e) => {
      e.preventDefault();
      const rawName = (newSessionInput.value || '').trim();
      if (!/^[a-zA-Z0-9_-]+$/.test(rawName) || rawName.toLowerCase() === 'localhost' || rawName === '__new__') {
        newSessionError.textContent =
          "Invalid session name. Use letters, numbers, '-', or '_'.";
        newSessionError.classList.remove('hidden');
        return;
      }
      newSessionError.classList.add('hidden');
      newSessionInput.disabled = true;
      newSessionSubmit.disabled = true;
      newSessionSubmit.textContent = 'Creating...';
      try {
        await activateSession(rawName);
        closeNewSessionModal();
      } catch (err) {
        newSessionError.textContent = err.message || String(err);
        newSessionError.classList.remove('hidden');
        newSessionInput.disabled = false;
        newSessionSubmit.disabled = false;
        newSessionSubmit.textContent = 'Create';
      }
    });
  }

  async function loadUIs(forceReload = false) {
    await updateCorrelationBadge();
    const hostUrl = await getHostUrl();
    try {
      const resp = await fetch(`${hostUrl}/api/uis`);
      if (!resp.ok) throw new Error('UI fetch failed');
      const data = await resp.json();

      const savedUiId = await getSavedUiId();
      const prevSelectedOpt = uiSelector.selectedOptions && uiSelector.selectedOptions[0];
      const prevSelectedUiId =
        (prevSelectedOpt && prevSelectedOpt.dataset && prevSelectedOpt.dataset.uiId) || '';
      const prevSelectedUrl = uiSelector.value;
      uiSelector.innerHTML = '';

      if (data.uis && data.uis.length > 0) {
        const availableIds = new Set(data.uis.map((u) => u.id));
        let targetUiId = '';
        if (savedUiId && availableIds.has(savedUiId)) {
          targetUiId = savedUiId;
        } else if (prevSelectedUiId && availableIds.has(prevSelectedUiId)) {
          targetUiId = prevSelectedUiId;
        } else if (data.default_ui && availableIds.has(data.default_ui)) {
          targetUiId = data.default_ui;
        }

        data.uis.forEach((ui) => {
          const opt = document.createElement('option');
          opt.value = ui.url;
          opt.textContent = ui.label;
          opt.dataset.uiId = ui.id;
          if (targetUiId ? ui.id === targetUiId : prevSelectedUrl && ui.url === prevSelectedUrl) {
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
    const selectedOpt = uiSelector.selectedOptions && uiSelector.selectedOptions[0];
    if (selectedOpt && selectedOpt.dataset && selectedOpt.dataset.uiId) {
      saveSelectedUiId(selectedOpt.dataset.uiId);
    }
    setViewportUrl(buildViewportUrl(uiSelector.value), true);
  });

  refreshBtn.addEventListener('click', () => {
    loadUIs(true);
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
