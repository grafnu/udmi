[**UDMI**](../../) / [**Docs**](../) / [**Specs**](./) / [Modbus](#)

THIS IS A PROVISIONAL SPEC THAT IS SUBJECT TO CHANGE.

# Modbus Specification

UDMI supports reading Modbus points by specifying them via a `modbus://` URL schema.

## URI Schema

`modbus://<addr>[:port]/<unitid>/<function>/<address>[/<quantity>][?interpretation]`

*   **`addr`**: An IPv4 address (e.g., `192.28.27.3`), a single-label network/host identifier (e.g., `modbus_rtu_1` or `localhost`, matched by exact key in `localnet.networks`), or a canonical lowercase FQDN without trailing dot (e.g., `foo.google.com` or `sensor-1.local`, matched by leading-dot suffix such as `.google.com` or `.local` in `localnet.networks`).
*   **`port`**: (Optional) The TCP port, e.g. "192.168.2.3:2833" or "hostname:2741".
*   **`unitid`**: The Slave ID (Unit Identifier).
*   **`function`**: The Function Code (see [Function Codes](#function-codes)).
*   **`address`**: The starting register address.
*   **`quantity`**: (Optional) The number of registers to fetch.
*   **`interpretation`**: (Optional) Query string properties that are necessary to properly interpret data fetched from a register.

### Function Codes

*   **`1`**: Read Coils
*   **`2`**: Read Discrete Inputs
*   **`3`**: Read Holding Registers
*   **`4`**: Read Input Registers
*   **`5`**: Write Single Coil
*   **`6`**: Write Single Register
*   **`15`**: Write Multiple Coils
*   **`16`**: Write Multiple Registers

### Interpretation Parameters

Parameters passed in the query string define how to interpret the fetched register data:

*   **`border`**: i.e., `MSB` (`Big-Endian`) or `LSB` (`Little-Endian`).
*   **`type`**: e.g., `INT16`, `UINT32`, `BOOLEAN`, `ASCII`, `FLOAT32` (base type and optional length).
*   **`worder`**: i.e., `HWF` (`High-Word First`) or `LWF` (`Low-Word First`) (for 32-bit values).
*   **`scale`**: e.g., `1.0`, `0.01`, `100.0` (scale factor).
*   **`offset`**: e.g., `0`, `0.5` (offset applied to value after scaling).

### Network Parameters

The `addr` field is validated as one of three forms: an IPv4 address, a single-label network/host key, or a canonical lowercase multi-label FQDN (no trailing dot). For proxied devices in a site model, any non-IP `addr` must match an entry in the parent gateway's `localnet.networks` map (defined in `model_localnet.json`) via exact key match (`addr.equals(key)`) for single-label keys or strict leading-dot suffix match (`addr.endsWith(key)`) for FQDNs. Each named network can define the following (optional) `adjunct` parameters for communication:

*   **`protocol`**: i.e., `RTU` or `TCP`.
*   **`device`**: The port device identifier, e.g. `/dev/tty1`.
*   **`baud`**: The baud rate.
*   **`parity`**: Serial parity.
*   **`data_bits`**: Number of serial data bits.
*   **`stop_bits`**: Number of serial stop bits.

## Examples

The metadata values in the examples below map to the following complete Modbus URIs:

*   **Network RTU Point**: `modbus://192.483.4.1/1/3/40001/1?type=INT16&border=MSB`
*   **Network TCP Point**: `modbus://192.168.2.3:2833/1/3/40001/1?type=INT16&border=MSB`
*   **`fan_status`**: `modbus://modbus_rtu_1/2/1/101?type=BOOLEAN`
*   **`filter_differential_pressure`**: `modbus://localhost/2/4/30005?type=UINT32&worder=LWF&scale=0.01`
*   **Hostname TCP Point**: `modbus://hostname:2741/2/4/30005?type=UINT32&worder=LWF&scale=0.01`

### Network Configuration Example

The serial-bus `RTU` and domain-suffix `TCP` network specifications as part of a gateway `metadata.json` file:

```json
{
  "localnet": {
    "networks": {
      "modbus_rtu_1": {
        "family": "modbus",
        "adjunct": {
          "protocol": "RTU",
          "baud": 9600,
          "parity": "none",
          "data_bits": 8,
          "stop_bits": 1,
          "device": "COM1"
        }
      },
      ".google.com": {
        "family": "fqdn"
      }
    }
  }
}
```

### Proxy Device Example

For a proxied Modbus device, the `gateway` block in the `metadata.json` specifies the target device ID (Unit ID), while the `pointset` block defines the individual points and their register mappings.

```json
{
  "gateway": {
    "target": {
      "family": "modbus",
      "addr": "modbus_rtu_1/2"
    }
  },
  "pointset": {
    "points": {
      "fan_status": {
        "ref": "1/101?type=BOOLEAN"
      },
      "filter_differential_pressure": {
        "units": "Pascals",
        "ref": "4/30005?type=UINT32&worder=LWF&scale=0.01"
      }
    }
  }
}
```
