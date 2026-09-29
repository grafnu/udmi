// Axoloctl Background Service Worker
// Correlates Web Viewer tabs (Path 1: /.axoloctl/beacon) with the Axoloctl
// Control Plane (Path 2: /api/status & /api/resolve on HOST_URL) using
// per-tab nonces or virtual-host (<tag>.localhost) matching, enabling
// port-forwarding-agnostic session identification and automatic tab reloads
// when a session's deployed commit changes.

const DEFAULT_HOST_URL = 'http://localhost:9290';
const POLL_INTERVAL = 2000;

// tabId -> { nonce, tag, commit, description, href, backendUrl, verified }
const tabBeacons = new Map();

chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: true }).catch(console.error);

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
      const matched =
        findSessionByNonce(sessions, entry.nonce) ||
        (entry.tag && sessions[entry.tag]
          ? { tag: entry.tag, info: sessions[entry.tag] }
          : null);
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

    if (Object.keys(sessions).length > 0 && tabBeacons.size === 0) {
      const activeTab = await getActiveTab();
      if (activeTab && activeTab.id != null) {
        const vtag = extractVhostTag(activeTab.url || '');
        if (vtag && sessions[vtag]) {
          correlationChanged = true;
        }
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
    tabBeacons.set(tabId, {
      nonce: viewerResp.nonce,
      tag: viewerResp.tag || existing.tag || '',
      commit: viewerResp.commit_hash || existing.commit || '',
      description: viewerResp.description || existing.description || '',
      href: viewerResp.href || tabUrl,
      backendUrl: existing.backendUrl || '',
      verified: sameNonce ? Boolean(existing.verified) : false,
    });
  } else if (!urlVhostTag) {
    // Active tab has neither a live Axoloctl content script nor a <tag>.localhost URL
    tabBeacons.delete(tabId);
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
          tabBeacons.set(tabId, {
            nonce: nonceVal,
            tag: vhostTag,
            commit: sInfo.commit || '',
            description: sInfo.description || '',
            href: tabUrl,
            backendUrl: sInfo.url || '',
            verified: true,
          });
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
          };
        }
      }
    } catch (_err) {
      // Ignore network error
    }
  }

  return {
    correlated: false,
    tabId,
    viewerUrl: tabUrl,
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

chrome.tabs.onUpdated.addListener((tabId, changeInfo, tab) => {
  if (changeInfo.url && !extractVhostTag(changeInfo.url)) {
    tabBeacons.delete(tabId);
  }
  if (changeInfo.status === 'complete' || changeInfo.url) {
    chrome.runtime.sendMessage({ type: 'AXOLOCTL_CORRELATION_UPDATED' }).catch(() => {});
  }
});

chrome.tabs.onRemoved.addListener((tabId) => {
  tabBeacons.delete(tabId);
});

setInterval(syncControlPlane, POLL_INTERVAL);
