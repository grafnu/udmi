"""Database access adapter for GUMMI querying Butler data stores or mock mode."""

from datetime import datetime, timezone, timedelta
import json
import os
import socket
import sys
import time
from typing import Any, Dict, List, Optional, Tuple, Union

try:
    from udmi.common.db.postgres import PostgresManager
except ImportError:
    PostgresManager = None

try:
    from udmi.common.db.influx import InfluxManager
except ImportError:
    InfluxManager = None


class GummiDB:
    """Manages read and query operations against Butler PostgreSQL and InfluxDB instances."""

    def __init__(
        self,
        pg_manager: Optional[Any] = None,
        influx_manager: Optional[Any] = None,
        uufi_client: Optional[Any] = None,
        mock_mode: bool = False,
    ):
        self.mock_mode = mock_mode
        self.pg = None if mock_mode else (pg_manager or (PostgresManager() if PostgresManager else None))
        self.influx = None if mock_mode else (influx_manager or (InfluxManager() if InfluxManager else None))
        self.uufi = None if mock_mode else uufi_client
        self._mock_fleet = self._generate_mock_fleet() if mock_mode else []
        self._mock_messages = self._generate_mock_messages() if mock_mode else {}

    def _get_pg_connection(self):
        """Returns a PostgreSQL connection or raises ConnectionError if unconfigured or unreachable."""
        if not self.pg:
            raise ConnectionError("PostgreSQL manager is not configured or unavailable")
        try:
            return self.pg.get_connection()
        except Exception as e:
            raise ConnectionError(f"PostgreSQL connection failed: {e}") from e

    # --------------------------------------------------------------------------
    # Health & Connectivity
    # --------------------------------------------------------------------------

    def check_component_health(self) -> Dict[str, Any]:
        """Probes local database and broker components to report latency and status."""
        if self.mock_mode:
            components = {
                "postgres": {
                    "status": "MOCK_MODE",
                    "endpoint": "mock://postgres",
                    "latency_ms": 0.0,
                    "note": "Running in mock mode",
                },
                "influxdb": {
                    "status": "MOCK_MODE",
                    "endpoint": "mock://influxdb",
                    "latency_ms": 0.0,
                    "note": "Running in mock mode",
                },
                "uufi_service": {
                    "status": "MOCK_MODE",
                    "endpoint": "mock://uufi",
                    "latency_ms": 0.0,
                    "note": "Running in mock mode",
                },
                "mqtt_broker": {
                    "status": "MOCK_MODE",
                    "endpoint": "mock://uufi",
                    "latency_ms": 0.0,
                    "note": "Running in mock mode",
                },
                "etcd": {
                    "status": "MOCK_MODE",
                    "endpoint": "mock://etcd",
                    "latency_ms": 0.0,
                    "note": "Running in mock mode",
                },
            }
            return {
                "overall_status": "MOCK_MODE",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "components": components,
            }

        components: Dict[str, Any] = {}

        # 1. PostgreSQL Probe
        pg_host = os.environ.get("POSTGRES_HOST", "127.0.0.1")
        pg_port = int(os.environ.get("POSTGRES_PORT", "5432"))
        if not self.pg:
            components["postgres"] = {
                "status": "DOWN",
                "endpoint": f"{pg_host}:{pg_port}",
                "error": "PostgresManager not configured or unavailable",
            }
        else:
            t0 = time.perf_counter()
            try:
                conn = self.pg.get_connection()
                with conn.cursor() as cur:
                    cur.execute("SELECT 1;")
                    cur.fetchone()
                conn.close()
                latency = round((time.perf_counter() - t0) * 1000, 2)
                components["postgres"] = {
                    "status": "UP",
                    "endpoint": f"{pg_host}:{pg_port}",
                    "latency_ms": latency,
                }
            except Exception as e:
                components["postgres"] = {
                    "status": "DOWN",
                    "endpoint": f"{pg_host}:{pg_port}",
                    "error": str(e),
                }

        # 2. InfluxDB Probe
        influx_port = int(os.environ.get("INFLUX_PORT", os.environ.get("INFLUXDB_PORT", "8086")))
        influx_host = os.environ.get("INFLUXDB_HOST", "127.0.0.1")
        if not self.influx:
            components["influxdb"] = {
                "status": "DOWN",
                "endpoint": f"{influx_host}:{influx_port}",
                "error": "InfluxManager not configured or unavailable",
            }
        else:
            t0 = time.perf_counter()
            try:
                client = self.influx.get_client()
                ready = client.ready()
                latency = round((time.perf_counter() - t0) * 1000, 2)
                status_str = "UP" if (ready and getattr(ready, "status", None) == "ready") else "DOWN"
                components["influxdb"] = {
                    "status": status_str,
                    "endpoint": f"{influx_host}:{influx_port}",
                    "latency_ms": latency,
                }
            except Exception as e:
                components["influxdb"] = {
                    "status": "DOWN",
                    "endpoint": f"{influx_host}:{influx_port}",
                    "error": str(e),
                }

        # 3. UUFI Messaging Service Probe
        if not self.uufi:
            uufi_comp = {
                "status": "DOWN",
                "endpoint": "uufi-service",
                "error": "UUFI client not configured or unavailable",
            }
        else:
            try:
                h = self.uufi.health()
                if h.get("status") in ("UP", "REACHABLE", "ACTIVE"):
                    uufi_comp = {
                        "status": "UP",
                        "endpoint": h.get("broker", "uufi-service"),
                        "latency_ms": h.get("latency_ms", 0.0),
                    }
                else:
                    uufi_comp = {
                        "status": "DOWN",
                        "endpoint": h.get("broker", "uufi-service"),
                        "error": h.get("error", "UUFI Service unreachable"),
                    }
            except Exception as e:
                uufi_comp = {
                    "status": "DOWN",
                    "endpoint": "uufi-service",
                    "error": str(e),
                }

        components["uufi_service"] = uufi_comp
        components["mqtt_broker"] = uufi_comp

        # 4. etcd Probe
        etcd_port = int(os.environ.get("ETCD_PORT", "2379"))
        etcd_host = os.environ.get("ETCD_HOST", "127.0.0.1")
        t0 = time.perf_counter()
        try:
            with socket.create_connection((etcd_host, etcd_port), timeout=0.5):
                latency = round((time.perf_counter() - t0) * 1000, 2)
                components["etcd"] = {
                    "status": "UP",
                    "endpoint": f"{etcd_host}:{etcd_port}",
                    "latency_ms": latency,
                }
        except Exception as e:
            components["etcd"] = {
                "status": "DOWN",
                "endpoint": f"{etcd_host}:{etcd_port}",
                "error": str(e),
            }

        all_up = all(c.get("status") == "UP" for c in components.values())
        overall = "HEALTHY" if all_up else "DEGRADED"
        return {
            "overall_status": overall,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "components": components,
        }

    # --------------------------------------------------------------------------
    # Portfolio Overview Queries
    # --------------------------------------------------------------------------

    def get_portfolio_summary(self) -> Dict[str, Any]:
        """Returns aggregate device counts, online/offline breakdown, and recent alerts."""
        if self.mock_mode:
            return self._mock_portfolio_summary()

        conn = self._get_pg_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT 
                        COUNT(DISTINCT (device_registry_id, device_id)) as total_devices,
                        COUNT(DISTINCT device_registry_id) as total_registries
                    FROM udmi_system_state;
                """)
                row = cur.fetchone()
                total_devices = row[0] if row else 0
                total_registries = row[1] if row else 0

                cur.execute("""
                    SELECT COUNT(*) 
                    FROM udmi_validation 
                    WHERE level >= 500 AND timestamp >= NOW() - INTERVAL '24 hours';
                """)
                crit_row = cur.fetchone()
                critical_alerts_24h = crit_row[0] if crit_row else 0

                cur.execute("""
                    SELECT COUNT(DISTINCT (device_registry_id, device_id))
                    FROM udmi_validation
                    WHERE level >= 500 AND timestamp >= NOW() - INTERVAL '15 minutes';
                """)
                err_row = cur.fetchone()
                error_devices = err_row[0] if err_row else 0

            online_devices = max(0, total_devices - error_devices)
            offline_devices = 0

            return {
                "device_counts": {
                    "total": total_devices,
                    "online": online_devices,
                    "offline": offline_devices,
                    "error": error_devices,
                },
                "registries_count": total_registries,
                "active_rollouts_count": 0,
                "critical_alerts_24h": critical_alerts_24h,
            }
        finally:
            conn.close()

    def get_alerts(self, limit: int = 50, min_level: int = 500) -> List[Dict[str, Any]]:
        """Queries recent validation and alarm events."""
        if self.mock_mode:
            return self._mock_alerts(limit=limit, min_level=min_level)

        conn = self._get_pg_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT id, device_registry_id, device_id, level, category, message, detail, timestamp
                    FROM udmi_validation
                    WHERE level >= %s
                    ORDER BY timestamp DESC
                    LIMIT %s;
                """, (min_level, limit))
                rows = cur.fetchall()

            alerts = []
            for r in rows:
                alerts.append({
                    "id": r[0],
                    "registry_id": r[1] or "default",
                    "device_id": r[2] or "unknown",
                    "level": r[3],
                    "category": r[4] or "validation",
                    "message": r[5] or "Validation Notice",
                    "detail": r[6],
                    "timestamp": r[7].isoformat() if hasattr(r[7], "isoformat") else str(r[7]),
                })
            return alerts
        finally:
            conn.close()

    # --------------------------------------------------------------------------
    # Devices Explorer Queries
    # --------------------------------------------------------------------------

    def get_devices(
        self,
        limit: int = 100,
        offset: int = 0,
        registry_id: Optional[str] = None,
        device_prefix: Optional[str] = None,
        make: Optional[str] = None,
        model: Optional[str] = None,
        status: Optional[str] = None,
        search: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Fetches a paginated, filtered list of devices from udmi_system_state."""
        if self.mock_mode:
            return self._filter_mock_devices(
                limit=limit,
                offset=offset,
                registry_id=registry_id,
                device_prefix=device_prefix,
                make=make,
                model=model,
                status=status,
                search=search,
            )

        conn = self._get_pg_connection()
        try:
            conditions = ["1=1"]
            params: List[Any] = []

            if registry_id:
                conditions.append("s.device_registry_id = %s")
                params.append(registry_id)
            if device_prefix:
                conditions.append("s.device_id LIKE %s")
                params.append(f"{device_prefix}%")
            if make:
                conditions.append("s.make ILIKE %s")
                params.append(f"%{make}%")
            if model:
                conditions.append("s.model ILIKE %s")
                params.append(f"%{model}%")
            if search:
                conditions.append("(s.device_id ILIKE %s OR s.make ILIKE %s OR s.model ILIKE %s)")
                params.extend([f"%{search}%", f"%{search}%", f"%{search}%"])

            where_clause = " AND ".join(conditions)

            with conn.cursor() as cur:
                count_query = f"""
                    SELECT COUNT(DISTINCT (s.device_registry_id, s.device_id))
                    FROM udmi_system_state s
                    WHERE {where_clause};
                """
                cur.execute(count_query, params)
                total = cur.fetchone()[0]

                if total == 0:
                    return {
                        "total": 0,
                        "limit": limit,
                        "offset": offset,
                        "devices": [],
                    }

                data_query = f"""
                    SELECT DISTINCT ON (s.device_registry_id, s.device_id)
                        s.id,
                        s.device_registry_id,
                        s.device_id,
                        s.make,
                        s.model,
                        s.serial_no,
                        s.software,
                        s.timestamp
                    FROM udmi_system_state s
                    WHERE {where_clause}
                    ORDER BY s.device_registry_id, s.device_id, s.timestamp DESC
                    LIMIT %s OFFSET %s;
                """
                cur.execute(data_query, params + [limit, offset])
                rows = cur.fetchall()

            devices = []
            for r in rows:
                software_raw = r[6]
                software_ver = None
                if isinstance(software_raw, list) and software_raw:
                    software_ver = software_raw[0].get("version") if isinstance(software_raw[0], dict) else None
                elif isinstance(software_raw, dict):
                    software_ver = software_raw.get("system")

                devices.append({
                    "id": r[0],
                    "registry_id": r[1] or "default",
                    "device_id": r[2],
                    "make": r[3] or "Unknown",
                    "model": r[4] or "Unknown",
                    "serial_no": r[5],
                    "software_version": software_ver,
                    "liveness_status": "ONLINE",
                    "last_seen": r[7].isoformat() if hasattr(r[7], "isoformat") else str(r[7]),
                })

            return {
                "total": total,
                "limit": limit,
                "offset": offset,
                "devices": devices,
            }
        finally:
            conn.close()

    # --------------------------------------------------------------------------
    # Device Detail & Telemetry Queries
    # --------------------------------------------------------------------------

    def get_device_detail(self, registry_id: str, device_id: str) -> Optional[Dict[str, Any]]:
        """Returns metadata, system state, point states, and recent validation errors for a device."""
        if self.mock_mode:
            return self._mock_device_detail(registry_id, device_id)

        conn = self._get_pg_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT make, model, serial_no, rev, sku, software, timestamp
                    FROM udmi_system_state
                    WHERE device_registry_id = %s AND device_id = %s
                    ORDER BY timestamp DESC
                    LIMIT 1;
                """, (registry_id, device_id))
                sys_row = cur.fetchone()

                if not sys_row:
                    return None

                cur.execute("""
                    SELECT system_location_room, system_location_floor, metadata
                    FROM udmi_metadata
                    WHERE device_registry_id = %s AND device_id = %s
                    ORDER BY timestamp DESC
                    LIMIT 1;
                """, (registry_id, device_id))
                meta_row = cur.fetchone()

                cur.execute("""
                    SELECT DISTINCT ON (point_name)
                        point_name, value_state, units, level, message, status_timestamp, timestamp
                    FROM udmi_point_state
                    WHERE device_registry_id = %s AND device_id = %s
                    ORDER BY point_name, timestamp DESC;
                """, (registry_id, device_id))
                point_rows = cur.fetchall()

                cur.execute("""
                    SELECT level, category, message, detail, timestamp
                    FROM udmi_validation
                    WHERE device_registry_id = %s AND device_id = %s
                    ORDER BY timestamp DESC
                    LIMIT 10;
                """, (registry_id, device_id))
                val_rows = cur.fetchall()

            meta_dict = meta_row[2] if (meta_row and isinstance(meta_row[2], dict)) else {}
            meta_dict.setdefault("make", sys_row[0] or "Unknown")
            meta_dict.setdefault("model", sys_row[1] or "Unknown")
            meta_dict.setdefault("serial_no", sys_row[2])
            meta_dict.setdefault("room", meta_row[0] if meta_row else None)
            meta_dict.setdefault("floor", meta_row[1] if meta_row else None)
            meta_dict.setdefault("last_seen", sys_row[6].isoformat() if hasattr(sys_row[6], "isoformat") else str(sys_row[6]))

            points_map = {}
            for pr in point_rows:
                points_map[pr[0]] = {
                    "value_state": pr[1],
                    "units": pr[2],
                    "level": pr[3],
                    "message": pr[4],
                    "status_timestamp": pr[5].isoformat() if hasattr(pr[5], "isoformat") else str(pr[5]),
                }

            events = [
                {
                    "level": vr[0],
                    "category": vr[1],
                    "message": vr[2],
                    "detail": vr[3],
                    "timestamp": vr[4].isoformat() if hasattr(vr[4], "isoformat") else str(vr[4]),
                }
                for vr in val_rows
            ]

            software_dict = {}
            if sys_row and sys_row[5]:
                if isinstance(sys_row[5], list):
                    for item in sys_row[5]:
                        if isinstance(item, dict) and "id" in item:
                            software_dict[item["id"]] = item.get("version")
                elif isinstance(sys_row[5], dict):
                    software_dict = sys_row[5]

            return {
                "registry_id": registry_id,
                "device_id": device_id,
                "metadata": meta_dict,
                "state": {
                    "system": {
                        "software": software_dict,
                    },
                    "pointset": {
                        "points": points_map,
                    },
                },
                "config": {
                    "system": {
                        "software": software_dict,
                    },
                },
                "events": events,
            }
        finally:
            conn.close()

    def get_device_telemetry(
        self,
        registry_id: str,
        device_id: str,
        point_names: List[str],
        start: str = "-1h",
        stop: str = "now()",
    ) -> Dict[str, Any]:
        """Queries InfluxDB for time-series point values."""
        if self.mock_mode:
            return self._mock_telemetry(registry_id, device_id, point_names)

        if not self.influx:
            raise ConnectionError("InfluxDB manager is not configured or unavailable")

        client = self.influx.get_client()
        query_api = client.query_api()

        point_filters = " or ".join([f'r["point_name"] == "{p.strip()}"' for p in point_names if p.strip()])
        if not point_filters:
            point_filters = 'true'

        flux_query = f"""
            from(bucket: "{self.influx.bucket}")
              |> range(start: {start}, stop: {stop})
              |> filter(fn: (r) => r["_measurement"] == "point_value")
              |> filter(fn: (r) => r["device_id"] == "{device_id}")
              |> filter(fn: (r) => {point_filters})
              |> yield(name: "points")
        """

        try:
            tables = query_api.query(flux_query)
        except Exception as e:
            raise ConnectionError(f"InfluxDB query failed: {e}") from e
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

    # --------------------------------------------------------------------------
    # Message Lifecycle & Mapping Queries (Model -> Discovery -> Proposal)
    # --------------------------------------------------------------------------

    def get_device_messages(
        self,
        registry_id: str,
        device_id: str,
    ) -> List[Dict[str, Any]]:
        """Queries udmi_messages for all lifecycle messages (model, discovery, propose) for a device."""
        if self.mock_mode:
            return self._mock_device_messages(registry_id, device_id)

        conn = self._get_pg_connection()
        try:
            with conn.cursor() as cur:
                # Query messages for this device directly, or discovery events that reference this device
                cur.execute("""
                    SELECT id, timestamp, registry_id, device_id, sub_type, sub_folder, payload, attributes
                    FROM udmi_messages
                    WHERE registry_id = %s
                      AND (device_id = %s OR attributes->>'gatewayId' = %s OR payload::text LIKE %s)
                    ORDER BY timestamp ASC, id ASC;
                """, (registry_id, device_id, device_id, f'%"{device_id}"%'))
                rows = cur.fetchall()

            messages = []
            for r in rows:
                p_load = r[6]
                if isinstance(p_load, str):
                    try:
                        p_load = json.loads(p_load)
                    except Exception:
                        pass
                attrs = r[7] if isinstance(r[7], dict) else {}
                if isinstance(attrs, str):
                    try:
                        attrs = json.loads(attrs)
                    except Exception:
                        attrs = {}

                messages.append({
                    "id": r[0],
                    "timestamp": r[1].isoformat() if hasattr(r[1], "isoformat") else str(r[1]),
                    "registry_id": r[2],
                    "device_id": r[3],
                    "sub_type": r[4],
                    "sub_folder": r[5],
                    "payload": p_load,
                    "updateFrom": attrs.get("updateFrom") or (p_load.get("updateFrom") if isinstance(p_load, dict) else None),
                    "source": attrs.get("source") or (p_load.get("source") if isinstance(p_load, dict) else "system"),
                    "transaction_id": attrs.get("transactionId"),
                })
            return messages
        finally:
            conn.close()

    def populate_mapping_scenario(
        self,
        registry_id: str = "ZZ-TRI-FECTA",
    ) -> Dict[str, Any]:
        """Populates the database or mock store with original models, discovery events, and generated proposals."""
        now = datetime.now(timezone.utc)
        t_model = (now - timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
        t_disc = (now - timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
        t_prop = (now - timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ")

        # 1. Base Model for AHU-22
        model_payload = {
            "version": "1.5.7",
            "timestamp": t_model,
            "system": {
                "location": {"site": "US-SFO-XYY", "room": "Room-204", "floor": "Floor-2"},
                "serial_no": "SN-AHU-22",
                "hardware": {"make": "Acme Controls", "model": "HVAC-3000"},
            },
            "localnet": {
                "families": {
                    "vendor": {"addr": "0x65"}
                }
            },
            "pointset": {
                "points": {
                    "supply_air_temperature_sensor": {"units": "Degrees-Celsius"}
                }
            }
        }

        # 2. Discovery Event from GAT-123 discovering new vendor address and bacnet address
        discovery_payload = {
            "timestamp": t_disc,
            "version": "1.5.7",
            "generation": t_disc,
            "family": "vendor",
            "addr": "0x68",
            "families": {
                "vendor": {"addr": "0x68"},
                "bacnet": {"addr": "10022"},
                "ipv4": {"addr": "192.168.1.122"}
            }
        }

        # 3. Reconciled Proposal generated by mapper
        proposal_localnet = {
            "version": "1.5.7",
            "timestamp": t_prop,
            "families": {
                "vendor": {"addr": "0x68"},
                "bacnet": {"addr": "10022"},
                "ipv4": {"addr": "192.168.1.122"}
            }
        }

        proposal_pointset = {
            "version": "1.5.7",
            "timestamp": t_prop,
            "points": {
                "supply_air_temperature_sensor": {"units": "Degrees-Celsius"},
                "return_air_temperature_sensor": {"ref": "point_ret_temp"}
            }
        }

        records = [
            {
                "timestamp": t_model,
                "registry_id": registry_id,
                "device_id": "AHU-22",
                "sub_type": "model",
                "sub_folder": "system",
                "payload": model_payload,
                "attributes": {"source": "registrar"},
            },
            {
                "timestamp": t_disc,
                "registry_id": registry_id,
                "device_id": "GAT-123",
                "sub_type": "events",
                "sub_folder": "discovery",
                "payload": discovery_payload,
                "attributes": {"source": "pubber", "gatewayId": "GAT-123"},
            },
            {
                "timestamp": t_prop,
                "registry_id": registry_id,
                "device_id": "AHU-22",
                "sub_type": "propose",
                "sub_folder": "localnet",
                "payload": proposal_localnet,
                "attributes": {"source": "butler", "updateFrom": t_model, "transactionId": "TXN-map-01"},
            },
            {
                "timestamp": t_prop,
                "registry_id": registry_id,
                "device_id": "AHU-22",
                "sub_type": "propose",
                "sub_folder": "pointset",
                "payload": proposal_pointset,
                "attributes": {"source": "butler", "updateFrom": t_model, "transactionId": "TXN-map-02"},
            },
        ]

        if not self.mock_mode:
            conn = self._get_pg_connection()
            try:
                with conn.cursor() as cur:
                    cur.execute("""
                        CREATE TABLE IF NOT EXISTS udmi_messages (
                            id SERIAL PRIMARY KEY,
                            timestamp TIMESTAMPTZ,
                            registry_id TEXT,
                            device_id TEXT,
                            sub_type TEXT,
                            sub_folder TEXT,
                            payload JSONB,
                            attributes JSONB
                        );
                    """)
                    for r in records:
                        cur.execute("""
                            INSERT INTO udmi_messages (timestamp, registry_id, device_id, sub_type, sub_folder, payload, attributes)
                            VALUES (%s, %s, %s, %s, %s, %s, %s);
                        """, (
                            r["timestamp"],
                            r["registry_id"],
                            r["device_id"],
                            r["sub_type"],
                            r["sub_folder"],
                            json.dumps(r["payload"]),
                            json.dumps(r["attributes"]),
                        ))
                    conn.commit()
            finally:
                conn.close()

            # Query real messages back from postgres
            messages = self.get_device_messages(registry_id, "AHU-22")
            return {
                "status": "SUCCESS",
                "registry_id": registry_id,
                "device_id": "AHU-22",
                "records_inserted": len(records),
                "messages": messages,
            }

        # Mock mode store
        key = (registry_id, "AHU-22")
        self._mock_messages[key] = [
            {
                "id": idx + 1,
                "timestamp": r["timestamp"],
                "registry_id": r["registry_id"],
                "device_id": r["device_id"],
                "sub_type": r["sub_type"],
                "sub_folder": r["sub_folder"],
                "payload": r["payload"],
                "updateFrom": r["attributes"].get("updateFrom"),
                "source": r["attributes"].get("source"),
                "transaction_id": r["attributes"].get("transactionId"),
            }
            for idx, r in enumerate(records)
        ]

        return {
            "status": "SUCCESS",
            "registry_id": registry_id,
            "device_id": "AHU-22",
            "records_inserted": len(records),
            "messages": self._mock_messages[key],
        }

    # --------------------------------------------------------------------------
    # Mock Data Generators
    # --------------------------------------------------------------------------

    def _generate_mock_fleet(self) -> List[Dict[str, Any]]:
        """Generates realistic mock device catalog."""
        devices = []
        registries = ["ZZ-TRI-FECTA", "US-MTV-1", "US-SFO-2"]
        makes_models = [
            ("Acme Controls", "HVAC-3000", "2.4.1"),
            ("Carrier", "WeatherMaster-500", "3.1.0"),
            ("Trane", "IntelliPak-2", "1.9.4"),
            ("Siemens", "Desigo-CC-40", "4.2.0"),
            ("Johnson Controls", "Metasys-FEC", "2.0.8"),
            ("Schneider Electric", "EcoStruxure-9", "3.0.2"),
        ]

        # 1. Primary Air Handling Units & Gateways (AHU & GAT)
        devices.append({
            "id": len(devices) + 1,
            "registry_id": "ZZ-TRI-FECTA",
            "device_id": "AHU-22",
            "make": "Acme Controls",
            "model": "HVAC-3000",
            "serial_no": "SN-AHU-22",
            "software_version": "2.4.1",
            "liveness_status": "ONLINE",
            "last_seen": datetime.now(timezone.utc).isoformat(),
        })
        devices.append({
            "id": len(devices) + 1,
            "registry_id": "ZZ-TRI-FECTA",
            "device_id": "GAT-123",
            "make": "Siemens",
            "model": "Desigo-CC-40",
            "serial_no": "SN-GAT-123",
            "software_version": "4.2.0",
            "liveness_status": "ONLINE",
            "last_seen": datetime.now(timezone.utc).isoformat(),
        })
        for i in range(1, 7):
            mm = makes_models[(i - 1) % len(makes_models)]
            devices.append({
                "id": len(devices) + 1,
                "registry_id": "ZZ-TRI-FECTA",
                "device_id": f"AHU-{i}",
                "make": mm[0],
                "model": mm[1],
                "serial_no": f"SN-AHU-990{i}",
                "software_version": mm[2],
                "liveness_status": "ONLINE" if i != 4 else "ERROR",
                "last_seen": (datetime.now(timezone.utc) - timedelta(minutes=i * 2)).isoformat(),
            })

        # 2. Variable Air Volume Boxes (VAV)
        for i in range(101, 116):
            mm = makes_models[i % len(makes_models)]
            devices.append({
                "id": len(devices) + 1,
                "registry_id": "ZZ-TRI-FECTA" if i < 110 else "US-MTV-1",
                "device_id": f"VAV-{i}",
                "make": mm[0],
                "model": f"VAV-Box-{i}",
                "serial_no": f"SN-VAV-{i}X",
                "software_version": mm[2],
                "liveness_status": "ONLINE" if i != 108 else "OFFLINE",
                "last_seen": (datetime.now(timezone.utc) - timedelta(minutes=(i % 10) * 3 + 1)).isoformat(),
            })

        # 3. Chillers and Central Plant (CHILLER, PUMP, BOILER)
        for i in range(1, 5):
            devices.append({
                "id": len(devices) + 1,
                "registry_id": "US-SFO-2",
                "device_id": f"CHILLER-{i}",
                "make": "Trane",
                "model": "Centravac-WaterCooled",
                "serial_no": f"SN-CHIL-{i}00",
                "software_version": "5.0.1",
                "liveness_status": "ONLINE",
                "last_seen": (datetime.now(timezone.utc) - timedelta(minutes=i)).isoformat(),
            })
            devices.append({
                "id": len(devices) + 1,
                "registry_id": "US-SFO-2",
                "device_id": f"PUMP-{i}",
                "make": "Grundfos",
                "model": "Magna3-VFD",
                "serial_no": f"SN-PUMP-{i}99",
                "software_version": "2.1.0",
                "liveness_status": "ONLINE",
                "last_seen": (datetime.now(timezone.utc) - timedelta(minutes=i + 3)).isoformat(),
            })

        # 4. Lighting & Power Meters
        for i in range(1, 5):
            devices.append({
                "id": len(devices) + 1,
                "registry_id": "US-MTV-1",
                "device_id": f"LIGHTING-CTR-{i}",
                "make": "Lutron",
                "model": "Quantum-Hub",
                "serial_no": f"SN-LUT-{i}00",
                "software_version": "3.8.2",
                "liveness_status": "ONLINE",
                "last_seen": (datetime.now(timezone.utc) - timedelta(minutes=4)).isoformat(),
            })
            devices.append({
                "id": len(devices) + 1,
                "registry_id": "US-MTV-1",
                "device_id": f"METER-PWR-{i}",
                "make": "Schneider Electric",
                "model": "PowerLogic-ION9000",
                "serial_no": f"SN-MET-{i}55",
                "software_version": "1.4.0",
                "liveness_status": "ONLINE" if i != 3 else "OFFLINE",
                "last_seen": (datetime.now(timezone.utc) - timedelta(minutes=i * 12)).isoformat(),
            })

        return devices

    def _filter_mock_devices(
        self,
        limit: int = 100,
        offset: int = 0,
        registry_id: Optional[str] = None,
        device_prefix: Optional[str] = None,
        make: Optional[str] = None,
        model: Optional[str] = None,
        status: Optional[str] = None,
        search: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Filters mock devices list according to query parameters."""
        filtered = self._mock_fleet
        if registry_id:
            filtered = [d for d in filtered if registry_id.lower() in d["registry_id"].lower()]
        if device_prefix:
            filtered = [d for d in filtered if d["device_id"].upper().startswith(device_prefix.upper())]
        if make:
            filtered = [d for d in filtered if make.lower() in d["make"].lower()]
        if model:
            filtered = [d for d in filtered if model.lower() in d["model"].lower()]
        if status:
            filtered = [d for d in filtered if d["liveness_status"].upper() == status.upper()]
        if search:
            s = search.lower()
            filtered = [
                d for d in filtered
                if s in d["device_id"].lower() or s in d["make"].lower() or s in d["model"].lower() or s in d["registry_id"].lower()
            ]

        total = len(filtered)
        paginated = filtered[offset : offset + limit]
        return {
            "total": total,
            "limit": limit,
            "offset": offset,
            "devices": paginated,
        }

    def _mock_portfolio_summary(self) -> Dict[str, Any]:
        fleet = self._mock_fleet
        online = len([d for d in fleet if d["liveness_status"] == "ONLINE"])
        offline = len([d for d in fleet if d["liveness_status"] == "OFFLINE"])
        error = len([d for d in fleet if d["liveness_status"] == "ERROR"])
        registries = len(set(d["registry_id"] for d in fleet))

        return {
            "device_counts": {
                "total": len(fleet),
                "online": online,
                "offline": offline,
                "error": error,
            },
            "registries_count": registries,
            "active_rollouts_count": 1,
            "critical_alerts_24h": 3,
        }

    def _mock_alerts(self, limit: int = 50, min_level: int = 500) -> List[Dict[str, Any]]:
        now = datetime.now(timezone.utc)
        return [
            {
                "id": 101,
                "registry_id": "ZZ-TRI-FECTA",
                "device_id": "AHU-4",
                "level": 800,
                "category": "system.hardware.comm_error",
                "message": "Supply fan VFD communication timeout on RS-485 bus",
                "detail": "BACnet MSTP timeout after 3 retries to controller 0x4A",
                "timestamp": (now - timedelta(minutes=14)).isoformat(),
            },
            {
                "id": 102,
                "registry_id": "ZZ-TRI-FECTA",
                "device_id": "VAV-108",
                "level": 600,
                "category": "pointset.sensor.out_of_range",
                "message": "Discharge air temperature exceeded high limit (28.4 C)",
                "detail": "Value above setpoint deadband 24.0 C for > 300s",
                "timestamp": (now - timedelta(hours=2, minutes=15)).isoformat(),
            },
            {
                "id": 103,
                "registry_id": "US-MTV-1",
                "device_id": "METER-PWR-3",
                "level": 500,
                "category": "system.liveness.heartbeat_missed",
                "message": "Device heartbeat missed interval (last seen > 15m)",
                "detail": "No UDP telemetry packet received on port 47808",
                "timestamp": (now - timedelta(hours=5)).isoformat(),
            },
        ][:limit]

    def _mock_device_detail(self, registry_id: str, device_id: str) -> Dict[str, Any]:
        match = next((d for d in self._mock_fleet if d["device_id"] == device_id), None)
        make = match["make"] if match else "Acme Controls"
        model = match["model"] if match else "HVAC-3000"
        serial = match["serial_no"] if match else f"SN-{device_id}-001"
        version = match["software_version"] if match else "2.4.1"
        now = datetime.now(timezone.utc).isoformat()

        # Realistic telemetry point set
        points_map = {
            "supply_air_temperature_sensor": {
                "value_state": "applied",
                "units": "Degrees-Celsius",
                "level": 300,
                "message": "Normal Operation (21.4 C)",
                "timestamp": now,
            },
            "return_air_temperature_sensor": {
                "value_state": "applied",
                "units": "Degrees-Celsius",
                "level": 300,
                "message": "Normal Operation (23.8 C)",
                "timestamp": now,
            },
            "supply_air_static_pressure_sensor": {
                "value_state": "applied",
                "units": "Pascals",
                "level": 300,
                "message": "Normal Operation (350 Pa)",
                "timestamp": now,
            },
            "fan_speed_command": {
                "value_state": "applied",
                "units": "Percent",
                "level": 300,
                "message": "Modulating (75%)",
                "timestamp": now,
            },
            "filter_alarm_status": {
                "value_state": "applied",
                "units": "Boolean",
                "level": 300,
                "message": "Filter Clean (False)",
                "timestamp": now,
            },
        }

        return {
            "registry_id": registry_id,
            "device_id": device_id,
            "metadata": {
                "make": make,
                "model": model,
                "serial_no": serial,
                "rev": "B.2",
                "sku": "SKU-HVAC-PRO",
                "room": "Mechanical Room 102",
                "floor": "Floor 1",
                "software": {"system": version, "hvac_app": "1.2.0"},
                "last_seen": now,
            },
            "state": {
                "system": {
                    "software": {"system": version, "hvac_app": "1.2.0"},
                    "operational": True,
                },
                "pointset": {
                    "points": points_map,
                },
            },
            "config": {
                "system": {
                    "software": {"system": version, "hvac_app": "1.2.0"},
                },
                "pointset": {
                    "points": {
                        "fan_speed_command": {"setpoint": 75},
                    },
                },
            },
            "events": [
                {
                    "level": 300,
                    "category": "system.config.applied",
                    "message": "Configuration successfully synchronized",
                    "detail": None,
                    "timestamp": now,
                }
            ],
        }

    def _mock_telemetry(self, registry_id: str, device_id: str, point_names: List[str]) -> Dict[str, Any]:
        now = datetime.now(timezone.utc)
        pts = point_names if point_names else ["supply_air_temperature_sensor", "fan_speed_command"]
        series_list = []

        for pt in pts:
            base_val = 21.5 if "temp" in pt else 75.0
            vals = []
            for m in range(15, -1, -1):
                t = (now - timedelta(minutes=m * 2)).isoformat()
                v = round(base_val + (m % 3) * 0.4 - 0.5, 2)
                vals.append({"time": t, "value": v, "field": "present_value_num"})
            series_list.append({"point_name": pt, "values": vals})

        return {
            "registry_id": registry_id,
            "device_id": device_id,
            "series": series_list,
        }

    def _generate_mock_messages(self) -> Dict[Tuple[str, str], List[Dict[str, Any]]]:
        """Generates initial mock message lifecycle records for AHU-22 and AHU-1."""
        now = datetime.now(timezone.utc)
        t_model = (now - timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
        t_disc = (now - timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
        t_prop = (now - timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ")

        mock_msgs: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}

        # AHU-22
        mock_msgs[("ZZ-TRI-FECTA", "AHU-22")] = [
            {
                "id": 1,
                "timestamp": t_model,
                "registry_id": "ZZ-TRI-FECTA",
                "device_id": "AHU-22",
                "sub_type": "model",
                "sub_folder": "system",
                "payload": {
                    "version": "1.5.7",
                    "timestamp": t_model,
                    "system": {
                        "location": {"site": "US-SFO-XYY", "room": "Room-204", "floor": "Floor-2"},
                        "serial_no": "SN-AHU-22",
                    },
                    "localnet": {
                        "families": {
                            "vendor": {"addr": "0x65"}
                        }
                    }
                },
                "updateFrom": None,
                "source": "registrar",
                "transaction_id": "TXN-init-01",
            },
            {
                "id": 2,
                "timestamp": t_disc,
                "registry_id": "ZZ-TRI-FECTA",
                "device_id": "GAT-123",
                "sub_type": "events",
                "sub_folder": "discovery",
                "payload": {
                    "timestamp": t_disc,
                    "generation": t_disc,
                    "family": "vendor",
                    "addr": "0x68",
                    "families": {
                        "vendor": {"addr": "0x68"},
                        "bacnet": {"addr": "10022"},
                        "ipv4": {"addr": "192.168.1.122"}
                    }
                },
                "updateFrom": None,
                "source": "pubber",
                "transaction_id": "TXN-scan-01",
            },
            {
                "id": 3,
                "timestamp": t_prop,
                "registry_id": "ZZ-TRI-FECTA",
                "device_id": "AHU-22",
                "sub_type": "propose",
                "sub_folder": "localnet",
                "payload": {
                    "version": "1.5.7",
                    "timestamp": t_prop,
                    "families": {
                        "vendor": {"addr": "0x68"},
                        "bacnet": {"addr": "10022"},
                        "ipv4": {"addr": "192.168.1.122"}
                    }
                },
                "updateFrom": t_model,
                "source": "butler",
                "transaction_id": "TXN-map-01",
            }
        ]

        return mock_msgs

    def _mock_device_messages(self, registry_id: str, device_id: str) -> List[Dict[str, Any]]:
        key = (registry_id, device_id)
        if key in self._mock_messages:
            return self._mock_messages[key]

        # Generate standard default lifecycle for any other device
        now = datetime.now(timezone.utc)
        t_model = (now - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
        return [
            {
                "id": 1,
                "timestamp": t_model,
                "registry_id": registry_id,
                "device_id": device_id,
                "sub_type": "model",
                "sub_folder": "system",
                "payload": {
                    "version": "1.5.7",
                    "timestamp": t_model,
                    "system": {"serial_no": f"SN-{device_id}-001"},
                    "localnet": {"families": {"bacnet": {"addr": "1001"}}}
                },
                "updateFrom": None,
                "source": "registrar",
                "transaction_id": "TXN-base",
            }
        ]
