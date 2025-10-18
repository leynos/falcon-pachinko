Feature: WebSocket simulator injection
  Scenario: router injects simulator connections
    Given a router configured with a simulator factory
    And the simulator has a queued message {"type": "ping"}
    When a websocket connection targets "/echo"
    Then the resource receives the simulator instance
    And the simulator records the acknowledged message
    And the simulator closes the connection
