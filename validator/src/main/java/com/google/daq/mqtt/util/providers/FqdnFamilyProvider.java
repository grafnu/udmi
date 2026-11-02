package com.google.daq.mqtt.util.providers;

import static com.google.common.base.Preconditions.checkState;
import static java.lang.String.format;
import static java.util.Objects.requireNonNull;
import static udmi.lib.ProtocolFamily.FQDN;

import java.util.regex.Pattern;

/**
 * General family of Fully Qualified Domain Names (FQDN) and domain suffixes.
 */
public class FqdnFamilyProvider implements FamilyProvider {

  private static final int MAX_DOMAIN_LENGTH = 253;
  private static final String LABEL = "[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?";
  private static final String TLD = "(?:[a-z]{2,63}|xn--[a-z0-9-]{2,59})";
  private static final Pattern FQDN_PATTERN =
      Pattern.compile("^(?:" + LABEL + "\\.)+" + TLD + "$");
  private static final Pattern DOMAIN_SUFFIX_PATTERN =
      Pattern.compile("^\\.(?:" + LABEL + "\\.)*" + TLD + "$");

  /**
   * Check whether the given address is a valid canonical lowercase FQDN (no trailing dot).
   */
  public static boolean isValidFqdn(String scanAddr) {
    return scanAddr != null
        && scanAddr.length() <= MAX_DOMAIN_LENGTH
        && FQDN_PATTERN.matcher(scanAddr).matches();
  }

  /**
   * Check whether the given string is a valid leading-dot domain suffix (e.g. ".google.com").
   */
  public static boolean isValidDomainSuffix(String network) {
    return network != null
        && network.length() <= MAX_DOMAIN_LENGTH
        && DOMAIN_SUFFIX_PATTERN.matcher(network).matches();
  }

  @Override
  public String familyKey() {
    return FQDN;
  }

  @Override
  public void validateAddr(String scanAddr) {
    requireNonNull(scanAddr, "missing required fqdn scan_addr");
    checkState(isValidFqdn(scanAddr),
        format("fqdn scan_addr %s is not a valid lowercase FQDN", scanAddr));
  }

  @Override
  public void validateNetwork(String networkAddr) {
    requireNonNull(networkAddr, "missing required fqdn network");
    checkState(isValidDomainSuffix(networkAddr),
        format("fqdn network %s must be a valid leading-dot domain suffix (e.g. .google.com)",
            networkAddr));
  }
}
