Feature: Simulator-backed router fixture

  Scenario: Simulated harness handles lifecycle
    Given a websocket simulator harness
    And the next connection is seeded with {"type": "ping"}
    When we connect to "/echo"
    Then the resource should receive the queued payload
    And the simulator helper exposes the ack frame
    And the connection is closed by the fixture
