[**UDMI**](../../) / [**Docs**](../) / [**Specs**](./) / [Family Providers](#)

# Family Provider URL Generation Specification

UDMI abstracts various fieldbus protocols (such as BACnet, Modbus, IP, etc.) through its `FamilyProvider` interface. A `FamilyProvider` is responsible for translating local protocol-specific addressing and metadata into standardized, globally unique Uniform Resource Locators (URLs) and vice-versa. 

These URLs provide a protocol-agnostic, structured path for identifying devices and data points:
`<family>://<addr>/<ref>`

This document details how a complete `FamilyProvider` URL is generated and resolved based on a device's metadata specification and how validation constraints enforce these rules.

---

## Core URL Structure

Every generated URL consists of three primary logical components:

| Component | Description | Mapping to Metadata | Example |
| :--- | :--- | :--- | :--- |
| **`family`** | The protocol/address family. | `gateway.target.family` | `bacnet` or `modbus` |
| **`addr`** | The physical device address or host on that family. | `gateway.target.addr` | `1234` or `modbus_rtu_1` |
| **`ref`** | The relative reference identifier of the data point. | `pointset.points.<point_name>.ref` | `AV/1` or `1/101?type=BOOLEAN` |

---

## URL Assembly Mechanics

The configuration manager uses a dynamic fallback strategy to resolve these components.

### 1. Device Address Selection (`device`)
The local device address `device` can be defined in one of two places, but **not both** (defining both raises a validation error):
- **Localnet Override**: `metadata.localnet.families.<family>.addr`
- **Gateway Target**: `metadata.gateway.target.addr`

The effective device address is resolved as:
`device_address = metadata.localnet.families.<family>.addr != null ? localnet.families.<family>.addr : gateway.target.addr`

#### Address Authority Classification & Gateway `localnet.networks` Matching
After stripping any optional `:port` suffix, the `<addr>` authority component of a point URL is validated under one of three mutually exclusive syntactic forms:

1. **Numeric / IP Literal** (e.g., IPv4 `192.168.1.1` or BACnet device instance `1234`):
   * Validated directly against the protocol family's numeric or IP address format.
   * Does not require an entry in the parent gateway's `localnet.networks` map.
2. **Single-Label Identifier** (no `.`, matching `^[a-zA-Z][a-zA-Z0-9]*(?:[_-][a-zA-Z0-9]+)*$`, e.g., `modbus_rtu_1`, `localhost`, `my-host`):
   * Represents a named serial/fieldbus network key or an unqualified single-label hostname.
   * When validated in a site model under a parent gateway (`gateway.gateway_id`), requires an **exact key match** in the gateway's `metadata.localnet.networks` map (`addr.equals(key)`).
3. **Canonical Multi-Label FQDN** (contains `.`, lowercase RFC 1123 labels ending in a valid public or private TLD such as `.com`, `.goog`, `.local`, `.lan`, with **no trailing dot**, e.g., `foo.google.com` or `sensor-1.local`):
   * Validated via `FqdnFamilyProvider` (trailing root dots such as `foo.google.com.` are rejected to enforce a single canonical representation).
   * When validated in a site model under a parent gateway, requires a **strict suffix match** against a leading-dot domain key in the gateway's `metadata.localnet.networks` map (e.g., `".google.com"` or `".local"`).

Across all non-numeric `<addr>` authorities, gateway network resolution reduces to a single deterministic predicate over the keys `key` of the parent gateway's `localnet.networks` map:

```java
key.startsWith(".") ? addr.endsWith(key) : addr.equals(key)
```

Because every domain suffix key starts with `.` and valid `<addr>` values never start or end with `.`, `addr.endsWith(key)` inherently enforces DNS label boundaries (`foo.google.com` and `a.b.google.com` match `.google.com`, whereas `badgoogle.com` and apex `google.com` do not). Matching is evaluated directly against the `localnet.networks` map keys and is independent of the optional `family` property inside the network entry, allowing a single domain suffix entry (such as `".google.com"`) to cover all hostname-addressed protocols on that gateway.

### 2. URL Combination Logic
For standard configurations, the system constructs the complete URL by concatenating the components:
`full_url = family + "://" + device_address + "/" + point_ref`

### 3. Normalized URL Output (`url`)
During normalization, the `registrar` tool compiles the fully resolved URL for each data point and writes it to the generated `metadata_norm.json` file inside the `pointset.points.<point_name>.url` field. If there is doubt about how the configuration resolves (including fallback behaviors or relative-to-absolute expansions), the generated `metadata_norm.json` file can be checked to see the exact URLs that were generated.

#### Example Generated Point Snippet
```json
{
  "pointset": {
    "points": {
      "fan_run_status": {
        "ref": "BI/1",
        "url": "bacnet://1234/BI/1"
      }
    }
  }
}
```

---

## Validation Constraints & Patterns

Because the validator constructs the URL programmatically as `family + "://" + device_address + "/" + point_ref`, the structure of the metadata determines whether point references can be relative or absolute.

### Pattern A: Combined Style (Target + Relative Reference)
In this pattern, a shared device/gateway target definition provides the default protocol family and target address for the device. The individual point definitions specify only their relative references. 

**This is the required pattern when a concrete gateway target family (e.g. `"bacnet"`, `"modbus"`) is specified.**

#### Example JSON Metadata
```json
{
  "gateway": {
    "target": {
      "family": "bacnet",
      "addr": "1234"
    }
  },
  "pointset": {
    "points": {
      "fan_run_status": {
        "ref": "BI/1"
      },
      "supply_air_temp": {
        "ref": "AV/2"
      }
    }
  }
}
```
**Generated & Validated URLs:**
* `fan_run_status`: `bacnet://1234/BI/1`
* `supply_air_temp`: `bacnet://1234/AV/2`

---

### Pattern B: Ref-Only Style (Absolute / Self-Contained)
In this pattern, each individual point defines its full, absolute URL directly within its `ref` field. 

Because the point references contain a protocol scheme (i.e., they contain `://`), they bypass the default fallback combination logic. Instead of prepending the default gateway target family and target address, the system preserves the absolute reference as the fully qualified URL.

Each absolute reference is validated under the concrete protocol family provider matching its prefix (e.g., `bacnet://` is strictly validated under the `bacnet` family provider, and `modbus://` is validated under the `modbus` family provider). Therefore, omitting the `gateway.target.family` does not prevent or bypass validation for absolute URLs; they are always validated against the protocol scheme specified in their `ref` field.

> **Note:** Strict protocol validation for absolute URLs can be completely disabled by setting `"vendor_ref": true` under `gateway.target` in your metadata. When active, this flag tells the system to bypass all protocol-level validation checks.

#### Example JSON Metadata (Ref-Only Style)
```json
{
  "gateway": {
    "gateway_id": "LTGW-123"
  },
  "pointset": {
    "points": {
      "fan_run_status": {
        "ref": "bacnet://1234/BI/1"
      },
      "supply_air_temp": {
        "ref": "bacnet://1234/AV/2"
      }
    }
  }
}
```
**Generated & Validated URLs:**
* `fan_run_status`: `bacnet://1234/BI/1` (Validated under `"bacnet"` due to the `bacnet://` prefix)
* `supply_air_temp`: `bacnet://1234/AV/2` (Validated under `"bacnet"` due to the `bacnet://` prefix)

---

## Protocol-Specific Concrete Examples

The URL syntax for different address families is defined by their respective `FamilyProvider` implementations. Please refer to protocol-specific documentation (such as [bacnet.md](bacnet.md) and [modbus.md](modbus.md)) for detailed, exhaustive specifications of their reference formats and parameters.

### 1. BACnet Family (`bacnet`)
An address family representing BACnet devices and objects.
* **Combined Example:**
  * **Gateway Target Family**: `bacnet`
  * **Gateway Target Addr**: `1234`
  * **Point Ref**: `AV/100#present_value`
  * **Resulting URL**: `bacnet://1234/AV/100#present_value`

---

### 2. Modbus Family (`modbus`)
An address family representing Modbus registers.
* **Combined Example:**
  * **Gateway Target Family**: `modbus`
  * **Gateway Target Addr**: `2`
  * **Point Ref**: `3/40005?type=UINT32&worder=LWF&scale=0.01&network=modbus_rtu_1`
  * **Resulting URL**: `modbus://2/3/40005?type=UINT32&worder=LWF&scale=0.01&network=modbus_rtu_1`

---

## Advanced Deployment Topologies

The flexibility of absolute references enables advanced topologies where one logical UDMI "device" maps to multiple physical fieldbus targets or multiple protocols. 

**Because these topologies mix different target addresses or families, the `gateway.target.family` field must be left empty or omitted.** Every point must specify its full, absolute URL explicitly in its `ref` field.

### A. Multi-Address Devices (Single Protocol)
A multi-address device represents a single logical UDMI device whose points physically reside on different physical devices on the same local fieldbus network (e.g. multiple distinct BACnet devices).

#### Multi-Address Device Metadata Example
In the example below, `point_a` resides on BACnet device `1234`, while `point_b` physically resides on BACnet device `5678`.

```json
{
  "gateway": {
    "gateway_id": "LTGW-123",
    "family": "bacnet"
  },
  "pointset": {
    "points": {
      "point_a": {
        "ref": "1234/AV/1"
      },
      "point_b": {
        "ref": "5678/AV/1"
      }
    }
  }
}
```
**Generated & Validated URLs:**
* `point_a`: `bacnet://1234/AV/1`
* `point_b`: `bacnet://5678/AV/1`

---

### B. Multi-Family Devices (Mixed Protocols)
A multi-family device represents a single logical UDMI device composed of points spanning different physical networks and entirely different protocol families (e.g., a combined Modbus and BACnet device).

#### Multi-Family Device Metadata Example
In the example below, the device collects data from a BACnet device (`1234`) and a Modbus device connected to the serial network `modbus_rtu_1`.

```json
{
  "gateway": {
    "gateway_id": "LTGW-123"
  },
  "pointset": {
    "points": {
      "room_temperature": {
        "ref": "bacnet://1234/AI/1"
      },
      "fan_power": {
        "ref": "modbus://2/3/40001/1?type=INT16&network=modbus_rtu_1"
      }
    }
  }
}
```
**Generated & Validated URLs:**
* `room_temperature`: `bacnet://1234/AI/1` (Validated under `"bacnet"` due to the `bacnet://` prefix)
* `fan_power`: `modbus://2/3/40001/1?type=INT16&network=modbus_rtu_1` (Validated under `"modbus"` due to the `modbus://` prefix)
