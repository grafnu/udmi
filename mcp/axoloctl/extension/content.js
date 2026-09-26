// Injects telemetry listener into the page
// Since we only run on 127.0.0.1 and localhost, we can catch unhandled errors

const WEBMCP_PORT = 9291;

// Only attach if this looks like an Axoloctl session (port >= 9300)
const port = parseInt(window.location.port, 10);
if (port >= 9300) {
  window.addEventListener('error', (event) => {
    const errorMsg = `${event.message} at ${event.filename}:${event.lineno}`;
    sendTelemetry(errorMsg);
  });

  window.addEventListener('unhandledrejection', (event) => {
    const errorMsg = `Unhandled Promise Rejection: ${event.reason}`;
    sendTelemetry(errorMsg);
  });

  // Optional: Monkey-patch console.error
  const originalConsoleError = console.error;
  console.error = function(...args) {
    originalConsoleError.apply(console, args);
    sendTelemetry(`Console Error: ${args.join(' ')}`);
  };
}

function sendTelemetry(message) {
  // Try to determine the tag from the port if possible, or let web_mcp handle it
  fetch(`http://127.0.0.1:${WEBMCP_PORT}/telemetry`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ port: port, message: message })
  }).catch(() => {
    // silently fail
  });
}
