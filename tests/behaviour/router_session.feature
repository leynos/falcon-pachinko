Feature: Router-owned WebSocket sessions
  The router receives and dispatches every client frame on an accepted
  connection and keeps the session active until the peer or server closes it.

  Scenario: A tagged message receives a server reply
    Given a live Falcon router session
    When the client sends an echo message
    Then the client receives the echo reply
    And the router records the dispatched message

  Scenario: Multiple frames are dispatched in order on one connection
    Given a live Falcon router session
    When the client sends three ordered messages including a binary frame
    Then the server replies in the same order
    And the router records all three messages in order

  Scenario: An unknown message leaves the connection usable
    Given a live Falcon router session
    When the client sends an unknown message followed by a known message
    Then the unknown message receives the fallback reply
    And the known message is dispatched on the same connection

  Scenario: A handler failure closes the connection and is reported
    Given a live Falcon router session
    When the client sends a message whose handler fails
    Then the client observes close code 1011
    And the router records the original handler failure and disconnect code

  Scenario: A client close reaches the disconnect lifecycle
    Given a live Falcon router session
    When the client closes with code 4008
    Then the resource receives the client close code
