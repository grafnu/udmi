// Axoloctl Background Service Worker
// Correlates Web Viewer tabs (Path 1: /.axoloctl/beacon) with the Axoloctl
// Control Plane (Path 2: /api/status & /api/resolve on HOST_URL) using
// per-tab nonces or virtual-host (<tag>.localhost) matching, enabling
// port-forwarding-agnostic session identification, automatic tab reloads
// when a session's deployed commit changes, and post-reload page health
// verification with automatic retry if the page loads with an error.

const DEFAULT_HOST_URL = 'http://localhost:9290';
const POLL_INTERVAL = 2000;
const RELOAD_RETRY_INTERVAL_MS = 2000;
const MAX_RELOAD_ATTEMPTS = 8;

// tabId -> { nonce, tag, commit, description, href, backendUrl, verified, pageLoadedOk, verifyingReload, lastReloadAt, reloadAttempts }
const tabBeacons = new Map();
// Tabs where the user explicitly clicked the toolbar icon to open the panel
const manualPanelTabs = new Set();

function isAxoloctlTab(tabId, tabUrl = '') {
  if (tabId != null && (tabBeacons.has(tabId) || manualPanelTabs.has(tabId))) {
    return true;
  }
  return Boolean(extractVhostTag(tabUrl));
}

function syncTabSidePanel(tabId, tabUrl = '') {
  if (tabId == null || typeof chrome === 'undefined' || !chrome.sidePanel || !chrome.sidePanel.setOptions) {
    return;
  }
  const enabled = isAxoloctlTab(tabId, tabUrl);
  chrome.sidePanel
    .setOptions({
      tabId,
      path: 'sidepanel.html',
      enabled,
    })
    .catch(() => {});
}

function initializeSidePanelVisibility() {
  if (typeof chrome === 'undefined' || !chrome.sidePanel) {
    return;
  }
  // Handle toolbar icon clicks manually so clicking the icon on an uncorrelated
  // tab enables and opens the side panel for that tab within the user gesture.
  if (chrome.sidePanel.setPanelBehavior) {
    chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: false }).catch(() => {});
  }
  // Disable the global side panel by default so non-Axoloctl tabs hide it.
  if (chrome.sidePanel.setOptions) {
    chrome.sidePanel
      .setOptions({ path: 'sidepanel.html', enabled: false })
      .catch(() => {});
  }
  if (chrome.tabs && chrome.tabs.query) {
    chrome.tabs
      .query({})
      .then((tabs) => {
        for (const tab of tabs || []) {
          if (tab && tab.id != null) {
            syncTabSidePanel(tab.id, tab.url || '');
          }
        }
      })
      .catch(() => {});
  }
}

initializeSidePanelVisibility();

if (typeof chrome !== 'undefined' && chrome.action && chrome.action.onClicked) {
  chrome.action.onClicked.addListener((tab) => {
    if (!tab || tab.id == null) {
      return;
    }
    manualPanelTabs.add(tab.id);
    chrome.sidePanel
      .setOptions({
        tabId: tab.id,
        path: 'sidepanel.html',
        enabled: true,
      })
      .catch(() => {});
    if (chrome.sidePanel.open) {
      chrome.sidePanel.open({ tabId: tab.id }).catch(() => {});
    }
  });
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
    // Ignore invalid URLs
  }
  return null;
}

function deriveHostUrlFromViewer(viewerUrl) {
  if (!viewerUrl) return null;
  try {
    const parsed = new URL(viewerUrl);
    if (parsed.hostname.endsWith('.localhost')) {
      const portSuffix = parsed.port ? `:${parsed.port}` : '';
      return `${parsed.protocol}//localhost${portSuffix}`;
    }
  } catch (_e) {
    // Ignore invalid URLs
  }
  return null;
}

async function getHostUrl(viewerUrl = '') {
  try {
    const stored = await chrome.storage.local.get(['axoloctlHostUrl']);
    if (stored && stored.axoloctlHostUrl) {
      return stored.axoloctlHostUrl.replace(/\/+$/, '');
    }
  } catch (_e) {
    // Fallback to derived or default
  }
  const derived = deriveHostUrlFromViewer(viewerUrl);
  if (derived) {
    return derived;
  }
  return DEFAULT_HOST_URL;
}

function findSessionByNonce(sessionsMap, nonce) {
  if (!sessionsMap || !nonce) {
    return null;
  }
  for (const [tag, info] of Object.entries(sessionsMap)) {
    const nonces = info.nonces || {};
    if (Object.prototype.hasOwnProperty.call(nonces, nonce)) {
      return { tag, info, beacon: nonces[nonce] };
    }
  }
  return null;
}

async function getActiveTab(windowId = null) {
  if (windowId != null) {
    const winTabs = await chrome.tabs.query({ active: true, windowId });
    if (winTabs && winTabs.length > 0) {
      return winTabs[0];
    }
  }
  const focusedTabs = await chrome.tabs.query({
    active: true,
    lastFocusedWindow: true,
    windowType: 'normal',
  });
  if (focusedTabs && focusedTabs.length > 0) {
    return focusedTabs[0];
  }
  const anyTabs = await chrome.tabs.query({ active: true, windowType: 'normal' });
  if (anyTabs && anyTabs.length > 0) {
    return anyTabs[0];
  }
  return null;
}

async function queryTabViewer(tabId, force = false) {
  try {
    return await chrome.tabs.sendMessage(tabId, {
      type: 'AXOLOCTL_QUERY_VIEWER',
      force,
    });
  } catch (_err) {
    return null;
  }
}

let isSyncing = false;

async function syncControlPlane() {
  if (isSyncing) {
    return null;
  }
  isSyncing = true;
  try {
    let candidateViewerUrl = '';
    for (const entry of tabBeacons.values()) {
      if (entry.href) {
        candidateViewerUrl = entry.href;
        break;
      }
    }
    const hostUrl = await getHostUrl(candidateViewerUrl);
    const resp = await fetch(`${hostUrl}/api/status`);
    if (!resp.ok) {
      return null;
    }
    const data = await resp.json();
    const sessions = data.sessions || {};

    // Ensure active <tag>.localhost tab is tracked even if its initial load failed with 502
    const activeTab = await getActiveTab();
    if (activeTab && activeTab.id != null) {
      const vtag = extractVhostTag(activeTab.url || '');
      if (vtag && sessions[vtag] && !tabBeacons.has(activeTab.id)) {
        tabBeacons.set(activeTab.id, {
          nonce: `vhost:${vtag}`,
          tag: vtag,
          commit: '',
          description: sessions[vtag].description || '',
          href: activeTab.url || '',
          backendUrl: sessions[vtag].url || '',
          verified: true,
          pageLoadedOk: false,
          verifyingReload: true,
          lastReloadAt: 0,
          reloadAttempts: 0,
        });
        syncTabSidePanel(activeTab.id, activeTab.url || '');
      }
    }

    let correlationChanged = false;
    for (const [tabId, entry] of tabBeacons.entries()) {
      const matched =
        findSessionByNonce(sessions, entry.nonce) ||
        (entry.tag && sessions[entry.tag]
          ? { tag: entry.tag, info: sessions[entry.tag] }
          : null);
      if (matched) {
        const { tag, info } = matched;
        const prevCommit = entry.commit;
        const wasVerified = entry.verified;
        const isServerReady = info.ready !== false;

        entry.verified = true;
        entry.tag = tag;
        entry.description = info.description || '';
        entry.backendUrl = info.url || entry.backendUrl || '';

        if (prevCommit && info.commit && prevCommit !== info.commit) {
          if (isServerReady) {
            console.log(
              `[Axoloctl] Commit changed for correlated session '${tag}' (${prevCommit.slice(0, 8)} -> ${info.commit.slice(0, 8)}). Reloading tab ${tabId}.`
            );
            entry.commit = info.commit;
            entry.pageLoadedOk = false;
            entry.verifyingReload = true;
            entry.lastReloadAt = Date.now();
            entry.reloadAttempts = 1;
            chrome.tabs.reload(tabId);
            correlationChanged = true;
          }
        } else if (entry.verifyingReload || entry.pageLoadedOk === false) {
          const viewerResp = await queryTabViewer(tabId, true);
          const commitMatches =
            !info.commit ||
            !viewerResp ||
            !viewerResp.commit_hash ||
            viewerResp.commit_hash === info.commit;
          if (
            viewerResp &&
            viewerResp.axoloctl === true &&
            viewerResp.pageLoadedOk === true &&
            commitMatches
          ) {
            entry.pageLoadedOk = true;
            entry.verifyingReload = false;
            entry.reloadAttempts = 0;
            entry.nonce = viewerResp.nonce || entry.nonce;
            entry.commit = viewerResp.commit_hash || info.commit || prevCommit;
            correlationChanged = true;
          } else if (isServerReady) {
            const now = Date.now();
            const elapsed = now - (entry.lastReloadAt || 0);
            const attempts = entry.reloadAttempts || 0;
            if (elapsed >= RELOAD_RETRY_INTERVAL_MS && attempts < MAX_RELOAD_ATTEMPTS) {
              console.log(
                `[Axoloctl] Page health check failed for session '${tag}' on tab ${tabId} (attempt ${attempts + 1}/${MAX_RELOAD_ATTEMPTS}). Reloading tab...`
              );
              entry.verifyingReload = true;
              entry.pageLoadedOk = false;
              entry.lastReloadAt = now;
              entry.reloadAttempts = attempts + 1;
              entry.commit = info.commit || prevCommit;
              chrome.tabs.reload(tabId);
              correlationChanged = true;
            }
          }
        } else {
          entry.commit = info.commit || prevCommit;
          if (!wasVerified) {
            correlationChanged = true;
          }
        }
      } else if (entry.verified && entry.tag && !sessions[entry.tag]) {
        entry.verified = false;
        correlationChanged = true;
      }
    }

    if (correlationChanged) {
      chrome.runtime.sendMessage({ type: 'AXOLOCTL_CORRELATION_UPDATED' }).catch(() => {});
    }
    return data;
  } catch (_err) {
    return null;
  } finally {
    isSyncing = false;
  }
}

async function resolveActiveTabCorrelation(windowId = null) {
  const activeTab = await getActiveTab(windowId);
  if (!activeTab || activeTab.id == null) {
    const hostUrl = await getHostUrl();
    return { correlated: false, hostUrl };
  }
  const tabId = activeTab.id;
  const tabUrl = activeTab.url || '';
  const hostUrl = await getHostUrl(tabUrl);

  // 1. Query content script in active tab
  let viewerResp = await queryTabViewer(tabId, false);
  const urlVhostTag = extractVhostTag(tabUrl);

  if (viewerResp && viewerResp.axoloctl && viewerResp.nonce) {
    const existing = tabBeacons.get(tabId) || {};
    const sameNonce = existing.nonce === viewerResp.nonce;
    const loadedOk = viewerResp.pageLoadedOk !== false;
    tabBeacons.set(tabId, {
      nonce: viewerResp.nonce,
      tag: viewerResp.tag || existing.tag || '',
      commit: viewerResp.commit_hash || existing.commit || '',
      description: viewerResp.description || existing.description || '',
      href: viewerResp.href || tabUrl,
      backendUrl: existing.backendUrl || '',
      verified: sameNonce ? Boolean(existing.verified) : false,
      pageLoadedOk: loadedOk,
      verifyingReload: loadedOk ? false : Boolean(existing.verifyingReload),
      lastReloadAt: existing.lastReloadAt || 0,
      reloadAttempts: loadedOk ? 0 : existing.reloadAttempts || 0,
    });
    syncTabSidePanel(tabId, tabUrl);
  } else if (!urlVhostTag) {
    tabBeacons.delete(tabId);
    syncTabSidePanel(tabId, tabUrl);
    return {
      correlated: false,
      tabId,
      viewerUrl: tabUrl,
      hostUrl,
    };
  }

  let entry = tabBeacons.get(tabId);

  // 2. Verify nonce over Path-2 (Control Plane /api/resolve), re-registering once if server state was wiped
  if (entry && entry.nonce && !entry.nonce.startsWith('vhost:')) {
    for (let attempt = 0; attempt < 2; attempt++) {
      try {
        const resp = await fetch(
          `${hostUrl}/api/resolve?nonce=${encodeURIComponent(entry.nonce)}`
        );
        if (resp.ok) {
          const resolved = await resp.json();
          if (resolved && resolved.resolved) {
            entry.verified = true;
            entry.tag = resolved.tag;
            entry.commit = resolved.commit;
            entry.description = resolved.description || '';
            entry.backendUrl = resolved.url || '';
            syncTabSidePanel(tabId, tabUrl);
            return {
              correlated: true,
              tabId,
              tag: resolved.tag,
              commit: resolved.commit,
              description: resolved.description || '',
              nonce: entry.nonce,
              viewerUrl: tabUrl || entry.href,
              backendUrl: resolved.url,
              hostUrl,
              pageLoadedOk: entry.pageLoadedOk !== false,
              verifyingReload: Boolean(entry.verifyingReload),
            };
          }
        }
      } catch (_err) {
        break;
      }
      if (attempt === 0) {
        const refreshed = await queryTabViewer(tabId, true);
        if (refreshed && refreshed.axoloctl && refreshed.nonce) {
          entry.nonce = refreshed.nonce;
          entry.tag = refreshed.tag || entry.tag;
          entry.commit = refreshed.commit_hash || entry.commit;
          entry.pageLoadedOk = refreshed.pageLoadedOk !== false;
        } else {
          break;
        }
      }
    }
  }

  // 3. Direct virtual-host (<tag>.localhost) correlation against /api/status
  const vhostTag = urlVhostTag || (entry && entry.tag) || null;
  if (vhostTag) {
    try {
      const statusResp = await fetch(`${hostUrl}/api/status`);
      if (statusResp.ok) {
        const statusData = await statusResp.json();
        const sessions = (statusData && statusData.sessions) || {};
        const sInfo = sessions[vhostTag];
        if (sInfo) {
          const nonceVal = (entry && entry.nonce) || `vhost:${vhostTag}`;
          const loadedOk = Boolean(
            viewerResp &&
              viewerResp.axoloctl === true &&
              viewerResp.pageLoadedOk !== false
          );
          const nextEntry = {
            nonce: nonceVal,
            tag: vhostTag,
            commit: sInfo.commit || '',
            description: sInfo.description || '',
            href: tabUrl,
            backendUrl: sInfo.url || '',
            verified: true,
            pageLoadedOk: loadedOk,
            verifyingReload: !loadedOk,
            lastReloadAt: (entry && entry.lastReloadAt) || 0,
            reloadAttempts: loadedOk ? 0 : (entry && entry.reloadAttempts) || 0,
          };
          tabBeacons.set(tabId, nextEntry);
          syncTabSidePanel(tabId, tabUrl);
          if (!loadedOk) {
            syncControlPlane();
          }
          return {
            correlated: true,
            tabId,
            tag: vhostTag,
            commit: sInfo.commit || '',
            description: sInfo.description || '',
            nonce: nonceVal,
            viewerUrl: tabUrl,
            backendUrl: sInfo.url || '',
            hostUrl,
            pageLoadedOk: loadedOk,
            verifyingReload: !loadedOk,
          };
        }
      }
    } catch (_err) {
      // Ignore network error
    }
  }

  syncTabSidePanel(tabId, tabUrl);
  return {
    correlated: false,
    tabId,
    viewerUrl: tabUrl,
    hostUrl,
  };
}

async function switchOrCreateSession(tag, windowId = null) {
  const hostUrl = await getHostUrl();
  const resp = await fetch(`${hostUrl}/api/sessions`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ tag }),
  });
  const data = await resp.json();
  if (!resp.ok || data.error || data.running === false) {
    throw new Error(data.error || `Failed to start session '${tag}'`);
  }

  const targetUrl = `${(data.url || `http://${tag}.localhost:9290`).replace(/\/+$/, '')}/`;
  const activeTab = await getActiveTab(windowId);
  let targetTabId = null;

  if (activeTab && activeTab.id != null) {
    targetTabId = activeTab.id;
    await chrome.tabs.update(targetTabId, { url: targetUrl });
  } else {
    const created = await chrome.tabs.create({ url: targetUrl });
    targetTabId = created && created.id;
  }

  if (targetTabId != null) {
    tabBeacons.set(targetTabId, {
      nonce: `vhost:${tag}`,
      tag,
      commit: data.commit || '',
      description: data.description || '',
      href: targetUrl,
      backendUrl: data.url || targetUrl,
      verified: true,
      pageLoadedOk: false,
      verifyingReload: true,
      lastReloadAt: Date.now(),
      reloadAttempts: 0,
    });
    syncTabSidePanel(targetTabId, targetUrl);
  }

  chrome.runtime.sendMessage({ type: 'AXOLOCTL_CORRELATION_UPDATED' }).catch(() => {});
  return {
    ok: true,
    correlated: true,
    tabId: targetTabId,
    tag,
    commit: data.commit || '',
    description: data.description || '',
    nonce: `vhost:${tag}`,
    viewerUrl: targetUrl,
    backendUrl: data.url || targetUrl,
    hostUrl,
    pageLoadedOk: true,
    verifyingReload: false,
  };
}

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (!msg || !msg.type) {
    return false;
  }

  if (msg.type === 'AXOLOCTL_ENSURE_SIDEPANEL_OPEN' && sender.tab && sender.tab.id != null) {
    const tabId = sender.tab.id;
    const tabUrl = msg.href || sender.tab.url || '';
    syncTabSidePanel(tabId, tabUrl);
    if (chrome.sidePanel && chrome.sidePanel.open) {
      chrome.sidePanel.open({ tabId }).catch(() => {});
    }
    sendResponse({ ok: true });
    return false;
  }

  if (msg.type === 'AXOLOCTL_VIEWER_BEACON' && sender.tab && sender.tab.id != null) {
    const tabId = sender.tab.id;
    const prev = tabBeacons.get(tabId);
    const sameNonce = Boolean(prev && prev.nonce === msg.nonce);
    const loadedOk = msg.pageLoadedOk !== false;
    const tabUrl = msg.href || sender.tab.url || '';
    tabBeacons.set(tabId, {
      nonce: msg.nonce,
      tag: msg.tag,
      commit: msg.commit_hash || (prev && prev.commit) || '',
      description: msg.description || (prev && prev.description) || '',
      href: tabUrl,
      backendUrl: (prev && prev.backendUrl) || '',
      verified: sameNonce ? Boolean(prev.verified) : false,
      pageLoadedOk: loadedOk,
      verifyingReload: !loadedOk,
      lastReloadAt: (prev && prev.lastReloadAt) || 0,
      reloadAttempts: loadedOk ? 0 : (prev && prev.reloadAttempts) || 0,
    });
    syncTabSidePanel(tabId, tabUrl);
    syncControlPlane().then(() => {
      sendResponse({ ok: true });
    });
    return true;
  }

  if (msg.type === 'AXOLOCTL_VIEWER_LOAD_ERROR' && sender.tab && sender.tab.id != null) {
    const tabId = sender.tab.id;
    const prev = tabBeacons.get(tabId) || {};
    const tabUrl = msg.href || sender.tab.url || prev.href || '';
    const vtag = msg.tag || prev.tag || extractVhostTag(tabUrl) || '';
    if (vtag) {
      tabBeacons.set(tabId, {
        nonce: prev.nonce || `vhost:${vtag}`,
        tag: vtag,
        commit: prev.commit || '',
        description: prev.description || '',
        href: tabUrl,
        backendUrl: prev.backendUrl || '',
        verified: true,
        pageLoadedOk: false,
        verifyingReload: true,
        lastReloadAt: prev.lastReloadAt || 0,
        reloadAttempts: prev.reloadAttempts || 0,
      });
      syncTabSidePanel(tabId, tabUrl);
      syncControlPlane().then(() => {
        sendResponse({ ok: true });
      });
      return true;
    }
    sendResponse({ ok: false });
    return false;
  }

  if (msg.type === 'AXOLOCTL_GET_ACTIVE_CORRELATION') {
    resolveActiveTabCorrelation(msg.windowId ?? null).then((res) => sendResponse(res));
    return true;
  }

  if (msg.type === 'AXOLOCTL_SWITCH_SESSION') {
    switchOrCreateSession(msg.tag, msg.windowId ?? null)
      .then((res) => sendResponse(res))
      .catch((err) => sendResponse({ ok: false, error: err.message || String(err) }));
    return true;
  }

  return false;
});

chrome.tabs.onActivated.addListener((activeInfo) => {
  if (activeInfo && activeInfo.tabId != null && chrome.tabs && chrome.tabs.get) {
    chrome.tabs
      .get(activeInfo.tabId)
      .then((tab) => {
        syncTabSidePanel(activeInfo.tabId, (tab && tab.url) || '');
      })
      .catch(() => {});
  }
  chrome.runtime.sendMessage({ type: 'AXOLOCTL_CORRELATION_UPDATED' }).catch(() => {});
});

chrome.tabs.onUpdated.addListener((tabId, changeInfo, tab) => {
  if (changeInfo.url && !extractVhostTag(changeInfo.url)) {
    tabBeacons.delete(tabId);
    manualPanelTabs.delete(tabId);
  }
  const effectiveUrl = changeInfo.url || (tab && tab.url) || '';
  syncTabSidePanel(tabId, effectiveUrl);
  if (changeInfo.status === 'complete' || changeInfo.url) {
    if (changeInfo.status === 'complete' && tabBeacons.has(tabId)) {
      syncControlPlane();
    }
    chrome.runtime.sendMessage({ type: 'AXOLOCTL_CORRELATION_UPDATED' }).catch(() => {});
  }
});

chrome.tabs.onRemoved.addListener((tabId) => {
  tabBeacons.delete(tabId);
  manualPanelTabs.delete(tabId);
});

setInterval(syncControlPlane, POLL_INTERVAL);
