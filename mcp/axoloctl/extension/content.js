// Axoloctl Viewer Content Script
// Performs Path-1 same-origin nonce registration (/.axoloctl/beacon) and
// forwards client-side runtime errors through /.axoloctl/telemetry.

(() => {
  // Only run in top-level frames, and never on Host UI pages (:9290/ui/*)
  if (window !== window.top || window.location.pathname.startsWith('/ui/')) {
    return;
  }

  function generateNonce() {
    if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
      return crypto.randomUUID();
    }
    const bytes = new Uint8Array(16);
    crypto.getRandomValues(bytes);
    return Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join('');
  }

  const VIEWER_NONCE = generateNonce();
  window.__axoloctlNonce = VIEWER_NONCE;

  let viewerSession = null;
  let registrationPromise = null;
  let telemetryAttached = false;

  function sendTelemetry(message) {
    if (!viewerSession || !viewerSession.axoloctl) {
      return;
    }
    fetch('/.axoloctl/telemetry', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        nonce: VIEWER_NONCE,
        message: String(message),
      }),
    }).catch(() => {
      // Ignore network errors if session is restarting
    });
  }

  function attachTelemetryListeners() {
    if (telemetryAttached) {
      return;
    }
    telemetryAttached = true;

    window.addEventListener('error', (event) => {
      const errorMsg = `${event.message} at ${event.filename}:${event.lineno}`;
      sendTelemetry(errorMsg);
    });

    window.addEventListener('unhandledrejection', (event) => {
      const errorMsg = `Unhandled Promise Rejection: ${event.reason}`;
      sendTelemetry(errorMsg);
    });

    const originalConsoleError = console.error;
    console.error = function (...args) {
      originalConsoleError.apply(console, args);
      sendTelemetry(`Console Error: ${args.join(' ')}`);
    };
  }

  async function registerViewerBeacon(notifyBackground = false, force = false) {
    if (!force && viewerSession && viewerSession.axoloctl) {
      return viewerSession;
    }
    if (registrationPromise) {
      return registrationPromise;
    }

    registrationPromise = (async () => {
      try {
        const resp = await fetch('/.axoloctl/beacon', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            nonce: VIEWER_NONCE,
            url: window.location.href,
            title: document.title || '',
          }),
        });
        if (!resp.ok) {
          return viewerSession || { axoloctl: false };
        }
        const data = await resp.json();
        if (!data || data.axoloctl !== true || !data.tag) {
          return { axoloctl: false };
        }

        viewerSession = {
          axoloctl: true,
          nonce: VIEWER_NONCE,
          tag: data.tag,
          commit_hash: data.commit_hash || '',
          description: data.description || '',
          origin: window.location.origin,
          href: window.location.href,
        };

        attachTelemetryListeners();

        if (
          notifyBackground &&
          typeof chrome !== 'undefined' &&
          chrome.runtime &&
          chrome.runtime.sendMessage
        ) {
          chrome.runtime
            .sendMessage({
              type: 'AXOLOCTL_VIEWER_BEACON',
              ...viewerSession,
            })
            .catch(() => {});
        }

        return viewerSession;
      } catch (_err) {
        return viewerSession || { axoloctl: false };
      } finally {
        registrationPromise = null;
      }
    })();

    return registrationPromise;
  }

  if (typeof chrome !== 'undefined' && chrome.runtime && chrome.runtime.onMessage) {
    chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
      if (msg && msg.type === 'AXOLOCTL_QUERY_VIEWER') {
        const force = Boolean(msg.force);
        if (!force && viewerSession && viewerSession.axoloctl) {
          sendResponse({ ...viewerSession, href: window.location.href });
          return false;
        }
        registerViewerBeacon(false, force).then((res) => sendResponse(res));
        return true;
      }
      return false;
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => {
      registerViewerBeacon(true, true);
    });
  } else {
    registerViewerBeacon(true, true);
  }
})();
