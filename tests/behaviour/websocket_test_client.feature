Feature: WebSocket test client

  Scenario: round-trip JSON payload with trace logging
    Given a running websocket echo service
    When the test client sends a JSON payload to "/echo"
    Then the server records the handshake metadata
    And the client observes the echoed payload
    And the session trace records the frames

  Scenario: malformed authentication frames stay out of diagnostics
    Given a running websocket echo service
    When the client receives malformed authentication text and binary frames
    Then the diagnostic output omits the authentication canary
    And the server and trace retain the original frames
