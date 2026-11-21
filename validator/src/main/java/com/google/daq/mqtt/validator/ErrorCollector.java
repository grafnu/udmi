package com.google.daq.mqtt.validator;

/**
 * Simple interface for collecting errors of various kinds.
 */
public interface ErrorCollector {

  void addError(Exception error, String category, String detail);

  /**
   * Record a message-scoped error without duplicating cumulative device summary state.
   */
  default void addMessageError(Exception error, String category, String detail) {
    addError(error, category, detail);
  }
}
