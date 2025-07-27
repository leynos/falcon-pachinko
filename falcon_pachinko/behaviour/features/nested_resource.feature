Feature: Nested resource composition

  Scenario: Connect to nested child resource
    Given a router with a nested child resource
    When a client connects to "/parents/42/child"
    Then the child resource should receive params {"pid": "42"}

  Scenario: Unmatched nested path returns 404
    Given a router with a nested child resource
    When a client connects to "/parents/42/missing"
    Then HTTPNotFound should be raised
