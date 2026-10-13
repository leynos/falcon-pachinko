Feature: Reference application production WebSocket path
  The reference application handles authenticated nested routes, messages,
  worker broadcasts, and disconnect cleanup through Falcon's live lifecycle.

  Scenario: authenticated task creation and workspace announcement
    Given the reference application is running on a live Falcon ASGI server
    When a client connects to "/ws/workspaces/atlas/projects/triage/tasks" with workspace token "seekrit" and user "casey"
    Then the client receives session.ready for workspace "atlas" and project "triage"
    And the connection manager registers the client in workspace "atlas"
    When the client sends task.add for task "T-42"
    Then the client receives task.added for task "T-42"
    And the worker broadcasts task_added to the client
    When the client disconnects with code 1000
    Then the connection manager removes the client
    And the reference hooks and services record this connection's full journey

  Scenario: invalid workspace token is rejected during the handshake
    Given the reference application is running on a live Falcon ASGI server
    When a client connects to "/ws/workspaces/atlas/projects/triage/tasks" with workspace token "wrong" and user "casey"
    Then the WebSocket handshake fails with status 403
    And the reference audit trail records invalid_token
