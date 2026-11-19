package com.google.daq.mqtt.validator;

import static com.google.common.base.Preconditions.checkNotNull;
import static com.google.daq.mqtt.util.providers.FamilyProvider.MAX_PORT_VALUE;
import static com.google.daq.mqtt.util.providers.FamilyProvider.constructUrl;
import static com.google.udmi.util.CleanDateFormat.cleanDate;
import static com.google.udmi.util.GeneralUtils.CSV_JOINER;
import static com.google.udmi.util.GeneralUtils.catchToNull;
import static com.google.udmi.util.JsonUtil.OBJECT_MAPPER;
import static com.google.udmi.util.JsonUtil.convertTo;
import static com.google.udmi.util.JsonUtil.isoConvert;
import static java.lang.String.format;
import static java.util.Optional.ofNullable;

import com.fasterxml.jackson.annotation.JsonInclude;
import com.fasterxml.jackson.annotation.JsonInclude.Include;
import com.fasterxml.jackson.annotation.JsonProperty;
import com.google.common.annotations.VisibleForTesting;
import com.google.daq.mqtt.util.providers.FamilyProvider;
import com.google.udmi.util.ValidationException;
import java.io.File;
import java.util.ArrayList;
import java.util.Date;
import java.util.HashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Map.Entry;
import java.util.Objects;
import java.util.Set;
import java.util.SortedSet;
import java.util.TreeMap;
import java.util.TreeSet;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.ScheduledFuture;
import java.util.concurrent.TimeUnit;
import java.util.function.Consumer;
import udmi.schema.Category;
import udmi.schema.DiscoveryEvents;
import udmi.schema.DiscoveryState;
import udmi.schema.FamilyDiscovery;
import udmi.schema.FamilyDiscoveryState;
import udmi.schema.FamilyDiscoveryState.Phase;
import udmi.schema.Level;
import udmi.schema.Metadata;
import udmi.schema.PointPointsetModel;
import udmi.schema.SystemDiscoveryData;

/**
 * Passively tracks and analyzes active UDMI discovery scans across event and state streams.
 */
public class DiscoveryAnalyzer {

  public static final String DISCOVERY_ANALYSIS_FILENAME = "discovery_analysis.json";
  private static final long LIVE_CLOSE_GRACE_SEC = 5;
  private static final String EVENTS_DETAIL_PREFIX = "events_discovery";
  private static final String STATE_DETAIL_PREFIX = "state_discovery";
  private static final String STATUS_IN_PROGRESS = "IN_PROGRESS";
  private static final String STATUS_PASSED = "PASSED";
  private static final String STATUS_PASSED_WITH_WARNINGS = "PASSED_WITH_WARNINGS";
  private static final String STATUS_FAILED = "FAILED";
  private static final String STATUS_INCOMPLETE = "INCOMPLETE";

  private final LoggingHandler outputLogger;
  private final Map<String, ReportingDevice> reportingDevices;
  private final Map<ScanKey, DiscoveryScanSession> scans = new TreeMap<>();
  private final Map<ScanKey, ScheduledFuture<?>> pendingTimers = new HashMap<>();

  private File analysisFile;
  private ScheduledExecutorService executor;
  private boolean simulatedMessages;
  private Consumer<ScanKey> graceCloseHandler;

  /**
   * Create an analyzer tied to validator logging and reporting devices.
   *
   * @param outputLogger     logger for analysis output
   * @param reportingDevices device registry map for metadata lookup and error reporting
   */
  public DiscoveryAnalyzer(LoggingHandler outputLogger,
      Map<String, ReportingDevice> reportingDevices) {
    this.outputLogger = outputLogger;
    this.reportingDevices = reportingDevices;
  }

  /**
   * Configure the output directory where discovery analysis JSON reports are written.
   *
   * @param outBaseDir validator output directory
   */
  public void setOutBaseDir(File outBaseDir) {
    if (outBaseDir == null) {
      analysisFile = null;
      return;
    }
    analysisFile = new File(outBaseDir, DISCOVERY_ANALYSIS_FILENAME);
    if (analysisFile.exists()) {
      analysisFile.delete();
    }
  }

  /**
   * Configure runtime timer callback.
   *
   * @param executor          scheduler for live grace timers
   * @param simulatedMessages true when replaying offline traces
   * @param graceCloseHandler callback invoked when a live scan grace timer expires
   */
  public void configureRuntime(ScheduledExecutorService executor, boolean simulatedMessages,
      Consumer<ScanKey> graceCloseHandler) {
    this.executor = executor;
    this.simulatedMessages = simulatedMessages;
    this.graceCloseHandler = graceCloseHandler;
  }

  /**
   * Determine if a raw state.discovery block contains active/pending or tracked scan updates.
   *
   * @param scannerDeviceId device publishing the state message
   * @param discoveryObj    raw state.discovery map or object
   * @return true if throttling should be bypassed to preserve discovery state transitions
   */
  public boolean hasRelevantDiscoveryState(String scannerDeviceId, Object discoveryObj) {
    if (discoveryObj == null) {
      return false;
    }
    DiscoveryState discoveryState = discoveryObj instanceof DiscoveryState ds
        ? ds : catchToNull(() -> convertTo(DiscoveryState.class, discoveryObj));
    if (discoveryState == null || discoveryState.families == null) {
      return false;
    }
    for (Entry<String, FamilyDiscoveryState> entry : discoveryState.families.entrySet()) {
      FamilyDiscoveryState famState = entry.getValue();
      if (famState == null || famState.generation == null) {
        continue;
      }
      if (famState.phase == Phase.PENDING || famState.phase == Phase.ACTIVE) {
        return true;
      }
      ScanKey key = new ScanKey(scannerDeviceId, entry.getKey(),
          canonicalGeneration(famState.generation));
      DiscoveryScanSession existing = scans.get(key);
      if (existing != null && existing.activated && !existing.closed) {
        return true;
      }
    }
    return false;
  }

  /**
   * Validate and record a DiscoveryEvents message.
   *
   * @param scannerDeviceId device emitting the discovery event
   * @param event           parsed discovery event
   * @param errorCollector  collector for per-message validation errors
   */
  public void processEvents(String scannerDeviceId, DiscoveryEvents event,
      ErrorCollector errorCollector) {
    if (event.family == null) {
      throw new IllegalArgumentException("discovery family not defined");
    }
    String scanFamily = event.family;
    FamilyProvider familyProvider = FamilyProvider.NAMED_FAMILIES.get(scanFamily);
    checkNotNull(familyProvider, "Unknown provider for discovery family " + scanFamily);

    if (event.generation == null) {
      validateNonActiveEvent(event, scanFamily, familyProvider);
      return;
    }

    String generationIso = canonicalGeneration(event.generation);
    ScanKey key = new ScanKey(scannerDeviceId, scanFamily, generationIso);
    DiscoveryScanSession existing = scans.get(key);
    if ((existing == null || !existing.activated) && isSelfEnumerationEvent(event)) {
      return;
    }

    DiscoveryScanSession session = scans.computeIfAbsent(key, DiscoveryScanSession::new);
    session.activated = true;

    if (session.closed) {
      recordError(session, errorCollector, EVENTS_DETAIL_PREFIX,
          format("Discovery event (event_no=%s) arrived after scan closed", event.event_no));
    }

    validateActiveEvent(session, event, familyProvider, errorCollector);
    writeAnalysisReport();
  }

  /**
   * Record and correlate a DiscoveryState update.
   *
   * @param scannerDeviceId device emitting the discovery state
   * @param state           parsed discovery state
   * @param errorCollector  collector for state validation errors
   */
  public void processState(String scannerDeviceId, DiscoveryState state,
      ErrorCollector errorCollector) {
    if (state == null || state.families == null) {
      return;
    }
    for (Entry<String, FamilyDiscoveryState> entry : state.families.entrySet()) {
      String family = entry.getKey();
      FamilyDiscoveryState famState = entry.getValue();
      if (famState == null || famState.generation == null) {
        continue;
      }
      String generationIso = canonicalGeneration(famState.generation);
      ScanKey key = new ScanKey(scannerDeviceId, family, generationIso);
      DiscoveryScanSession session = scans.computeIfAbsent(key, DiscoveryScanSession::new);
      session.stateObserved = true;

      if (famState.phase != null) {
        String phaseValue = famState.phase.value();
        if (session.statePhases.isEmpty()
            || !session.statePhases.get(session.statePhases.size() - 1).equals(phaseValue)) {
          session.statePhases.add(phaseValue);
        }
        session.latestPhase = famState.phase;
        if (famState.phase == Phase.PENDING || famState.phase == Phase.ACTIVE) {
          session.activated = true;
        }
      }
      if (famState.active_count != null) {
        session.stateActiveCount = famState.active_count;
      }
      if (famState.status != null && famState.status.level != null
          && famState.status.level >= Level.ERROR.value()) {
        String detail = ofNullable(famState.status.message).orElse("error status reported");
        recordError(session, errorCollector, STATE_DETAIL_PREFIX,
            format("Discovery state %s error: %s", family, detail));
      }
      if (famState.phase == Phase.STOPPED && session.activated) {
        onClosingSignal(session, errorCollector);
      }
    }
    writeAnalysisReport();
  }

  /**
   * Close an open scan when its live settle grace period expires.
   *
   * @param key scan identifier
   * @return true if the scan was open and closed by this call
   */
  public boolean closeIfExpired(ScanKey key) {
    pendingTimers.remove(key);
    DiscoveryScanSession session = scans.get(key);
    if (session == null || !session.activated || session.closed) {
      return false;
    }
    ErrorCollector errorCollector = reportingDevices.get(key.scannerDeviceId);
    closeScan(session, errorCollector, false);
    writeAnalysisReport();
    return true;
  }

  /**
   * Finalize all active scans that remain open at trace end or shutdown.
   *
   * @return true if at least one scan was finalized
   */
  public boolean finalizeAllScans() {
    for (ScheduledFuture<?> timer : pendingTimers.values()) {
      if (timer != null) {
        timer.cancel(false);
      }
    }
    pendingTimers.clear();

    boolean finalizedAny = false;
    for (DiscoveryScanSession session : scans.values()) {
      if (session.activated && !session.closed) {
        ErrorCollector errorCollector = reportingDevices.get(session.key.scannerDeviceId);
        closeScan(session, errorCollector, true);
        finalizedAny = true;
      }
    }
    if (finalizedAny) {
      writeAnalysisReport();
    }
    return finalizedAny;
  }

  /**
   * Write the high-level discovery analysis JSON file when active scans exist.
   */
  public void writeAnalysisReport() {
    if (analysisFile == null || !hasActivatedScans()) {
      return;
    }
    try {
      File parent = analysisFile.getParentFile();
      if (parent != null) {
        parent.mkdirs();
      }
      OBJECT_MAPPER.writeValue(analysisFile, getAnalysisReport());
    } catch (Exception e) {
      throw new RuntimeException(
          "While writing discovery analysis report " + analysisFile.getAbsolutePath(), e);
    }
  }

  /**
   * Build the high-level discovery analysis report snapshot.
   *
   * @return current report covering all activated scans
   */
  @VisibleForTesting
  public DiscoveryAnalysisReport getAnalysisReport() {
    DiscoveryAnalysisReport report = new DiscoveryAnalysisReport();
    report.timestamp = new Date();
    report.scans = new ArrayList<>();
    for (DiscoveryScanSession session : scans.values()) {
      if (session.activated) {
        report.scans.add(buildScanSummary(session));
      }
    }
    return report;
  }

  private boolean hasActivatedScans() {
    return scans.values().stream().anyMatch(session -> session.activated);
  }

  private void validateNonActiveEvent(DiscoveryEvents event, String scanFamily,
      FamilyProvider familyProvider) {
    if (event.addr != null) {
      if (event.refs == null) {
        familyProvider.validateUrl(constructUrl(scanFamily, event.addr, null));
      } else {
        event.refs.forEach((key, value) ->
            familyProvider.validateUrl(constructUrl(scanFamily, event.addr, key)));
      }
    } else if (event.network != null) {
      familyProvider.validateNetwork(event.network);
    }
  }

  private boolean isSelfEnumerationEvent(DiscoveryEvents event) {
    return event.event_no == null && event.addr == null && event.network == null;
  }

  private void validateActiveEvent(DiscoveryScanSession session, DiscoveryEvents event,
      FamilyProvider familyProvider, ErrorCollector errorCollector) {
    if (event.event_no == null) {
      recordError(session, errorCollector, EVENTS_DETAIL_PREFIX,
          "Active discovery event missing event_no");
      return;
    }
    int eventNo = event.event_no;
    if (eventNo == 0) {
      if (session.startMarkerSeen) {
        recordError(session, errorCollector, EVENTS_DETAIL_PREFIX,
            "Duplicate discovery start marker (event_no=0)");
      }
      session.startMarkerSeen = true;
      if (event.addr != null || event.network != null) {
        recordError(session, errorCollector, EVENTS_DETAIL_PREFIX,
            "Start marker (event_no=0) must not specify addr or network");
      }
      return;
    }
    if (eventNo < 0) {
      if (session.endMarkerEventNo != null) {
        recordError(session, errorCollector, EVENTS_DETAIL_PREFIX,
            format("Duplicate discovery end marker (event_no=%d)", eventNo));
      }
      session.endMarkerEventNo = eventNo;
      if (event.addr != null || event.network != null) {
        recordError(session, errorCollector, EVENTS_DETAIL_PREFIX,
            format("End marker (event_no=%d) must not specify addr or network", eventNo));
      }
      onClosingSignal(session, errorCollector);
      return;
    }

    session.rawItemCount++;
    if (!session.positiveEventNos.add(eventNo)) {
      session.duplicateEventNos.add(eventNo);
      recordError(session, errorCollector, EVENTS_DETAIL_PREFIX,
          format("Duplicate discovery event_no %d", eventNo));
    }

    boolean hasErrorStatus = event.status != null && event.status.level != null
        && event.status.level >= Level.ERROR.value();
    if (hasErrorStatus) {
      String detail = ofNullable(event.status.message).orElse("error status reported");
      recordError(session, errorCollector, EVENTS_DETAIL_PREFIX,
          format("Discovery event %d error status: %s", eventNo, detail));
    }

    if (event.addr == null && event.network != null) {
      validateBucketsItem(session, event, familyProvider, errorCollector);
    } else if (event.addr != null) {
      validateDeviceItem(session, event, familyProvider, errorCollector);
    } else if (!hasErrorStatus) {
      recordError(session, errorCollector, EVENTS_DETAIL_PREFIX,
          format("Discovery event %d missing both addr and network", eventNo));
    }
  }

  private void validateBucketsItem(DiscoveryScanSession session, DiscoveryEvents event,
      FamilyProvider familyProvider, ErrorCollector errorCollector) {
    session.sawBucketsItem = true;
    String network = event.network;
    if (!session.discoveredNetworks.add(network)) {
      session.duplicateNetworks.add(network);
      recordError(session, errorCollector, EVENTS_DETAIL_PREFIX,
          format("Duplicate discovered network %s", network));
    }
    try {
      familyProvider.validateNetwork(network);
    } catch (Exception e) {
      recordError(session, errorCollector, EVENTS_DETAIL_PREFIX,
          format("Invalid network %s: %s", network, e.getMessage()));
    }
    if (event.system != null) {
      recordError(session, errorCollector, EVENTS_DETAIL_PREFIX,
          format("Unexpected system block in buckets scan for network %s", network));
    }
    if (event.refs != null) {
      recordError(session, errorCollector, EVENTS_DETAIL_PREFIX,
          format("Unexpected refs block in buckets scan for network %s", network));
    }
    validateDiscoveredFamilies(session, event.families, errorCollector);
  }

  private void validateDeviceItem(DiscoveryScanSession session, DiscoveryEvents event,
      FamilyProvider familyProvider, ErrorCollector errorCollector) {
    session.sawEntriesItem = true;
    String addr = event.addr;
    if (!session.discoveredAddrs.add(addr)) {
      session.duplicateAddrs.add(addr);
      recordError(session, errorCollector, EVENTS_DETAIL_PREFIX,
          format("Duplicate discovered addr %s", addr));
    }
    try {
      familyProvider.validateAddr(addr);
    } catch (Exception e) {
      recordError(session, errorCollector, EVENTS_DETAIL_PREFIX,
          format("Invalid addr %s: %s", addr, e.getMessage()));
    }

    if (event.network != null) {
      session.discoveredNetworks.add(event.network);
      try {
        familyProvider.validateNetwork(event.network);
      } catch (Exception e) {
        recordError(session, errorCollector, EVENTS_DETAIL_PREFIX,
            format("Invalid network %s for addr %s: %s", event.network, addr, e.getMessage()));
      }
    }

    validateDiscoveredFamilies(session, event.families, errorCollector);

    if (event.system != null) {
      session.sawSystemBlock = true;
    }
    if (event.refs != null) {
      session.sawDetailsRefs = true;
      for (String refKey : event.refs.keySet()) {
        try {
          familyProvider.validateUrl(constructUrl(event.family, addr, refKey));
        } catch (Exception e) {
          recordError(session, errorCollector, EVENTS_DETAIL_PREFIX,
              format("Invalid ref %s for addr %s: %s", refKey, addr, e.getMessage()));
        }
      }
    }

    compareWithSiteModel(session, event, errorCollector);
  }

  private void validateDiscoveredFamilies(DiscoveryScanSession session,
      Map<String, FamilyDiscovery> families, ErrorCollector errorCollector) {
    if (families == null) {
      return;
    }
    for (Entry<String, FamilyDiscovery> entry : families.entrySet()) {
      String famKey = entry.getKey();
      FamilyDiscovery famDiscovery = entry.getValue();
      FamilyProvider famProvider = FamilyProvider.NAMED_FAMILIES.get(famKey);
      if (famProvider == null) {
        recordError(session, errorCollector, EVENTS_DETAIL_PREFIX,
            format("Unknown family %s in discovered families map", famKey));
        continue;
      }
      if (famDiscovery == null) {
        continue;
      }
      if (famDiscovery.addr != null) {
        try {
          famProvider.validateAddr(famDiscovery.addr);
        } catch (Exception e) {
          recordError(session, errorCollector, EVENTS_DETAIL_PREFIX,
              format("Invalid %s family addr %s: %s", famKey, famDiscovery.addr, e.getMessage()));
        }
      }
      if (famDiscovery.port != null
          && (famDiscovery.port < 0 || famDiscovery.port > MAX_PORT_VALUE)) {
        recordError(session, errorCollector, EVENTS_DETAIL_PREFIX,
            format("Invalid %s family port %d", famKey, famDiscovery.port));
      }
    }
  }

  private void compareWithSiteModel(DiscoveryScanSession session, DiscoveryEvents event,
      ErrorCollector errorCollector) {
    if (!hasSiteModelMetadata()) {
      return;
    }
    Entry<String, Metadata> match = findDeviceByFamilyAddr(event.family, event.addr);
    if (match == null) {
      session.extraAddrs.add(event.addr);
      return;
    }
    String matchedDeviceId = match.getKey();
    Metadata metadata = match.getValue();
    session.matchedDevices.add(matchedDeviceId);

    String expectedNetwork = catchToNull(
        () -> metadata.localnet.families.get(event.family).network);
    if ((expectedNetwork != null || event.network != null)
        && !Objects.equals(expectedNetwork, event.network)) {
      recordError(session, errorCollector, EVENTS_DETAIL_PREFIX,
          format("Device %s network mismatch: expected %s, got %s",
              matchedDeviceId, expectedNetwork, event.network));
    }

    if (event.families != null) {
      for (Entry<String, FamilyDiscovery> entry : event.families.entrySet()) {
        String famKey = entry.getKey();
        String discoveredFamAddr = catchToNull(() -> entry.getValue().addr);
        if (discoveredFamAddr == null) {
          continue;
        }
        if ("iot".equals(famKey)) {
          if (!Objects.equals(matchedDeviceId, discoveredFamAddr)) {
            recordError(session, errorCollector, EVENTS_DETAIL_PREFIX,
                format("Device %s iot family addr mismatch: expected %s, got %s",
                    matchedDeviceId, matchedDeviceId, discoveredFamAddr));
          }
        } else {
          String expectedFamAddr = catchToNull(
              () -> metadata.localnet.families.get(famKey).addr);
          if (expectedFamAddr != null && !Objects.equals(expectedFamAddr, discoveredFamAddr)) {
            recordError(session, errorCollector, EVENTS_DETAIL_PREFIX,
                format("Device %s family %s addr mismatch: expected %s, got %s",
                    matchedDeviceId, famKey, expectedFamAddr, discoveredFamAddr));
          }
        }
      }
    }

    if (event.system != null) {
      compareSystem(session, matchedDeviceId, metadata, event.system, errorCollector);
    }

    Set<String> expectedRefs = extractMetadataRefs(metadata);
    if (event.refs != null) {
      Set<String> discoveredRefs = new TreeSet<>(event.refs.keySet());
      Set<String> missingRefs = new TreeSet<>(expectedRefs);
      missingRefs.removeAll(discoveredRefs);
      Set<String> extraRefs = new TreeSet<>(discoveredRefs);
      extraRefs.removeAll(expectedRefs);
      if (!missingRefs.isEmpty() || !extraRefs.isEmpty()) {
        recordError(session, errorCollector, EVENTS_DETAIL_PREFIX,
            format("Device %s refs mismatch: missing=%s, extra=%s",
                matchedDeviceId, missingRefs, extraRefs));
      }
    } else if (!expectedRefs.isEmpty()) {
      session.matchedDevicesWithoutRefs.put(matchedDeviceId, expectedRefs);
    }
  }

  private void compareSystem(DiscoveryScanSession session, String matchedDeviceId,
      Metadata metadata, SystemDiscoveryData system, ErrorCollector errorCollector) {
    String expectedName = catchToNull(() -> metadata.system.physical_tag.asset.name);
    if (!Objects.equals(expectedName, system.name)) {
      recordError(session, errorCollector, EVENTS_DETAIL_PREFIX,
          format("Device %s system.name mismatch: expected %s, got %s",
              matchedDeviceId, expectedName, system.name));
    }
    String expectedSerial = catchToNull(() -> metadata.system.serial_no);
    if (!Objects.equals(expectedSerial, system.serial_no)) {
      recordError(session, errorCollector, EVENTS_DETAIL_PREFIX,
          format("Device %s system.serial_no mismatch: expected %s, got %s",
              matchedDeviceId, expectedSerial, system.serial_no));
    }
    String expectedMake = catchToNull(() -> metadata.system.hardware.make);
    String actualMake = catchToNull(() -> system.hardware.make);
    if (!Objects.equals(expectedMake, actualMake)) {
      recordError(session, errorCollector, EVENTS_DETAIL_PREFIX,
          format("Device %s system.hardware.make mismatch: expected %s, got %s",
              matchedDeviceId, expectedMake, actualMake));
    }
    String expectedModel = catchToNull(() -> metadata.system.hardware.model);
    String actualModel = catchToNull(() -> system.hardware.model);
    if (!Objects.equals(expectedModel, actualModel)) {
      recordError(session, errorCollector, EVENTS_DETAIL_PREFIX,
          format("Device %s system.hardware.model mismatch: expected %s, got %s",
              matchedDeviceId, expectedModel, actualModel));
    }
  }

  private void onClosingSignal(DiscoveryScanSession session, ErrorCollector errorCollector) {
    if (session.closed) {
      return;
    }
    if (session.endMarkerEventNo != null && session.latestPhase == Phase.STOPPED) {
      ScheduledFuture<?> timer = pendingTimers.remove(session.key);
      if (timer != null) {
        timer.cancel(false);
      }
      closeScan(session, errorCollector, false);
      return;
    }
    if (!simulatedMessages && !session.graceScheduled
        && executor != null && graceCloseHandler != null) {
      session.graceScheduled = true;
      ScheduledFuture<?> future = executor.schedule(
          () -> graceCloseHandler.accept(session.key),
          LIVE_CLOSE_GRACE_SEC, TimeUnit.SECONDS);
      pendingTimers.put(session.key, future);
    }
  }

  private void closeScan(DiscoveryScanSession session, ErrorCollector errorCollector,
      boolean atFinalization) {
    if (session.closed) {
      return;
    }
    session.closed = true;

    String detailPrefix = session.endMarkerEventNo != null
        ? EVENTS_DETAIL_PREFIX : STATE_DETAIL_PREFIX;

    if (!session.startMarkerSeen) {
      recordError(session, errorCollector, detailPrefix,
          "Missing discovery start marker (event_no=0)");
    }

    int expectedUpper = session.positiveEventNos.isEmpty()
        ? 0 : Math.max(session.rawItemCount, session.positiveEventNos.last());
    for (int i = 1; i <= expectedUpper; i++) {
      if (!session.positiveEventNos.contains(i)) {
        session.missingEventNos.add(i);
      }
    }
    if (!session.missingEventNos.isEmpty()) {
      recordError(session, errorCollector, detailPrefix,
          format("Missing discovery event_no values: %s", session.missingEventNos));
    }

    if (session.endMarkerEventNo == null) {
      session.incomplete = atFinalization;
      recordError(session, errorCollector, detailPrefix,
          "Missing discovery end marker (event_no<0)");
    } else {
      int expectedEndMarker = -(session.rawItemCount + 1);
      if (session.endMarkerEventNo != expectedEndMarker) {
        recordError(session, errorCollector, detailPrefix,
            format("Discovery end marker mismatch: expected %d, got %d",
                expectedEndMarker, session.endMarkerEventNo));
      }
    }

    if (session.stateObserved) {
      if (session.latestPhase != Phase.STOPPED) {
        session.incomplete = atFinalization;
        recordError(session, errorCollector, STATE_DETAIL_PREFIX,
            format("Discovery state did not reach stopped phase (last phase: %s)",
                session.latestPhase == null ? "null" : session.latestPhase.value()));
      }
      if (session.stateActiveCount == null) {
        recordWarning(session, "Discovery state did not report active_count");
      } else if (session.stateActiveCount != session.rawItemCount) {
        recordError(session, errorCollector, STATE_DETAIL_PREFIX,
            format("Discovery state active_count mismatch: expected %d, got %d",
                session.rawItemCount, session.stateActiveCount));
      }
    } else {
      recordWarning(session, "DiscoveryState not observed for active scan");
    }

    if (session.sawDetailsRefs) {
      for (Entry<String, Set<String>> entry : session.matchedDevicesWithoutRefs.entrySet()) {
        recordError(session, errorCollector, detailPrefix,
            format("Device %s omitted refs in details scan (expected %d)",
                entry.getKey(), entry.getValue().size()));
      }
    }

    evaluateSiteModelCoverage(session);
    logScanSummary(session);
  }

  private void evaluateSiteModelCoverage(DiscoveryScanSession session) {
    if (!hasSiteModelMetadata()) {
      return;
    }
    if (session.sawBucketsItem && !session.sawEntriesItem) {
      Set<String> expectedNetworks = findSiteNetworksForFamily(session.key.family);
      Set<String> missingNetworks = new TreeSet<>(expectedNetworks);
      missingNetworks.removeAll(session.discoveredNetworks);
      if (!missingNetworks.isEmpty()) {
        recordWarning(session,
            format("Site model networks not discovered: %s", CSV_JOINER.join(missingNetworks)));
      }
      Set<String> extraNetworks = new TreeSet<>(session.discoveredNetworks);
      extraNetworks.removeAll(expectedNetworks);
      if (!extraNetworks.isEmpty()) {
        recordWarning(session,
            format("Discovered networks not in site model: %s", CSV_JOINER.join(extraNetworks)));
      }
      return;
    }

    Set<String> expectedDevices = findSiteDevicesForFamily(session.key.family);
    session.missingDevices.addAll(expectedDevices);
    session.missingDevices.removeAll(session.matchedDevices);
    if (!session.missingDevices.isEmpty()) {
      recordWarning(session, format("Site model devices not discovered for %s: %s",
          session.key.family, CSV_JOINER.join(session.missingDevices)));
    }
    if (!session.extraAddrs.isEmpty()) {
      recordWarning(session, format("Discovered %s addrs not in site model: %s",
          session.key.family, CSV_JOINER.join(session.extraAddrs)));
    }
  }

  private void logScanSummary(DiscoveryScanSession session) {
    if (outputLogger == null) {
      return;
    }
    DiscoveryScanSummary summary = buildScanSummary(session);
    String stateDesc = session.stateObserved
        ? format("%s(%s)",
            session.latestPhase == null ? "unknown" : session.latestPhase.value(),
            session.stateActiveCount == null ? "?" : session.stateActiveCount.toString())
        : "unobserved";
    String markersDesc = format("%s..%s",
        session.startMarkerSeen ? "0" : "missing",
        session.endMarkerEventNo == null ? "missing" : session.endMarkerEventNo.toString());
    String line = format(
        "Discovery scan analysis [%s / %s @ %s]: status=%s, depth=%s, items=%d, "
            + "markers=%s, state=%s, matched=%d %s, missing=%d, extra=%d, errors=%d, warnings=%d",
        summary.scannerDeviceId, summary.family, summary.generation, summary.status,
        summary.inferredDepth, summary.receivedItemCount, markersDesc, stateDesc,
        summary.matchedDevices.size(), summary.matchedDevices,
        summary.missingDevices.size(), summary.extraAddrs.size(),
        summary.errors.size(), summary.warnings.size());
    if (!summary.errors.isEmpty()) {
      outputLogger.error("%s", line);
      summary.errors.forEach(err -> outputLogger.error("  [error] %s", err));
    } else if (!summary.warnings.isEmpty()) {
      outputLogger.warn(line);
    } else {
      outputLogger.notice(line);
    }
    summary.warnings.forEach(warn -> outputLogger.warn("  [warning] " + warn));
  }

  private DiscoveryScanSummary buildScanSummary(DiscoveryScanSession session) {
    DiscoveryScanSummary summary = new DiscoveryScanSummary();
    summary.scannerDeviceId = session.key.scannerDeviceId;
    summary.family = session.key.family;
    summary.generation = session.key.generationIso;
    summary.status = resolveStatus(session);
    summary.inferredDepth = inferDepth(session);
    summary.stateObserved = session.stateObserved;
    summary.statePhases = new ArrayList<>(session.statePhases);
    summary.stateActiveCount = session.stateActiveCount;
    summary.startMarkerSeen = session.startMarkerSeen;
    summary.endMarkerEventNo = session.endMarkerEventNo;
    summary.receivedItemCount = session.rawItemCount;
    summary.missingEventNos = new ArrayList<>(session.missingEventNos);
    summary.duplicateEventNos = new ArrayList<>(session.duplicateEventNos);
    summary.discoveredAddrs = new ArrayList<>(session.discoveredAddrs);
    summary.duplicateAddrs = new ArrayList<>(session.duplicateAddrs);
    summary.discoveredNetworks = new ArrayList<>(session.discoveredNetworks);
    summary.duplicateNetworks = new ArrayList<>(session.duplicateNetworks);
    summary.matchedDevices = new ArrayList<>(session.matchedDevices);
    summary.missingDevices = new ArrayList<>(session.missingDevices);
    summary.extraAddrs = new ArrayList<>(session.extraAddrs);
    summary.errors = new ArrayList<>(session.errors);
    summary.warnings = new ArrayList<>(session.warnings);
    return summary;
  }

  private String resolveStatus(DiscoveryScanSession session) {
    if (!session.closed) {
      return STATUS_IN_PROGRESS;
    }
    if (session.incomplete) {
      return STATUS_INCOMPLETE;
    }
    if (!session.errors.isEmpty()) {
      return STATUS_FAILED;
    }
    if (!session.warnings.isEmpty()) {
      return STATUS_PASSED_WITH_WARNINGS;
    }
    return STATUS_PASSED;
  }

  private String inferDepth(DiscoveryScanSession session) {
    if (session.sawDetailsRefs) {
      return "details";
    }
    if (session.sawSystemBlock) {
      return "system";
    }
    if (session.sawEntriesItem) {
      return "entries";
    }
    if (session.sawBucketsItem) {
      return "buckets";
    }
    return "unknown";
  }

  private void recordError(DiscoveryScanSession session, ErrorCollector errorCollector,
      String detailPrefix, String message) {
    session.errors.add(message);
    if (errorCollector != null) {
      errorCollector.addError(new ValidationException(message),
          Category.VALIDATION_DEVICE_CONTENT, format("%s: %s", detailPrefix, message));
    }
  }

  private void recordWarning(DiscoveryScanSession session, String message) {
    session.warnings.add(message);
  }

  private boolean hasSiteModelMetadata() {
    if (reportingDevices == null || reportingDevices.isEmpty()) {
      return false;
    }
    return reportingDevices.values().stream().anyMatch(device -> device.getMetadata() != null);
  }

  private Entry<String, Metadata> findDeviceByFamilyAddr(String family, String addr) {
    for (Entry<String, ReportingDevice> entry : reportingDevices.entrySet()) {
      Metadata metadata = entry.getValue().getMetadata();
      if (metadata == null) {
        continue;
      }
      String modelAddr = catchToNull(() -> metadata.localnet.families.get(family).addr);
      if (Objects.equals(addr, modelAddr)) {
        return Map.entry(entry.getKey(), metadata);
      }
    }
    return null;
  }

  private Set<String> findSiteDevicesForFamily(String family) {
    Set<String> expected = new TreeSet<>();
    for (Entry<String, ReportingDevice> entry : reportingDevices.entrySet()) {
      Metadata metadata = entry.getValue().getMetadata();
      if (metadata == null) {
        continue;
      }
      String modelAddr = catchToNull(() -> metadata.localnet.families.get(family).addr);
      if (modelAddr != null) {
        expected.add(entry.getKey());
      }
    }
    return expected;
  }

  private Set<String> findSiteNetworksForFamily(String family) {
    Set<String> expected = new TreeSet<>();
    for (ReportingDevice device : reportingDevices.values()) {
      Metadata metadata = device.getMetadata();
      if (metadata == null) {
        continue;
      }
      String modelNetwork = catchToNull(() -> metadata.localnet.families.get(family).network);
      if (modelNetwork != null) {
        expected.add(modelNetwork);
      }
    }
    return expected;
  }

  private Set<String> extractMetadataRefs(Metadata metadata) {
    Map<String, PointPointsetModel> points = catchToNull(() -> metadata.pointset.points);
    if (points == null) {
      return Set.of();
    }
    Set<String> refs = new TreeSet<>();
    for (PointPointsetModel point : points.values()) {
      if (point != null && point.ref != null) {
        refs.add(point.ref);
      }
    }
    return refs;
  }

  private static String canonicalGeneration(Date generation) {
    return isoConvert(cleanDate(new Date(generation.getTime())));
  }

  /**
   * Comparable key identifying a single active discovery scan.
   */
  public static final class ScanKey implements Comparable<ScanKey> {

    public final String scannerDeviceId;
    public final String family;
    public final String generationIso;

    /**
     * Create a scan key.
     */
    public ScanKey(String scannerDeviceId, String family, String generationIso) {
      this.scannerDeviceId = checkNotNull(scannerDeviceId, "scannerDeviceId");
      this.family = checkNotNull(family, "family");
      this.generationIso = checkNotNull(generationIso, "generationIso");
    }

    @Override
    public int compareTo(ScanKey other) {
      int cmp = scannerDeviceId.compareTo(other.scannerDeviceId);
      if (cmp != 0) {
        return cmp;
      }
      cmp = family.compareTo(other.family);
      return cmp != 0 ? cmp : generationIso.compareTo(other.generationIso);
    }

    @Override
    public boolean equals(Object obj) {
      if (this == obj) {
        return true;
      }
      if (!(obj instanceof ScanKey other)) {
        return false;
      }
      return scannerDeviceId.equals(other.scannerDeviceId)
          && family.equals(other.family)
          && generationIso.equals(other.generationIso);
    }

    @Override
    public int hashCode() {
      return Objects.hash(scannerDeviceId, family, generationIso);
    }
  }

  private static final class DiscoveryScanSession {

    private final ScanKey key;
    private final SortedSet<Integer> positiveEventNos = new TreeSet<>();
    private final Set<Integer> duplicateEventNos = new TreeSet<>();
    private final Set<Integer> missingEventNos = new TreeSet<>();
    private final Set<String> discoveredAddrs = new TreeSet<>();
    private final Set<String> duplicateAddrs = new TreeSet<>();
    private final Set<String> discoveredNetworks = new TreeSet<>();
    private final Set<String> duplicateNetworks = new TreeSet<>();
    private final Set<String> matchedDevices = new TreeSet<>();
    private final Set<String> missingDevices = new TreeSet<>();
    private final Set<String> extraAddrs = new TreeSet<>();
    private final Map<String, Set<String>> matchedDevicesWithoutRefs = new TreeMap<>();
    private final List<String> statePhases = new ArrayList<>();
    private final Set<String> errors = new LinkedHashSet<>();
    private final Set<String> warnings = new LinkedHashSet<>();

    private boolean activated;
    private boolean closed;
    private boolean graceScheduled;
    private boolean incomplete;
    private boolean startMarkerSeen;
    private Integer endMarkerEventNo;
    private int rawItemCount;
    private boolean stateObserved;
    private Phase latestPhase;
    private Integer stateActiveCount;
    private boolean sawBucketsItem;
    private boolean sawEntriesItem;
    private boolean sawSystemBlock;
    private boolean sawDetailsRefs;

    private DiscoveryScanSession(ScanKey key) {
      this.key = key;
    }
  }

  /**
   * High-level discovery analysis report written to discovery_analysis.json.
   */
  @JsonInclude(Include.NON_NULL)
  public static class DiscoveryAnalysisReport {

    @JsonProperty("timestamp")
    public Date timestamp;

    @JsonProperty("scans")
    public List<DiscoveryScanSummary> scans;
  }

  /**
   * High-level summary of a single active discovery scan.
   */
  @JsonInclude(Include.NON_NULL)
  public static class DiscoveryScanSummary {

    @JsonProperty("scanner_device_id")
    public String scannerDeviceId;

    @JsonProperty("family")
    public String family;

    @JsonProperty("generation")
    public String generation;

    @JsonProperty("status")
    public String status;

    @JsonProperty("inferred_depth")
    public String inferredDepth;

    @JsonProperty("state_observed")
    public boolean stateObserved;

    @JsonProperty("state_phases")
    public List<String> statePhases;

    @JsonProperty("state_active_count")
    public Integer stateActiveCount;

    @JsonProperty("start_marker_seen")
    public boolean startMarkerSeen;

    @JsonProperty("end_marker_event_no")
    public Integer endMarkerEventNo;

    @JsonProperty("received_item_count")
    public int receivedItemCount;

    @JsonProperty("missing_event_nos")
    public List<Integer> missingEventNos;

    @JsonProperty("duplicate_event_nos")
    public List<Integer> duplicateEventNos;

    @JsonProperty("discovered_addrs")
    public List<String> discoveredAddrs;

    @JsonProperty("duplicate_addrs")
    public List<String> duplicateAddrs;

    @JsonProperty("discovered_networks")
    public List<String> discoveredNetworks;

    @JsonProperty("duplicate_networks")
    public List<String> duplicateNetworks;

    @JsonProperty("matched_devices")
    public List<String> matchedDevices;

    @JsonProperty("missing_devices")
    public List<String> missingDevices;

    @JsonProperty("extra_addrs")
    public List<String> extraAddrs;

    @JsonProperty("errors")
    public List<String> errors;

    @JsonProperty("warnings")
    public List<String> warnings;
  }
}
