[**UDMI**](../../) / [**MCP**](../) / [Axoloctl](#)

# Axoloctl — Git-Backed Web Server Lifecycle MCP Server

`axoloctl` is a Model Context Protocol (MCP) server that enables an Agent to manage the lifecycle of one or more remote or local web server sessions in a deterministic, controlled environment.

Rather than running ad-hoc shell commands against a mutable working tree, the backing code for each web server is provided through an out-of-band Git mechanism. The Agent commits/pushes code changes to Git and calls `start_server(tag, commit_hash, description)`. `axoloctl` deploys that exact immutable revision under the specified `tag`, returns the immutable access `url`, manages the session lifecycle (`stop_server`, `get_status`, `list_servers`), and streams back the session's web server logs (`read_logs`).

---

## Objective

1. **Multi-Session Lifecycle Management**: Address each web server session by a caller-supplied `tag` and list active sessions with their `tag` and human-readable `description`.
2. **Immutable Code Deployment via Git Hash**: Deploy an explicit, verifiable Git commit SHA (`commit_hash`) fetched from the backing repository, queryable via `get_status`.
3. **Immutable Access URL on Startup**: Return the complete, immutable access `url` exclusively from `start_server` when a session is launched.
4. **Structured Log Streaming**: Allow the Agent to incrementally read and stream `stdout`/`stderr` logs for a specific `tag` using cursor-based pagination.

---

## Theory of Operation

`axoloctl` is designed around a collaborative workflow where the **Agent** serves as the operator's primary entry point for investigation and workflow discovery, and progressively transitions high-volume execution into a custom-built, tabular **Web View**:

1. **Conversational Discovery & Single-Example Triage**:
   The operator begins by working directly with the Agent in chat to describe an operational goal or diagnose an initial problem. During this preliminary phase, the Agent queries the external **Data MCPs** (`butler`, `barbican`, `uufi`) to verify that the necessary data is available, confirm how records across services correlate, and walk through a concrete single-device or small-sample example with the operator.
2. **Shaping the Interface Through Dialogue**:
   While conversational chat is ideal for open-ended reasoning and rapid diagnosis on a single example, it scales poorly when inspecting or acting upon thousands of IoT devices. As the operator and Agent iterate on the initial example—identifying which device attributes matter, what filter criteria isolate the target cohort, and how reported state should be compared against target configuration—those exact decisions define the specification for a scalable graphical utility.
3. **Background Synthesis of the Tabular Web Interface**:
   Once the data structure and workflow are validated on the representative example, the Agent automatically constructs or updates a custom web interface in the background. This web application translates the conversational workflow into a purpose-built tabular interface capable of filtering, correlating, and managing the entire fleet.
4. **Automated Testing, Deployment & Live Handoff**:
   Before exposing any changes to the operator, the Agent validates the web server code against both backend unit tests and end-to-end Playwright browser tests. Once all tests pass, the Agent commits and pushes the code to the local Git repository, deploys the immutable revision via `start_server(tag, commit_hash, description)`, verifies clean runtime logs via `read_logs`, and notifies the operator that the graphical interface is live and ready to scale their workflow across the full system.

---

## Critical User Journeys (CUJs)

The following Critical User Journeys describe the core operator goals and workflows for managing large-scale tables of IoT devices (see [GUMMI.md](../../gummi/GUMMI.md)):

1. **Portfolio Triage & Custom Alert Visualization**
   > *"In order to **rapidly identify and prioritize offline or misbehaving IoT devices across multiple sites**, as a **Fleet Operations Manager**, I need to **request a custom summary view of fleet health grouped by site and error severity and immediately explore the resulting breakdown**."*

2. **Multi-Attribute Fleet Filtering & Exploration**
   > *"In order to **isolate a specific cohort of devices across a fleet of hundreds of thousands of units**, as a **Site Reliability Engineer**, I need to **define custom filtering criteria and tabular columns over device attributes (such as site, hardware model, firmware version, and device prefix) and interactively browse the matching devices**."*

3. **Single-Device Configuration Drift Diagnosis**
   > *"In order to **diagnose why a specific field device is not converging to its intended operating parameters**, as a **Field Controls Technician**, I need to **inspect a side-by-side comparison of the device's reported state against its target configuration, identify discrepancies, and submit a validated configuration update**."*

4. **Staged Bulk Configuration Rollout**
   > *"In order to **safely apply a configuration change across a targeted group of devices without risking a fleet-wide disruption**, as a **Deployment Engineer**, I need to **select a filtered subset of devices, declare their target configuration, and monitor real-time convergence across the cohort**."*

5. **Site Discovery Reconciliation & Onboarding**
   > *"In order to **reconcile newly discovered field devices and telemetry points against the existing site inventory**, as an **Onboarding Specialist**, I need to **import external discovery datasets, match and review proposed device mappings in a tailored workspace, and iteratively refine the view without losing my in-progress onboarding state**."*

---

## System Architecture

The `axoloctl` system separates the **Code Plane** (immutable web server code and static assets delivered via Git `commit_hash` via the internal **Web MCP**) from the **Data Plane** (a dynamic **Shared Runtime Directory** inside the **Axoloctl Host** paired with external **Data MCPs** representing external data sources):

```mermaid
flowchart TD
  subgraph Browser["Web Browser"]
    direction LR
    Extension["Browser Extension"]
    WebView["Web View"]
    Extension -. "Inspects / Reloads" .-> WebView
  end

  subgraph AxoloctlHost["Axoloctl Host"]
    direction TB
    Agent["Agent Server"]
    WebMCP["Web MCP"]
    Git[("Git Repository\n(Code Plane)")]
    SharedDir[("Shared Runtime Directory\n(Data Plane)")]

    subgraph WebContext["Web Context"]
      direction TB
      StaticRoot["Static Files (Git Root @ commit_hash)"]
      WebServer["Managed Web Server"]
      StaticRoot --> WebServer
    end

    Agent <-->|"MCP Lifecycle & Logs"| WebMCP
    WebMCP -->|"Spawns & Monitors (tag)"| WebContext
    Agent <-->|"HTTP (url)"| WebServer
    Agent -->|"1. Push Code (commit_hash)"| Git
    WebMCP -->|"2. Checkout (commit_hash)"| Git
    Git -.->|"Populate"| StaticRoot
    Agent <-->|"Direct File Read / Write"| SharedDir
    WebServer <-->|"Serve Content / Store Uploads"| SharedDir
  end

  DataMCP["Data MCPs\n(External Data Sources)"]

  Extension <-->|"Agent Control & Telemetry"| Agent
  WebView <-->|"HTTP (url)"| WebServer
  Agent <-->|"External Data Queries"| DataMCP
  WebServer <-->|"External Data Queries"| DataMCP
```

### Code Plane vs. Data Plane

The web server environment is partitioned into two distinct planes:

1. **Code Plane — Static Files (`Git Root @ commit_hash`)**:
   * Corresponds to the root of the Git repository hierarchy where the web server application and static assets live.
   * Provisioned immutably by the **Web MCP** (`axoloctl`) when `start_server(tag, commit_hash, description)` checks out the target `commit_hash`.
   * Modifying the web server logic or static bundle requires committing to Git and passing the new `commit_hash` to `start_server`.
2. **Data Plane — Shared Runtime Directory & External Data MCPs**:
   * **Shared Runtime Directory**: A dynamic runtime directory mounted/shared between the **Agent Server** and the **Managed Web Server** inside the **Axoloctl Host**.
     * **Agent $\rightarrow$ Web Server**: The **Agent Server** can directly create or update files in the shared runtime directory (including data fetched from external **Data MCPs**) so the **Managed Web Server** immediately serves dynamic content without requiring a Git commit or server restart.
     * **Web Server $\rightarrow$ Agent**: Files uploaded or generated via the **Managed Web Server** (e.g., user uploads from the **Web View**) are written to the shared runtime directory where the **Agent Server** can directly read and process them.
   * **Data MCPs (`External Data Sources`)**: External MCP servers located outside the **Axoloctl Host** that provide both the **Agent Server** and the **Managed Web Server** with access to external data sources, APIs, and domain services.

### Components

* **Axoloctl Host**:
  * **Agent Server**: Queries external data sources via the **Data MCPs**, commits/pushes application code changes to the Git repository (**Code Plane**), reads/writes dynamic artifacts in the **Shared Runtime Directory** (**Data Plane**), manages the web server lifecycle through the **Web MCP**, connects to the **Managed Web Server** over `HTTP (url)`, and communicates with the **Browser Extension**.
  * **Web MCP (`axoloctl`)**: Fetches the requested `commit_hash` from Git and manages the **Web Context** for each session `tag`.
  * **Web Context**: Isolated runtime context managed by the **Web MCP** that binds the immutable **Static Files (`Git Root @ commit_hash`)** and the dynamic **Shared Runtime Directory** to the **Managed Web Server** (`HTTP (url)` endpoint for both the **Agent Server** and the **Web View**, with direct connectivity to external **Data MCPs**).
* **Data MCPs (`External Data Sources`)**: External MCP interfaces providing domain data and external system state to both the **Agent Server** and the **Managed Web Server**.
* **Web Browser**:
  * **Web View**: Connects to the **Managed Web Server** over `HTTP (url)` to render the web application and upload/download data-plane content.
  * **Browser Extension**: Connects directly to the **Agent Server** to coordinate browser navigation, page reloads, and client-side telemetry on the **Web View**.

---

## Lifecycle Workflow

### 1. Development

```mermaid
sequenceDiagram
  participant Ext as Browser Extension
  participant Agent as Agent Server
  participant Git as Git Repository
  participant DataMCP as Data MCPs

  Ext->>Agent: Send development prompt
  Agent->>DataMCP: External Data Queries
  DataMCP-->>Agent: Returned domain data used to design interactive interface
  Agent->>Git: Commit/push code changes (produces <commit_hash>)
```

### 2. Deployment

```mermaid
sequenceDiagram
  participant Ext as Browser Extension
  participant View as Browser Web View
  participant Agent as Agent Server
  participant WebMCP as Web MCP
  participant Git as Git Repository
  participant Web as Managed Web Server (tag)

  Agent->>WebMCP: start_server(tag, commit_hash, description)
  WebMCP->>Git: Fetch & verify <commit_hash>
  WebMCP->>Web: Deploy & launch session <tag> at <commit_hash>
  Web-->>WebMCP: Endpoint ready & streaming stdout/stderr logs
  WebMCP-->>Agent: Return (running, url, cursor, logs)
  Agent->>Ext: Navigate / reload Web View at <url>
  Ext->>View: Load <url>
```

### 3. Ongoing Use

```mermaid
sequenceDiagram
  participant View as Browser Web View
  participant Web as Managed Web Server (tag)
  participant DataMCP as Data MCPs

  View->>Web: HTTP (url) requests
  Web->>DataMCP: External Data Queries
  DataMCP-->>Web: Domain data & external state
  Web-->>View: HTTP responses / application data
```

---

## MCP Tool Interface

`axoloctl` exposes five canonical MCP tools:

### 1. `start_server`
Deploys the specified Git commit hash for the given session `tag` and starts the web server via `bin/serve`. If a session with the same `tag` is already running, `start_server` cleanly stops the existing instance before starting the new revision on the same sticky port and `url`.

* **Arguments**:
  * `tag` (`string`, **required**): Unique identifier for the web server session (e.g., `"ui-dev"`, `"pr-42"`).
  * `commit_hash` (`string`, **required**): The 40-character hexadecimal Git commit SHA (`^[0-9a-f]{40}$`) of the code to run.
  * `description` (`string`, **required**): Human-readable description of the web server session (returned by `list_servers`).
* **Returns**:
  * `running` (`boolean`): `true` when the web server session passes the readiness probe (`HTTP GET <url>` status `< 500`); `false` if startup fails or times out.
  * `url` (`string | null`): Complete, immutable, sticky URL assigned to the session `tag` (returned only by `start_server`; `null` if startup fails).
  * `cursor` (`integer`): Initial log cursor position after startup.
  * `logs` (`string[]`): Initial startup log lines captured during launch (or failure diagnostics if `running` is `false`).

### 2. `stop_server`
Stops the running web server session identified by `tag` while preserving its per-tag **Shared Runtime Directory** (`<shared_root>/<tag>/`) and sticky port assignment for future restarts.

* **Arguments**:
  * `tag` (`string`, **required**): Session identifier of the web server to stop.
* **Returns**:
  * `running` (`boolean`): `false`.
  * `exit_code` (`integer | null`): Exit status code of the terminated server session.
  * `logs` (`string[]`): Trailing log lines emitted during shutdown.

### 3. `get_status`
Returns the current lifecycle state and deployed `commit_hash` of the web server session identified by `tag`.

* **Arguments**:
  * `tag` (`string`, **required**): Session identifier of the web server to query.
* **Returns**:
  * `running` (`boolean`): Whether the web server session is currently active.
  * `commit_hash` (`string`): Git commit SHA of the session (returned only by `get_status`).
  * `exit_code` (`integer | null`): Exit status code if the server session has stopped.

### 4. `list_servers`
Lists all currently active remote web server sessions.

* **Arguments**: None (`{}`)
* **Returns**:
  * `servers` (`object[]`): Array of active server session summaries, each containing:
    * `tag` (`string`): Session identifier.
    * `description` (`string`): Description provided when the session was started.

### 5. `read_logs`
Streams captured unified log lines (`[server]` `stdout`/`stderr` from `bin/serve` and `[browser]` client-side console/runtime errors forwarded from the **Web View**) for the session identified by `tag` starting from a line offset cursor.

* **Arguments**:
  * `tag` (`string`, **required**): Session identifier of the web server whose logs are being read.
  * `cursor` (`integer`, optional, default `0`): Zero-based line offset returned as `next_cursor` (or `cursor` from `start_server`) from a previous call.
  * `max_lines` (`integer`, optional, default `200`): Maximum number of log lines to return in a single call.
* **Returns**:
  * `running` (`boolean`): Whether the web server session is currently active.
  * `lines` (`string[]`): Unified `[server]` and `[browser]` log lines from `cursor` up to `cursor + max_lines`.
  * `next_cursor` (`integer`): Updated cursor offset for subsequent incremental `read_logs` calls.

---

## Runtime & Boundary Specifications

### 1. Web Context Execution Contract
* **Read-Only Code Plane**: When `start_server(tag, commit_hash, description)` is invoked, `Web MCP` checks out `<commit_hash>` into an isolated session code directory (`<runtime_root>/sessions/<tag>/code`) and marks the tree read-only (`chmod -R a-w`). Any runtime file writes attempted inside the Code Plane fail immediately at the OS level, enforcing that mutable state goes to the Data Plane.
* **Canonical Entrypoint (`bin/serve`)**: `Web MCP` executes `./bin/serve` from the root of the read-only `<commit_hash>` checkout. If `bin/serve` is missing or not executable, `start_server` fails immediately.
* **Environment Variables**: `Web MCP` injects the following canonical environment variables into the `bin/serve` process:
  * `AXOLOCTL_PORT`: Local TCP port assigned to `tag`.
  * `AXOLOCTL_DATA_DIR`: Absolute path to the session's persistent **Shared Runtime Directory** (`<shared_root>/<tag>/`).
  * `AXOLOCTL_MCP_PROXY_URL`: Base HTTP URL of the host's local HTTP-to-MCP proxy for invoking external **Data MCPs**.
  * `AXOLOCTL_TAG`: Active session identifier (`tag`).
  * `AXOLOCTL_COMMIT`: Deployed 40-character hexadecimal Git commit SHA (`commit_hash`).

### 2. Sticky URL & Port Allocation per `tag`
* Each session `tag` is assigned a sticky local port (`AXOLOCTL_PORT`) and access `url` that remains constant across `start_server` redeployments and across `stop_server` / restart cycles for the same `tag`.
* Keeping the origin (`url`) invariant across iterative commits preserves browser state (`localStorage`, session cookies, DevTools state) and allows the **Browser Extension** to reload the **Web View** in place.

### 3. Per-Tag Shared Runtime Directory (`Data Plane`) Lifecycle
* The **Shared Runtime Directory** is scoped per session `tag` at `<shared_root>/<tag>/` and exposed to both the **Agent Server** and the **Managed Web Server** (`AXOLOCTL_DATA_DIR`).
* **Persistence**: Contents of `<shared_root>/<tag>/` are preserved across `start_server` code redeployments (which replace only the read-only `code/` directory) and across `stop_server(tag)` invocations, ensuring uploaded files, cached query results, and application state survive iterative development.

### 4. Data MCP Connectivity (`AXOLOCTL_MCP_PROXY_URL`)
* To allow both the **Agent Server** and the **Managed Web Server** to query external **Data MCPs** concurrently without `stdio` pipe contention or requiring an MCP client SDK inside every web app, the **Axoloctl Host** exposes a local HTTP-to-MCP bridge at `AXOLOCTL_MCP_PROXY_URL`.
* The **Managed Web Server** invokes **Data MCP** tools via standard HTTP JSON requests (`POST ${AXOLOCTL_MCP_PROXY_URL}/<mcp_server>/<tool_name>`) and receives structured JSON responses.

### 5. Readiness Probe & Startup Failure Semantics
* After spawning `bin/serve`, `Web MCP` polls `HTTP GET <url>` (succeeding on any HTTP status `< 500`) for up to a fixed startup timeout (`10 seconds`) while monitoring the child PID.
* If `bin/serve` exits prematurely or fails to respond with HTTP `< 500` within `10 seconds`, `Web MCP` terminates the process tree and returns `{ running: false, url: null, cursor, logs }` containing the captured `stdout`/`stderr` output so the **Agent Server** can immediately diagnose the startup failure.

### 6. Browser Extension Integration & Unified Log Stream
* **Control Channel**: The **Browser Extension** connects directly to the **Agent Server** to accept navigation and page-reload commands after `start_server` succeeds.
* **Unified Telemetry (`[server]` + `[browser]`)**: Client-side runtime errors (`console.error`, uncaught exceptions, failed resource loads) captured from the **Web View** are forwarded into the session's log buffer with a `[browser]` prefix alongside `[server]` `stdout`/`stderr` lines, allowing `read_logs(tag)` to provide a single, time-ordered diagnostic stream.

---

## Browser Extension $\leftrightarrow$ Agent Server Integration

The **Browser Extension** is a **static Chrome Extension (Manifest V3 Side Panel)** designed to provide a clean, user-friendly interface without exposing developer-only tools (such as Chrome DevTools) and without ever requiring updates when the **Managed Web Server** application pages or the **Agent UIs** evolve.

Rather than hardcoding specific UI implementations inside the extension bundle, the **Axoloctl Host** exposes a dynamic catalog of available Agent UIs (`GET /api/uis` — e.g., `{ cliView, hubView, apiView }`), and the Chrome Side Panel renders a dynamic dropdown selector above a **single `<iframe id="agent-viewport">`**.

```mermaid
flowchart TB
  subgraph SidePanel["Chrome Side Panel (sidepanel.html)"]
    direction TB
    Header["Unified Control Bar\n(Session Status, Context Actions, Dynamic UI Dropdown)"]
    Viewport["Single Viewport Container\n(<iframe id='agent-viewport' src='...'>)"]
    Header -->|"Sets iframe.src"| Viewport
  end

  BgWorker["Extension Background Worker\n(Tab Reload & [browser] Error Capture)"]

  subgraph Host["Axoloctl Host / Agent Server"]
    direction TB
    UIRegistry["UI Discovery Endpoint\n(GET /api/uis)"]
    AgentAPI["Agent API Side-Channel\n(agentapi send-message <conv-id>)"]
    WebMCP["Web MCP\n(Reload & Unified Logs)"]

    subgraph HostUIs["Host-Served Agent UIs"]
      direction LR
      CLIView["cliView\n(/ui/cli — xterm.js + PTY)"]
      HubView["hubView\n(/ui/hub — Agent Web Hub)"]
      APIView["apiView\n(/ui/api — Custom Agent API Chat)"]
    end
  end

  Header -->|"1. Fetch UI List"| UIRegistry
  Header -->|"2. Prompts & Page Context"| AgentAPI
  Viewport <-->|"3. Load Selected UI URL"| HostUIs
  BgWorker <-->|"Reload Events & [browser] Errors"| WebMCP
```

### 1. Invariant Extension Layer (Shared Across All UIs)
Because the Chrome Extension only hosts the control bar, the single `<iframe id="agent-viewport">`, and the background service worker, three core mechanisms operate identically regardless of which UI is selected:
1. **Dynamic UI Discovery (`GET /api/uis`)**:
   * On startup, the Side Panel fetches the list of available UIs from the **Axoloctl Host**:
     ```json
     {
       "default_ui": "cliView",
       "uis": [
         { "id": "cliView", "label": "CLI Console", "url": "http://localhost:8080/ui/cli" },
         { "id": "hubView", "label": "Web Hub", "url": "http://localhost:8080/ui/hub" },
         { "id": "apiView", "label": "Custom Chat (Agent API)", "url": "http://localhost:8080/ui/api" }
       ]
     }
     ```
   * The extension populates its `<select>` dropdown from `uis` and binds the selected entry's `url` directly to `<iframe id="agent-viewport">`. If `uis` contains only a single entry (`uis.length === 1`), the dropdown selector is automatically hidden and that single UI candidate is loaded directly. Adding, removing, or modifying a UI on the host requires **zero changes to the Chrome Extension**.
2. **`agentapi` Side-Channel (`agentapi send-message <conversation-id>`)**:
   * All host-served UIs (`cliView`, `hubView`, `apiView`) attach to the same active `<conversation-id>` on the **Agent Server**.
   * When the extension's control bar sends page context or prompts via the `agentapi` side-channel, the active conversation updates immediately in whichever UI is currently loaded in the `<iframe>`.
3. **Automatic Tab Reload & `[browser]` Error Capture**:
   * The extension's background worker listens for `start_server` deployment events to reload/navigate the primary **Web View** tab and streams client-side `console.error` / uncaught exceptions to `Web MCP` (`[browser]` prefix in `read_logs(tag)`).

### 2. Pluggable Host-Served UI Implementations (`{ cliView, hubView, apiView }`)

| UI ID | Host Endpoint | Underlying Mechanism | Strengths |
| :--- | :--- | :--- | :--- |
| **`cliView`** | `/ui/cli` | Host serves an `xterm.js` terminal page connected over WebSocket to the **Agent CLI** PTY. | 100% CLI feature parity; focused stream of agent actions without extra navigation chrome. |
| **`hubView`** | `/ui/hub` | Proxies/serves the native **Agent Web Hub** (framed via a static Manifest V3 `declarativeNetRequest` rule). | Complete rich GUI (Markdown, Mermaid diagrams, artifacts, interactive `ask_question` modals) with zero UI re-implementation. |
| **`apiView`** | `/ui/api` | Host serves a streamlined, custom HTML/JS chat page powered by the **Agent API** (`agentapi`). | Purpose-built, simplified user interface tailored specifically for non-developer audiences. |

---

## Agentic Web Application Engineering & Verification

While the [Theory of Operation](#theory-of-operation) describes the human-facing workflow, this section defines how the active **Agent** pragmatically engineers, tests, and deploys web server code. Authoritative operational rules for the Agent are maintained in [`AGENTS.md`](AGENTS.md).

### 1. Data MCP Probing & Schema Discovery

Before writing or modifying any web server code, the Agent directly queries the external **Data MCPs** (`butler`, `barbican`, `uufi`) during the initial chat session with the operator to:
* Inspect the exact structure, column names, nested JSON paths, and data types returned by the MCP tools.
* Verify that the target records exist and test candidate filter predicates, joins, and correlations on a concrete sample device or site.
* Use the operator's conversational feedback (which filters are needed, how columns should be grouped or compared, and what actions should be exposed) as the specification for the web application's backend API routes and tabular frontend columns.

### 2. Background Web Application Synthesis

While working through the preliminary example with the operator in chat, the Agent delegates or executes web application construction in the background inside `var/axoloctl/workspace/` so conversational interaction remains responsive:
* **Server-Side Tabular Processing**: The web server queries the **Data MCPs** through `AXOLOCTL_MCP_PROXY_URL` (`POST /mcp/<server_name>/<tool_name>`) or reads staged datasets in `AXOLOCTL_DATA_DIR`, performing all filtering, multi-source correlation, sorting, and pagination (`LIMIT` / `OFFSET`) on the server rather than shipping unbounded fleet tables to the browser.
* **Code vs. Data Plane Separation**: All code and templates are committed to the Git working tree (`var/axoloctl/workspace/`) and started via `./bin/serve` on `AXOLOCTL_PORT`. All mutable state (SQLite databases, user filter presets, staging imports) is written exclusively to `AXOLOCTL_DATA_DIR`.

### 3. Mandatory Verification Gate (Unit + Playwright E2E Tests)

No web server revision may be pushed or deployed via `start_server` until it passes both automated test tiers in `var/axoloctl/workspace/`:

1. **Backend Unit Tests (`pytest tests/unit`)**:
   * Validate backend route handlers, MCP proxy payload parsing, filter/correlation logic, pagination bounds, and fail-fast error responses against representative fixture data.
2. **Playwright End-to-End Browser Tests (`pytest tests/e2e`)**:
   * Launch the web application against an isolated test port and `AXOLOCTL_DATA_DIR`, and drive a headless Chromium instance via Playwright to verify the complete operator workflow:
     * Tabular columns render the expected device rows and correlated attributes.
     * Interactive filter controls, search inputs, and pagination update the rendered table state deterministically.
     * Detail views and mutation workflows (e.g., configuration diff inspection or staged rollouts) execute end-to-end.
     * Zero client-side `console.error` messages, uncaught `pageerror` exceptions, or HTTP `5xx` responses occur during test execution.

### 4. Git Deployment & Operator Handoff

Once both unit and Playwright test suites pass:

```bash
# 1. Run backend unit tests and Playwright E2E browser tests
pytest tests/unit tests/e2e

# 2. Commit and push the verified revision to the local bare Git repository
git -C var/axoloctl/workspace add -A
git -C var/axoloctl/workspace commit -m "Add tabular view and filters for <workflow>"
git -C var/axoloctl/workspace push origin main
COMMIT_HASH=$(git -C var/axoloctl/workspace rev-parse HEAD)
```

The Agent then invokes `start_server(tag, commit_hash, description)` via `Web MCP`, inspects `read_logs(tag)` to confirm zero `[server]` or `[browser]` startup errors, and notifies the operator in chat that the custom web utility is live at `url` (and automatically reloaded in their **Browser Web View**).

---

## Local Development Setup

In a local UDMI development environment, the external **Data MCPs** are the standard UDMI MCP servers (`butler`, `barbican`, `uufi`), the **Git Repository** is a local on-disk bare repository, and process isolation is managed across **two dedicated `tmux` sessions** (one for the **Agent**, one for the **Web Server**) alongside the standard UDMI service sessions.

### 1. Required Components Overview

1. **UDMI Backend & Data MCPs**:
   * Standard UDMI local services (`udmi_barbican` and `udmi_butler` `tmux` sessions started via `bin/udmi start`).
   * UDMI Data MCP entrypoints: [`bin/mcp_butler`](../../bin/mcp_butler) (device inventory, state/config tables, managed rollouts), [`bin/mcp_barbican`](../../bin/mcp_barbican) (etcd/mosquitto/UDMIS state), and [`bin/mcp_uufi`](../../bin/mcp_uufi) (UUFI operations).
2. **On-Disk Runtime Layout (`$UDMI_ROOT/var/axoloctl/`)**:
   * `repo.git/`: Local bare Git repository acting as the immutable **Code Plane** remote (`file://$UDMI_ROOT/var/axoloctl/repo.git`).
   * `workspace/`: Local Git working tree where the **Agent Server** edits code, commits, and pushes to `repo.git`.
   * `sessions/<tag>/code/`: Read-only (`chmod -R a-w`) checkout of `<commit_hash>` provisioned by `Web MCP` on `start_server`.
   * `shared/<tag>/`: Persistent per-tag **Shared Runtime Directory** (`AXOLOCTL_DATA_DIR`) shared between the **Agent Server** and the **Managed Web Server**.
3. **Two Dedicated `axoloctl` `tmux` Sessions**:
   * **`udmi_axoloctl_agent` (Agent Session)**:
     * `agent`: Runs the **Agent Server / CLI** inside `var/axoloctl/workspace/`, configured with `Web MCP` (`axoloctl`) and the UDMI **Data MCPs** (`butler`, `barbican`, `uufi`).
     * `ui_host`: Runs the host gateway serving `GET /api/uis`, the pluggable Agent UIs (`/ui/cli`, `/ui/hub`, `/ui/api`), the `agentapi` side-channel, and the HTTP-to-MCP proxy (`AXOLOCTL_MCP_PROXY_URL`).
   * **`udmi_axoloctl_web` (Web Server Session)**:
     * `web_mcp`: Runs the `axoloctl` **Web MCP** lifecycle daemon and browser reload/log collector.
     * `<tag>` (e.g., `gummi`): Dedicated `tmux` window per active session `tag` executing `./bin/serve` from `var/axoloctl/sessions/<tag>/code/`.
4. **Additional Developer Prerequisites**:
   * **Unified `mcp_config.json`**: Registers `axoloctl` (`Web MCP`) alongside `butler`, `barbican`, and `uufi` (**Data MCPs**) so both the **Agent Server** and `AXOLOCTL_MCP_PROXY_URL` share a single source of truth.
   * **Seed Repository Commit (`bin/serve`)**: The local `repo.git` is initialized with a baseline commit containing an executable `bin/serve` script (e.g., launching the GUMMI Flask server on `AXOLOCTL_PORT`) so the initial session can be started immediately.
   * **Unpacked Chrome Extension**: Loaded once in Chrome (`chrome://extensions` $\rightarrow$ *Developer mode* $\rightarrow$ *Load unpacked* pointing to `mcp/axoloctl/extension/`).

### 2. Step-by-Step Developer Bootstrap

```bash
# 1. Install base dependencies (unprivileged user-space default)
bin/setup_base

# 2. Start local UDMI infrastructure (Barbican & Butler) on unprivileged ports
bin/udmi start sites/udmi_site_model //mqtt/localhost:18833

# 3. Initialize local on-disk Git repository, workspace, and shared runtime directories
mkdir -p var/axoloctl/shared
git init --bare var/axoloctl/repo.git
git clone var/axoloctl/repo.git var/axoloctl/workspace

# 4. Seed baseline executable entrypoint (bin/serve) in the local workspace and push
mkdir -p var/axoloctl/workspace/bin
cp -r gummi/* var/axoloctl/workspace/
chmod +x var/axoloctl/workspace/bin/serve
git -C var/axoloctl/workspace add -A
git -C var/axoloctl/workspace commit -m "Initial web app baseline"
git -C var/axoloctl/workspace push -u origin main

# 5. Start the two axoloctl tmux sessions (udmi_axoloctl_web and udmi_axoloctl_agent)
bin/tmux_axoloctl start //mqtt/localhost:18833
```

### 3. Inspecting & Attaching to the `tmux` Sessions

| Session Name | Windows | Purpose | Command to Inspect / Attach |
| :--- | :--- | :--- | :--- |
| **`udmi_axoloctl_agent`** | `agent`, `ui_host` | Runs the **Agent Server** in `var/axoloctl/workspace/`, the host UI endpoints (`/ui/*`), and `AXOLOCTL_MCP_PROXY_URL`. | `tmux attach -t udmi_axoloctl_agent` |
| **`udmi_axoloctl_web`** | `web_mcp`, `<tag>` | Runs the **Web MCP** controller and each deployed **Managed Web Server** session window (`<tag>`). | `tmux attach -t udmi_axoloctl_web` |
| **`udmi_butler`** | `postgres`, `influxdb`, `butler`, `registrar` | Backing datastore and Butler service for [`bin/mcp_butler`](../../bin/mcp_butler). | `bin/tmux_butler status` |
| **`udmi_barbican`** | `mosquitto`, `etcd`, `udmis` | Backing MQTT broker, etcd state store, and UDMIS pipeline for [`bin/mcp_barbican`](../../bin/mcp_barbican). | `bin/tmux_barbican status` |

---

## Fail-Fast Guarantees

* **No Implicit Branch or `HEAD` Fallbacks**: `start_server` requires an explicit 40-character Git commit hash (`commit_hash`). Symbolic refs (such as `HEAD` or branch names) and missing commits are rejected immediately with an error.
* **Mandatory Canonical Entrypoint**: If the checked-out `commit_hash` does not contain an executable `bin/serve` at the repository root, `start_server` fails immediately without falling back to generic static file serving.
* **Unknown Session Tag Rejection**: Calling `stop_server`, `get_status`, or `read_logs` with an unknown `tag` fails immediately with an explicit error.
* **Startup Readiness Verification**: `start_server` verifies that the web server session responds to `HTTP GET <url>` (`status < 500`) within the `10s` startup timeout window; if startup fails, `start_server` terminates the process and returns `{ running: false, url: null, cursor, logs }`.
* **Explicit Stop Semantics**: Calling `stop_server` for a `tag` that is not currently running fails immediately with an explicit error rather than silently succeeding.
