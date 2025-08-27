Feature: WebSocket connection manager broadcasting

  Scenario: broadcast message to all connections in a room
    Given a connection manager with two connections in room "lobby"
    When a message is broadcast to room "lobby"
    Then both connections receive that message

    Scenario: broadcast message to a room with one connection excluded
      Given a connection manager with two connections in room "lobby"
      When a message is broadcast to room "lobby" excluding connection "a"
      Then only connection "b" receives that message

    Scenario: iterate over connections in a room
      Given a connection manager with two connections in room "lobby"
      When we iterate over connections in room "lobby"
      Then both connections are yielded
