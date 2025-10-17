Feature: WebSocket test client

  Scenario: round-trip JSON payload with trace logging
    Given a running websocket echo service
    When the test client sends a JSON payload to "/echo"
    Then the server records the handshake metadata
    And the client observes the echoed payload
    And the session trace records the frames
