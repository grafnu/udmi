// Axoloctl Background Service Worker
// Correlates Web Viewer tabs (Path 1: /.axoloctl/beacon) with the Axoloctl
// Control Plane (Path 2: /api/status & /api/resolve on HOST_URL) using
// per-tab nonces, enabling port-forwarding-agnostic session identification
// and automatic tab reloads when a session's deployed commit changes.

const DEFAULT_HOST_URL = 'http://localhost:9290';
const POLL_INTERVAL = 2000;

// tabId -> { nonce, tag, commit, description, href, verified }
const tabBeacons = new Map();

chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: true }).catch(console.error);

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

    let correlationChanged = false;
    for (const [tabId, entry] of tabBeacons.entries()) {
      const matched = findSessionByNonce(sessions, entry.nonce);
      if (matched) {
        const { tag, info } = matched;
        const prevCommit = entry.commit;
        const wasVerified = entry.verified;

        entry.verified = true;
        entry.tag = tag;
        entry.description = info.description || '';
        entry.backendUrl = info.url || entry.backendUrl || '';

        if (prevCommit && info.commit && prevCommit !== info.commit) {
          console.log(
            `[Axoloctl] Commit changed for correlated session '${tag}' (${prevCommit.slice(0, 8)} -> ${info.commit.slice(0, 8)}). Reloading tab ${tabId}.`
          );
          entry.commit = info.commit;
          chrome.tabs.reload(tabId);
          correlationChanged = true;
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
    return sessions;
  } catch (_err) {
    return null;
  } finally {
    isSyncing = false;
  }
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

async function resolveActiveTabCorrelation(windowId = null) {
  const activeTab = await getActiveTab(windowId);
  if (!activeTab || activeTab.id == null) {
    const hostUrl = await getHostUrl();
    return { correlated: false, hostUrl };
  }
  const tabId = activeTab.id;
  const hostUrl = await getHostUrl(activeTab.url || '');

  // Query content script in active tab (returns cached viewerSession if already registered)
  let viewerResp = null;
  try {
    viewerResp = await chrome.tabs.sendMessage(tabId, { type: 'AXOLOCTL_QUERY_VIEWER' });
  } catch (_err) {
    viewerResp = null;
  }

  if (viewerResp && viewerResp.axoloctl === false) {
    tabBeacons.delete(tabId);
    return {
      correlated: false,
      tabId,
      viewerUrl: activeTab.url || '',
      hostUrl,
    };
  }

  if (viewerResp && viewerResp.axoloctl && viewerResp.nonce) {
    const existing = tabBeacons.get(tabId) || {};
    const sameNonce = existing.nonce === viewerResp.nonce;
    tabBeacons.set(tabId, {
      nonce: viewerResp.nonce,
      tag: viewerResp.tag || existing.tag || '',
      commit: viewerResp.commit_hash || existing.commit || '',
      description: viewerResp.description || existing.description || '',
      href: viewerResp.href || activeTab.url || '',
      backendUrl: existing.backendUrl || '',
      verified: sameNonce ? Boolean(existing.verified) : false,
    });
  }

  const entry = tabBeacons.get(tabId);
  if (!entry || !entry.nonce) {
    return {
      correlated: false,
      tabId,
      viewerUrl: activeTab.url || '',
      hostUrl,
    };
  }

  if (entry.verified && entry.tag && entry.commit) {
    return {
      correlated: true,
      tabId,
      tag: entry.tag,
      commit: entry.commit,
      description: entry.description || '',
      nonce: entry.nonce,
      viewerUrl: activeTab.url || entry.href,
      backendUrl: entry.backendUrl || '',
      hostUrl,
    };
  }

  // Verify nonce over Path-2 (Control Plane /api/resolve)
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
        return {
          correlated: true,
          tabId,
          tag: resolved.tag,
          commit: resolved.commit,
          description: resolved.description || '',
          nonce: entry.nonce,
          viewerUrl: activeTab.url || entry.href,
          backendUrl: resolved.url,
          hostUrl,
        };
      }
    }
  } catch (_err) {
    // Fall through to uncorrelated response
  }

  return {
    correlated: false,
    tabId,
    unverifiedTag: entry.tag || null,
    nonce: entry.nonce,
    viewerUrl: activeTab.url || entry.href,
    hostUrl,
  };
}

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (!msg || !msg.type) {
    return false;
  }

  if (msg.type === 'AXOLOCTL_VIEWER_BEACON' && sender.tab && sender.tab.id != null) {
    const tabId = sender.tab.id;
    const prev = tabBeacons.get(tabId);
    const sameNonce = Boolean(prev && prev.nonce === msg.nonce);
    tabBeacons.set(tabId, {
      nonce: msg.nonce,
      tag: msg.tag,
      commit: msg.commit_hash || (prev && prev.commit) || '',
      description: msg.description || (prev && prev.description) || '',
      href: msg.href || sender.tab.url || '',
      backendUrl: (prev && prev.backendUrl) || '',
      verified: sameNonce ? Boolean(prev.verified) : false,
    });
    syncControlPlane().then(() => {
      sendResponse({ ok: true });
    });
    return true;
  }

  if (msg.type === 'AXOLOCTL_GET_ACTIVE_CORRELATION') {
    resolveActiveTabCorrelation(msg.windowId ?? null).then((res) => sendResponse(res));
    return true;
  }

  return false;
});

chrome.tabs.onActivated.addListener(() => {
  chrome.runtime.sendMessage({ type: 'AXOLOCTL_CORRELATION_UPDATED' }).catch(() => {});
});

chrome.tabs.onUpdated.addListener((tabId, changeInfo) => {
  if (changeInfo.status === 'complete' && tabBeacons.has(tabId)) {
    chrome.runtime.sendMessage({ type: 'AXOLOCTL_CORRELATION_UPDATED' }).catch(() => {});
  }
});

chrome.tabs.onRemoved.addListener((tabId) => {
  tabBeacons.delete(tabId);
});

setInterval(syncControlPlane, POLL_INTERVAL);
