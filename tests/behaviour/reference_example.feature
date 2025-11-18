Feature: Full reference example workflow
  The comprehensive example application should exercise the advanced
  features provided by the router, including dependency injection,
  schema-driven dispatch, hooks, and connection state management.

  Scenario: Task creation flows through the router, schema dispatch, and feed
    Given the reference router with a recording factory
    When a client connects to "/ws/workspaces/atlas/projects/triage/tasks" using token "seekrit" as user "casey"
    And they send a "task.add" message for task "T-42"
    Then the connection is accepted
    And the task stream resource replies with a task acknowledgement
    And the announcement feed captures an event for workspace "atlas"
