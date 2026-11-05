
package udmi.schema;

import com.fasterxml.jackson.annotation.JsonInclude;
import com.fasterxml.jackson.annotation.JsonProperty;
import com.fasterxml.jackson.annotation.JsonPropertyDescription;
import com.fasterxml.jackson.annotation.JsonPropertyOrder;


/**
 * Alarm Notifs Events
 * <p>
 * Alarm-specific properties for a notification event (such as BACnet alarm parameters).
 * 
 */
@JsonInclude(JsonInclude.Include.NON_NULL)
public class AlarmNotifsEvents {

    /**
     * Alarm priority level (e.g. BACnet Notification Class priority)
     * 
     */
    @JsonProperty("priority")
    @JsonPropertyDescription("Alarm priority level (e.g. BACnet Notification Class priority)")
    public Integer priority;
    /**
     * Alarm notification class identifier governing routing and acknowledgment behavior
     * 
     */
    @JsonProperty("notification_class")
    @JsonPropertyDescription("Alarm notification class identifier governing routing and acknowledgment behavior")
    public Integer notification_class;
    /**
     * Alarm algorithm or event classification (e.g. out_of_range, change_of_state, floating_limit, command_failure)
     * 
     */
    @JsonProperty("event_type")
    @JsonPropertyDescription("Alarm algorithm or event classification (e.g. out_of_range, change_of_state, floating_limit, command_failure)")
    public String event_type;
    /**
     * Indicates whether operator acknowledgment is required for this alarm transition
     * 
     */
    @JsonProperty("ack_required")
    @JsonPropertyDescription("Indicates whether operator acknowledgment is required for this alarm transition")
    public Boolean ack_required;
    /**
     * Descriptive alarm text or operator message emitted with the alarm event
     * 
     */
    @JsonProperty("message")
    @JsonPropertyDescription("Descriptive alarm text or operator message emitted with the alarm event")
    public String message;

    @Override
    public int hashCode() {
        int result = 1;
        result = ((result* 31)+((this.notification_class == null)? 0 :this.notification_class.hashCode()));
        result = ((result* 31)+((this.event_type == null)? 0 :this.event_type.hashCode()));
        result = ((result* 31)+((this.priority == null)? 0 :this.priority.hashCode()));
        result = ((result* 31)+((this.ack_required == null)? 0 :this.ack_required.hashCode()));
        result = ((result* 31)+((this.message == null)? 0 :this.message.hashCode()));
        return result;
    }

    @Override
    public boolean equals(Object other) {
        if (other == this) {
            return true;
        }
        if ((other instanceof AlarmNotifsEvents) == false) {
            return false;
        }
        AlarmNotifsEvents rhs = ((AlarmNotifsEvents) other);
        return ((((((this.notification_class == rhs.notification_class)||((this.notification_class!= null)&&this.notification_class.equals(rhs.notification_class)))&&((this.event_type == rhs.event_type)||((this.event_type!= null)&&this.event_type.equals(rhs.event_type))))&&((this.priority == rhs.priority)||((this.priority!= null)&&this.priority.equals(rhs.priority))))&&((this.ack_required == rhs.ack_required)||((this.ack_required!= null)&&this.ack_required.equals(rhs.ack_required))))&&((this.message == rhs.message)||((this.message!= null)&&this.message.equals(rhs.message))));
    }

}
