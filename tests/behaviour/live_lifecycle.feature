Feature: WebSocket acceptance and accepted-session lifecycle
  The router accepts a negotiated connection once, then owns its active
  session and disconnect cleanup.

  Scenario: Missing request metadata rejects before acceptance
    Given a live Falcon ASGI WebSocket server
    When the client attempts a missing-header rejection
    Then the upgrade is rejected without an accepted session

  Scenario: Request metadata can approve acceptance
    Given a live Falcon ASGI WebSocket server
    When the client provides the required request metadata
    Then the live transport records one acceptance

  Scenario: Successful negotiation accepts exactly once
    Given a live Falcon ASGI WebSocket server
    When the client opens and closes an accepted session
    Then the live transport records one acceptance

  Scenario: Deprecated resource acceptance remains single-call compatible
    Given a live Falcon ASGI WebSocket server
    When the client opens a deprecated-accept session
    Then the live transport records one acceptance

  Scenario: Post-accept setup exchanges a hello frame
    Given a live Falcon ASGI WebSocket server
    When the client completes the post-accept hello exchange
    Then both session-start frames arrive over the live connection

  Scenario: Native media receive retains Falcon decoding
    Given a live Falcon ASGI WebSocket server
    When the client sends JSON during post-accept setup
    Then the application receives decoded JSON media

  Scenario: Session-start failure closes and removes registration
    Given a live Falcon ASGI WebSocket server
    When post-accept setup fails after manager registration
    Then the live session closes with 1011 and finalizes once

  Scenario: Nested hooks follow the documented lifecycle order
    Given a live Falcon ASGI WebSocket server
    When the client exchanges one frame on a nested route and closes normally
    Then the live hook events match the documented order
