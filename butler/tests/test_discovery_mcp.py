"""Unit tests for Butler MCP discovery capabilities."""

from datetime import datetime, timezone
import json
import os
import sys
import unittest
from unittest.mock import MagicMock

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
sys.path.insert(0, os.path.join(REPO_ROOT, "common", "src", "main", "python"))
sys.path.insert(0, os.path.join(REPO_ROOT, "gencode", "python"))
sys.path.insert(0, REPO_ROOT)

from mcp.butler.provider import ButlerProvider
from mcp.butler.server import ButlerMcpServer


class ButlerDiscoveryMcpTests(unittest.TestCase):
    """Tests for get_discovery_devices, trigger_discovery_scan, and get_discovery_state."""

    def setUp(self):
        self.mock_pg = MagicMock()
        self.mock_influx = MagicMock()
        self.mock_conn = MagicMock()
        self.mock_cur = MagicMock()
        self.mock_conn.cursor.return_value.__enter__.return_value = self.mock_cur
        self.mock_pg.get_connection.return_value = self.mock_conn
        self.mock_runner = MagicMock()

        self.provider = ButlerProvider(
            pg_manager=self.mock_pg,
            influx_manager=self.mock_influx,
            project_spec="//mqtt/localhost:18833",
            site_model="sites/udmi_site_model",
            discovery_runner=self.mock_runner,
        )
        self.server = ButlerMcpServer(provider=self.provider)

    def test_get_discovery_devices_lists_supported_families(self):
        # 1. udmi_metadata rows: (device_id, families)
        meta_rows = [
            ("AHU-1", {"bacnet": {}, "vendor": {}}),
            ("GAT-123", {"bacnet": {}, "vendor": {}}),
        ]
        # 2. udmi_messages rows: (device_id, sub_folder, payload)
        msg_rows = [
            (
                "ACME-2301",
                "update",
                {
                    "discovery": {"families": {"bacnet": {}, "vendor": {}}},
                    "system": {"hardware": {"make": "ACME", "model": "2301"}},
                },
            ),
        ]
        self.mock_cur.fetchall.side_effect = [meta_rows, msg_rows]

        devices = self.provider.get_discovery_devices(registry_id="ZZ-TRI-FECTA")

        self.assertEqual(
            devices,
            {
                "ACME-2301": {
                    "bacnet": {},
                    "vendor": {},
                },
                "AHU-1": {
                    "bacnet": {},
                    "vendor": {},
                },
                "GAT-123": {
                    "bacnet": {},
                    "vendor": {},
                },
            },
        )

    def test_get_discovery_devices_requires_registry_id(self):
        with self.assertRaises(ValueError):
            self.provider.get_discovery_devices(registry_id="")

    def test_trigger_discovery_scan_dispatches_and_records_pending(self):
        self.mock_cur.fetchall.side_effect = [
            [("AHU-1", {"bacnet": {}, "vendor": {}})],
            [],
        ]
        self.mock_runner.return_value = {
            "transactionId": "TXN-12345678",
            "projectId": "localhost",
        }

        res = self.provider.trigger_discovery_scan(
            registry_id="ZZ-TRI-FECTA",
            device_id="AHU-1",
            family="bacnet",
            depth="system",
        )

        gen_ts = res["generation"]
        self.assertTrue(isinstance(gen_ts, str) and gen_ts.endswith("Z"))
        self.assertEqual(res, {"generation": gen_ts})

        self.mock_runner.assert_called_once_with(
            "//mqtt/localhost:18833",
            "ZZ-TRI-FECTA",
            "AHU-1",
            ["bacnet"],
            site_model="sites/udmi_site_model",
            all_families=["bacnet", "vendor"],
            generation=gen_ts,
            depth="system",
        )
        self.mock_pg.insert_row.assert_called_once()
        table_name, row_data = self.mock_pg.insert_row.call_args[0]
        self.assertEqual(table_name, "udmi_messages")
        self.assertEqual(row_data["sub_type"], "config")
        self.assertEqual(row_data["sub_folder"], "discovery")
        self.assertEqual(
            row_data["payload"]["families"]["bacnet"]["generation"],
            gen_ts,
        )
        self.assertEqual(
            row_data["payload"]["families"]["bacnet"]["depth"],
            "system",
        )

    def test_trigger_discovery_scan_fails_fast_for_unsupported_device_or_family(self):
        # Case 1: Device SNS-4 is not in discovery devices
        self.mock_cur.fetchall.side_effect = [
            [("AHU-1", {"bacnet": {}, "vendor": {}})],
            [],
        ]
        with self.assertRaises(ValueError) as ctx1:
            self.provider.trigger_discovery_scan(
                registry_id="ZZ-TRI-FECTA",
                device_id="SNS-4",
                family="bacnet",
            )
        self.assertIn("does not support discovery", str(ctx1.exception))

        # Case 2: Device AHU-1 does not support 'modbus'
        self.mock_cur.fetchall.side_effect = [
            [("AHU-1", {"bacnet": {}, "vendor": {}})],
            [],
        ]
        with self.assertRaises(ValueError) as ctx2:
            self.provider.trigger_discovery_scan(
                registry_id="ZZ-TRI-FECTA",
                device_id="AHU-1",
                family="modbus",
            )
        self.assertIn("does not support discovery family 'modbus'", str(ctx2.exception))

        # Case 3: Invalid depth 'parts'
        with self.assertRaises(ValueError) as ctx3:
            self.provider.trigger_discovery_scan(
                registry_id="ZZ-TRI-FECTA",
                device_id="AHU-1",
                family="bacnet",
                depth="parts",
            )
        self.assertIn("Must be one of: entries, system, details", str(ctx3.exception))

    def test_get_discovery_state_lifecycle_transitions(self):
        meta_rows = [
            ("AHU-1", {"bacnet": {}, "vendor": {}})
        ]

        # Simulate lifecycle for AHU-1:
        # 1. bacnet scan completed at 18:54:51Z, followed by heartbeat at 18:56:10Z and empty state reset at 18:56:19Z
        # 2. vendor scan completed at 18:55:25Z
        msg_rows_stopped = [
            (
                10,
                "state",
                "discovery",
                {"families": {"bacnet": {"phase": "active", "generation": "2026-09-23T03:30:10Z"}}},
                datetime(2026, 9, 29, 18, 54, 41, tzinfo=timezone.utc),
            ),
            (
                11,
                "state",
                "discovery",
                {
                    "families": {
                        "bacnet": {
                            "phase": "stopped",
                            "generation": "2026-09-23T03:30:10Z",
                            "active_count": 2,
                        }
                    }
                },
                datetime(2026, 9, 29, 18, 54, 51, tzinfo=timezone.utc),
            ),
            (
                12,
                "state",
                "discovery",
                {"families": {"vendor": {"phase": "active", "generation": "2026-09-23T03:33:26Z"}}},
                datetime(2026, 9, 29, 18, 55, 5, tzinfo=timezone.utc),
            ),
            (
                13,
                "state",
                "discovery",
                {
                    "families": {
                        "vendor": {
                            "phase": "stopped",
                            "generation": "2026-09-23T03:33:26Z",
                            "active_count": 3,
                        }
                    }
                },
                datetime(2026, 9, 29, 18, 55, 25, tzinfo=timezone.utc),
            ),
            # Periodic heartbeat with same generation should NOT overwrite last_done
            (
                14,
                "state",
                "discovery",
                {
                    "families": {
                        "vendor": {
                            "phase": "stopped",
                            "generation": "2026-09-23T03:33:26Z",
                            "active_count": 3,
                        }
                    }
                },
                datetime(2026, 9, 29, 18, 56, 19, tzinfo=timezone.utc),
            ),
            # Subsequent config/state reset (families: {}) should preserve last_done
            (
                15,
                "config",
                "discovery",
                {"families": None},
                datetime(2026, 9, 29, 18, 56, 20, tzinfo=timezone.utc),
            ),
            (
                16,
                "state",
                "discovery",
                {"families": {}},
                datetime(2026, 9, 29, 18, 56, 20, tzinfo=timezone.utc),
            ),
        ]

        # Step A: Verify stopped state and preserved last_done timestamps
        self.mock_cur.fetchall.side_effect = [
            meta_rows,
            [],
            msg_rows_stopped,
            [],
        ]
        state_stopped = self.provider.get_discovery_state("ZZ-TRI-FECTA", "AHU-1")
        self.assertEqual(
            state_stopped,
            {
                "families": {
                    "bacnet": {
                        "phase": "stopped",
                        "generation": "2026-09-23T03:30:10Z",
                        "last_done": "2026-09-29T18:54:51Z",
                        "active_count": 2,
                    },
                    "vendor": {
                        "phase": "stopped",
                        "generation": "2026-09-23T03:33:26Z",
                        "last_done": "2026-09-29T18:55:25Z",
                        "active_count": 3,
                    },
                },
            },
        )

        # Step B: Trigger new sporadic bacnet scan -> transitions bacnet to pending
        msg_rows_pending = msg_rows_stopped + [
            (
                17,
                "config",
                "discovery",
                {
                    "families": {
                        "bacnet": {"generation": "2026-09-30T10:05:00Z"},
                        "vendor": {},
                    }
                },
                datetime(2026, 9, 30, 10, 5, 0, tzinfo=timezone.utc),
            )
        ]
        self.mock_cur.fetchall.side_effect = [
            meta_rows,
            [],
            msg_rows_pending,
            [],
        ]
        state_pending = self.provider.get_discovery_state("ZZ-TRI-FECTA", "AHU-1")
        self.assertEqual(state_pending["families"]["bacnet"]["phase"], "pending")
        self.assertEqual(
            state_pending["families"]["bacnet"]["last_done"],
            "2026-09-29T18:54:51Z",
        )

        # Step C: Device reports active scan -> transitions bacnet to active
        msg_rows_active = msg_rows_pending + [
            (
                18,
                "state",
                "discovery",
                {
                    "families": {
                        "bacnet": {
                            "phase": "active",
                            "generation": "2026-09-30T10:05:00Z",
                        }
                    }
                },
                datetime(2026, 9, 30, 10, 5, 2, tzinfo=timezone.utc),
            )
        ]
        self.mock_cur.fetchall.side_effect = [
            meta_rows,
            [],
            msg_rows_active,
            [],
        ]
        state_active = self.provider.get_discovery_state("ZZ-TRI-FECTA", "AHU-1")
        self.assertEqual(state_active["families"]["bacnet"]["phase"], "active")

        # Step D: Device finishes scan with terminal negative event_no -> transitions to stopped and updates last_done
        msg_rows_done = msg_rows_active + [
            (
                19,
                "events",
                "discovery",
                {
                    "family": "bacnet",
                    "generation": "2026-09-30T10:05:00Z",
                    "event_no": -3,
                },
                datetime(2026, 9, 30, 10, 5, 12, tzinfo=timezone.utc),
            )
        ]
        self.mock_cur.fetchall.side_effect = [
            meta_rows,
            [],
            msg_rows_done,
            [],
        ]
        state_done = self.provider.get_discovery_state("ZZ-TRI-FECTA", "AHU-1")
        self.assertEqual(
            state_done["families"]["bacnet"],
            {
                "phase": "stopped",
                "generation": "2026-09-30T10:05:00Z",
                "last_done": "2026-09-30T10:05:12Z",
                "active_count": 2,
            },
        )

    def test_mcp_server_tools_list_and_rpc_dispatch(self):
        tools_resp = self.server.handle_request({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        tool_names = {t["name"] for t in tools_resp["result"]["tools"]}
        self.assertIn("get_discovery_devices", tool_names)
        self.assertIn("trigger_discovery_scan", tool_names)
        self.assertIn("get_discovery_state", tool_names)

    def test_butler_client_http_round_trip(self):
        from http.server import HTTPServer
        import threading
        from mcp.butler.client import ButlerClient
        from mcp.butler.server import ButlerMcpHttpHandler

        ButlerMcpHttpHandler.server_instance = self.server
        httpd = HTTPServer(("127.0.0.1", 0), ButlerMcpHttpHandler)
        port = httpd.server_port
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            client = ButlerClient(port=port)
            meta_rows = [
                ("AHU-1", {"bacnet": {}, "vendor": {}})
            ]
            # 1. get_discovery_devices via ButlerClient
            self.mock_cur.fetchall.side_effect = [meta_rows, []]
            devs = client.get_discovery_devices(registry_id="ZZ-TRI-FECTA")
            self.assertEqual(
                devs,
                {
                    "AHU-1": {
                        "bacnet": {},
                        "vendor": {},
                    }
                },
            )

            # 2. trigger_discovery_scan via ButlerClient
            self.mock_cur.fetchall.side_effect = [meta_rows, []]
            self.mock_runner.return_value = {"transactionId": "TXN-ABCD", "projectId": "localhost"}
            trig = client.trigger_discovery_scan(
                registry_id="ZZ-TRI-FECTA",
                device_id="AHU-1",
                family="bacnet",
                depth="details",
            )
            self.assertEqual(list(trig.keys()), ["generation"])
            self.assertTrue(trig["generation"].endswith("Z"))

            # 3. get_discovery_state via MCP tools/call
            self.mock_cur.fetchall.side_effect = [
                meta_rows,
                [],
                [
                    (
                        1,
                        "config",
                        "discovery",
                        {"families": {"bacnet": {"generation": "2026-09-30T11:00:00Z"}}},
                        datetime(2026, 9, 30, 11, 0, 0, tzinfo=timezone.utc),
                    )
                ],
                [],
            ]
            st = client.call_tool(
                "get_discovery_state",
                {"registry_id": "ZZ-TRI-FECTA", "device_id": "AHU-1"},
            )
            self.assertEqual(st["families"]["bacnet"]["phase"], "pending")
        finally:
            httpd.shutdown()
            httpd.server_close()


if __name__ == "__main__":
    unittest.main()
