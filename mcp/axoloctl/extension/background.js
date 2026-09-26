// Service worker for Axoloctl
const WEBMCP_PORT = 9291;
const POLL_INTERVAL = 2000;

let sessionCommits = {};

chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: true }).catch(console.error);

async function checkReloadEvents() {
  try {
    const resp = await fetch(`http://127.0.0.1:${WEBMCP_PORT}/status`);
    if (!resp.ok) return;
    const data = await resp.json();
    
    // data expected format: { "sessions": { "gummi": { "port": 9300, "commit": "abc..." } } }
    if (data.sessions) {
      for (const [tag, info] of Object.entries(data.sessions)) {
        if (sessionCommits[tag] && sessionCommits[tag] !== info.commit) {
          console.log(`Commit changed for session ${tag}. Reloading tabs for port ${info.port}.`);
          reloadTabsForPort(info.port);
        }
        sessionCommits[tag] = info.commit;
      }
    }
  } catch (e) {
    // Ignore fetch errors if web_mcp is down
  }
}

function reloadTabsForPort(port) {
  const urlPattern = `*://127.0.0.1:${port}/*`;
  const localhostPattern = `*://localhost:${port}/*`;
  
  chrome.tabs.query({ url: [urlPattern, localhostPattern] }, (tabs) => {
    tabs.forEach(tab => {
      chrome.tabs.reload(tab.id);
    });
  });
}

setInterval(checkReloadEvents, POLL_INTERVAL);
