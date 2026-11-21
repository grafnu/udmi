package com.google.daq.mqtt.validator;

import static com.google.udmi.util.Common.DEVICE_ID_KEY;
import static com.google.udmi.util.Common.TIMESTAMP_KEY;
import static com.google.udmi.util.JsonUtil.getInstant;
import static com.google.udmi.util.JsonUtil.isoConvert;
import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertNull;
import static org.junit.Assert.assertTrue;
import static udmi.schema.Level.INFO;

import com.google.common.collect.ImmutableList;
import com.google.daq.mqtt.TestCommon;
import com.google.daq.mqtt.util.MessagePublisher;
import com.google.daq.mqtt.validator.Validator.MessageBundle;
import com.google.udmi.util.SiteModel;
import java.time.Instant;
import java.util.Date;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.atomic.AtomicReference;
import org.junit.Test;
import org.mockito.Mockito;
import udmi.schema.Category;
import udmi.schema.CloudModel;
import udmi.schema.CloudModel.ModelOperation;
import udmi.schema.Config;
import udmi.schema.DeviceValidationEvents;
import udmi.schema.DiscoveryEvents;
import udmi.schema.Metadata;
import udmi.schema.PointPointsetEvents;
import udmi.schema.PointsetEvents;
import udmi.schema.PointsetState;
import udmi.schema.PointsetSummary;
import udmi.schema.State;
import udmi.schema.SystemModel;
import udmi.schema.SystemState;
import udmi.schema.ValidationEvents;
import udmi.schema.ValidationState;

/**
 * Unit test suite for the validator.
 */
public class BasicTest extends TestBase {

  private static final String EVENTS_SUBTYPE = "events";
  private static final String CONFIG_SUBTYPE = "config";
  private static final String STATE_SUBTYPE = "state";
  private static final String MODEL_SUBTYPE = "model";
  private static final String POINTSET_SUBFOLDER = "pointset";
  private static final String DISCOVERY_SUBFOLDER = "discovery";
  private static final String UPDATE_SUBFOLDER = "update";
  private static final List<String> TEST_ARGS = ImmutableList.of(
      "-n",
      "-p", SiteModel.MOCK_PROJECT,
      "-a", TestCommon.SCHEMA_SPEC,
      "-s", TestCommon.SITE_DIR);
  private static final String FLUX_READING = "FLUX_READING";
  private static final long TWO_SECONDS_MS = 1000 * 2;
  private final Validator validator = new Validator(TEST_ARGS).prepForMock();

  @Test
  public void emptySystemBlock() {
    Validator.MessageBundle bundle = getMessageBundle(EVENTS_SUBTYPE, POINTSET_SUBFOLDER,
        new PointsetEvents());
    bundle.message.remove("system");
    validator.validateMessage(bundle);
    ValidationState report = getValidationReport();
    assertEquals("One error summary", 1, report.summary.error_devices.size());
  }

  @Test
  public void emptyPointsetEvents() {
    MessageBundle bundle = getMessageBundle(EVENTS_SUBTYPE, POINTSET_SUBFOLDER,
        new PointsetEvents());
    validator.validateMessage(bundle);
    ValidationState report = getValidationReport();
    assertEquals("One error summary", 1, report.summary.error_devices.size());
  }

  @Test
  public void validPointsetEvents() {
    PointsetEvents messageObject = basePointsetEvents();
    MessageBundle bundle = getMessageBundle(EVENTS_SUBTYPE, POINTSET_SUBFOLDER, messageObject);
    validator.validateMessage(bundle);
    ValidationState report = getValidationReport();
    assertEquals("No error devices", 0, report.summary.error_devices.size());
    DeviceValidationEvents deviceValidationEvents = report.devices.get(TestCommon.DEVICE_ID);
    assertEquals("report status level", (Object) INFO.value(),
        deviceValidationEvents.status.level);
    String expected = isoConvert(messageObject.timestamp);
    String lastSeen = isoConvert(deviceValidationEvents.last_seen);
    assertEquals("status last_seen", expected, lastSeen);
  }

  @Test
  public void numericStringPointsetEvents() {
    PointsetEvents messageObject = basePointsetEvents();
    messageObject.points.get(FILTER_DIFFERENTIAL_PRESSURE_SETPOINT).present_value = "20.0";
    MessageBundle bundle = getMessageBundle(EVENTS_SUBTYPE, POINTSET_SUBFOLDER, messageObject);
    validator.validateMessage(bundle);
    ValidationState report = getValidationReport();
    assertEquals("One error devices", 1, report.summary.error_devices.size());
  }

  @Test
  public void invalidMultistatePointsetEvents() {
    PointsetEvents messageObject = basePointsetEvents();
    messageObject.points.get(SUPPLY_FAN_RUN_STATUS).present_value = "ON";
    MessageBundle bundle = getMessageBundle(EVENTS_SUBTYPE, POINTSET_SUBFOLDER, messageObject);
    validator.validateMessage(bundle);
    ValidationState report = getValidationReport();
    assertEquals("One error devices", 1, report.summary.error_devices.size());
  }

  @Test
  public void invalidBinaryPointsetEvents() {
    PointsetEvents messageObject = basePointsetEvents();
    messageObject.points.get(FILTER_ALARM_PRESSURE_STATUS).present_value = "true";
    MessageBundle bundle = getMessageBundle(EVENTS_SUBTYPE, POINTSET_SUBFOLDER, messageObject);
    validator.validateMessage(bundle);
    ValidationState report = getValidationReport();
    assertEquals("One error devices", 1, report.summary.error_devices.size());
  }

  @Test
  public void invalidStringPointsetEvents() {
    PointsetEvents messageObject = basePointsetEvents();
    messageObject.points.get(ACTIVE_FAULT_CODE).present_value = 404;
    MessageBundle bundle = getMessageBundle(EVENTS_SUBTYPE, POINTSET_SUBFOLDER, messageObject);
    validator.validateMessage(bundle);
    ValidationState report = getValidationReport();
    assertEquals("One error devices", 1, report.summary.error_devices.size());
  }

  @Test
  public void missingPointsetEvents() {
    PointsetEvents messageObject = basePointsetEvents();
    messageObject.points.remove(FILTER_ALARM_PRESSURE_STATUS);
    MessageBundle bundle = getMessageBundle(EVENTS_SUBTYPE, POINTSET_SUBFOLDER, messageObject);
    validator.validateMessage(bundle);
    ValidationState report = getValidationReport();
    assertEquals("One error devices", 1, report.summary.error_devices.size());
    ValidationEvents result = getValidationResult(TestCommon.DEVICE_ID, EVENTS_SUBTYPE,
        POINTSET_SUBFOLDER);
    PointsetSummary pointset = result.pointset;
    assertEquals("Missing one point", 1, pointset.missing.size());
    assertTrue("Missing correct point", pointset.missing.contains(FILTER_ALARM_PRESSURE_STATUS));
    assertEquals("No extra points", 0, pointset.extra.size());
  }

  @Test
  public void additionalPointsetEvents() {
    PointsetEvents messageObject = basePointsetEvents();
    messageObject.points.put(FLUX_READING, new PointPointsetEvents());
    MessageBundle bundle = getMessageBundle(EVENTS_SUBTYPE, POINTSET_SUBFOLDER, messageObject);
    validator.validateMessage(bundle);
    ValidationState report = getValidationReport();
    assertEquals("One error devices", 1, report.summary.error_devices.size());
    ValidationEvents result = getValidationResult(TestCommon.DEVICE_ID, EVENTS_SUBTYPE,
        POINTSET_SUBFOLDER);
    PointsetSummary points = result.pointset;
    assertEquals("No missing points", 0, points.missing.size());
    assertEquals("One extra point", 1, points.extra.size());
    assertTrue("Missing correct point", points.extra.contains(FLUX_READING));
  }

  @Test
  public void missingPointsetState() {
    PointsetState messageObject = basePointsetState();
    messageObject.points.remove(FILTER_ALARM_PRESSURE_STATUS);
    MessageBundle bundle = getMessageBundle(STATE_SUBTYPE, POINTSET_SUBFOLDER, messageObject);
    validator.validateMessage(bundle);
    ValidationState report = getValidationReport();
    assertEquals("One error devices", 1, report.summary.error_devices.size());
    ValidationEvents result = getValidationResult(TestCommon.DEVICE_ID, STATE_SUBTYPE,
        POINTSET_SUBFOLDER);
    List<String> missingPoints = result.pointset.missing;
    assertEquals("Missing one point", 1, missingPoints.size());
    assertTrue("Missing correct point", missingPoints.contains(FILTER_ALARM_PRESSURE_STATUS));
  }

  @Test
  public void lastSeenUpdate() {
    Validator.MessageBundle eventBundle = getMessageBundle(EVENTS_SUBTYPE, DISCOVERY_SUBFOLDER,
        new DiscoveryEvents());
    validator.validateMessage(eventBundle);

    advanceClockSec(10);

    Validator.MessageBundle configBundle = getMessageBundle(CONFIG_SUBTYPE, UPDATE_SUBFOLDER,
        new Config());
    validator.validateMessage(configBundle);

    // Only the event should update the last seen, since config is not from the device.
    ValidationState report = getValidationReport();
    DeviceValidationEvents deviceValidationEvents = report.devices.get(TestCommon.DEVICE_ID);
    Date lastSeen = deviceValidationEvents.last_seen;
    Instant parse = getInstant((String) eventBundle.message.get(TIMESTAMP_KEY));
    assertEquals("device last seen", isoConvert(Date.from(parse)), isoConvert(lastSeen));
  }

  @Test
  public void deviceMetadataUpdate() {
    Metadata messageObject = new Metadata();
    messageObject.version = TestCommon.UDMI_VERSION;
    messageObject.system = new SystemModel();
    messageObject.system.description = "Updated description";
    MessageBundle messageBundle = getMessageBundle(MODEL_SUBTYPE, UPDATE_SUBFOLDER, messageObject);

    validator.validateMessage(messageBundle);

    Map<String, ReportingDevice> reportingDevices = validator.getReportingDevices();
    assertEquals(messageObject.system.description,
        reportingDevices.get(TestCommon.DEVICE_ID).getMetadata().system.description);
  }

  @Test
  public void deviceDeleteMetadataUpdate() {
    Metadata messageObject = new Metadata();
    messageObject.version = TestCommon.UDMI_VERSION;
    messageObject.system = new SystemModel();
    messageObject.cloud = new CloudModel();
    messageObject.cloud.operation = ModelOperation.DELETE;
    MessageBundle messageBundle = getMessageBundle(MODEL_SUBTYPE, UPDATE_SUBFOLDER, messageObject);

    validator.validateMessage(messageBundle);

    Map<String, ReportingDevice> reportingDevices = validator.getReportingDevices();
    assertNull(reportingDevices.get(TestCommon.DEVICE_ID));
  }

  @Test
  public void addNewDeviceWithModelUpdate() {
    Metadata messageObject = new Metadata();
    messageObject.version = TestCommon.UDMI_VERSION;
    messageObject.system = new SystemModel();
    messageObject.system.description = "New description";
    MessageBundle messageBundle = getMessageBundle(MODEL_SUBTYPE, UPDATE_SUBFOLDER, messageObject);

    String newDeviceId = TestCommon.DEVICE_ID + "_1";
    messageBundle.attributes.put(DEVICE_ID_KEY, newDeviceId);

    validator.validateMessage(messageBundle);

    Map<String, ReportingDevice> reportingDevices = validator.getReportingDevices();
    Set<String> expectedDevices = validator.getExpectedDevices();

    assertTrue(expectedDevices.contains(newDeviceId));
    assertTrue(reportingDevices.containsKey(newDeviceId));
    assertEquals(messageObject.system.description,
        reportingDevices.get(newDeviceId).getMetadata().system.description);
  }

  @Test
  public void pendingDeviceLifecycle() {
    // 1. Device sends state message only -> should be marked as pending_devices
    State state = new State();
    state.timestamp = new Date();
    state.version = TestCommon.UDMI_VERSION;
    state.system = new SystemState();
    state.system.last_config = new Date();
    state.system.serial_no = "12345";
    state.system.hardware = new udmi.schema.StateSystemHardware();
    state.system.hardware.make = "Google";
    state.system.hardware.model = "UDMI";
    state.system.software = new java.util.HashMap<>();
    state.system.operation = new udmi.schema.StateSystemOperation();
    state.system.operation.operational = true;
    validator.validateMessage(getMessageBundle(STATE_SUBTYPE, UPDATE_SUBFOLDER, state));
    ValidationState report1 = getValidationReport();
    assertTrue("Device should be pending when only state received",
        report1.summary.pending_devices.contains(TestCommon.DEVICE_ID));

    // 2. Device subsequently sends telemetry -> transitions from pending_devices to correct_devices
    PointsetEvents pointsetEvents = basePointsetEvents();
    validator.validateMessage(getMessageBundle(EVENTS_SUBTYPE, POINTSET_SUBFOLDER, pointsetEvents));
    ValidationState report2 = getValidationReport();
    assertFalse("Device should no longer be pending after telemetry",
        report2.summary.pending_devices.contains(TestCommon.DEVICE_ID));
    assertTrue("Device should be marked correct after both state and telemetry",
        report2.summary.correct_devices.contains(TestCommon.DEVICE_ID));
  }

  @Test
  public void invalidTimestampFormat() throws Exception {
    PointsetEvents messageObject = basePointsetEvents();
    MessageBundle bundle = getMessageBundle(EVENTS_SUBTYPE, POINTSET_SUBFOLDER, messageObject);
    bundle.message.put("timestamp", "Jun 11, 2026, 8:04:00 AM");
    validator.validateMessage(bundle);
    ValidationState report = getValidationReport();
    assertEquals("One error summary", 1, report.summary.error_devices.size());
    ValidationEvents result = getValidationResult(TestCommon.DEVICE_ID, EVENTS_SUBTYPE,
        POINTSET_SUBFOLDER);
    boolean hasTimestampError = result.errors.stream()
        .anyMatch(entry -> (entry.message.toLowerCase().contains("timestamp")
            || entry.detail.toLowerCase().contains("timestamp")
            || entry.message.contains("could not be parsed"))
            && Category.VALIDATION_DEVICE_CONTENT.equals(entry.category));
    assertTrue("Should have timestamp error", hasTimestampError);
  }

  @Test
  public void excessivePresentValuePrecision() {
    PointsetEvents messageObject = basePointsetEvents();
    messageObject.points.get(FILTER_DIFFERENTIAL_PRESSURE_SETPOINT)
        .present_value = 296.04999961853025;
    MessageBundle bundle = getMessageBundle(EVENTS_SUBTYPE, POINTSET_SUBFOLDER, messageObject);
    validator.validateMessage(bundle);
    ValidationState report = getValidationReport();
    assertEquals("One error summary", 1, report.summary.error_devices.size());
    ValidationEvents result = getValidationResult(TestCommon.DEVICE_ID, EVENTS_SUBTYPE,
        POINTSET_SUBFOLDER);
    boolean hasPrecisionError = result.errors.stream()
        .anyMatch(entry -> entry.message.contains("excessive decimal precision")
            && Category.VALIDATION_DEVICE_CONTENT.equals(entry.category));
    assertTrue("Should have precision error", hasPrecisionError);
  }

  @Test
  public void baselineValueMismatch() {
    PointsetEvents messageObject = basePointsetEvents();
    messageObject.points.get(FILTER_DIFFERENTIAL_PRESSURE_SETPOINT).present_value = 20;
    MessageBundle bundle = getMessageBundle(EVENTS_SUBTYPE, POINTSET_SUBFOLDER, messageObject);
    validator.validateMessage(bundle);
    ValidationState report = getValidationReport();
    assertEquals("One error summary", 1, report.summary.error_devices.size());
    ValidationEvents result = getValidationResult(TestCommon.DEVICE_ID, EVENTS_SUBTYPE,
        POINTSET_SUBFOLDER);
    boolean hasBaselineError = result.errors.stream()
        .anyMatch(entry -> entry.message.contains("baseline_value")
            && Category.VALIDATION_DEVICE_CONTENT.equals(entry.category));
    assertTrue("Should have baseline_value error", hasBaselineError);
  }

  @Test
  public void baselineToleranceOutOfRange() {
    PointsetEvents messageObject = basePointsetEvents();
    messageObject.points.get(FILTER_DIFFERENTIAL_PRESSURE_SENSOR).present_value = 15;
    MessageBundle bundle = getMessageBundle(EVENTS_SUBTYPE, POINTSET_SUBFOLDER, messageObject);
    validator.validateMessage(bundle);
    ValidationState report = getValidationReport();
    assertEquals("One error summary", 1, report.summary.error_devices.size());
    ValidationEvents result = getValidationResult(TestCommon.DEVICE_ID, EVENTS_SUBTYPE,
        POINTSET_SUBFOLDER);
    boolean hasBaselineError = result.errors.stream()
        .anyMatch(entry -> entry.message.contains("baseline_value")
            && Category.VALIDATION_DEVICE_CONTENT.equals(entry.category));
    assertTrue("Should have baseline_tolerance error", hasBaselineError);
  }

  private record CapturedPubSub(String projectId, String subscriptionId, boolean reset) {
  }

  private static CapturedPubSub capturePubSub(boolean viaExecute, List<String> args) {
    AtomicReference<CapturedPubSub> captured = new AtomicReference<>();
    Validator.PubSubClientFactory factory =
        (projectId, registryId, subscriptionId, updateTopic, reflect, reset) -> {
          captured.set(new CapturedPubSub(projectId, subscriptionId, reset));
          return Mockito.mock(MessagePublisher.class);
        };
    if (viaExecute) {
      Validator v = new Validator();
      v.setPubSubClientFactory(factory);
      v.processArgs(args).execute();
    } else {
      new Validator(args, factory);
    }
    return captured.get();
  }

  @Test
  public void pubSubNoResetOption() {
    CapturedPubSub defaultCall = capturePubSub(true, ImmutableList.of(
        TestCommon.SITE_DIR, "-p", SiteModel.MOCK_PROJECT, "-t", "test-sub"));
    assertEquals(SiteModel.MOCK_PROJECT, defaultCall.projectId());
    assertEquals("test-sub", defaultCall.subscriptionId());
    assertTrue(defaultCall.reset());

    CapturedPubSub afterTarget = capturePubSub(true, ImmutableList.of(
        TestCommon.SITE_DIR, "-t", "test-sub", "-p", SiteModel.MOCK_PROJECT, "-R"));
    assertEquals(SiteModel.MOCK_PROJECT, afterTarget.projectId());
    assertEquals("test-sub", afterTarget.subscriptionId());
    assertFalse(afterTarget.reset());

    CapturedPubSub beforeTarget = capturePubSub(true, ImmutableList.of(
        TestCommon.SITE_DIR, "-R", "-p", SiteModel.MOCK_PROJECT, "-t", "test-sub"));
    assertEquals("test-sub", beforeTarget.subscriptionId());
    assertFalse(beforeTarget.reset());

    CapturedPubSub providerSpec = capturePubSub(true, ImmutableList.of(
        TestCommon.SITE_DIR, "//pubsub/" + SiteModel.MOCK_PROJECT + "+testuser", "-R"));
    assertEquals(SiteModel.MOCK_PROJECT, providerSpec.projectId());
    assertEquals("udmi_target+testuser", providerSpec.subscriptionId());
    assertFalse(providerSpec.reset());

    CapturedPubSub argListCtor = capturePubSub(false, ImmutableList.of(
        "-p", SiteModel.MOCK_PROJECT, "-s", TestCommon.SITE_DIR, "-t", "ctor-sub", "-R"));
    assertEquals("ctor-sub", argListCtor.subscriptionId());
    assertFalse(argListCtor.reset());
  }

  @Test(expected = RuntimeException.class)
  public void negativeStartupBufferDelayRejected() {
    new Validator(ImmutableList.of(
        "-n",
        "-p", SiteModel.MOCK_PROJECT,
        "-a", TestCommon.SCHEMA_SPEC,
        "-s", TestCommon.SITE_DIR,
        "-b", "-1"));
  }

}
