Feature: Custom connection manager backends

  Scenario: broadcast through a custom backend
    Given a connection manager configured with a recording backend
    When a message is broadcast to room "crew" via the manager
    Then the backend records the broadcast snapshot
    And the websocket receives the broadcast payload
