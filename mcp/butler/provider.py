"""Butler Data Provider for UDMI MCP Server.

Encapsulates relational (PostgreSQL) and time-series (InfluxDB) datastores to provide
pure domain-specific data access for device discovery, telemetry, and mapping flows
without exposing database credentials, query languages, or internal storage schemas.
"""

from datetime import datetime, timezone
import json
import os
import sys
from typing import Any, Dict, List, Optional, Union

try:
    from udmi.common.db.postgres import PostgresManager
except (ImportError, ModuleNotFoundError):
    PostgresManager = None

try:
    from udmi.common.db.influx import InfluxManager
except (ImportError, ModuleNotFoundError):
    InfluxManager = None


try:
    from udmi.common.project_spec import parse_project_spec
except (ImportError, ModuleNotFoundError):
    parse_project_spec = None


class ButlerProvider:
    """Encapsulates Butler datastore access for mapping and telemetry reconciliation."""

    def __init__(
        self,
        pg_manager: Optional[Any] = None,
        influx_manager: Optional[Any] = None,
        project_spec: Optional[str] = None,
        pg_port: Optional[Union[str, int]] = None,
        influx_port: Optional[Union[str, int]] = None,
    ):
        """Initializes ButlerProvider with relational and timeseries managers."""
        if pg_manager is not None:
            self.pg_manager = pg_manager
        elif PostgresManager is not None:
            resolved_port = pg_port
            if not resolved_port and project_spec and parse_project_spec:
                spec_info = parse_project_spec(project_spec)
                p = spec_info.get("port")
                if p and str(p) != "8883":
                    resolved_port = str(int(p) + 3)
            if not resolved_port:
                resolved_port = os.environ.get("POSTGRES_PORT", "5432")
            self.pg_manager = PostgresManager(port=resolved_port)
        else:
            self.pg_manager = None

        if influx_manager is not None:
            self.influx_manager = influx_manager
        elif InfluxManager is not None:
            resolved_inf_port = influx_port
            if not resolved_inf_port and project_spec and parse_project_spec:
                spec_info = parse_project_spec(project_spec)
                p = spec_info.get("port")
                if p and str(p) != "8883":
                    resolved_inf_port = str(int(p) + 2)
            if not resolved_inf_port:
                resolved_inf_port = os.environ.get("INFLUX_PORT", os.environ.get("INFLUXDB_PORT", "8086"))
            host = os.environ.get("INFLUXDB_HOST", "127.0.0.1")
            url = f"http://{host}:{resolved_inf_port}"
            self.influx_manager = InfluxManager(url=url)
        else:
            self.influx_manager = None

    def health(self) -> Dict[str, Any]:
        """Probes relational and timeseries datastores to report abstract health status."""
        relational_ok = False
        timeseries_ok = False

        if self.pg_manager:
            try:
                conn = self.pg_manager.get_connection()
                with conn.cursor() as cur:
                    cur.execute("SELECT 1;")
                    cur.fetchone()
                conn.close()
                relational_ok = True
            except Exception:
                relational_ok = False

        if self.influx_manager:
            try:
                client = self.influx_manager.get_client()
                ready = client.ready()
                timeseries_ok = bool(ready and getattr(ready, "status", None) == "ready")
            except Exception:
                timeseries_ok = False

        if relational_ok and timeseries_ok:
            overall = "UP"
        elif relational_ok or timeseries_ok:
            overall = "DEGRADED"
        else:
            overall = "DOWN"

        return {
            "status": overall,
            "service": "butler",
            "connected": relational_ok or timeseries_ok,
            "datastores": {
                "relational": relational_ok,
                "timeseries": timeseries_ok,
            },
        }

    def get_discovery_events(self, registry_id: str) -> List[Dict[str, Any]]:
        """Queries discovery events for a device registry ordered chronologically.

        Extracts discovery payloads and reporting gateway IDs directly from the
        lifecycle audit store or specialized discovery tables.

        Args:
            registry_id: Target device registry identifier.

        Returns:
            List of discovery event objects.
        """
        if not self.pg_manager:
            return []

        try:
            conn = self.pg_manager.get_connection()
            with conn.cursor() as cur:
                # 1. Query udmi_messages for discovery events
                cur.execute(
                    """
                    SELECT id, device_id, payload, publish_time
                    FROM udmi_messages
                    WHERE registry_id = %s AND sub_folder = 'discovery' AND sub_type = 'events'
                    ORDER BY id ASC;
                    """,
                    (registry_id,),
                )
                rows = cur.fetchall()

                if rows:
                    conn.close()
                    results = []
                    for row in rows:
                        msg_id, dev_id, payload, pub_time = row
                        if isinstance(payload, str):
                            try:
                                payload = json.loads(payload)
                            except Exception:
                                pass
                        results.append({
                            "id": msg_id,
                            "gateway_id": dev_id,
                            "payload": payload,
                            "timestamp": pub_time.isoformat() if hasattr(pub_time, "isoformat") else str(pub_time) if pub_time else None,
                        })
                    return results

                # 2. Fallback to specialized udmi_discovery table if udmi_messages has no records
                cur.execute(
                    """
                    SELECT id, device_id, scan_family, ether_addr, ipv4_addr, bacnet_addr,
                           hardware_make, hardware_model, firmware_version, generation, timestamp, ports
                    FROM udmi_discovery
                    WHERE device_registry_id = %s
                    ORDER BY id ASC;
                    """,
                    (registry_id,),
                )
                disc_rows = cur.fetchall()
            conn.close()

            results = []
            for d in disc_rows:
                disc_id, dev_id, fam, ether, ipv4, bacnet, make, model, fw, gen, ts, ports = d
                families_dict: Dict[str, Any] = {}
                if bacnet:
                    families_dict["bacnet"] = {"addr": bacnet}
                if ipv4:
                    families_dict["ipv4"] = {"addr": ipv4}
                if fam and fam not in ("bacnet", "ipv4") and (ether or bacnet or ipv4):
                    families_dict[fam] = {"addr": ether or bacnet or ipv4}

                payload = {
                    "version": "1.5.7",
                    "timestamp": ts.isoformat() if hasattr(ts, "isoformat") else str(ts) if ts else None,
                    "generation": gen.isoformat() if hasattr(gen, "isoformat") else str(gen) if gen else None,
                    "family": fam,
                    "addr": bacnet or ipv4 or ether,
                    "families": families_dict,
                }
                if make or model or fw:
                    payload["system"] = {
                        "hardware": {"make": make, "model": model},
                        "ancillary": {"firmware": fw},
                    }
                if ports:
                    payload["refs"] = {p.get("port", f"p_{i}"): {"adjunct": p} for i, p in enumerate(ports) if isinstance(p, dict)}

                results.append({
                    "id": disc_id,
                    "gateway_id": dev_id,
                    "payload": payload,
                    "timestamp": payload["timestamp"],
                })
            return results

        except Exception as e:
            print(f"ButlerProvider error fetching discovery events: {e}", file=sys.stderr)
            return []

    def get_discovered_devices(self, registry_id: str) -> List[Dict[str, Any]]:
        """Parses discovery events for a registry into normalized discovered device records.

        Extracts network addresses across families (BACnet, IPv4, vendor) and associated
        gateway IDs ready for mapping reconciliation.

        Args:
            registry_id: Target device registry identifier.

        Returns:
            List of normalized discovered device summaries.
        """
        events = self.get_discovery_events(registry_id)
        devices: List[Dict[str, Any]] = []

        for ev in events:
            payload = ev.get("payload", {})
            gateway_id = ev.get("gateway_id")

            if isinstance(payload, dict) and "payload" in payload and isinstance(payload.get("payload"), dict):
                payload = payload["payload"]

            bacnet_addr = None
            ipv4_addr = None
            vendor_addr = None

            if payload.get("family") == "bacnet":
                bacnet_addr = payload.get("addr")
            elif payload.get("family") == "vendor":
                vendor_addr = payload.get("addr")
            elif payload.get("family") == "ipv4":
                ipv4_addr = payload.get("addr")

            families = payload.get("families", {})
            if isinstance(families, dict):
                if "bacnet" in families:
                    bacnet_addr = bacnet_addr or families["bacnet"].get("addr")
                if "ipv4" in families:
                    ipv4_addr = families["ipv4"].get("addr")
                if "vendor" in families:
                    vendor_addr = vendor_addr or families["vendor"].get("addr")

            if bacnet_addr or ipv4_addr or vendor_addr:
                devices.append({
                    "gateway_id": gateway_id,
                    "generation": payload.get("generation"),
                    "bacnet": str(bacnet_addr) if bacnet_addr else None,
                    "ipv4": str(ipv4_addr) if ipv4_addr else None,
                    "vendor": str(vendor_addr) if vendor_addr else None,
                    "timestamp": ev.get("timestamp"),
                })

        return devices

    def get_device_messages(
        self,
        registry_id: str,
        device_id: str,
    ) -> List[Dict[str, Any]]:
        """Queries the lifecycle message progression (model, discovery, proposal) for a device.

        Args:
            registry_id: Target registry identifier.
            device_id: Target device identifier.

        Returns:
            Chronologically ordered list of lifecycle messages.
        """
        if not self.pg_manager:
            return []

        try:
            conn = self.pg_manager.get_connection()
            with conn.cursor() as cur:
                # Query messages for this device directly or mentioning this device in payload
                cur.execute(
                    """
                    SELECT id, publish_time, registry_id, device_id, sub_type, sub_folder, payload
                    FROM udmi_messages
                    WHERE registry_id = %s
                      AND (device_id = %s OR payload::text LIKE %s)
                    ORDER BY id ASC;
                    """,
                    (registry_id, device_id, f'%"{device_id}"%'),
                )
                rows = cur.fetchall()
            conn.close()

            messages = []
            for r in rows:
                p_load = r[6]
                if isinstance(p_load, str):
                    try:
                        p_load = json.loads(p_load)
                    except Exception:
                        pass

                update_from = p_load.get("updateFrom") if isinstance(p_load, dict) else None
                source = p_load.get("source", "system") if isinstance(p_load, dict) else "system"
                tx_id = p_load.get("transactionId") if isinstance(p_load, dict) else None

                pub_time = r[1]
                ts_str = pub_time.isoformat() if hasattr(pub_time, "isoformat") else str(pub_time) if pub_time else None

                messages.append({
                    "id": r[0],
                    "timestamp": ts_str,
                    "registry_id": r[2],
                    "device_id": r[3],
                    "sub_type": r[4],
                    "sub_folder": r[5],
                    "payload": p_load,
                    "updateFrom": update_from,
                    "source": source,
                    "transaction_id": tx_id,
                })
            return messages

        except Exception as e:
            print(f"ButlerProvider error fetching device messages: {e}", file=sys.stderr)
            return []

    def record_message(
        self,
        registry_id: str,
        device_id: str,
        sub_type: str,
        sub_folder: str,
        payload: Dict[str, Any],
        project_id: Optional[str] = None,
        timestamp: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Persists a message (base model, discovery event, or proposal) into the Butler lifecycle store.

        Args:
            registry_id: Device registry identifier.
            device_id: Device identifier.
            sub_type: Message subType (e.g. 'model', 'events', 'propose').
            sub_folder: Message subFolder (e.g. 'system', 'discovery', 'localnet', 'pointset').
            payload: Structured JSON payload.
            project_id: Optional GCP/UDMI project identifier.
            timestamp: Optional message timestamp.

        Returns:
            Dict indicating status and operation summary.
        """
        if not self.pg_manager:
            raise RuntimeError("Butler relational datastore is unavailable.")

        now_str = timestamp or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        row = {
            "project_id": project_id or "default",
            "registry_id": registry_id,
            "device_id": device_id,
            "sub_folder": sub_folder,
            "sub_type": sub_type,
            "publish_time": now_str,
            "payload": payload,
        }

        self.pg_manager.insert_row("udmi_messages", row)

        return {
            "status": "SUCCESS",
            "registry_id": registry_id,
            "device_id": device_id,
            "sub_folder": sub_folder,
            "sub_type": sub_type,
        }

    def get_device_telemetry(
        self,
        registry_id: str,
        device_id: str,
        point_names: Optional[List[str]] = None,
        start: str = "-1h",
        stop: str = "now()",
    ) -> Dict[str, Any]:
        """Queries time-series telemetry point values for a device from the timeseries datastore.

        Args:
            registry_id: Target registry identifier.
            device_id: Target device identifier.
            point_names: Optional list of point names to filter by.
            start: Query time window start (e.g. '-1h').
            stop: Query time window stop (e.g. 'now()').

        Returns:
            Dict containing time-series data grouped by point name.
        """
        if not self.influx_manager:
            return {
                "registry_id": registry_id,
                "device_id": device_id,
                "series": [],
            }

        try:
            client = self.influx_manager.get_client()
            query_api = client.query_api()

            filter_clauses = [
                'r["_measurement"] == "point_value"',
                f'r["device_id"] == "{device_id}"',
            ]
            if point_names:
                pt_filters = " or ".join([f'r["point_name"] == "{p.strip()}"' for p in point_names if p.strip()])
                if pt_filters:
                    filter_clauses.append(f"({pt_filters})")

            filter_expr = " and ".join(filter_clauses)
            flux_query = f"""
                from(bucket: "{self.influx_manager.bucket}")
                  |> range(start: {start}, stop: {stop})
                  |> filter(fn: (r) => {filter_expr})
                  |> yield(name: "points")
            """

            tables = query_api.query(flux_query)
            series_by_point: Dict[str, List[Dict[str, Any]]] = {}

            for table in tables:
                for record in table.records:
                    pt_name = record.values.get("point_name")
                    val = record.get_value()
                    ts = record.get_time()
                    if pt_name not in series_by_point:
                        series_by_point[pt_name] = []
                    series_by_point[pt_name].append({
                        "time": ts.isoformat() if hasattr(ts, "isoformat") else str(ts),
                        "value": val,
                        "field": record.get_field(),
                    })

            series_list = [
                {"point_name": k, "values": v}
                for k, v in series_by_point.items()
            ]

            return {
                "registry_id": registry_id,
                "device_id": device_id,
                "series": series_list,
            }

        except Exception as e:
            print(f"ButlerProvider error fetching telemetry: {e}", file=sys.stderr)
            return {
                "registry_id": registry_id,
                "device_id": device_id,
                "series": [],
            }

    def write_telemetry(
        self,
        registry_id: str,
        device_id: str,
        points: Dict[str, Any],
        timestamp: Optional[str] = None,
        project_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Writes point telemetry values for a device into the timeseries datastore.

        Args:
            registry_id: Device registry identifier.
            device_id: Device identifier.
            points: Dictionary mapping point names to numeric, boolean, or string values.
            timestamp: Optional publish timestamp.
            project_id: Optional project identifier.

        Returns:
            Dict containing operation summary.
        """
        if not self.influx_manager:
            raise RuntimeError("Butler timeseries datastore is unavailable.")

        now_str = timestamp or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        envelope = {
            "deviceId": device_id,
            "deviceRegistryId": registry_id,
            "projectId": project_id or "default",
            "publishTime": now_str,
        }
        points_payload: Dict[str, Any] = {}
        for pt_name, pt_val in points.items():
            if isinstance(pt_val, dict):
                points_payload[pt_name] = pt_val
            else:
                points_payload[pt_name] = {"present_value": pt_val}

        count = self.influx_manager.write_pointset_payload(envelope, {"points": points_payload})
        return {
            "status": "SUCCESS",
            "registry_id": registry_id,
            "device_id": device_id,
            "points_written": count,
        }

    def clear_registry_mapping_data(self, registry_id: str) -> Dict[str, Any]:
        """Deletes discovery events and proposals for a registry to reset mapping state.

        Args:
            registry_id: Target registry identifier.

        Returns:
            Dict indicating status and count of deleted records.
        """
        if not self.pg_manager:
            raise RuntimeError("Butler relational datastore is unavailable.")

        conn = self.pg_manager.get_connection()
        deleted_count = 0
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    DELETE FROM udmi_messages
                    WHERE registry_id = %s
                      AND (sub_folder = 'discovery' OR sub_type IN ('propose', 'model'));
                    """,
                    (registry_id,),
                )
                deleted_count += cur.rowcount

                try:
                    cur.execute(
                        "DELETE FROM udmi_discovery WHERE device_registry_id = %s;",
                        (registry_id,),
                    )
                    deleted_count += cur.rowcount
                except Exception:
                    pass

            conn.commit()
        finally:
            conn.close()

        return {
            "status": "SUCCESS",
            "registry_id": registry_id,
            "deleted_records": deleted_count,
        }
