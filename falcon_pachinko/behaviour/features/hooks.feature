Feature: Hook execution order
  Scenario: Global and resource hooks wrap lifecycle
    Given a router with multi-tier hooks
    When a client connects and sends a message
    Then the hook log should show layered connect order
    And the hook log should show layered receive order
    And the child resource records hook-injected params

  Scenario: Router global hooks fire without resource hooks
    Given a router with only global hooks
    When a client connects and sends a message
    Then only global hooks are recorded

  Scenario: Errors propagate through after hooks
    Given a router with multi-tier hooks
    When a client connects and sends a message that triggers an error
    Then the error is propagated to after_receive hook and the hook chain remains intact
