Feature: Nested resource composition

  Scenario: Connect to nested child resource
    Given a router with a nested child resource
    When a client connects to "/parents/42/child"
    Then the child resource should receive params {"pid": "42"}

  Scenario: Unmatched nested path returns 404
    Given a router with a nested child resource
    When a client connects to "/parents/42/missing"
    Then HTTPNotFound should be raised

  Scenario: Connect to deeply nested grandchild
    Given a router with a nested child resource
    When a client connects to "/parents/42/child/99/grandchild"
    Then the grandchild resource should capture params {"pid": "42", "cid": "99"}

  Scenario: Parameter shadowing overrides parent value
    Given a router with parameter shadowing resources
    When a client connects to "/shadow/1/2"
    Then the shadow child resource should capture params {"pid": "2"}

  Scenario: Parent passes context to child resource
    Given a router with context-passing resources
    When a client connects to "/ctx/child"
    Then the context child resource should receive project "acme"
    And the shared state should contain flags from both resources
