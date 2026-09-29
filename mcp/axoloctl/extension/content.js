// Axoloctl Viewer Content Script
// Performs Path-1 same-origin nonce registration (/.axoloctl/beacon),
// verifies that the viewer page loaded without proxy/gateway errors, and
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

  function isVhostViewerPage() {
    const host = (window.location.hostname || '').toLowerCase();
    return host.endsWith('.localhost');
  }

  function checkPageHealth() {
    const readyState = document.readyState;
    const bodyText = (
      document.body && document.body.innerText ? document.body.innerText : ''
    ).trim();

    let isProxyError = false;
    let errorDetail = '';

    if (bodyText.startsWith('{') && bodyText.endsWith('}')) {
      try {
        const parsed = JSON.parse(bodyText);
        if (parsed && typeof parsed.error === 'string') {
          if (
            parsed.error.includes('is not currently running or unreachable') ||
            parsed.error === 'Bad Gateway' ||
            parsed.error.includes('Unknown session tag') ||
            parsed.error.includes('Failed to reach')
          ) {
            isProxyError = true;
            errorDetail = parsed.message || parsed.error;
          }
        }
      } catch (_e) {
        // Not a JSON error payload
      }
    }

    const hasContent = Boolean(
      document.body &&
        (document.body.children.length > 1 ||
          (document.body.children.length === 1 &&
            document.body.children[0].tagName !== 'PRE') ||
          (bodyText.length > 0 && !isProxyError))
    );

    const pageLoadedOk = Boolean(
      !isProxyError &&
        hasContent &&
        viewerSession &&
        viewerSession.axoloctl === true
    );

    return {
      readyState,
      pageLoadedOk,
      isProxyError,
      errorDetail,
    };
  }

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
    const healthBefore = checkPageHealth();
    if (!force && viewerSession && viewerSession.axoloctl && !healthBefore.isProxyError) {
      return { ...viewerSession, ...healthBefore };
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
          const health = checkPageHealth();
          const errState = {
            axoloctl: false,
            href: window.location.href,
            ...health,
            pageLoadedOk: false,
          };
          if (
            notifyBackground &&
            isVhostViewerPage() &&
            typeof chrome !== 'undefined' &&
            chrome.runtime &&
            chrome.runtime.sendMessage
          ) {
            chrome.runtime
              .sendMessage({
                type: 'AXOLOCTL_VIEWER_LOAD_ERROR',
                ...errState,
              })
              .catch(() => {});
          }
          return errState;
        }
        const data = await resp.json();
        if (!data || data.axoloctl !== true || !data.tag) {
          return { axoloctl: false, pageLoadedOk: false };
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
        const health = checkPageHealth();
        const fullState = { ...viewerSession, ...health };

        if (
          notifyBackground &&
          typeof chrome !== 'undefined' &&
          chrome.runtime &&
          chrome.runtime.sendMessage
        ) {
          chrome.runtime
            .sendMessage({
              type: health.pageLoadedOk
                ? 'AXOLOCTL_VIEWER_BEACON'
                : 'AXOLOCTL_VIEWER_LOAD_ERROR',
              ...fullState,
            })
            .catch(() => {});
        }

        return fullState;
      } catch (_err) {
        const health = checkPageHealth();
        const errState = {
          ...(viewerSession || { axoloctl: false }),
          href: window.location.href,
          ...health,
          pageLoadedOk: false,
        };
        if (
          notifyBackground &&
          isVhostViewerPage() &&
          typeof chrome !== 'undefined' &&
          chrome.runtime &&
          chrome.runtime.sendMessage
        ) {
          chrome.runtime
            .sendMessage({
              type: 'AXOLOCTL_VIEWER_LOAD_ERROR',
              ...errState,
            })
            .catch(() => {});
        }
        return errState;
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
        const health = checkPageHealth();
        if (!force && viewerSession && viewerSession.axoloctl && !health.isProxyError) {
          sendResponse({
            ...viewerSession,
            href: window.location.href,
            ...health,
          });
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
