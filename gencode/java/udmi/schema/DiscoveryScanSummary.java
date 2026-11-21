
package udmi.schema;

import java.util.Date;
import java.util.List;
import com.fasterxml.jackson.annotation.JsonInclude;
import com.fasterxml.jackson.annotation.JsonProperty;
import com.fasterxml.jackson.annotation.JsonPropertyDescription;
import com.fasterxml.jackson.annotation.JsonPropertyOrder;
import udmi.schema.Enumerations.Depth;


/**
 * Discovery Scan Summary
 * <p>
 * Summary of a single active [discovery](../docs/specs/discovery.md) scan generation, reporting unique identifiers and deviations from expected behavior
 * 
 */
@JsonInclude(JsonInclude.Include.NON_NULL)
public class DiscoveryScanSummary {

    /**
     * Device ID of the scanner publishing the discovery scan
     * (Required)
     * 
     */
    @JsonProperty("scanner_device_id")
    @JsonPropertyDescription("Device ID of the scanner publishing the discovery scan")
    public String scanner_device_id;
    /**
     * Protocol address family scanned
     * (Required)
     * 
     */
    @JsonProperty("family")
    @JsonPropertyDescription("Protocol address family scanned")
    public String family;
    /**
     * Generational timestamp identifying the active discovery scan
     * (Required)
     * 
     */
    @JsonProperty("generation")
    @JsonPropertyDescription("Generational timestamp identifying the active discovery scan")
    public Date generation;
    /**
     * Number of positive active discovery event items received for this scan
     * (Required)
     * 
     */
    @JsonProperty("active_count")
    @JsonPropertyDescription("Number of positive active discovery event items received for this scan")
    public Integer active_count;
    /**
     * Inferred enumeration depth from observed event fields; omitted when unknown
     * 
     */
    @JsonProperty("depth")
    @JsonPropertyDescription("Inferred enumeration depth from observed event fields; omitted when unknown")
    public Depth depth;
    /**
     * Scan validation outcome; omitted when the scan passed with no errors or warnings
     * 
     */
    @JsonProperty("status")
    @JsonPropertyDescription("Scan validation outcome; omitted when the scan passed with no errors or warnings")
    public String status;
    /**
     * Expected positive sequence numbers missing from the received event stream
     * 
     */
    @JsonProperty("missing_event_nos")
    @JsonPropertyDescription("Expected positive sequence numbers missing from the received event stream")
    public List<Integer> missing_event_nos;
    /**
     * Positive sequence numbers received more than once
     * 
     */
    @JsonProperty("duplicate_event_nos")
    @JsonPropertyDescription("Positive sequence numbers received more than once")
    public List<Integer> duplicate_event_nos;
    /**
     * Family addresses reported multiple times within the same scan
     * 
     */
    @JsonProperty("duplicate_addrs")
    @JsonPropertyDescription("Family addresses reported multiple times within the same scan")
    public List<String> duplicate_addrs;
    /**
     * Networks reported multiple times within the same buckets scan
     * 
     */
    @JsonProperty("duplicate_networks")
    @JsonPropertyDescription("Networks reported multiple times within the same buckets scan")
    public List<String> duplicate_networks;
    /**
     * Discovered family addresses not matching any modeled site device
     * 
     */
    @JsonProperty("extra_addrs")
    @JsonPropertyDescription("Discovered family addresses not matching any modeled site device")
    public List<String> extra_addrs;
    /**
     * Validation errors recorded for this scan session
     * 
     */
    @JsonProperty("errors")
    @JsonPropertyDescription("Validation errors recorded for this scan session")
    public List<String> errors;
    /**
     * Validation warnings recorded for this scan session
     * 
     */
    @JsonProperty("warnings")
    @JsonPropertyDescription("Validation warnings recorded for this scan session")
    public List<String> warnings;

    @Override
    public int hashCode() {
        int result = 1;
        result = ((result* 31)+((this.scanner_device_id == null)? 0 :this.scanner_device_id.hashCode()));
        result = ((result* 31)+((this.generation == null)? 0 :this.generation.hashCode()));
        result = ((result* 31)+((this.missing_event_nos == null)? 0 :this.missing_event_nos.hashCode()));
        result = ((result* 31)+((this.duplicate_event_nos == null)? 0 :this.duplicate_event_nos.hashCode()));
        result = ((result* 31)+((this.duplicate_networks == null)? 0 :this.duplicate_networks.hashCode()));
        result = ((result* 31)+((this.warnings == null)? 0 :this.warnings.hashCode()));
        result = ((result* 31)+((this.active_count == null)? 0 :this.active_count.hashCode()));
        result = ((result* 31)+((this.depth == null)? 0 :this.depth.hashCode()));
        result = ((result* 31)+((this.duplicate_addrs == null)? 0 :this.duplicate_addrs.hashCode()));
        result = ((result* 31)+((this.family == null)? 0 :this.family.hashCode()));
        result = ((result* 31)+((this.extra_addrs == null)? 0 :this.extra_addrs.hashCode()));
        result = ((result* 31)+((this.errors == null)? 0 :this.errors.hashCode()));
        result = ((result* 31)+((this.status == null)? 0 :this.status.hashCode()));
        return result;
    }

    @Override
    public boolean equals(Object other) {
        if (other == this) {
            return true;
        }
        if ((other instanceof DiscoveryScanSummary) == false) {
            return false;
        }
        DiscoveryScanSummary rhs = ((DiscoveryScanSummary) other);
        return ((((((((((((((this.scanner_device_id == rhs.scanner_device_id)||((this.scanner_device_id!= null)&&this.scanner_device_id.equals(rhs.scanner_device_id)))&&((this.generation == rhs.generation)||((this.generation!= null)&&this.generation.equals(rhs.generation))))&&((this.missing_event_nos == rhs.missing_event_nos)||((this.missing_event_nos!= null)&&this.missing_event_nos.equals(rhs.missing_event_nos))))&&((this.duplicate_event_nos == rhs.duplicate_event_nos)||((this.duplicate_event_nos!= null)&&this.duplicate_event_nos.equals(rhs.duplicate_event_nos))))&&((this.duplicate_networks == rhs.duplicate_networks)||((this.duplicate_networks!= null)&&this.duplicate_networks.equals(rhs.duplicate_networks))))&&((this.warnings == rhs.warnings)||((this.warnings!= null)&&this.warnings.equals(rhs.warnings))))&&((this.active_count == rhs.active_count)||((this.active_count!= null)&&this.active_count.equals(rhs.active_count))))&&((this.depth == rhs.depth)||((this.depth!= null)&&this.depth.equals(rhs.depth))))&&((this.duplicate_addrs == rhs.duplicate_addrs)||((this.duplicate_addrs!= null)&&this.duplicate_addrs.equals(rhs.duplicate_addrs))))&&((this.family == rhs.family)||((this.family!= null)&&this.family.equals(rhs.family))))&&((this.extra_addrs == rhs.extra_addrs)||((this.extra_addrs!= null)&&this.extra_addrs.equals(rhs.extra_addrs))))&&((this.errors == rhs.errors)||((this.errors!= null)&&this.errors.equals(rhs.errors))))&&((this.status == rhs.status)||((this.status!= null)&&this.status.equals(rhs.status))));
    }

}
