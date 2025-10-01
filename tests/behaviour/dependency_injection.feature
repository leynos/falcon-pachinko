Feature: Router-level dependency injection

  Scenario: route resources are constructed through the configured factory
    Given a router configured with a resource factory injecting service "svc"
    When a websocket connection targets "/rooms/alpha/child/beta"
    Then the parent resource receives the "svc" dependency
    And the child resource receives the "svc" dependency
    And the connection attempt is rejected
