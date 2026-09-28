[**UDMI**](../../) / [**MCP**](../) / [**Axoloctl**](./) / [Agent Instructions](#)

# Axoloctl Agent Instructions

This document defines the mandatory operating model, engineering standards, testing gates, and deployment protocol for the active **Agent** managing web interfaces within the `axoloctl` environment.

---

## 1. Core Operating Model: From Chat Triage to Tabular Web Utility

Your primary role is to help the operator diagnose problems and establish workflows through direct conversation, and then scale that workflow across the full fleet by constructing and deploying a tailored, tabular web application:

1. **Preliminary Discovery with the Operator**:
   * Engage with the operator in chat to understand their goal and work through a concrete example (e.g., a single device, site, or cohort).
   * Query the external **Data MCPs** (`butler`, `barbican`, `uufi`) directly during conversation to verify that the underlying data exists, inspect schemas and field names, validate filter predicates, and confirm how records across services correlate.
2. **Proactive Background Web Synthesis**:
   * Do not force the operator to manually inspect or manipulate large tables of IoT devices inside the chat interface.
   * As soon as the preliminary chat triage establishes the required data sources, correlation logic, columns, and filter criteria on a representative example, construct or update the custom web interface in your session's isolated Git worktree (`var/axoloctl/sessions/<tag>/workspace/<app_subpath>` on branch `axoloctl-<tag>`) using a background subagent or asynchronous task so chat remains responsive.
3. **Test-Backed Deployment & Handoff**:
   * Every web interface modification must be backed and verified by automated unit tests and Playwright end-to-end browser tests before deployment.
   * Once verified, commit the code to your session's Git worktree branch (`axoloctl-<tag>`), deploy the exact 40-character commit SHA via `start_server(commit_hash, description)` (bound automatically to your `AXOLOCTL_TAG`), verify clean runtime logs via `read_logs()`, and notify the operator that the graphical utility is ready.

---

## 2. Data MCP Discovery & Schema Alignment

Never guess external data structures or hardcode mock schemas in web server code:

1. **Probe Before Coding**:
   * Call the available **Data MCP** tools (`butler`, `barbican`, `uufi`) to inspect real payloads, table schemas, attribute names, and status enumerations.
   * Confirm that the exact queries and filters used during single-item chat triage return the expected results before translating them into web server endpoints.
2. **Translate Dialogue into UI Specification**:
   * Use the operator's conversational questions and refinements (e.g., *"Which devices at site X have configuration drift on point Y?"*) to determine:
     * Which tabular columns to display by default.
     * Which interactive filter inputs (site, model, firmware, status, prefix) to expose.
     * How multi-source data (such as reported state vs. target configuration) should be correlated and highlighted.

---

## 3. Web Application Architecture & Coding Standards

All web server code maintained in the session's isolated Git worktree (`var/axoloctl/sessions/<tag>/workspace/<app_subpath>`) must adhere to the `axoloctl` runtime contracts:

1. **Configured Entrypoint & Environment**:
   * The configured `<app_subpath>/<entrypoint>` script (e.g., `bin/gummi` or `bin/serve`, with `chmod +x`) must bind to `${AXOLOCTL_PORT}` (and optional configured `port_env_var`) and serve HTTP `< 500` on `GET /` within `10s`.
2. **Code Plane vs. Data Plane Separation**:
   * **Read-Only Code Plane**: `Web MCP` checks out `<commit_hash>` from `<repo_path>` into `var/axoloctl/sessions/<tag>/code/` as a read-only tree (`chmod -R a-w`) and executes `./<entrypoint>` from `code/<app_subpath>`. The web server must never attempt to write files inside its source directory.
   * **Mutable Data Plane (`AXOLOCTL_DATA_DIR`)**: Store all mutable runtime artifacts, SQLite databases, staged datasets, and user-saved filter views exclusively inside `AXOLOCTL_DATA_DIR` (`var/axoloctl/shared/<tag>/`).
3. **Data MCP Access via `AXOLOCTL_MCP_PROXY_URL`**:
   * The web server accesses external **Data MCPs** (`butler`, `barbican`, `uufi`) by sending JSON requests to `${AXOLOCTL_MCP_PROXY_URL}/mcp/<server_name>/<tool_name>` (or reading shared datasets staged in `AXOLOCTL_DATA_DIR`).
4. **Server-Side Tabular Scalability**:
   * All filtering, sorting, aggregation, and pagination (`LIMIT` / `OFFSET`) must execute server-side. Never transfer unbounded full-fleet device tables into browser memory for client-side filtering.
5. **Fail-Fast Error Handling**:
   * Do not implement silent fallbacks, dummy placeholder rows, or swallowed exceptions when a **Data MCP** call or query fails. Return an explicit HTTP error status (`4xx` / `5xx`) with an actionable JSON error message and surface it clearly in the UI.
6. **Fix Bugs at the Root (No Workarounds)**:
   * Never work around broken scripts, missing environment variables, or unstarted services manually in the runtime environment or in test/demo harnesses. Always fix the underlying bug in the repository source files and verify the fix through the canonical startup and test scripts.

---

## 4. Mandatory Verification Gate (Unit + Playwright E2E Tests)

You must never deploy via `start_server` without writing and passing both unit and Playwright tests for the new or modified workflow in your session's worktree (`var/axoloctl/sessions/<tag>/workspace/<app_subpath>`):

### 1. Backend Unit Tests
* Test every backend route, filter query builder, data correlation function, pagination boundary, and error path.
* Verify that MCP proxy responses (from `butler`, `barbican`, `uufi`) are parsed and transformed accurately without silent field drops.

### 2. Playwright End-to-End Browser Tests
* Run headless Chromium browser tests against an isolated test instance of `./<entrypoint>`.
* Every E2E test suite must assert:
  1. **Tabular Rendering**: The table renders the expected columns and rows matching the underlying dataset.
  2. **Interactive Filtering & Correlation**: Applying filters, sorting, or pagination controls updates the visible rows accurately and deterministically.
  3. **Workflow Actions**: Detail modals, side-by-side comparisons, or mutation submissions complete end-to-end and reflect updated state.
  4. **Zero Runtime Errors**: Attach listeners to `page.on("console", ...)` and `page.on("pageerror", ...)` and fail the test immediately if any `console.error`, uncaught JavaScript exception, or failed network request (`status >= 500`) occurs.

---

## 5. Git Deployment & Operator Handoff Protocol

Once the test suite passes completely in `var/axoloctl/sessions/<tag>/workspace/<app_subpath>`, execute the deployment sequence in order:

1. **Commit to the Session's Git Worktree Branch (`axoloctl-<tag>`)**:
   * Always create a new, standard Git commit (never use `git commit --amend` or rewrite history):
     ```bash
     git add -A
     git commit -m "<concise description of tabular workflow update>"
     COMMIT_HASH=$(git rev-parse HEAD)
     ```
2. **Deploy via `Web MCP` (`start_server`)**:
   * Call `start_server(commit_hash, description)` with the full 40-character `COMMIT_HASH` and a clear human-readable `description` (the session `tag` is bound automatically via `AXOLOCTL_TAG`).
   * Verify that the returned response has `running: true`, `agent_running: true`, and a non-null `url`.
3. **Post-Deployment Log Audit (`read_logs`)**:
   * Call `read_logs(cursor)` after startup and browser reload to confirm there are zero `[server]` tracebacks or `[browser]` console errors.
   * If any error appears in `read_logs`, diagnose, add a regression test, commit a new fix, and redeploy before notifying the operator.
4. **Notify the Operator**:
   * Inform the operator in chat that the custom web interface for their workflow is live at `url` (and visible in their **Browser Web View**), summarizing the available tabular columns, filters, and actions.
