package com.google.daq.mqtt.util;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertNotNull;
import static org.junit.Assert.assertNull;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.verifyNoInteractions;
import static org.mockito.Mockito.when;

import com.google.api.core.ApiService;
import com.google.cloud.pubsub.v1.AckReplyConsumer;
import com.google.cloud.pubsub.v1.MessageReceiver;
import com.google.cloud.pubsub.v1.Subscriber;
import com.google.cloud.pubsub.v1.SubscriptionAdminClient;
import com.google.daq.mqtt.util.MessagePublisher.QuerySpeed;
import com.google.daq.mqtt.validator.Validator.MessageBundle;
import com.google.protobuf.ByteString;
import com.google.protobuf.Timestamp;
import com.google.pubsub.v1.ProjectSubscriptionName;
import com.google.pubsub.v1.PubsubMessage;
import java.time.Instant;
import org.junit.After;
import org.junit.Before;
import org.junit.Test;
import org.mockito.ArgumentCaptor;
import org.mockito.MockedStatic;
import org.mockito.Mockito;

/**
 * Unit tests for PubSubClient subscription reset behavior.
 */
public class PubSubClientTest {

  private static final String TEST_PROJECT = "test-project";
  private static final String TEST_REGISTRY = "test-registry";
  private static final String TEST_SUB = "test-sub";

  private MockedStatic<SubscriptionAdminClient> adminClientStatic;
  private MockedStatic<Subscriber> subscriberStatic;
  private SubscriptionAdminClient mockAdminClient;
  private Subscriber mockSubscriber;
  private ArgumentCaptor<MessageReceiver> receiverCaptor;

  /**
   * Set up mocked PubSub admin and subscriber clients.
   */
  @Before
  public void setUp() {
    mockAdminClient = mock(SubscriptionAdminClient.class);
    adminClientStatic = Mockito.mockStatic(SubscriptionAdminClient.class);
    adminClientStatic.when(SubscriptionAdminClient::create).thenReturn(mockAdminClient);

    mockSubscriber = mock(Subscriber.class);
    ApiService mockService = mock(ApiService.class);
    when(mockSubscriber.startAsync()).thenReturn(mockService);
    when(mockSubscriber.stopAsync()).thenReturn(mockService);

    Subscriber.Builder mockBuilder = mock(Subscriber.Builder.class);
    receiverCaptor = ArgumentCaptor.forClass(MessageReceiver.class);
    subscriberStatic = Mockito.mockStatic(Subscriber.class);
    subscriberStatic.when(() -> Subscriber.newBuilder(
        any(ProjectSubscriptionName.class), receiverCaptor.capture())).thenReturn(mockBuilder);
    when(mockBuilder.build()).thenReturn(mockSubscriber);
  }

  /**
   * Close static Mockito hooks.
   */
  @After
  public void tearDown() {
    adminClientStatic.close();
    subscriberStatic.close();
  }

  @Test
  public void noResetSkipsSeekAndRetainsOlderMessages() {
    PubSubClient client = new PubSubClient(TEST_PROJECT, TEST_REGISTRY, TEST_SUB, null,
        true, false);
    verifyNoInteractions(mockAdminClient);

    long twoHoursAgo = Instant.now().minusSeconds(7200).getEpochSecond();
    PubsubMessage oldMessage = PubsubMessage.newBuilder()
        .setData(ByteString.copyFromUtf8("{\"version\":\"1.5.2\"}"))
        .setPublishTime(Timestamp.newBuilder().setSeconds(twoHoursAgo).setNanos(123456000).build())
        .putAttributes("deviceId", "DSN-1")
        .build();
    AckReplyConsumer mockAck = mock(AckReplyConsumer.class);

    receiverCaptor.getValue().receiveMessage(oldMessage, mockAck);
    MessageBundle bundle = client.takeNextMessage(QuerySpeed.SHORT);

    assertNotNull("Historical message must not be flushed when reset=false", bundle);
    assertEquals("DSN-1", bundle.attributes.get("deviceId"));
    assertEquals("1.5.2", bundle.message.get("version"));
    assertEquals(Instant.ofEpochSecond(twoHoursAgo, 123456000).toString(), bundle.timestamp);
    assertNull("Polling empty queue should time out cleanly",
        client.takeNextMessage(QuerySpeed.QUICK));
    assertNull("Publishing without updateTopic must be a safe no-op",
        client.publish("DSN-1", "update/query", "{}"));
    client.close();
  }
}
