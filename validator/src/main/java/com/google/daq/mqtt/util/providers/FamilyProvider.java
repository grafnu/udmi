package com.google.daq.mqtt.util.providers;

import static com.google.common.base.Preconditions.checkState;
import static com.google.udmi.util.GeneralUtils.ifTrueThen;
import static java.lang.String.format;

import com.google.common.collect.ImmutableSet;
import java.util.Map;
import java.util.Set;
import java.util.stream.Collectors;
import udmi.schema.FamilyLocalnetModel;

/**
 * Abstract collection of stuff for managing vendor families.
 */
public interface FamilyProvider {

  int MAX_PORT_VALUE = 65535;
  String PORT_SEPARATOR = ":";

  /**
   * Set of all the supported protocol families.
   */
  Set<Class<? extends FamilyProvider>> PROTOCOL_FAMILIES = ImmutableSet.of(
      IotFamilyProvider.class,
      VendorFamilyProvider.class,
      EtherFamilyProvider.class,
      Ipv4FamilyProvider.class,
      Ipv6FamilyProvider.class,
      BacnetFamilyProvider.class,
      FqdnFamilyProvider.class,
      ModbusFamilyProvider.class
  );

  /**
   * Map of family name to instance.
   */
  Map<String, FamilyProvider> NAMED_FAMILIES = generateFamilyMap(PROTOCOL_FAMILIES);

  /**
   * Generate a named map from the listed families.
   */
  static Map<String, FamilyProvider> generateFamilyMap(
      Set<Class<? extends FamilyProvider>> protocolFamilies) {
    return protocolFamilies.stream().map(clazz -> {
      try {
        return clazz.getDeclaredConstructor().newInstance();
      } catch (Exception e) {
        throw new RuntimeException("While creating protocol family map", e);
      }
    }).collect(Collectors.toMap(FamilyProvider::familyKey, family -> family));
  }

  /**
   * Construct a URL for the given family, device, and point.
   */
  static String constructUrl(String family, String device, String point) {
    return constructUrl(family, device, null, point);
  }

  /**
   * Construct a URL for the given family, device, unitId, and point.
   */
  static String constructUrl(String family, String device, String unitId, String point) {
    if (unitId != null) {
      return family + "://" + device + "/" + unitId + "/" + (point != null ? point : "");
    }
    return family + "://" + device + "/" + (point != null ? point : "");
  }

  /**
   * Validate a complete URL for the family.
   */
  default void validateUrl(String url) {
    String[] parts = url.split("/", 4);
    checkState(parts.length >= 3, "Expected at least 3 parts, had " + parts.length);
    String familyPrefix = familyKey() + ":";
    checkState(familyPrefix.equals(parts[0]), format("Given family %s does not match expected %s",
        familyPrefix, parts[0]));
    ifTrueThen(parts[1].length() > 0, () -> validateNetwork(parts[1]));
    validateAddrUrl(parts[2]);
    ifTrueThen(parts.length > 3, () -> validatePoint(parts[3]));
  }

  /**
   * Return the family name represented by this class.
   */
  String familyKey();

  /**
   * Validate a family network identifier.
   */
  default void validateNetwork(String network) {
    throw new IllegalArgumentException("Network designator not allowed for family " + familyKey());
  }

  /**
   * Validate a standalone family address.
   */
  void validateAddr(String scanAddr);

  /**
   * Validate a family address as part of a URL, which may have an associated port with it.
   */
  default void validateAddrUrl(String urlAddr) {
    validateAddr(validatePort(urlAddr));
  }

  /**
   * Validate and remove the port (if present), from a URL hostname designator.
   */
  default String validatePort(String urlAddr) {
    int separatorIndex = urlAddr.lastIndexOf(PORT_SEPARATOR);
    if (separatorIndex >= 0) {
      int port = Integer.parseInt(urlAddr.substring(separatorIndex + 1));
      checkState(port >= 0 && port <= MAX_PORT_VALUE,
          format("ipv4 ref port %s exceeds maximum %d", port, MAX_PORT_VALUE));
      return urlAddr.substring(0, separatorIndex);
    }
    return urlAddr;
  }

  /**
   * Validate the given point ref for the address family.
   */
  default void validatePoint(String metadataRef) {
    throw new IllegalArgumentException("Point reference not allowed for family " + familyKey());
  }

  /**
   * Extract the host/network authority component (without port) from a full point URL.
   */
  static String extractUrlAddr(String url) {
    String[] parts = url.split("/", 4);
    checkState(parts.length >= 3, "Expected at least 3 parts, had " + parts.length);
    String urlAddr = parts[2];
    int lastBracket = urlAddr.lastIndexOf(']');
    int lastColon = urlAddr.lastIndexOf(PORT_SEPARATOR);
    if (lastColon > lastBracket) {
      return urlAddr.substring(0, lastColon);
    }
    return urlAddr;
  }

  /**
   * Return true if the given non-port address authority requires matching against a gateway's
   * localnet.networks map (alphabetic network key or hostname rather than a numeric/IP literal).
   */
  static boolean requiresNetworkMatch(String addrWithoutPort) {
    if (addrWithoutPort == null || addrWithoutPort.isEmpty()) {
      return false;
    }
    if (ModbusFamilyProvider.isValidIpv4(addrWithoutPort)) {
      return false;
    }
    if (addrWithoutPort.startsWith("[") || addrWithoutPort.chars().allMatch(Character::isDigit)) {
      return false;
    }
    return true;
  }

  /**
   * Check whether a localnet.networks key matches the given address authority:
   * leading-dot keys (e.g. ".google.com") use strict suffix match; all other keys use exact match.
   */
  static boolean matchesNetworkKey(String networkKey, String addrWithoutPort) {
    return networkKey.startsWith(".")
        ? addrWithoutPort.endsWith(networkKey)
        : addrWithoutPort.equals(networkKey);
  }

  /**
   * Validate that the given full URL's authority matches at least one entry in the gateway's
   * localnet.networks map when required.
   */
  static void validateUrlNetwork(String url, Map<String, FamilyLocalnetModel> gatewayNetworks) {
    String targetFamily = url.contains("://") ? url.substring(0, url.indexOf("://")) : "vendor";
    if ("vendor".equals(targetFamily) || "iot".equals(targetFamily)) {
      return;
    }
    String addrWithoutPort = extractUrlAddr(url);
    if (!requiresNetworkMatch(addrWithoutPort)) {
      return;
    }
    if (gatewayNetworks == null || gatewayNetworks.isEmpty()) {
      throw new IllegalArgumentException(format(
          "No localnet.networks defined on gateway to match address '%s'", addrWithoutPort));
    }
    boolean matched = gatewayNetworks.keySet().stream()
        .anyMatch(key -> matchesNetworkKey(key, addrWithoutPort));
    if (!matched) {
      throw new IllegalArgumentException(format(
          "Address '%s' does not match any network in gateway localnet.networks %s",
          addrWithoutPort, gatewayNetworks.keySet()));
    }
  }

  /**
   * Validate a family localnet model.
   */
  default void validateModel(FamilyLocalnetModel familyModel) {
    if (familyModel.adjunct != null && !familyModel.adjunct.isEmpty()) {
      throw new IllegalArgumentException(String.format(
          "Adjunct properties not allowed for family %s: %s",
          familyKey(), familyModel.adjunct.keySet()));
    }
  }
}
