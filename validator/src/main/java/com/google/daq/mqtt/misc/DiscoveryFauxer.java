package com.google.daq.mqtt.misc;

import static com.google.common.base.Preconditions.checkState;
import static com.google.udmi.util.JsonUtil.loadMap;
import static com.google.udmi.util.JsonUtil.stringify;
import static com.google.udmi.util.JsonUtil.stringifyTerse;
import static com.google.udmi.util.JsonUtil.toList;
import static com.google.udmi.util.JsonUtil.toMap;
import static java.lang.String.format;

import com.google.udmi.util.JsonUtil;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import udmi.schema.DiscoveryEvents;
import udmi.schema.PointPointsetModel;

/**
 * Simple converter for some foreign setup files into synthetic discovery events.
 */
public class DiscoveryFauxer {

  public static final String DATA_POINTS_KEY = "dataPoints";
  public static final String DEVICE_NAME_KEY = "deviceName";
  private static final String POINT_NAME_KEY = "name";
  private static final String OBJECT_TYPE_KEY = "objectType";
  private static final String INSTANCE_NUM_KEY = "objectInstanceNumber";
  public static final String DEVICE_ADDR_KEY = "remoteDeviceInstanceNumber";
  public static final String DATA_SOURCE_KEY = "dataSourceXid";
  public static final String DATA_SOURCES_KEY = "dataSources";
  public static final String SOURCE_TYPE_KEY = "type";
  private static final Object BACNET_IP_SOURCE_TYPE = "BACnetIP";
  private static final String SOURCE_ID_KEY = "xid";
  private Map<String, DiscoveryEvents> discoveryMap = new HashMap<>();
  private String discoveryFamily;
  private String deviceDataSource;

  /**
   * Let's go.
   */
  public static void main(String[] args) {
    if (args.length != 1) {
      throw new IllegalArgumentException("Expecting one arg: input file");
    }

    new DiscoveryFauxer().process(loadMap(args[0]));
  }

  private void process(Map<String, Object> inputMap) {
    processDataSource(toList(inputMap.get(DATA_SOURCES_KEY)));
    processPointList(toList(inputMap.get(DATA_POINTS_KEY)));
    emitDiscoveryMessages();
  }

  private void processDataSource(List<Object> sourcesList) {
    checkState(sourcesList.size() == 1, "exactly one data source expected");
    Map<String, Object> sourceProperties = toMap(sourcesList.get(0));
    // TODO: Make this also handle modbus and other protocols.
    checkState(BACNET_IP_SOURCE_TYPE.equals(sourceProperties.get(SOURCE_TYPE_KEY)));
    discoveryFamily = "bacnet";
    deviceDataSource = (String) sourceProperties.get(SOURCE_ID_KEY);
  }

  private void emitDiscoveryMessages() {
    DiscoveryEvents startEvent = newDiscoveryEvent(null);
    emitMessage(startEvent);
    discoveryMap.values().forEach(this::emitMessage);
    DiscoveryEvents stopEvent = newDiscoveryEvent(null);
    emitMessage(stopEvent);
  }

  private void emitMessage(Object event) {
    System.err.println(stringifyTerse(event));
  }

  private void processPointList(List<Object> pointList) {
    pointList.stream().map(JsonUtil::toMap).forEach(this::processPoint);
  }

  private void processPoint(Map<String, Object> point) {
    String deviceName = (String) point.get(DEVICE_NAME_KEY);
    addDevicePoint(discoveryMap.computeIfAbsent(deviceName, this::newDiscoveryEvent), point);
  }

  private void addDevicePoint(DiscoveryEvents device, Map<String, Object> point) {
    updateDeviceProperties(device, point);
    String pointName = (String) point.get(POINT_NAME_KEY);
    PointPointsetModel model = new PointPointsetModel();
    model.ref = makePointRef(point);
    checkState(device.points.put(pointName, model) == null, "duplicate point entry: " + pointName);
  }

  private void updateDeviceProperties(DiscoveryEvents device, Map<String, Object> point) {
    checkState(point.get(DATA_SOURCE_KEY).equals(deviceDataSource), "inconsistent device source");
    String deviceAddr = (String) point.get(DEVICE_ADDR_KEY);
    checkState(device.addr == null || device.addr.equals(deviceAddr), "inconsistent device addr");
    device.addr = deviceAddr;
  }

  private static String makePointRef(Map<String, Object> point) {
    String objectType = (String) point.get(OBJECT_TYPE_KEY);
    String instanceNum = (String) point.get(INSTANCE_NUM_KEY);
    return format("%s:%s", objectType, instanceNum);
  }

  private DiscoveryEvents newDiscoveryEvent(String deviceId) {
    DiscoveryEvents discoveryEvents = new DiscoveryEvents();
    discoveryEvents.family = discoveryFamily;
    discoveryEvents.points = new HashMap<>();
    return discoveryEvents;
  }
}
