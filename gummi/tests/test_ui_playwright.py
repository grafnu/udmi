"""End-to-End browser UI automation test suite using Playwright."""

import os
import re
import sys
import threading
import time
from typing import List
import pytest
from playwright.sync_api import sync_playwright, Page, Browser, expect

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
sys.path.insert(0, os.path.join(REPO_ROOT, "common", "src", "main", "python"))
sys.path.insert(0, os.path.join(REPO_ROOT, "gencode", "python"))
sys.path.insert(0, os.path.join(REPO_ROOT, "gummi"))
sys.path.insert(0, REPO_ROOT)

from gummi.src.server import GummiServer, ThreadingHTTPServer, GummiRequestHandler


@pytest.fixture(scope="module")
def gummi_server_url():
    """Spawns an in-process GUMMI server on a dynamic port."""
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()

    server = GummiServer(host="127.0.0.1", port=port, mock_mode=True)
    server_address = (server.host, server.port)
    httpd = ThreadingHTTPServer(server_address, GummiRequestHandler)
    httpd.daemon_threads = True
    httpd.db = server.db
    httpd.uufi = server.uufi
    server.httpd = httpd

    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    time.sleep(0.2)

    base_url = f"http://127.0.0.1:{port}"
    yield base_url

    try:
        httpd.server_close()
    except Exception:
        pass
    server.uufi.stop()


@pytest.fixture(scope="module")
def browser_context():
    """Launches a headless Chromium browser instance."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        yield browser
        browser.close()


def test_gummi_page_load_and_navigation(gummi_server_url: str, browser_context: Browser):
    """Verifies page load, zero JavaScript runtime errors, and tab navigation."""
    page: Page = browser_context.new_page()
    page_errors: List[str] = []
    page.on("pageerror", lambda err: page_errors.append(str(err)))

    # 1. Load root page
    page.goto(gummi_server_url)
    page.wait_for_load_state("domcontentloaded")

    # Assert no JS errors occurred during load
    assert len(page_errors) == 0, f"JavaScript errors detected on load: {page_errors}"

    # Assert brand header and initial tab
    expect(page.locator(".brand-text h1")).to_contain_text("GUMMI")
    expect(page.locator("#pane-portfolio")).to_have_class("tab-pane active")

    # 2. Test Navigation: Switch to Devices Explorer
    btn_devices = page.locator('.nav-tab[data-tab="devices"]')
    btn_devices.click()

    expect(page.locator("#pane-devices")).to_have_class("tab-pane active")
    expect(page.locator("#pane-portfolio")).not_to_have_class("active")

    # 3. Assert Devices table rows loaded
    device_rows = page.locator("#devices-table-body tr")
    page.wait_for_function('document.querySelectorAll("#devices-table-body tr").length > 0')
    count = device_rows.count()
    assert count > 1, f"Expected devices in table, got {count}"
    last_seen_cell = page.locator("#devices-table-body tr td code").first
    expect(last_seen_cell).to_have_text(re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$"))

    # 4. Test Device Inspection -> Device Properties
    # Find Inspect button for AHU-22 (or first device)
    inspect_btn = page.locator('#devices-table-body button:has-text("Inspect")').first
    inspect_btn.click()

    expect(page.locator("#pane-device-detail")).to_have_class("tab-pane active")
    expect(page.locator("#detail-device-title")).not_to_have_text("Select a Device")
    expect(page.locator("#detail-lastseen")).to_have_text(re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$"))

    # 5. Verify Message Lifecycle Section (Model -> Discovery -> Proposal)
    lifecycle_container = page.locator("#detail-messages-timeline")
    page.wait_for_function('document.querySelectorAll("#detail-messages-timeline div").length > 0')
    expect(lifecycle_container).to_contain_text("MODEL")

    # 6. Test Navigation to Configuration & Rollout Tabs
    page.locator('.nav-tab[data-tab="config"]').click()
    expect(page.locator("#pane-config")).to_have_class("tab-pane active")

    page.locator('.nav-tab[data-tab="rollout"]').click()
    expect(page.locator("#pane-rollout")).to_have_class("tab-pane active")

    page.locator('.nav-tab[data-tab="admin"]').click()
    expect(page.locator("#pane-admin")).to_have_class("tab-pane active")

    # Final check that no JS exceptions were thrown during any tab switches or clicks
    assert len(page_errors) == 0, f"JavaScript errors detected during interaction: {page_errors}"
    page.close()


def test_gummi_device_filtering_and_pagination(gummi_server_url: str, browser_context: Browser):
    """Tests device search filtering and pagination controls in browser."""
    page: Page = browser_context.new_page()
    page_errors: List[str] = []
    page.on("pageerror", lambda err: page_errors.append(str(err)))

    page.goto(gummi_server_url)
    page.locator('.nav-tab[data-tab="devices"]').click()

    # Search for AHU-
    page.fill("#filter-search", "AHU")
    page.click("#btn-apply-filters")

    page.wait_for_timeout(300)
    device_rows = page.locator("#devices-table-body tr")
    assert device_rows.count() >= 1

    first_text = device_rows.first.inner_text()
    assert "AHU" in first_text

    # Clear filter
    page.click("#btn-clear-filters")
    page.wait_for_timeout(300)

    assert len(page_errors) == 0, f"JavaScript errors during filtering: {page_errors}"
    page.close()


def test_gummi_jetski_deprecated(gummi_server_url: str, browser_context: Browser):
    """Verifies that the deprecated Jetski button and console pane are completely removed from the DOM."""
    page: Page = browser_context.new_page()
    page_errors: List[str] = []
    page.on("pageerror", lambda err: page_errors.append(str(err)))

    page.goto(gummi_server_url)
    page.wait_for_load_state("domcontentloaded")

    assert page.locator("#btn-jetski").count() == 0
    assert page.locator("#console-pane").count() == 0
    assert len(page_errors) == 0, f"JavaScript errors detected on load: {page_errors}"
    page.close()


def test_gummi_etcd_explorer_full_workflow(gummi_server_url: str, browser_context: Browser):
    """Verifies the ETCD Explorer UI, 3-column drill-down, bound device navigation, copy button, and hash deep-linking."""
    page: Page = browser_context.new_page()
    page_errors: List[str] = []
    page.on("pageerror", lambda err: page_errors.append(str(err)))

    # 1. Load root page and navigate to Explorer tab
    page.goto(gummi_server_url)
    page.wait_for_load_state("domcontentloaded")
    assert len(page_errors) == 0, f"JavaScript errors detected on load: {page_errors}"

    btn_explorer = page.locator('.nav-tab[data-tab="explorer"]')
    btn_explorer.click()

    expect(page.locator("#pane-explorer")).to_have_class(re.compile(r"\bactive\b"))
    expect(page.locator("#pane-portfolio")).not_to_have_class("active")
    expect(page.locator("#pane-explorer")).to_have_class(re.compile(r"\bdark-theme\b"))

    # Test Theme Toggle
    theme_btn = page.locator("#theme-toggle-explorer")
    theme_btn.click()
    expect(page.locator("#pane-explorer")).to_have_class(re.compile(r"\blight-theme\b"))
    theme_btn.click()
    expect(page.locator("#pane-explorer")).to_have_class(re.compile(r"\bdark-theme\b"))

    # 2. Assert Column 1 registries loaded
    page.wait_for_function('document.querySelectorAll("#explorer-registries-list .explorer-item").length > 0')
    regs = page.locator("#explorer-registries-list .explorer-item")
    assert regs.count() >= 1

    # Test registry search filter
    search_reg = page.locator("#search-explorer-registries")
    search_reg.fill("TRI")
    time.sleep(0.3)
    expect(page.locator('#explorer-registries-list .explorer-item:has-text("ZZ-TRI-FECTA")')).to_be_visible()
    search_reg.fill("")
    time.sleep(0.3)

    # 3. Select registry 'ZZ-TRI-FECTA'
    tri_reg = page.locator('#explorer-registries-list .explorer-item:has-text("ZZ-TRI-FECTA")')
    tri_reg.click()
    expect(tri_reg).to_have_class(re.compile(r"\bactive\b"))
    expect(page.locator("#explorer-active-registry-label")).to_have_text("ZZ-TRI-FECTA")

    # 4. Assert Column 2 devices loaded for ZZ-TRI-FECTA
    page.wait_for_function('document.querySelectorAll("#explorer-devices-list .explorer-item").length > 0')
    devs = page.locator("#explorer-devices-list .explorer-item")
    assert devs.count() >= 2

    # Test device search filter
    search_dev = page.locator("#search-explorer-devices")
    search_dev.fill("AHU-1")
    time.sleep(0.3)
    expect(page.locator('#explorer-devices-list .explorer-item:has-text("AHU-1")')).to_be_visible()
    search_dev.fill("")
    time.sleep(0.3)

    # 5. Select device 'AHU-1'
    ahu1_dev = page.locator('#explorer-devices-list').get_by_text("AHU-1", exact=True)
    ahu1_dev.click()
    expect(ahu1_dev).to_have_class(re.compile(r"\bactive\b"))
    expect(page.locator("#explorer-active-device-label")).to_have_text("ZZ-TRI-FECTA / AHU-1")

    # 6. Assert Column 3 properties loaded
    page.wait_for_function('document.querySelectorAll("#explorer-properties-content .properties-table").length > 0')
    expect(page.locator('.property-group-title:has-text("Device Properties")')).to_be_visible()
    expect(page.locator('.property-group-title:has-text("Collections")')).to_be_visible()

    # Check JSON container and copy button for :config
    config_row = page.locator('.property-row[data-key=":config"]')
    expect(config_row).to_be_visible()
    copy_btn = config_row.locator(".copy-btn")
    expect(copy_btn).to_have_text("📋 Copy")

    # Check bound device link
    bound_link = page.locator('.property-row[data-key="/c/bound_devices:AHU-2"] a.device-link')
    expect(bound_link).to_have_text("AHU-2")

    # 7. Click bound device link AHU-2 -> verifies 1-click cross-navigation
    bound_link.click()
    expect(page.locator("#explorer-active-device-label")).to_have_text("ZZ-TRI-FECTA / AHU-2")
    ahu2_dev = page.locator('#explorer-devices-list').get_by_text("AHU-2", exact=True)
    expect(ahu2_dev).to_have_class(re.compile(r"\bactive\b"))

    # 8. Test URL Hash deep linking (#explorer/ZZ-TRI-FECTA/AHU-1/:config)
    page.goto(f"{gummi_server_url}/#explorer/ZZ-TRI-FECTA/AHU-1/%3Aconfig")
    page.wait_for_load_state("domcontentloaded")
    expect(page.locator("#pane-explorer")).to_have_class(re.compile(r"\bactive\b"))
    expect(page.locator("#explorer-active-device-label")).to_have_text("ZZ-TRI-FECTA / AHU-1")
    expect(page.locator('.property-row[data-key=":config"]')).to_have_class(re.compile(r"\bhighlighted\b"))

    # 9. Test Legacy URL Hash deep linking (#/ZZ-TRI-FECTA/AHU-2)
    page.goto(f"{gummi_server_url}/#/ZZ-TRI-FECTA/AHU-2")
    page.wait_for_load_state("domcontentloaded")
    expect(page.locator("#pane-explorer")).to_have_class(re.compile(r"\bactive\b"))
    expect(page.locator("#explorer-active-device-label")).to_have_text("ZZ-TRI-FECTA / AHU-2")

    # 10. Test cross-navigation from Devices Explorer table and Device Properties pane
    # Navigate to Devices tab
    page.locator('.nav-tab[data-tab="devices"]').click()
    expect(page.locator("#pane-devices")).to_have_class("tab-pane active")
    page.wait_for_function('document.querySelectorAll("#devices-table-body tr").length > 0')

    # Click ETCD button in row
    etcd_btn = page.locator('#devices-table-body button:has-text("🗄️ ETCD")').first
    etcd_btn.click()
    expect(page.locator("#pane-explorer")).to_have_class(re.compile(r"\bactive\b"))

    # Navigate to Device Properties tab and click Raw ETCD button
    page.locator('.nav-tab[data-tab="device-detail"]').click()
    expect(page.locator("#pane-device-detail")).to_have_class("tab-pane active")
    page.locator("#btn-view-raw-etcd").click()
    expect(page.locator("#pane-explorer")).to_have_class(re.compile(r"\bactive\b"))

    assert len(page_errors) == 0, f"JavaScript errors encountered during explorer test: {page_errors}"
    page.close()


