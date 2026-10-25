Feature: WebSocket session disconnect lifecycle
  The production router owns every accepted connection through disconnect
  and completed resource cleanup.

  Scenario: A normal client close runs disconnect once with its close code
    Given a live Falcon ASGI WebSocket server
    When the client closes the session with code 4000
    Then disconnect cleanup is recorded once with code 4000

  Scenario: A server close runs disconnect once with its close code
    Given a live Falcon ASGI WebSocket server
    When the connection manager closes the session with code 4002
    Then the client observes close code 4002
    And disconnect cleanup is recorded once

  Scenario: A handler close runs disconnect once with its close code
    Given a live Falcon ASGI WebSocket server
    When the handler closes the session
    Then the client observes close code 4001
    And disconnect cleanup is recorded once with code 4001

  Scenario: A send after handler closure preserves the disconnect code
    Given a live Falcon ASGI WebSocket server
    When the handler sends after closing the session
    Then the client observes close code 4001
    And disconnect cleanup is recorded once with code 4001

  Scenario: Messages are handled before a client close removes membership
    Given a live Falcon ASGI WebSocket server
    When the client exchanges messages and closes the session
    Then all replies arrive and the manager has no connection
    And disconnect cleanup is recorded once with code 4000

  Scenario: A handler failure still runs disconnect cleanup
    Given a live Falcon ASGI WebSocket server
    When a handler raises an unexpected error
    Then the client observes close code 1011
    And disconnect cleanup is recorded once with code 1011
    And the original handler error is captured and membership is removed

  Scenario: Stopping the server closes an idle accepted session
    Given a live Falcon ASGI WebSocket server
    When the server stops with an idle accepted session
    Then disconnect cleanup records the close code sent by Uvicorn

  Scenario: Stopping the server cancels a blocked handler safely
    Given a live Falcon ASGI WebSocket server
    When the server stops while a handler is blocked
    Then disconnect cleanup is recorded once with code 1006
    And the manager has no connection

  Scenario: A rejected handshake does not run disconnect cleanup
    Given a live Falcon ASGI WebSocket server
    When the client attempts a rejected handshake
    Then the handshake is rejected without disconnect cleanup

  Scenario: Rejecting by close does not run disconnect cleanup
    Given a live Falcon ASGI WebSocket server
    When the resource closes the handshake without accepting
    Then the handshake is rejected without disconnect cleanup

  Scenario: Acceptance followed immediately by close still runs cleanup
    Given a live Falcon ASGI WebSocket server
    When the resource accepts and closes during connection setup
    Then the client observes close code 4003
    And disconnect cleanup is recorded once with code 4003
    And the manager has no connection

  Scenario: Non-JSON text reaches the unhandled fallback unchanged
    Given a live Falcon ASGI WebSocket server
    When the client sends text outside a JSON envelope
    Then disconnect cleanup is recorded once with code 4000
    And the manager has no connection

  Scenario: Native text frames retain their original JSON formatting
    Given a live Falcon ASGI WebSocket server
    When the client sends JSON text with its original formatting
    Then disconnect cleanup is recorded once with code 4000

  Scenario: JSON binary frames reach the resource unchanged
    Given a live Falcon ASGI WebSocket server
    When the client sends a JSON binary frame
    Then disconnect cleanup is recorded once with code 4000

  Scenario: Failed close validation leaves the accepted connection usable
    Given a live Falcon ASGI WebSocket server
    When the manager attempts a reserved close code and then exchanges a message
    Then disconnect cleanup is recorded once with code 4000
    And the manager has no connection

  Scenario: Native Falcon media keyword arguments remain usable
    Given a live Falcon ASGI WebSocket server
    When the manager sends media using Falcon keyword arguments
    Then disconnect cleanup is recorded once with code 4000
