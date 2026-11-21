
package udmi.schema;

import java.util.Date;
import java.util.List;
import com.fasterxml.jackson.annotation.JsonInclude;
import com.fasterxml.jackson.annotation.JsonProperty;
import com.fasterxml.jackson.annotation.JsonPropertyDescription;
import com.fasterxml.jackson.annotation.JsonPropertyOrder;


/**
 * Discovery Analysis
 * <p>
 * Site-level passive analysis summary of active [discovery](../docs/specs/discovery.md) scans
 * 
 */
@JsonInclude(JsonInclude.Include.NON_NULL)
public class DiscoveryAnalysis {

    /**
     * RFC 3339 UTC timestamp the discovery analysis report was generated
     * (Required)
     * 
     */
    @JsonProperty("timestamp")
    @JsonPropertyDescription("RFC 3339 UTC timestamp the discovery analysis report was generated")
    public Date timestamp;
    /**
     * Version of the UDMI schema
     * (Required)
     * 
     */
    @JsonProperty("version")
    @JsonPropertyDescription("Version of the UDMI schema")
    public String version;
    /**
     * Non-empty list of analyzed active discovery scan sessions
     * 
     */
    @JsonProperty("scans")
    @JsonPropertyDescription("Non-empty list of analyzed active discovery scan sessions")
    public List<DiscoveryScanSummary> scans;

    @Override
    public int hashCode() {
        int result = 1;
        result = ((result* 31)+((this.version == null)? 0 :this.version.hashCode()));
        result = ((result* 31)+((this.scans == null)? 0 :this.scans.hashCode()));
        result = ((result* 31)+((this.timestamp == null)? 0 :this.timestamp.hashCode()));
        return result;
    }

    @Override
    public boolean equals(Object other) {
        if (other == this) {
            return true;
        }
        if ((other instanceof DiscoveryAnalysis) == false) {
            return false;
        }
        DiscoveryAnalysis rhs = ((DiscoveryAnalysis) other);
        return ((((this.version == rhs.version)||((this.version!= null)&&this.version.equals(rhs.version)))&&((this.scans == rhs.scans)||((this.scans!= null)&&this.scans.equals(rhs.scans))))&&((this.timestamp == rhs.timestamp)||((this.timestamp!= null)&&this.timestamp.equals(rhs.timestamp))));
    }

}
