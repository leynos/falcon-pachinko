Feature: Live Falcon ASGI WebSocket server

  Scenario: client opens a WebSocket through Falcon
    Given a Falcon ASGI app mounted with the live WebSocket router
    When the client opens "/ws/socket"
    Then the WebSocket connection is open

  Scenario: server sends a frame to the client
    Given a Falcon ASGI app mounted with the live WebSocket router
    When the client opens "/ws/socket"
    Then the client receives the welcome frame

  Scenario: client frames reach application code
    Given a Falcon ASGI app mounted with the live WebSocket router
    When the client opens "/ws/socket"
    And the client records "from the client"
    Then the application records the client frame

  Scenario: multiple frames traverse one connection
    Given a Falcon ASGI app mounted with the live WebSocket router
    When the client opens "/ws/socket"
    And the client exchanges two echo frames
    Then both frames return over the same connection

  Scenario: clean client disconnect tears down the server session
    Given a Falcon ASGI app mounted with the live WebSocket router
    When the client opens "/ws/socket"
    And the client disconnects cleanly
    Then the server session closes with code 1000

  Scenario: server failure is surfaced by the live harness
    Given a Falcon ASGI app mounted with the live WebSocket router
    When the client opens "/ws/socket"
    And the client triggers a server failure
    Then the client observes close code 1011
    And the harness exposes the server exception
