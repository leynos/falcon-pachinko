Feature: WebSocket connection manager broadcasting

  Scenario: broadcast message to all connections in a room
    Given a connection manager with two connections in room "lobby"
    When a message is broadcast to room "lobby"
    Then both connections receive that message
