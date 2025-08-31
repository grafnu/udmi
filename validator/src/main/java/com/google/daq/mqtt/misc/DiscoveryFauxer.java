package com.google.daq.mqtt.misc;

import static com.google.common.base.Preconditions.checkState;
import static com.google.udmi.util.GeneralUtils.friendlyStackTrace;
import static com.google.udmi.util.GeneralUtils.getNow;
import static com.google.udmi.util.GeneralUtils.stackTraceString;
import static com.google.udmi.util.JsonUtil.loadMap;
import static com.google.udmi.util.JsonUtil.stringifyTerse;
import static com.google.udmi.util.JsonUtil.toList;
import static com.google.udmi.util.JsonUtil.toMap;
import static java.lang.String.format;
import static java.util.Objects.requireNonNull;

import com.google.common.collect.ImmutableMap;
import com.google.daq.mqtt.util.RunningAverageBase;
import com.google.udmi.util.JsonUtil;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import udmi.schema.Category;
import udmi.schema.DiscoveryEvents;
import udmi.schema.Entry;
import udmi.schema.Level;
import udmi.schema.RefDiscovery;

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
  private static final String TAGS_KEY = "tags";
  private static final String BACNET_TYPE_KEY = "BACnetPropertyName";
  private static final String POINT_XID_KEY = "xid";
  public static final String POINT_LOCATOR_KEY = "pointLocator";
  private static final Map<String, String> BACNET_TYPES = ImmutableMap.of(
      "ANALOG_INPUT", "AI",
      "ANALOG_VALUE", "AV",
      "ANALOG_OUTPUT", "AO",
      "BINARY_INPUT", "BI",
      "BINARY_VALUE", "BV",
      "BINARY_OUTPUT", "BO");
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
    try {
      pointList.stream().map(JsonUtil::toMap).forEach(this::processPoint);
    } catch (Exception e) {
      throw new RuntimeException("While processing points list", e);
    }
  }

  private void processPoint(Map<String, Object> point) {
    String xid = requireNonNull((String) point.get(POINT_XID_KEY));
    String deviceName = requireNonNull((String) point.get(DEVICE_NAME_KEY), "missing device name");
    try {
      addDevicePoint(discoveryMap.computeIfAbsent(deviceName, this::newDiscoveryEvent), point);
    } catch (Exception e) {
      System.err.printf("Error processing %s: %s", xid, friendlyStackTrace(e));
      discoveryMap.get(deviceName).status = discoveryErrorStatus(e);
    }
  }

  private Entry discoveryErrorStatus(Exception e) {
    Entry entry = new Entry();
    entry.level = Level.ERROR.value();
    entry.message = "Exception processing point: " + friendlyStackTrace(e);
    entry.detail = stackTraceString(e);
    entry.category = Category.DISCOVERY_POINT_DESCRIBE;
    entry.timestamp = getNow();
    return entry;
  }

  private void addDevicePoint(DiscoveryEvents device, Map<String, Object> point) {
    updateDeviceProperties(device, point);
    String pointRef = makePointRef(point);
    RefDiscovery model = new RefDiscovery();
    checkState(device.refs.put(pointRef, model) == null, "duplicate ref entry: " + pointRef);
    model.name = requireNonNull((String) point.get(POINT_NAME_KEY), "missing point name");
    // model.ancillary = point;
  }

  private void updateDeviceProperties(DiscoveryEvents device, Map<String, Object> point) {
    checkState(point.get(DATA_SOURCE_KEY).equals(deviceDataSource), "inconsistent device source");
    String deviceAddr = (String) point.get(DEVICE_ADDR_KEY);
    checkState(device.addr == null || device.addr.equals(deviceAddr), "inconsistent device addr");
    device.addr = deviceAddr;
  }

  private static String makePointRef(Map<String, Object> point) {
    Map<String, Object> locator = toMap(point.get(POINT_LOCATOR_KEY));
    String type = requireNonNull((String) locator.get(OBJECT_TYPE_KEY), "missing object type");
    Integer num = requireNonNull((Integer) locator.get(INSTANCE_NUM_KEY), "missing instance num");
    return format("%s:%s", convertBacnetType(type), num);
  }

  private static String convertBacnetType(String type) {
    return requireNonNull(BACNET_TYPES.get(type), "unknown bacnet type: " + type);
  }

  private DiscoveryEvents newDiscoveryEvent(String deviceId) {
    DiscoveryEvents discoveryEvents = new DiscoveryEvents();
    discoveryEvents.family = discoveryFamily;
    discoveryEvents.refs = new HashMap<>();
    return discoveryEvents;
  }
}
