package com.google.daq.mqtt.validator;

import static com.google.common.base.Preconditions.checkNotNull;
import static com.google.daq.mqtt.util.providers.FamilyProvider.constructUrl;
import static java.util.Optional.ofNullable;

import com.google.daq.mqtt.util.providers.FamilyProvider;
import java.util.Map;
import udmi.schema.DiscoveryEvents;
import udmi.schema.DiscoveryState;
import udmi.schema.Metadata;
import udmi.schema.State;

/**
 * Class to handle validating all sorts of discovery messages.
 */
public class DiscoveryValidator {

  private final ErrorCollector errorCollector;
  private final String deviceId;
  private final DiscoveryAnalyzer analyzer;

  public DiscoveryValidator(ErrorCollector errorCollector, Metadata metadata) {
    this(errorCollector, null, metadata, null);
  }

  /**
   * Create a discovery validator bound to a device and optional site-wide analyzer.
   */
  public DiscoveryValidator(ErrorCollector errorCollector, String deviceId, Metadata metadata,
      DiscoveryAnalyzer analyzer) {
    this.errorCollector = errorCollector;
    this.deviceId = deviceId;
    this.analyzer = analyzer;
  }

  void validateMessage(Object message, Map<String, String> attributes) {
    String effectiveDeviceId = ofNullable(deviceId)
        .orElseGet(() -> attributes == null ? null : attributes.get("deviceId"));
    if (message instanceof State stateMessage) {
      validateMessage(stateMessage, effectiveDeviceId);
    } else if (message instanceof DiscoveryState discoveryState) {
      validateMessage(discoveryState, effectiveDeviceId);
    } else if (message instanceof DiscoveryEvents discoveryEvents) {
      validateMessage(discoveryEvents, effectiveDeviceId);
    }
  }

  public void validateMessage(State stateMessage) {
    validateMessage(stateMessage, deviceId);
  }

  private void validateMessage(State stateMessage, String effectiveDeviceId) {
    if (stateMessage != null && stateMessage.discovery != null) {
      validateMessage(stateMessage.discovery, effectiveDeviceId);
    }
  }

  /**
   * Validate a discovery event.
   */
  public void validateMessage(DiscoveryEvents discoveryEvents) {
    validateMessage(discoveryEvents, deviceId);
  }

  private void validateMessage(DiscoveryEvents discoveryEvents, String effectiveDeviceId) {
    if (discoveryEvents.family == null) {
      throw new IllegalArgumentException("discovery family not defined");
    }
    if (analyzer != null && effectiveDeviceId != null) {
      analyzer.processEvents(effectiveDeviceId, discoveryEvents, errorCollector);
      return;
    }
    String scanFamily = discoveryEvents.family;
    FamilyProvider familyProvider = FamilyProvider.NAMED_FAMILIES.get(scanFamily);
    checkNotNull(familyProvider, "Unknown provider for discovery family " + scanFamily);
    if (discoveryEvents.addr != null) {
      if (discoveryEvents.refs == null) {
        familyProvider.validateUrl(constructUrl(scanFamily, discoveryEvents.addr, null));
      } else {
        discoveryEvents.refs.forEach((key, value) ->
            familyProvider.validateUrl(constructUrl(scanFamily, discoveryEvents.addr, key)));
      }
    } else if (discoveryEvents.network != null) {
      familyProvider.validateNetwork(discoveryEvents.network);
    }
  }

  public void validateMessage(DiscoveryState discoveryState) {
    validateMessage(discoveryState, deviceId);
  }

  private void validateMessage(DiscoveryState discoveryState, String effectiveDeviceId) {
    if (analyzer != null && effectiveDeviceId != null && discoveryState != null) {
      analyzer.processState(effectiveDeviceId, discoveryState, errorCollector);
    }
  }
}