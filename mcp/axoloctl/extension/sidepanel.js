document.addEventListener('DOMContentLoaded', async () => {
  const uiSelector = document.getElementById('ui-selector');
  const viewport = document.getElementById('agent-viewport');
  const refreshBtn = document.getElementById('refresh-btn');

  // Hardcode fallback host port
  const HOST_PORT = 9290;
  
  async function loadUIs() {
    try {
      const resp = await fetch(`http://127.0.0.1:${HOST_PORT}/api/uis`);
      if (!resp.ok) throw new Error("UI fetch failed");
      const data = await resp.json();
      
      uiSelector.innerHTML = '';
      
      if (data.uis && data.uis.length > 0) {
        data.uis.forEach(ui => {
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
        
        // Load default or first
        viewport.src = uiSelector.value;
      }
    } catch (e) {
      console.warn("Failed to load /api/uis, showing placeholder", e);
      // Fallback
      uiSelector.classList.add('hidden');
      viewport.src = 'data:text/html,<h3>Agent UI not running on port ' + HOST_PORT + '</h3><p>Run <code>bin/tmux_axoloctl start</code></p>';
    }
  }

  uiSelector.addEventListener('change', () => {
    viewport.src = uiSelector.value;
  });

  refreshBtn.addEventListener('click', () => {
    loadUIs();
  });

  // Initial load
  loadUIs();
});
