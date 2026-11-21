package com.google.daq.mqtt.validator;

import static com.google.daq.mqtt.validator.DiscoveryAnalyzer.DISCOVERY_ANALYSIS_FILENAME;
import static com.google.udmi.util.Common.PUBLISH_TIME_KEY;
import static com.google.udmi.util.Common.SUBFOLDER_PROPERTY_KEY;
import static com.google.udmi.util.Common.SUBTYPE_PROPERTY_KEY;
import static com.google.udmi.util.JsonUtil.OBJECT_MAPPER;
import static com.google.udmi.util.JsonUtil.getDate;
import static com.google.udmi.util.JsonUtil.isoConvert;
import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertNull;
import static org.junit.Assert.assertTrue;

import com.fasterxml.jackson.databind.JsonNode;
import com.google.common.collect.ImmutableList;
import com.google.common.collect.Sets;
import com.google.daq.mqtt.TestCommon;
import com.google.daq.mqtt.util.ConfigUtil;
import com.google.daq.mqtt.util.MessagePublisher;
import com.google.daq.mqtt.validator.DiscoveryAnalyzer.ScanKey;
import com.google.daq.mqtt.validator.Validator.MessageBundle;
import com.google.udmi.util.MessageValidator;
import com.google.udmi.util.SiteModel;
import com.networknt.schema.JsonSchema;
import com.networknt.schema.ValidationMessage;
import java.io.File;
import java.time.Instant;
import java.util.ArrayDeque;
import java.util.Date;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.Queue;
import java.util.Set;
import org.junit.After;
import org.junit.Rule;
import org.junit.Test;
import org.junit.rules.TemporaryFolder;
import udmi.schema.Category;
import udmi.schema.DeviceValidationEvents;
import udmi.schema.DiscoveryAnalysis;
import udmi.schema.DiscoveryEvents;
import udmi.schema.DiscoveryScanSummary;
import udmi.schema.DiscoveryScanSummary.ScanStatus;
import udmi.schema.DiscoveryState;
import udmi.schema.Entry;
import udmi.schema.Enumerations.Depth;
import udmi.schema.FamilyDiscovery;
import udmi.schema.FamilyDiscoveryState;
import udmi.schema.FamilyDiscoveryState.Phase;
import udmi.schema.Level;
import udmi.schema.PointPointsetModel;
import udmi.schema.RefDiscovery;
import udmi.schema.State;
import udmi.schema.StateSystemHardware;
import udmi.schema.SystemDiscoveryData;
import udmi.schema.ValidationState;

/**
 * Unit and trace-playback tests for passive discovery validation and analysis.
 */
public class DiscoveryValidatorTest extends TestBase {

  private static final String EVENTS_SUBTYPE = "events";
  private static final String STATE_SUBTYPE = "state";
  private static final String DISCOVERY_SUBFOLDER = "discovery";
  private static final String SCAN_GEN_ISO = "2026-11-18T21:00:00Z";
  private static final String BACNET_FAMILY = "bacnet";
  private static final List<String> TEST_ARGS = ImmutableList.of(
      "-n",
      "-p", SiteModel.MOCK_PROJECT,
      "-a", TestCommon.SCHEMA_SPEC,
      "-s", TestCommon.SITE_DIR);

  @Rule
  public final TemporaryFolder tempDir = new TemporaryFolder();

  private File createdAnalysisFile;

  /**
   * Remove generated discovery analysis files between test cases.
   */
  @After
  public void cleanupAnalysisFile() {
    File defaultAnalysis = new File(TestCommon.SITE_DIR, "out/" + DISCOVERY_ANALYSIS_FILENAME);
    if (defaultAnalysis.exists()) {
      defaultAnalysis.delete();
    }
    if (createdAnalysisFile != null && createdAnalysisFile.exists()) {
      createdAnalysisFile.delete();
    }
  }

  @Test
  public void validCompleteDetailsScan() throws Exception {
    Validator validator = new Validator(TEST_ARGS).prepForMock();
    Date generation = getDate(SCAN_GEN_ISO);

    sendState(validator, TestCommon.GATEWAY_ID, Phase.PENDING, generation, 0);
    sendState(validator, TestCommon.GATEWAY_ID, Phase.ACTIVE, generation, 0);
    sendMarker(validator, TestCommon.GATEWAY_ID, BACNET_FAMILY, generation, 0);
    sendDiscoveredAhu1(validator, generation, 1, true);
    sendDiscoveredAhu22(validator, generation, 2);
    sendDiscoveredGat123(validator, generation, 3);
    sendMarker(validator, TestCommon.GATEWAY_ID, BACNET_FAMILY, generation, -4);
    sendState(validator, TestCommon.GATEWAY_ID, Phase.STOPPED, generation, 3);

    File analysisFile = new File(TestCommon.SITE_DIR, "out/" + DISCOVERY_ANALYSIS_FILENAME);
    assertTrue("discovery_analysis.json should exist", analysisFile.exists());
    JsonNode rawReport = validateAnalysisSchemaAndNoEmptyArrays(analysisFile);
    assertEquals(
        Set.of("scanner_device_id", "family", "generation", "depth", "active_count"),
        Sets.newHashSet(rawReport.get("scans").get(0).fieldNames()));

    DiscoveryAnalysis report = OBJECT_MAPPER.readValue(analysisFile, DiscoveryAnalysis.class);
    assertEquals(ConfigUtil.UDMI_VERSION, report.version);
    assertEquals(1, report.scans.size());
    DiscoveryScanSummary summary = report.scans.get(0);
    assertNull(summary.status);
    assertEquals(Depth.DETAILS, summary.depth);
    assertEquals(Integer.valueOf(3), summary.active_count);
    assertNull(summary.missing_devices);
    assertNull(summary.extra_addrs);
    assertNull(summary.errors);
    assertNull(summary.warnings);

    ValidationState validationReport = getValidationReport();
    assertFalse(validationReport.summary.error_devices.contains(TestCommon.GATEWAY_ID));
  }

  @Test
  public void partialScanProducesWarningsNotErrors() throws Exception {
    Validator validator = new Validator(TEST_ARGS).prepForMock();
    Date generation = getDate(SCAN_GEN_ISO);

    sendState(validator, TestCommon.GATEWAY_ID, Phase.ACTIVE, generation, 0);
    sendMarker(validator, TestCommon.GATEWAY_ID, BACNET_FAMILY, generation, 0);
    sendDiscoveredAhu1(validator, generation, 1, false);
    DiscoveryEvents unknown = baseDiscoveryEvent(BACNET_FAMILY, generation, 2);
    unknown.addr = "99999";
    unknown.network = "9288";
    validator.validateMessage(makeBundle(TestCommon.GATEWAY_ID, EVENTS_SUBTYPE,
        DISCOVERY_SUBFOLDER, unknown));
    sendMarker(validator, TestCommon.GATEWAY_ID, BACNET_FAMILY, generation, -3);
    sendState(validator, TestCommon.GATEWAY_ID, Phase.STOPPED, generation, 2);

    File analysisFile = new File(TestCommon.SITE_DIR, "out/" + DISCOVERY_ANALYSIS_FILENAME);
    validateAnalysisSchemaAndNoEmptyArrays(analysisFile);

    DiscoveryAnalysis report = validator.getDiscoveryAnalyzer().getAnalysisReport();
    assertEquals(1, report.scans.size());
    DiscoveryScanSummary summary = report.scans.get(0);
    assertEquals(ScanStatus.PASSED_WITH_WARNINGS, summary.status);
    assertEquals(Integer.valueOf(2), summary.active_count);
    assertEquals(List.of("AHU-22", "GAT-123"), summary.missing_devices);
    assertEquals(List.of("99999"), summary.extra_addrs);
    assertNull(summary.errors);
    assertEquals(2, summary.warnings.size());

    ValidationState validationReport = getValidationReport();
    assertFalse(validationReport.summary.error_devices.contains(TestCommon.GATEWAY_ID));
  }

  @Test
  public void outOfOrderStoppedStateCorrelatesCleanly() {
    Validator validator = new Validator(TEST_ARGS).prepForMock();
    Date generation = getDate(SCAN_GEN_ISO);

    // Monolithic state.discovery arrives before events_discovery
    State state = new State();
    state.version = TestCommon.UDMI_VERSION;
    state.discovery = buildDiscoveryState(BACNET_FAMILY, Phase.STOPPED, generation, 1);
    validator.validateMessage(makeBundle(TestCommon.GATEWAY_ID, STATE_SUBTYPE, "update", state));

    sendMarker(validator, TestCommon.GATEWAY_ID, BACNET_FAMILY, generation, 0);
    sendDiscoveredAhu1(validator, generation, 1, false);
    sendMarker(validator, TestCommon.GATEWAY_ID, BACNET_FAMILY, generation, -2);

    DiscoveryScanSummary summary = validator.getDiscoveryAnalyzer().getAnalysisReport()
        .scans.get(0);
    assertEquals(ScanStatus.PASSED_WITH_WARNINGS, summary.status);
    assertEquals(Integer.valueOf(1), summary.active_count);
    assertNull(summary.errors);
  }

  @Test
  public void staleStoppedStateIsIgnored() {
    Validator validator = new Validator(TEST_ARGS).prepForMock();
    Date generation = getDate("2026-11-18T10:00:00Z");

    sendState(validator, TestCommon.GATEWAY_ID, Phase.STOPPED, generation, 5);

    File analysisFile = new File(TestCommon.SITE_DIR, "out/" + DISCOVERY_ANALYSIS_FILENAME);
    assertFalse("Stale STOPPED state must not create discovery_analysis.json",
        analysisFile.exists());
    assertNull(validator.getDiscoveryAnalyzer().getAnalysisReport().scans);
    ValidationState validationReport = getValidationReport();
    assertFalse(validationReport.summary.error_devices.contains(TestCommon.GATEWAY_ID));
  }

  @Test
  public void matchedDeviceMismatchesAndExactRefsEnforced() throws Exception {
    Validator validator = new Validator(TEST_ARGS).prepForMock();
    Date generation = getDate(SCAN_GEN_ISO);

    sendState(validator, TestCommon.GATEWAY_ID, Phase.ACTIVE, generation, 0);
    sendMarker(validator, TestCommon.GATEWAY_ID, BACNET_FAMILY, generation, 0);

    DiscoveryEvents ahu1 = baseDiscoveryEvent(BACNET_FAMILY, generation, 1);
    ahu1.addr = "27312";
    ahu1.network = "9999"; // mismatch vs 9288
    ahu1.system = new SystemDiscoveryData();
    ahu1.system.name = "WRONG-NAME";
    ahu1.system.hardware = new StateSystemHardware();
    ahu1.system.hardware.make = "WRONG-MAKE";
    ahu1.system.hardware.model = "pubber";
    ahu1.refs = new HashMap<>();
    ahu1.refs.put("AV/4#present_value", new RefDiscovery());
    ahu1.refs.put("AV/99#present_value", new RefDiscovery()); // extra unmapped ref
    validator.validateMessage(makeBundle(TestCommon.GATEWAY_ID, EVENTS_SUBTYPE,
        DISCOVERY_SUBFOLDER, ahu1));

    sendMarker(validator, TestCommon.GATEWAY_ID, BACNET_FAMILY, generation, -2);
    sendState(validator, TestCommon.GATEWAY_ID, Phase.STOPPED, generation, 1);

    File analysisFile = new File(TestCommon.SITE_DIR, "out/" + DISCOVERY_ANALYSIS_FILENAME);
    validateAnalysisSchemaAndNoEmptyArrays(analysisFile);

    DiscoveryScanSummary summary = validator.getDiscoveryAnalyzer().getAnalysisReport()
        .scans.get(0);
    assertEquals(ScanStatus.FAILED, summary.status);
    assertTrue(summary.errors.stream().anyMatch(e -> e.contains("network mismatch")));
    assertTrue(summary.errors.stream().anyMatch(e -> e.contains("system.name mismatch")));
    assertTrue(summary.errors.stream().anyMatch(e -> e.contains("system.hardware.make mismatch")));
    assertTrue(summary.errors.stream().anyMatch(e -> e.contains("refs mismatch")));

    ValidationState validationReport = getValidationReport();
    assertTrue(validationReport.summary.error_devices.contains(TestCommon.GATEWAY_ID));
  }

  @Test
  public void sequenceGapsDuplicatesAndActiveCountMismatchDetected() throws Exception {
    Validator validator = new Validator(TEST_ARGS).prepForMock();
    Date generation = getDate(SCAN_GEN_ISO);

    sendState(validator, TestCommon.GATEWAY_ID, Phase.ACTIVE, generation, 0);
    // Missing start marker (event_no=0)
    sendDiscoveredAhu1(validator, generation, 1, false);
    // Skip event_no=2, send event_no=3 with duplicate addr
    DiscoveryEvents dup = baseDiscoveryEvent(BACNET_FAMILY, generation, 3);
    dup.addr = "27312";
    dup.network = "9288";
    validator.validateMessage(makeBundle(TestCommon.GATEWAY_ID, EVENTS_SUBTYPE,
        DISCOVERY_SUBFOLDER, dup));
    // Wrong end marker and wrong active_count
    sendMarker(validator, TestCommon.GATEWAY_ID, BACNET_FAMILY, generation, -9);
    sendState(validator, TestCommon.GATEWAY_ID, Phase.STOPPED, generation, 5);

    File analysisFile = new File(TestCommon.SITE_DIR, "out/" + DISCOVERY_ANALYSIS_FILENAME);
    validateAnalysisSchemaAndNoEmptyArrays(analysisFile);

    DiscoveryScanSummary summary = validator.getDiscoveryAnalyzer().getAnalysisReport()
        .scans.get(0);
    assertEquals(ScanStatus.FAILED, summary.status);
    assertEquals(List.of(2), summary.missing_event_nos);
    assertEquals(List.of("27312"), summary.duplicate_addrs);
    assertTrue(summary.errors.stream().anyMatch(e -> e.contains("Missing discovery start marker")));
    assertTrue(summary.errors.stream().anyMatch(e -> e.contains("Missing discovery event_no")));
    assertTrue(summary.errors.stream().anyMatch(e -> e.contains("Duplicate discovered addr")));
    assertTrue(summary.errors.stream().anyMatch(e -> e.contains("end marker mismatch")));
    assertTrue(summary.errors.stream().anyMatch(e -> e.contains("active_count mismatch")));
  }

  @Test
  public void tracePlaybackFinalizesCompleteAndIncompleteScans() throws Exception {
    File traceRoot = tempDir.newFolder("incomplete_trace");
    File gatDir = new File(traceRoot, "devices/" + TestCommon.GATEWAY_ID);
    assertTrue(gatDir.mkdirs());

    Date generation = getDate(SCAN_GEN_ISO);
    writeTraceMessage(gatDir, "001_state_discovery.json",
        buildDiscoveryState(BACNET_FAMILY, Phase.ACTIVE, generation, 0));
    DiscoveryEvents start = baseDiscoveryEvent(BACNET_FAMILY, generation, 0);
    writeTraceMessage(gatDir, "002_events_discovery.json", start);
    DiscoveryEvents item = baseDiscoveryEvent(BACNET_FAMILY, generation, 1);
    item.network = "9288";
    item.families = Map.of("ipv4", ipv4Discovery(null, 47808));
    writeTraceMessage(gatDir, "003_events_discovery.json", item);

    List<String> traceArgs = ImmutableList.of(
        "-p", SiteModel.MOCK_PROJECT,
        "-a", TestCommon.SCHEMA_SPEC,
        "-s", TestCommon.SITE_DIR,
        "-r", traceRoot.getAbsolutePath());
    Validator validator = new Validator(traceArgs);
    validator.messageLoop();

    DiscoveryAnalysis report = validator.getDiscoveryAnalyzer().getAnalysisReport();
    assertEquals(1, report.scans.size());
    DiscoveryScanSummary summary = report.scans.get(0);
    assertEquals(ScanStatus.INCOMPLETE, summary.status);
    assertEquals(Depth.BUCKETS, summary.depth);
    assertTrue(summary.warnings.stream().anyMatch(
        w -> w.contains("Site model networks not discovered: 9289")));
    assertTrue(summary.errors.stream().anyMatch(e -> e.contains("Missing discovery end marker")));
    assertTrue(summary.errors.stream().anyMatch(e -> e.contains("stopped phase")));
  }

  @Test
  public void graceExpirationClosesStalledScanAsFailed() {
    Validator validator = new Validator(TEST_ARGS).prepForMock();
    Date generation = getDate(SCAN_GEN_ISO);

    sendState(validator, TestCommon.GATEWAY_ID, Phase.ACTIVE, generation, 0);
    sendMarker(validator, TestCommon.GATEWAY_ID, BACNET_FAMILY, generation, 0);
    sendDiscoveredAhu1(validator, generation, 1, false);
    sendState(validator, TestCommon.GATEWAY_ID, Phase.STOPPED, generation, 1);

    ScanKey key = new ScanKey(TestCommon.GATEWAY_ID, BACNET_FAMILY, SCAN_GEN_ISO);
    assertTrue(validator.getDiscoveryAnalyzer().closeIfExpired(key));

    DiscoveryScanSummary summary = validator.getDiscoveryAnalyzer().getAnalysisReport()
        .scans.get(0);
    assertEquals(ScanStatus.FAILED, summary.status);
    assertTrue(summary.errors.stream().anyMatch(e -> e.contains("Missing discovery end marker")));
  }

  @Test
  public void selfEnumerationAndNoSiteUnregisteredScannerWork() throws Exception {
    File noSiteOut = new File("out/" + DISCOVERY_ANALYSIS_FILENAME);
    createdAnalysisFile = noSiteOut;
    if (noSiteOut.exists()) {
      noSiteOut.delete();
    }

    Date generation = getDate(SCAN_GEN_ISO);

    // Self-enumeration should not create an active scan
    DiscoveryEvents selfEnum = new DiscoveryEvents();
    selfEnum.version = TestCommon.UDMI_VERSION;
    selfEnum.generation = generation;
    selfEnum.family = "iot";
    selfEnum.points = Map.of("temp_point", new PointPointsetModel());

    List<String> noSiteArgs = ImmutableList.of(
        "-n",
        "-p", SiteModel.MOCK_PROJECT,
        "-a", TestCommon.SCHEMA_SPEC,
        "-s", "--");
    Validator validator = new Validator(noSiteArgs).prepForMock();
    validator.validateMessage(makeBundle("SPT-99", EVENTS_SUBTYPE, DISCOVERY_SUBFOLDER, selfEnum));
    assertNull(validator.getDiscoveryAnalyzer().getAnalysisReport().scans);

    // Active scan from unregistered scanner works without siteModel
    sendState(validator, "SPT-99", Phase.ACTIVE, generation, 0);
    sendMarker(validator, "SPT-99", BACNET_FAMILY, generation, 0);
    DiscoveryEvents item = baseDiscoveryEvent(BACNET_FAMILY, generation, 1);
    item.addr = "12345";
    validator.validateMessage(makeBundle("SPT-99", EVENTS_SUBTYPE, DISCOVERY_SUBFOLDER, item));
    sendMarker(validator, "SPT-99", BACNET_FAMILY, generation, -2);
    sendState(validator, "SPT-99", Phase.STOPPED, generation, 1);

    DiscoveryScanSummary summary = validator.getDiscoveryAnalyzer().getAnalysisReport()
        .scans.get(0);
    assertNull(summary.status);
    assertEquals("SPT-99", summary.scanner_device_id);
    assertEquals(Depth.ENTRIES, summary.depth);
    assertEquals(Integer.valueOf(1), summary.active_count);
    assertTrue(noSiteOut.exists());
    JsonNode rawReport = validateAnalysisSchemaAndNoEmptyArrays(noSiteOut);
    assertFalse(rawReport.get("scans").get(0).has("discovered_addrs"));
    assertFalse(rawReport.get("scans").get(0).has("discovered_networks"));
  }

  @Test
  public void unregisteredScannerValidatesAgainstLoadedSiteModel() {
    Validator validator = new Validator(TEST_ARGS).prepForMock();
    Date generation = getDate(SCAN_GEN_ISO);

    sendState(validator, "SPT-99", Phase.ACTIVE, generation, 0);
    sendMarker(validator, "SPT-99", BACNET_FAMILY, generation, 0);
    sendDiscoveredFrom("SPT-99", validator, generation, 1);
    sendMarker(validator, "SPT-99", BACNET_FAMILY, generation, -2);
    sendState(validator, "SPT-99", Phase.STOPPED, generation, 1);

    DiscoveryScanSummary summary = validator.getDiscoveryAnalyzer().getAnalysisReport()
        .scans.get(0);
    assertEquals(ScanStatus.PASSED_WITH_WARNINGS, summary.status);
    assertEquals("SPT-99", summary.scanner_device_id);
    assertEquals(List.of("AHU-22", "GAT-123"), summary.missing_devices);
  }

  @Test
  public void matchedDeviceOmitsRefsInDetailsScan() {
    Validator validator = new Validator(TEST_ARGS).prepForMock();
    Date generation = getDate(SCAN_GEN_ISO);

    sendState(validator, TestCommon.GATEWAY_ID, Phase.ACTIVE, generation, 0);
    sendMarker(validator, TestCommon.GATEWAY_ID, BACNET_FAMILY, generation, 0);
    sendDiscoveredAhu1(validator, generation, 1, false);
    sendDiscoveredAhu22(validator, generation, 2);
    sendMarker(validator, TestCommon.GATEWAY_ID, BACNET_FAMILY, generation, -3);
    sendState(validator, TestCommon.GATEWAY_ID, Phase.STOPPED, generation, 2);

    DiscoveryScanSummary summary = validator.getDiscoveryAnalyzer().getAnalysisReport()
        .scans.get(0);
    assertEquals(ScanStatus.FAILED, summary.status);
    assertEquals(Depth.DETAILS, summary.depth);
    assertTrue(summary.errors.stream().anyMatch(
        e -> e.contains("Device AHU-1 omitted refs in details scan")));
  }

  @Test
  public void completeTracePlaybackProducesPassedAnalysis() throws Exception {
    File traceRoot = tempDir.newFolder("complete_trace");
    File gatDir = new File(traceRoot, "devices/" + TestCommon.GATEWAY_ID);
    assertTrue(gatDir.mkdirs());

    Date generation = getDate(SCAN_GEN_ISO);
    writeTraceMessage(gatDir, "001_state_discovery.json",
        buildDiscoveryState(BACNET_FAMILY, Phase.PENDING, generation, 0));
    writeTraceMessage(gatDir, "002_events_discovery.json",
        baseDiscoveryEvent(BACNET_FAMILY, generation, 0));
    writeTraceMessage(gatDir, "003_events_discovery.json",
        buildAhu1Event(generation, 1, true));
    writeTraceMessage(gatDir, "004_events_discovery.json",
        buildAhu22Event(generation, 2));
    writeTraceMessage(gatDir, "005_events_discovery.json",
        buildGat123Event(generation, 3));
    writeTraceMessage(gatDir, "006_events_discovery.json",
        baseDiscoveryEvent(BACNET_FAMILY, generation, -4));
    writeTraceMessage(gatDir, "007_state_discovery.json",
        buildDiscoveryState(BACNET_FAMILY, Phase.STOPPED, generation, 3));

    List<String> traceArgs = ImmutableList.of(
        "-p", SiteModel.MOCK_PROJECT,
        "-a", TestCommon.SCHEMA_SPEC,
        "-s", TestCommon.SITE_DIR,
        "-r", traceRoot.getAbsolutePath());
    Validator validator = new Validator(traceArgs);
    validator.messageLoop();

    File analysisFile = new File(TestCommon.SITE_DIR, "out/" + DISCOVERY_ANALYSIS_FILENAME);
    assertTrue(analysisFile.exists());
    validateAnalysisSchemaAndNoEmptyArrays(analysisFile);
    DiscoveryAnalysis report = OBJECT_MAPPER.readValue(analysisFile, DiscoveryAnalysis.class);
    assertEquals(1, report.scans.size());
    DiscoveryScanSummary summary = report.scans.get(0);
    assertNull(summary.status);
    assertEquals(Depth.DETAILS, summary.depth);
    assertEquals(Integer.valueOf(3), summary.active_count);
    assertNull(summary.errors);
    assertNull(summary.warnings);
  }

  @Test
  public void invalidRefsUnknownFamiliesAndEventStatusCaptured() {
    Validator validator = new Validator(TEST_ARGS).prepForMock();
    Date generation = getDate(SCAN_GEN_ISO);

    sendState(validator, TestCommon.GATEWAY_ID, Phase.ACTIVE, generation, 0);
    sendMarker(validator, TestCommon.GATEWAY_ID, BACNET_FAMILY, generation, 0);

    DiscoveryEvents item = baseDiscoveryEvent(BACNET_FAMILY, generation, 1);
    item.addr = "27312";
    item.network = "9288";
    item.families = Map.of("bogus_family", addrDiscovery("1.2.3.4"));
    item.refs = new HashMap<>();
    item.refs.put("INVALID:1", new RefDiscovery());
    item.status = new Entry();
    item.status.level = Level.ERROR.value();
    item.status.message = "point read timed out";
    validator.validateMessage(makeBundle(TestCommon.GATEWAY_ID, EVENTS_SUBTYPE,
        DISCOVERY_SUBFOLDER, item));

    sendMarker(validator, TestCommon.GATEWAY_ID, BACNET_FAMILY, generation, -2);
    sendState(validator, TestCommon.GATEWAY_ID, Phase.STOPPED, generation, 1);

    DiscoveryScanSummary summary = validator.getDiscoveryAnalyzer().getAnalysisReport()
        .scans.get(0);
    assertEquals(ScanStatus.FAILED, summary.status);
    assertTrue(summary.errors.stream().anyMatch(e -> e.contains("point read timed out")));
    assertTrue(summary.errors.stream().anyMatch(e -> e.contains("Unknown family bogus_family")));
    assertTrue(summary.errors.stream().anyMatch(e -> e.contains("Invalid ref INVALID:1")));
  }

  @Test
  public void multiScanErrorsProduceTractableDeviceStatusSummary() throws Exception {
    Validator validator = new Validator(TEST_ARGS).prepForMock();
    Date gen1 = getDate("2026-11-18T21:00:00Z");

    // Scan 1 (ether): unknown depth (events missing addr and network x3)
    DiscoveryState etherActive = buildDiscoveryState("ether", Phase.ACTIVE, gen1, 0);
    validator.validateMessage(makeBundle(TestCommon.GATEWAY_ID, STATE_SUBTYPE,
        DISCOVERY_SUBFOLDER, etherActive));
    sendMarker(validator, TestCommon.GATEWAY_ID, "ether", gen1, 0);
    for (int i = 1; i <= 3; i++) {
      validator.validateMessage(makeBundle(TestCommon.GATEWAY_ID, EVENTS_SUBTYPE,
          DISCOVERY_SUBFOLDER, baseDiscoveryEvent("ether", gen1, i)));
    }
    sendMarker(validator, TestCommon.GATEWAY_ID, "ether", gen1, -4);
    DiscoveryState etherStopped = buildDiscoveryState("ether", Phase.STOPPED, gen1, 3);
    validator.validateMessage(makeBundle(TestCommon.GATEWAY_ID, STATE_SUBTYPE,
        DISCOVERY_SUBFOLDER, etherStopped));

    // Scan 2 (bacnet): missing start marker + mismatched network
    Date gen2 = getDate("2026-11-18T22:00:00Z");
    sendState(validator, TestCommon.GATEWAY_ID, Phase.ACTIVE, gen2, 0);
    DiscoveryEvents badAhu = baseDiscoveryEvent(BACNET_FAMILY, gen2, 1);
    badAhu.addr = "27312";
    badAhu.network = "9999";
    validator.validateMessage(makeBundle(TestCommon.GATEWAY_ID, EVENTS_SUBTYPE,
        DISCOVERY_SUBFOLDER, badAhu));
    sendMarker(validator, TestCommon.GATEWAY_ID, BACNET_FAMILY, gen2, -2);
    sendState(validator, TestCommon.GATEWAY_ID, Phase.STOPPED, gen2, 1);

    File analysisFile = new File(TestCommon.SITE_DIR, "out/" + DISCOVERY_ANALYSIS_FILENAME);
    final JsonNode rawReport = validateAnalysisSchemaAndNoEmptyArrays(analysisFile);
    // Scan 1 (ether, index 1 after alphabetical family sort) has unknown depth -> omitted
    DiscoveryAnalysis report = OBJECT_MAPPER.readValue(analysisFile, DiscoveryAnalysis.class);
    assertEquals(2, report.scans.size());
    DiscoveryScanSummary etherScan = report.scans.stream()
        .filter(s -> "ether".equals(s.family)).findFirst().orElseThrow();
    assertNull(etherScan.depth);
    assertEquals(3, etherScan.errors.size());
    assertFalse(rawReport.get("scans").get(1).has("depth"));

    ValidationState validationReport = getValidationReport();
    DeviceValidationEvents gatValidation = validationReport.devices.get(TestCommon.GATEWAY_ID);
    assertEquals(Category.VALIDATION_DEVICE_MULTIPLE, gatValidation.status.category);
    assertEquals("Multiple validation errors", gatValidation.status.message);
    assertEquals(
        "Discovery errors across 2/2 scans (bacnet: 1, ether: 1); "
            + "events missing addr and network (x3); "
            + "missing start marker (x1); "
            + "device network mismatch (x1)",
        gatValidation.status.detail);
  }

  @Test
  public void startupBufferReordersOutOfOrderBacklogThenStreamsLive() throws Exception {
    final Date gen1 = getDate("2026-11-18T21:00:00Z");
    final Instant baseTs = Instant.parse("2026-11-18T21:00:00Z");

    // Negative check: without -b, out-of-order delivery fails with late-event errors
    Queue<MessageBundle> unbufferedLive = buildOutOfOrderBacklog(gen1, baseTs);
    MessagePublisher unbufferedMock = new TwoPhaseMockPublisher(new ArrayDeque<>(), unbufferedLive);
    Validator unbufferedValidator = new Validator();
    unbufferedValidator.setPubSubClientFactory(
        (projectId, registryId, subscriptionId, updateTopic, reflect, reset) -> unbufferedMock);
    unbufferedValidator.processArgs(ImmutableList.of(
        TestCommon.SITE_DIR, "-p", SiteModel.MOCK_PROJECT, "-a", TestCommon.SCHEMA_SPEC,
        "-t", "mock-sub", "-R", TestCommon.GATEWAY_ID)).execute();
    DiscoveryScanSummary unbufferedScan = unbufferedValidator.getDiscoveryAnalyzer()
        .getAnalysisReport().scans.get(0);
    assertEquals(ScanStatus.FAILED, unbufferedScan.status);
    assertTrue(unbufferedScan.errors.stream().anyMatch(
        err -> err.contains("arrived after scan closed")));

    // Positive check: with -b 1, startup backlog is sorted before replay and live scan follows
    Queue<MessageBundle> backlog = buildOutOfOrderBacklog(gen1, baseTs);

    // Post-buffer live scan (gen2) delivered once startup buffer window ends
    Date gen2 = getDate("2026-11-18T22:00:00Z");
    Queue<MessageBundle> liveQueue = new ArrayDeque<>();
    liveQueue.add(timedBundle(TestCommon.GATEWAY_ID, STATE_SUBTYPE, DISCOVERY_SUBFOLDER,
        buildDiscoveryState(BACNET_FAMILY, Phase.ACTIVE, gen2, 0),
        baseTs.plusSeconds(3600), baseTs.plusSeconds(3600)));
    liveQueue.add(timedBundle(TestCommon.GATEWAY_ID, EVENTS_SUBTYPE, DISCOVERY_SUBFOLDER,
        baseDiscoveryEvent(BACNET_FAMILY, gen2, 0),
        baseTs.plusSeconds(3601), baseTs.plusSeconds(3601)));
    liveQueue.add(timedBundle(TestCommon.GATEWAY_ID, EVENTS_SUBTYPE, DISCOVERY_SUBFOLDER,
        buildAhu1Event(gen2, 1, true),
        baseTs.plusSeconds(3602), baseTs.plusSeconds(3602)));
    liveQueue.add(timedBundle(TestCommon.GATEWAY_ID, EVENTS_SUBTYPE, DISCOVERY_SUBFOLDER,
        buildAhu22Event(gen2, 2),
        baseTs.plusSeconds(3603), baseTs.plusSeconds(3603)));
    liveQueue.add(timedBundle(TestCommon.GATEWAY_ID, EVENTS_SUBTYPE, DISCOVERY_SUBFOLDER,
        buildGat123Event(gen2, 3),
        baseTs.plusSeconds(3604), baseTs.plusSeconds(3604)));
    liveQueue.add(timedBundle(TestCommon.GATEWAY_ID, EVENTS_SUBTYPE, DISCOVERY_SUBFOLDER,
        baseDiscoveryEvent(BACNET_FAMILY, gen2, -4),
        baseTs.plusSeconds(3605), baseTs.plusSeconds(3605)));
    liveQueue.add(timedBundle(TestCommon.GATEWAY_ID, STATE_SUBTYPE, DISCOVERY_SUBFOLDER,
        buildDiscoveryState(BACNET_FAMILY, Phase.STOPPED, gen2, 3),
        baseTs.plusSeconds(3606), baseTs.plusSeconds(3606)));

    MessagePublisher mockPubSub = new TwoPhaseMockPublisher(backlog, liveQueue);
    Validator.PubSubClientFactory factory =
        (projectId, registryId, subscriptionId, updateTopic, reflect, reset) -> mockPubSub;

    List<String> args = ImmutableList.of(
        TestCommon.SITE_DIR,
        "-p", SiteModel.MOCK_PROJECT,
        "-a", TestCommon.SCHEMA_SPEC,
        "-t", "mock-sub",
        "-R",
        "-b", "1",
        TestCommon.GATEWAY_ID);

    Validator bufferedValidator = new Validator();
    bufferedValidator.setPubSubClientFactory(factory);
    bufferedValidator.processArgs(args).execute();

    DiscoveryAnalysis report = bufferedValidator.getDiscoveryAnalyzer().getAnalysisReport();
    assertEquals(2, report.scans.size());
    for (DiscoveryScanSummary scan : report.scans) {
      assertNull("Both buffered and live scans should pass cleanly", scan.status);
      assertNull(scan.errors);
      assertEquals(Integer.valueOf(3), scan.active_count);
    }
  }

  private Queue<MessageBundle> buildOutOfOrderBacklog(Date generation, Instant baseTs) {
    Queue<MessageBundle> backlog = new ArrayDeque<>();
    backlog.add(timedBundle(TestCommon.GATEWAY_ID, STATE_SUBTYPE, DISCOVERY_SUBFOLDER,
        buildDiscoveryState(BACNET_FAMILY, Phase.STOPPED, generation, 3),
        baseTs.plusSeconds(2), baseTs.plusMillis(2500)));
    backlog.add(timedBundle(TestCommon.GATEWAY_ID, EVENTS_SUBTYPE, DISCOVERY_SUBFOLDER,
        baseDiscoveryEvent(BACNET_FAMILY, generation, -4),
        baseTs.plusSeconds(2), baseTs.plusMillis(2200)));
    backlog.add(timedBundle(TestCommon.GATEWAY_ID, EVENTS_SUBTYPE, DISCOVERY_SUBFOLDER,
        buildGat123Event(generation, 3),
        baseTs.plusSeconds(1), baseTs.plusMillis(1300)));
    backlog.add(timedBundle(TestCommon.GATEWAY_ID, EVENTS_SUBTYPE, DISCOVERY_SUBFOLDER,
        buildAhu22Event(generation, 2),
        baseTs.plusSeconds(1), baseTs.plusMillis(1200)));
    backlog.add(timedBundle(TestCommon.GATEWAY_ID, EVENTS_SUBTYPE, DISCOVERY_SUBFOLDER,
        buildAhu1Event(generation, 1, true),
        baseTs.plusSeconds(1), baseTs.plusMillis(1100)));
    backlog.add(timedBundle(TestCommon.GATEWAY_ID, EVENTS_SUBTYPE, DISCOVERY_SUBFOLDER,
        baseDiscoveryEvent(BACNET_FAMILY, generation, 0),
        baseTs, baseTs.plusMillis(100)));
    return backlog;
  }

  private MessageBundle timedBundle(String deviceId, String subType, String subFolder,
      Object messageObj, Instant payloadInstant, Instant publishInstant) {
    MessageBundle bundle = getMessageBundle(subType, subFolder, messageObj);
    bundle.attributes.put("deviceId", deviceId);
    bundle.attributes.put(SUBTYPE_PROPERTY_KEY, subType);
    bundle.attributes.put(SUBFOLDER_PROPERTY_KEY, subFolder);
    String secIso = isoConvert(Date.from(payloadInstant));
    bundle.message.put("timestamp", secIso);
    bundle.attributes.put(PUBLISH_TIME_KEY, secIso);
    bundle.timestamp = publishInstant.toString();
    return bundle;
  }

  private void sendState(Validator validator, String deviceId, Phase phase, Date generation,
      int activeCount) {
    DiscoveryState state = buildDiscoveryState(BACNET_FAMILY, phase, generation, activeCount);
    validator.validateMessage(makeBundle(deviceId, STATE_SUBTYPE, DISCOVERY_SUBFOLDER, state));
  }

  private DiscoveryState buildDiscoveryState(String family, Phase phase, Date generation,
      int activeCount) {
    FamilyDiscoveryState famState = new FamilyDiscoveryState();
    famState.phase = phase;
    famState.generation = generation;
    famState.active_count = activeCount;
    DiscoveryState state = new DiscoveryState();
    state.version = TestCommon.UDMI_VERSION;
    state.families = new HashMap<>();
    state.families.put(family, famState);
    return state;
  }

  private void sendMarker(Validator validator, String deviceId, String family, Date generation,
      int eventNo) {
    DiscoveryEvents marker = baseDiscoveryEvent(family, generation, eventNo);
    validator.validateMessage(makeBundle(deviceId, EVENTS_SUBTYPE, DISCOVERY_SUBFOLDER, marker));
  }

  private void sendDiscoveredAhu1(Validator validator, Date generation, int eventNo,
      boolean withDetails) {
    sendDiscoveredAhu1From(TestCommon.GATEWAY_ID, validator, generation, eventNo, withDetails);
  }

  private void sendDiscoveredFrom(String scannerId, Validator validator, Date generation,
      int eventNo) {
    sendDiscoveredAhu1From(scannerId, validator, generation, eventNo, false);
  }

  private void sendDiscoveredAhu1From(String scannerId, Validator validator, Date generation,
      int eventNo, boolean withDetails) {
    DiscoveryEvents event = buildAhu1Event(generation, eventNo, withDetails);
    validator.validateMessage(makeBundle(scannerId, EVENTS_SUBTYPE,
        DISCOVERY_SUBFOLDER, event));
  }

  private DiscoveryEvents buildAhu1Event(Date generation, int eventNo, boolean withDetails) {
    DiscoveryEvents event = baseDiscoveryEvent(BACNET_FAMILY, generation, eventNo);
    event.addr = "27312";
    event.network = "9288";
    event.families = new HashMap<>();
    event.families.put("ipv4", ipv4Discovery("192.168.2.1", 47808));
    event.families.put("ether", addrDiscovery("00:50:b6:ed:5f:77"));
    if (withDetails) {
      event.system = new SystemDiscoveryData();
      event.system.name = "AHU-1";
      event.system.hardware = new StateSystemHardware();
      event.system.hardware.make = "BOS";
      event.system.hardware.model = "pubber";
      event.refs = new HashMap<>();
      for (String ref : List.of(
          "CSV/1#present_value", "AV/4#present_value", "BV/11#present_value",
          "AV/12#present_value", "MV/1#present_value", "BV/2#present_value")) {
        event.refs.put(ref, new RefDiscovery());
      }
    }
    return event;
  }

  private void sendDiscoveredAhu22(Validator validator, Date generation, int eventNo) {
    validator.validateMessage(makeBundle(TestCommon.GATEWAY_ID, EVENTS_SUBTYPE,
        DISCOVERY_SUBFOLDER, buildAhu22Event(generation, eventNo)));
  }

  private DiscoveryEvents buildAhu22Event(Date generation, int eventNo) {
    DiscoveryEvents event = baseDiscoveryEvent(BACNET_FAMILY, generation, eventNo);
    event.addr = "58231";
    event.network = "9289";
    event.system = new SystemDiscoveryData();
    event.system.name = "AHU-22";
    event.system.serial_no = "pubber22";
    event.refs = new HashMap<>();
    event.refs.put("BV/11#present_value", new RefDiscovery());
    event.refs.put("AV/12#present_value", new RefDiscovery());
    return event;
  }

  private void sendDiscoveredGat123(Validator validator, Date generation, int eventNo) {
    validator.validateMessage(makeBundle(TestCommon.GATEWAY_ID, EVENTS_SUBTYPE,
        DISCOVERY_SUBFOLDER, buildGat123Event(generation, eventNo)));
  }

  private DiscoveryEvents buildGat123Event(Date generation, int eventNo) {
    DiscoveryEvents event = baseDiscoveryEvent(BACNET_FAMILY, generation, eventNo);
    event.addr = "217321";
    event.network = "9288";
    event.system = new SystemDiscoveryData();
    event.system.name = "GAT-123";
    return event;
  }

  private DiscoveryEvents baseDiscoveryEvent(String family, Date generation, int eventNo) {
    DiscoveryEvents event = new DiscoveryEvents();
    event.version = TestCommon.UDMI_VERSION;
    event.family = family;
    event.generation = generation;
    event.event_no = eventNo;
    return event;
  }

  private FamilyDiscovery addrDiscovery(String addr) {
    FamilyDiscovery fd = new FamilyDiscovery();
    fd.addr = addr;
    return fd;
  }

  private FamilyDiscovery ipv4Discovery(String addr, Integer port) {
    FamilyDiscovery fd = new FamilyDiscovery();
    fd.addr = addr;
    fd.port = port;
    return fd;
  }

  private MessageBundle makeBundle(String deviceId, String subType, String subFolder,
      Object messageObject) {
    advanceClockSec(1);
    MessageBundle bundle = getMessageBundle(subType, subFolder, messageObject);
    bundle.attributes.put("deviceId", deviceId);
    bundle.attributes.put(SUBTYPE_PROPERTY_KEY, subType);
    bundle.attributes.put(SUBFOLDER_PROPERTY_KEY, subFolder);
    return bundle;
  }

  private void writeTraceMessage(File deviceDir, String fileName, Object messageObj)
      throws Exception {
    advanceClockSec(1);
    MessageBundle bundle = getMessageBundle(EVENTS_SUBTYPE, DISCOVERY_SUBFOLDER, messageObj);
    OBJECT_MAPPER.writeValue(new File(deviceDir, fileName), bundle.message);
  }

  private JsonNode validateAnalysisSchemaAndNoEmptyArrays(File analysisFile) throws Exception {
    JsonNode rawReport = OBJECT_MAPPER.readTree(analysisFile);
    assertNoEmptyArrays(rawReport);
    JsonSchema schema = MessageValidator.getSchema(
        new File(TestCommon.SCHEMA_SPEC, "analysis_discovery.json"));
    Set<ValidationMessage> errors = schema.validate(rawReport);
    assertTrue("analysis_discovery.json schema violations: " + errors, errors.isEmpty());
    return rawReport;
  }

  private void assertNoEmptyArrays(JsonNode node) {
    if (node.isArray()) {
      assertFalse("Serialized JSON must not contain empty arrays", node.isEmpty());
      node.forEach(this::assertNoEmptyArrays);
    } else if (node.isObject()) {
      node.fields().forEachRemaining(entry -> assertNoEmptyArrays(entry.getValue()));
    }
  }

  private static final class TwoPhaseMockPublisher implements MessagePublisher {

    private final Queue<MessageBundle> backlog;
    private final Queue<MessageBundle> liveQueue;
    private boolean active = true;

    private TwoPhaseMockPublisher(Queue<MessageBundle> backlog, Queue<MessageBundle> liveQueue) {
      this.backlog = backlog;
      this.liveQueue = liveQueue;
    }

    @Override
    public String publish(String deviceId, String topic, String data) {
      return null;
    }

    @Override
    public void close() {
      active = false;
    }

    @Override
    public String getSubscriptionId() {
      return "projects/mock/subscriptions/mock-sub";
    }

    @Override
    public void activate() {
    }

    @Override
    public boolean isActive() {
      return active;
    }

    @Override
    public MessageBundle takeNextMessage(QuerySpeed querySpeed) {
      if (querySpeed == QuerySpeed.QUICK) {
        if (!backlog.isEmpty()) {
          return backlog.poll();
        }
        try {
          Thread.sleep(250);
        } catch (InterruptedException e) {
          Thread.currentThread().interrupt();
        }
        return null;
      }
      if (!liveQueue.isEmpty()) {
        return liveQueue.poll();
      }
      active = false;
      return null;
    }
  }
}
