
package udmi.schema;

import java.util.Date;
import java.util.HashMap;
import com.fasterxml.jackson.annotation.JsonInclude;
import com.fasterxml.jackson.annotation.JsonProperty;
import com.fasterxml.jackson.annotation.JsonPropertyDescription;
import com.fasterxml.jackson.annotation.JsonPropertyOrder;


/**
 * Notifs Config
 * <p>
 * [Notifs Config Documentation](../docs/messages/notifs.md#config)
 * 
 */
@JsonInclude(JsonInclude.Include.NON_NULL)
public class NotifsConfig {

    /**
     * RFC 3339 UTC timestamp the configuration was generated
     * 
     */
    @JsonProperty("timestamp")
    @JsonPropertyDescription("RFC 3339 UTC timestamp the configuration was generated")
    public Date timestamp;
    /**
     * Version of the UDMI schema
     * 
     */
    @JsonProperty("version")
    @JsonPropertyDescription("Version of the UDMI schema")
    public java.lang.String version;
    /**
     * Map of notification acknowledgment entries keyed by opaque notification identifier. [Notifs documentation](../docs/messages/notifs.md)
     * 
     */
    @JsonProperty("notif_id")
    @JsonPropertyDescription("Map of notification acknowledgment entries keyed by opaque notification identifier. [Notifs documentation](../docs/messages/notifs.md)")
    public HashMap<String, NotifNotifsConfig> notif_id;

    @Override
    public int hashCode() {
        int result = 1;
        result = ((result* 31)+((this.notif_id == null)? 0 :this.notif_id.hashCode()));
        result = ((result* 31)+((this.version == null)? 0 :this.version.hashCode()));
        result = ((result* 31)+((this.timestamp == null)? 0 :this.timestamp.hashCode()));
        return result;
    }

    @Override
    public boolean equals(Object other) {
        if (other == this) {
            return true;
        }
        if ((other instanceof NotifsConfig) == false) {
            return false;
        }
        NotifsConfig rhs = ((NotifsConfig) other);
        return ((((this.notif_id == rhs.notif_id)||((this.notif_id!= null)&&this.notif_id.equals(rhs.notif_id)))&&((this.version == rhs.version)||((this.version!= null)&&this.version.equals(rhs.version))))&&((this.timestamp == rhs.timestamp)||((this.timestamp!= null)&&this.timestamp.equals(rhs.timestamp))));
    }

}
