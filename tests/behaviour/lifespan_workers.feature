Feature: Lifespan-based worker management

  Scenario: worker runs during lifespan
    Given an app with a lifespan-managed worker
    When the app lifespan is executed
    Then the worker has run
    And the worker stops after the lifespan ends
