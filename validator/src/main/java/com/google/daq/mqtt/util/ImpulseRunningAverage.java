package com.google.daq.mqtt.util;

/**
 * Basic class to gather and manage time statistics.
 */
public class ImpulseRunningAverage extends RunningAverage {

  private static final double BASELINE_VALUE = 0.0;

  public ImpulseRunningAverage(String name) {
    super(name, () -> BASELINE_VALUE);
  }

  @Override
  protected double getImpulse(double value) {
    return value;
  }

  @Override
  protected double getDelta(double value) {
    return running;
  }

  @Override
  protected double getBaseline(double value) {
    return 0;
  }
}