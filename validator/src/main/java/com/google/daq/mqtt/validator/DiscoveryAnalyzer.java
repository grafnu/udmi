package com.google.daq.mqtt.validator;

import static com.google.common.base.Preconditions.checkNotNull;
import static com.google.daq.mqtt.util.ConfigUtil.UDMI_VERSION;
import static com.google.daq.mqtt.util.providers.FamilyProvider.MAX_PORT_VALUE;
import static com.google.daq.mqtt.util.providers.FamilyProvider.constructUrl;
import static com.google.udmi.util.CleanDateFormat.cleanDate;
import static com.google.udmi.util.GeneralUtils.catchToNull;
import static com.google.udmi.util.JsonUtil.OBJECT_MAPPER;
import static com.google.udmi.util.JsonUtil.convertTo;
import static com.google.udmi.util.JsonUtil.getDate;
import static com.google.udmi.util.JsonUtil.isoConvert;
import static java.lang.String.format;
import static java.util.Optional.ofNullable;

import com.google.common.annotations.VisibleForTesting;
import com.google.daq.mqtt.util.providers.FamilyProvider;
import com.google.udmi.util.ValidationException;
import java.io.File;
import java.util.ArrayList;
import java.util.Collection;
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
import java.util.stream.Collectors;
import udmi.schema.Category;
import udmi.schema.DiscoveryAnalysis;
import udmi.schema.DiscoveryEvents;
import udmi.schema.DiscoveryScanSummary;
import udmi.schema.DiscoveryState;
import udmi.schema.Enumerations.Depth;
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
  public static final String STATUS_WITH_WARNINGS = "WITH_WARNINGS";
  public static final String STATUS_WITH_ERRORS = "WITH_ERRORS";
  public static final String STATUS_FAILED = "FAILED";
  public static final String STATUS_INCOMPLETE = "INCOMPLETE";
  public static final String STATUS_IN_PROGRESS = "IN_PROGRESS";
  private static final long LIVE_CLOSE_GRACE_SEC = 5;
  private static final String EVENTS_DETAIL_PREFIX = "events_discovery";
  private static final String STATE_DETAIL_PREFIX = "state_discovery";

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
   * @param simulatedMessages true when replaying offline traces or draining startup buffers
   * @param graceCloseHandler callback invoked when a live scan grace timer expires
   */
  public void configureRuntime(ScheduledExecutorService executor, boolean simulatedMessages,
      Consumer<ScanKey> graceCloseHandler) {
    this.executor = executor;
    this.simulatedMessages = simulatedMessages;
    this.graceCloseHandler = graceCloseHandler;
  }

  /**
   * Schedule live close grace timers for any open active scans that observed a partial closing
   * signal during a startup buffer drain.
   */
  public void armPendingGraceTimers() {
    if (simulatedMessages || executor == null || graceCloseHandler == null) {
      return;
    }
    for (DiscoveryScanSession session : scans.values()) {
      if (!session.activated || session.closed || session.graceScheduled) {
        continue;
      }
      if (session.endMarkerEventNo != null || session.latestPhase == Phase.STOPPED) {
        session.graceScheduled = true;
        ScheduledFuture<?> future = executor.schedule(
            () -> graceCloseHandler.accept(session.key),
            LIVE_CLOSE_GRACE_SEC, TimeUnit.SECONDS);
        pendingTimers.put(session.key, future);
      }
    }
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
   * Check whether a scanner device has any recorded discovery validation errors.
   *
   * @param scannerDeviceId scanner device identifier
   * @return true if at least one active scan recorded an error for this scanner
   */
  public boolean hasDeviceErrors(String scannerDeviceId) {
    if (scannerDeviceId == null) {
      return false;
    }
    for (DiscoveryScanSession session : scans.values()) {
      if (session.activated
          && scannerDeviceId.equals(session.key.scannerDeviceId)
          && !session.errors.isEmpty()) {
        return true;
      }
    }
    return false;
  }

  /**
   * Build summarized device-level validation error entries for a scanner device.
   *
   * @param scannerDeviceId scanner device identifier
   * @param timestamp       timestamp to stamp on generated entries
   * @return summarized error entries for device validation reports
   */
  public List<udmi.schema.Entry> getDeviceErrorEntries(String scannerDeviceId, Date timestamp) {
    if (scannerDeviceId == null) {
      return List.of();
    }
    Map<String, Integer> scansWithoutErrors = new TreeMap<>();
    Map<String, Integer> scansWithErrors = new TreeMap<>();

    for (DiscoveryScanSession session : scans.values()) {
      if (!session.activated || !scannerDeviceId.equals(session.key.scannerDeviceId)) {
        continue;
      }
      if (session.errors.isEmpty()) {
        scansWithoutErrors.merge(session.key.family, 1, Integer::sum);
      } else {
        scansWithErrors.merge(session.key.family, 1, Integer::sum);
      }
    }

    if (scansWithErrors.isEmpty()) {
      return List.of();
    }

    String withErrors = formatFamilyCounts(scansWithErrors);
    String summaryText = scansWithoutErrors.isEmpty()
        ? format("Scans with errors (%s)", withErrors)
        : format("Scans without errors (%s), scans with errors (%s)",
            formatFamilyCounts(scansWithoutErrors), withErrors);
    return List.of(makeSummaryEntry(summaryText, summaryText, timestamp));
  }

  private static String formatFamilyCounts(Map<String, Integer> counts) {
    return counts.entrySet().stream()
        .map(entry -> entry.getKey() + ": " + entry.getValue())
        .collect(Collectors.joining(", "));
  }

  private static udmi.schema.Entry makeSummaryEntry(String message, String detail, Date timestamp) {
    udmi.schema.Entry entry = new udmi.schema.Entry();
    entry.category = Category.VALIDATION_DEVICE_CONTENT;
    entry.level = Level.ERROR.value();
    entry.message = message;
    entry.detail = detail;
    entry.timestamp = timestamp;
    return entry;
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
      recordError(session, IssueKind.LATE_EVENT, errorCollector, EVENTS_DETAIL_PREFIX,
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
        recordError(session, IssueKind.STATE_ERROR_STATUS, errorCollector, STATE_DETAIL_PREFIX,
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
  public DiscoveryAnalysis getAnalysisReport() {
    DiscoveryAnalysis report = new DiscoveryAnalysis();
    report.version = UDMI_VERSION;
    report.timestamp = new Date();
    List<DiscoveryScanSummary> summaries = new ArrayList<>();
    for (DiscoveryScanSession session : scans.values()) {
      if (session.activated) {
        summaries.add(buildScanSummary(session));
      }
    }
    report.scans = emptyToNull(summaries);
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
      recordError(session, IssueKind.MISSING_EVENT_NO, errorCollector, EVENTS_DETAIL_PREFIX,
          "Active discovery event missing event_no");
      return;
    }
    int eventNo = event.event_no;
    if (eventNo == 0) {
      if (session.startMarkerSeen) {
        recordError(session, IssueKind.DUPLICATE_START_MARKER, errorCollector, EVENTS_DETAIL_PREFIX,
            "Duplicate discovery start marker (event_no=0)");
      }
      session.startMarkerSeen = true;
      if (event.addr != null || event.network != null) {
        recordError(session, IssueKind.INVALID_START_MARKER, errorCollector, EVENTS_DETAIL_PREFIX,
            "Start marker (event_no=0) must not specify addr or network");
      }
      return;
    }
    if (eventNo < 0) {
      if (session.endMarkerEventNo != null) {
        recordError(session, IssueKind.DUPLICATE_END_MARKER, errorCollector, EVENTS_DETAIL_PREFIX,
            format("Duplicate discovery end marker (event_no=%d)", eventNo));
      }
      session.endMarkerEventNo = eventNo;
      if (event.addr != null || event.network != null) {
        recordError(session, IssueKind.INVALID_END_MARKER, errorCollector, EVENTS_DETAIL_PREFIX,
            format("End marker (event_no=%d) must not specify addr or network", eventNo));
      }
      onClosingSignal(session, errorCollector);
      return;
    }

    session.rawItemCount++;
    if (!session.positiveEventNos.add(eventNo)) {
      session.duplicateEventNos.add(eventNo);
      recordError(session, IssueKind.DUPLICATE_EVENT_NO, errorCollector, EVENTS_DETAIL_PREFIX,
          format("Duplicate discovery event_no %d", eventNo));
    }

    boolean hasErrorStatus = event.status != null && event.status.level != null
        && event.status.level >= Level.ERROR.value();
    if (hasErrorStatus) {
      String detail = ofNullable(event.status.message).orElse("error status reported");
      recordError(session, IssueKind.EVENT_ERROR_STATUS, errorCollector, EVENTS_DETAIL_PREFIX,
          format("Discovery event %d error status: %s", eventNo, detail));
    }

    if (event.addr == null && event.network != null) {
      validateBucketsItem(session, event, familyProvider, errorCollector);
    } else if (event.addr != null) {
      validateDeviceItem(session, event, familyProvider, errorCollector);
    } else if (!hasErrorStatus) {
      recordError(session, IssueKind.MISSING_ADDR_AND_NETWORK, errorCollector, EVENTS_DETAIL_PREFIX,
          format("Discovery event %d missing both addr and network", eventNo));
    }
  }

  private void validateBucketsItem(DiscoveryScanSession session, DiscoveryEvents event,
      FamilyProvider familyProvider, ErrorCollector errorCollector) {
    session.sawBucketsItem = true;
    String network = event.network;
    if (!session.discoveredNetworks.add(network)) {
      session.duplicateNetworks.add(network);
      recordError(session, IssueKind.DUPLICATE_NETWORK, errorCollector, EVENTS_DETAIL_PREFIX,
          format("Duplicate discovered network %s", network));
    }
    try {
      familyProvider.validateNetwork(network);
    } catch (Exception e) {
      recordError(session, IssueKind.INVALID_NETWORK, errorCollector, EVENTS_DETAIL_PREFIX,
          format("Invalid network %s: %s", network, e.getMessage()));
    }
    if (event.system != null) {
      recordError(session, IssueKind.UNEXPECTED_BUCKETS_PAYLOAD, errorCollector,
          EVENTS_DETAIL_PREFIX, format("Unexpected system block in buckets scan for network %s",
              network));
    }
    if (event.refs != null) {
      recordError(session, IssueKind.UNEXPECTED_BUCKETS_PAYLOAD, errorCollector,
          EVENTS_DETAIL_PREFIX, format("Unexpected refs block in buckets scan for network %s",
              network));
    }
    validateDiscoveredFamilies(session, event.families, errorCollector);
  }

  private void validateDeviceItem(DiscoveryScanSession session, DiscoveryEvents event,
      FamilyProvider familyProvider, ErrorCollector errorCollector) {
    session.sawEntriesItem = true;
    String addr = event.addr;
    if (!session.discoveredAddrs.add(addr)) {
      session.duplicateAddrs.add(addr);
      recordError(session, IssueKind.DUPLICATE_ADDR, errorCollector, EVENTS_DETAIL_PREFIX,
          format("Duplicate discovered addr %s", addr));
    }
    try {
      familyProvider.validateAddr(addr);
    } catch (Exception e) {
      recordError(session, IssueKind.INVALID_ADDR, errorCollector, EVENTS_DETAIL_PREFIX,
          format("Invalid addr %s: %s", addr, e.getMessage()));
    }

    if (event.network != null) {
      session.discoveredNetworks.add(event.network);
      try {
        familyProvider.validateNetwork(event.network);
      } catch (Exception e) {
        recordError(session, IssueKind.INVALID_NETWORK, errorCollector, EVENTS_DETAIL_PREFIX,
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
          recordError(session, IssueKind.INVALID_REF, errorCollector, EVENTS_DETAIL_PREFIX,
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
        recordError(session, IssueKind.UNKNOWN_DISCOVERED_FAMILY, errorCollector,
            EVENTS_DETAIL_PREFIX, format("Unknown family %s in discovered families map", famKey));
        continue;
      }
      if (famDiscovery == null) {
        continue;
      }
      if (famDiscovery.addr != null) {
        try {
          famProvider.validateAddr(famDiscovery.addr);
        } catch (Exception e) {
          recordError(session, IssueKind.INVALID_FAMILY_ENDPOINT, errorCollector,
              EVENTS_DETAIL_PREFIX,
              format("Invalid %s family addr %s: %s", famKey, famDiscovery.addr, e.getMessage()));
        }
      }
      if (famDiscovery.port != null
          && (famDiscovery.port < 0 || famDiscovery.port > MAX_PORT_VALUE)) {
        recordError(session, IssueKind.INVALID_FAMILY_ENDPOINT, errorCollector,
            EVENTS_DETAIL_PREFIX, format("Invalid %s family port %d", famKey, famDiscovery.port));
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
      recordError(session, IssueKind.DEVICE_NETWORK_MISMATCH, errorCollector, EVENTS_DETAIL_PREFIX,
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
            recordError(session, IssueKind.DEVICE_FAMILY_ADDR_MISMATCH, errorCollector,
                EVENTS_DETAIL_PREFIX,
                format("Device %s iot family addr mismatch: expected %s, got %s",
                    matchedDeviceId, matchedDeviceId, discoveredFamAddr));
          }
        } else {
          String expectedFamAddr = catchToNull(
              () -> metadata.localnet.families.get(famKey).addr);
          if (expectedFamAddr != null && !Objects.equals(expectedFamAddr, discoveredFamAddr)) {
            recordError(session, IssueKind.DEVICE_FAMILY_ADDR_MISMATCH, errorCollector,
                EVENTS_DETAIL_PREFIX,
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
        recordError(session, IssueKind.DEVICE_REFS_MISMATCH, errorCollector, EVENTS_DETAIL_PREFIX,
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
      recordError(session, IssueKind.DEVICE_SYSTEM_NAME_MISMATCH, errorCollector,
          EVENTS_DETAIL_PREFIX, format("Device %s system.name mismatch: expected %s, got %s",
              matchedDeviceId, expectedName, system.name));
    }
    String expectedSerial = catchToNull(() -> metadata.system.serial_no);
    if (!Objects.equals(expectedSerial, system.serial_no)) {
      recordError(session, IssueKind.DEVICE_SYSTEM_SERIAL_MISMATCH, errorCollector,
          EVENTS_DETAIL_PREFIX, format("Device %s system.serial_no mismatch: expected %s, got %s",
              matchedDeviceId, expectedSerial, system.serial_no));
    }
    String expectedMake = catchToNull(() -> metadata.system.hardware.make);
    String actualMake = catchToNull(() -> system.hardware.make);
    if (!Objects.equals(expectedMake, actualMake)) {
      recordError(session, IssueKind.DEVICE_SYSTEM_MAKE_MISMATCH, errorCollector,
          EVENTS_DETAIL_PREFIX,
          format("Device %s system.hardware.make mismatch: expected %s, got %s",
              matchedDeviceId, expectedMake, actualMake));
    }
    String expectedModel = catchToNull(() -> metadata.system.hardware.model);
    String actualModel = catchToNull(() -> system.hardware.model);
    if (!Objects.equals(expectedModel, actualModel)) {
      recordError(session, IssueKind.DEVICE_SYSTEM_MODEL_MISMATCH, errorCollector,
          EVENTS_DETAIL_PREFIX,
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
      recordError(session, IssueKind.MISSING_START_MARKER, errorCollector, detailPrefix,
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
      recordError(session, IssueKind.MISSING_EVENT_NOS, errorCollector, detailPrefix,
          format("Missing discovery event_no values: %s", session.missingEventNos));
    }

    if (session.endMarkerEventNo == null) {
      session.incomplete = atFinalization;
      recordError(session, IssueKind.MISSING_END_MARKER, errorCollector, detailPrefix,
          "Missing discovery end marker (event_no<0)");
    } else {
      int expectedEndMarker = -(session.rawItemCount + 1);
      if (session.endMarkerEventNo != expectedEndMarker) {
        recordError(session, IssueKind.END_MARKER_MISMATCH, errorCollector, detailPrefix,
            format("Discovery end marker mismatch: expected %d, got %d",
                expectedEndMarker, session.endMarkerEventNo));
      }
    }

    if (session.stateObserved) {
      if (session.latestPhase != Phase.STOPPED) {
        session.incomplete = atFinalization;
        recordError(session, IssueKind.STATE_NOT_STOPPED, errorCollector, STATE_DETAIL_PREFIX,
            format("Discovery state did not reach stopped phase (last phase: %s)",
                session.latestPhase == null ? "null" : session.latestPhase.value()));
      }
      if (session.stateActiveCount == null) {
        recordWarning(session, WarningKind.STATE_MISSING_ACTIVE_COUNT);
      } else if (session.stateActiveCount != session.rawItemCount) {
        recordError(session, IssueKind.STATE_ACTIVE_COUNT_MISMATCH, errorCollector,
            STATE_DETAIL_PREFIX,
            format("Discovery state active_count mismatch: expected %d, got %d",
                session.rawItemCount, session.stateActiveCount));
      }
    } else {
      recordWarning(session, WarningKind.STATE_NOT_OBSERVED);
    }

    if (session.sawDetailsRefs) {
      for (Entry<String, Set<String>> entry : session.matchedDevicesWithoutRefs.entrySet()) {
        recordError(session, IssueKind.DEVICE_REFS_MISMATCH, errorCollector, detailPrefix,
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
        recordWarning(session, WarningKind.MISSING_NETWORKS);
      }
      Set<String> extraNetworks = new TreeSet<>(session.discoveredNetworks);
      extraNetworks.removeAll(expectedNetworks);
      if (!extraNetworks.isEmpty()) {
        recordWarning(session, WarningKind.EXTRA_NETWORKS);
      }
      return;
    }

    Set<String> missingDevices = findSiteDevicesForFamily(session.key.family);
    missingDevices.removeAll(session.matchedDevices);
    session.missingDeviceCount = missingDevices.size();
    if (!missingDevices.isEmpty()) {
      recordWarning(session, WarningKind.MISSING_DEVICES);
    }
    if (!session.extraAddrs.isEmpty()) {
      recordWarning(session, WarningKind.EXTRA_ADDRS);
    }
  }

  private void logScanSummary(DiscoveryScanSession session) {
    if (outputLogger == null) {
      return;
    }
    String resolved = resolveStatus(session);
    String status = resolved == null ? "PASSED" : resolved;
    String depth = ofNullable(inferDepth(session)).map(Depth::value).orElse("unknown");
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
        session.key.scannerDeviceId, session.key.family, session.key.generationIso, status,
        depth, session.rawItemCount, markersDesc, stateDesc,
        session.matchedDevices.size(), session.matchedDevices,
        session.missingDeviceCount, session.extraAddrs.size(),
        session.errors.size(), session.warnings.size());
    if (!session.errors.isEmpty()) {
      outputLogger.error("%s", line);
      session.errors.forEach(err -> outputLogger.error("  [error] %s", err));
    } else if (!session.warnings.isEmpty()) {
      outputLogger.warn(line);
    } else {
      outputLogger.notice(line);
    }
    session.warnings.forEach(warn -> outputLogger.warn("  [warning] " + warn));
  }

  private DiscoveryScanSummary buildScanSummary(DiscoveryScanSession session) {
    DiscoveryScanSummary summary = new DiscoveryScanSummary();
    summary.scanner_device_id = session.key.scannerDeviceId;
    summary.family = session.key.family;
    summary.generation = getDate(session.key.generationIso);
    summary.status = resolveStatus(session);
    summary.depth = inferDepth(session);
    summary.active_count = session.rawItemCount;
    summary.missing_event_nos = emptyToNull(session.missingEventNos);
    summary.duplicate_event_nos = emptyToNull(session.duplicateEventNos);
    summary.duplicate_addrs = emptyToNull(session.duplicateAddrs);
    summary.duplicate_networks = emptyToNull(session.duplicateNetworks);
    summary.extra_addrs = emptyToNull(session.extraAddrs);
    summary.errors = emptyToNull(session.errors);
    summary.warnings = emptyToNull(session.warnings);
    return summary;
  }

  private static <T> List<T> emptyToNull(Collection<T> items) {
    return items == null || items.isEmpty() ? null : new ArrayList<>(items);
  }

  private String resolveStatus(DiscoveryScanSession session) {
    if (!session.closed) {
      return STATUS_IN_PROGRESS;
    }
    if (session.incomplete) {
      return STATUS_INCOMPLETE;
    }
    if (session.hasStructuralErrors) {
      return STATUS_FAILED;
    }
    if (!session.errors.isEmpty()) {
      return STATUS_WITH_ERRORS;
    }
    if (!session.warnings.isEmpty()) {
      return STATUS_WITH_WARNINGS;
    }
    return null;
  }

  private Depth inferDepth(DiscoveryScanSession session) {
    if (session.sawDetailsRefs) {
      return Depth.DETAILS;
    }
    if (session.sawSystemBlock) {
      return Depth.SYSTEM;
    }
    if (session.sawEntriesItem) {
      return Depth.ENTRIES;
    }
    if (session.sawBucketsItem) {
      return Depth.BUCKETS;
    }
    return null;
  }

  private void recordError(DiscoveryScanSession session, IssueKind kind,
      ErrorCollector errorCollector, String detailPrefix, String message) {
    if (kind.structural) {
      session.hasStructuralErrors = true;
    }
    session.errors.add(kind.summaryDescription);
    if (errorCollector != null) {
      errorCollector.addMessageError(new ValidationException(message),
          Category.VALIDATION_DEVICE_CONTENT, format("%s: %s", detailPrefix, message));
    }
  }

  private void recordWarning(DiscoveryScanSession session, WarningKind kind) {
    session.warnings.add(kind.summaryDescription);
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

  private enum IssueKind {
    MISSING_START_MARKER(true, "Missing discovery start marker (event_no=0)"),
    DUPLICATE_START_MARKER(true, "Duplicate discovery start marker"),
    INVALID_START_MARKER(true, "Start marker must not specify addr or network"),
    MISSING_END_MARKER(true, "Missing discovery end marker (event_no<0)"),
    DUPLICATE_END_MARKER(true, "Duplicate discovery end marker"),
    INVALID_END_MARKER(true, "End marker must not specify addr or network"),
    MISSING_EVENT_NO(true, "Some active discovery events are missing event_no"),
    MISSING_EVENT_NOS(true, "Missing discovery event_no values in sequence"),
    DUPLICATE_EVENT_NO(true, "Some active discovery events have duplicate event_no"),
    END_MARKER_MISMATCH(true, "Discovery end marker count mismatch"),
    LATE_EVENT(true, "Some discovery events arrived after scan closed"),
    STATE_NOT_STOPPED(true, "Discovery state did not reach stopped phase"),
    STATE_ACTIVE_COUNT_MISMATCH(true, "Discovery state active_count mismatch"),
    STATE_ERROR_STATUS(true, "Discovery state reported error status"),
    MISSING_ADDR_AND_NETWORK(false, "Some events are missing both addr and network"),
    EVENT_ERROR_STATUS(false, "Some discovery events reported error status"),
    DUPLICATE_ADDR(false, "Some discovered addresses were reported more than once"),
    DUPLICATE_NETWORK(false, "Some discovered networks were reported more than once"),
    INVALID_ADDR(false, "Some discovered addresses are invalid"),
    INVALID_NETWORK(false, "Some discovered networks are invalid"),
    INVALID_REF(false, "Some discovered point refs are invalid"),
    UNKNOWN_DISCOVERED_FAMILY(false, "Some discovered entries reference an unknown family"),
    INVALID_FAMILY_ENDPOINT(false, "Some discovered family endpoints are invalid"),
    UNEXPECTED_BUCKETS_PAYLOAD(false,
        "Some buckets scan events contain unexpected system or refs blocks"),
    DEVICE_NETWORK_MISMATCH(false, "Some devices have network mismatch"),
    DEVICE_FAMILY_ADDR_MISMATCH(false, "Some devices have family addr mismatch"),
    DEVICE_REFS_MISMATCH(false, "Some devices have refs mismatch"),
    DEVICE_SYSTEM_NAME_MISMATCH(false, "Some devices have system.name mismatch"),
    DEVICE_SYSTEM_SERIAL_MISMATCH(false, "Some devices have system.serial_no mismatch"),
    DEVICE_SYSTEM_MAKE_MISMATCH(false, "Some devices have system.hardware.make mismatch"),
    DEVICE_SYSTEM_MODEL_MISMATCH(false, "Some devices have system.hardware.model mismatch");

    private final boolean structural;
    private final String summaryDescription;

    IssueKind(boolean structural, String summaryDescription) {
      this.structural = structural;
      this.summaryDescription = summaryDescription;
    }
  }

  private enum WarningKind {
    STATE_MISSING_ACTIVE_COUNT("Discovery state did not report active_count"),
    STATE_NOT_OBSERVED("DiscoveryState not observed for active scan"),
    MISSING_NETWORKS("Some site model networks were not discovered"),
    EXTRA_NETWORKS("Some discovered networks are not in site model"),
    MISSING_DEVICES("Some site model devices were not discovered"),
    EXTRA_ADDRS("Some discovered addresses are not in site model");

    private final String summaryDescription;

    WarningKind(String summaryDescription) {
      this.summaryDescription = summaryDescription;
    }
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
    private final Set<String> extraAddrs = new TreeSet<>();
    private final Map<String, Set<String>> matchedDevicesWithoutRefs = new TreeMap<>();
    private final List<String> statePhases = new ArrayList<>();
    private final Set<String> errors = new LinkedHashSet<>();
    private final Set<String> warnings = new LinkedHashSet<>();

    private boolean activated;
    private boolean closed;
    private boolean graceScheduled;
    private boolean incomplete;
    private boolean hasStructuralErrors;
    private boolean startMarkerSeen;
    private Integer endMarkerEventNo;
    private int rawItemCount;
    private int missingDeviceCount;
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
}
