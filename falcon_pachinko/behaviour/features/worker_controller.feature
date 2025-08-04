Feature: Worker lifecycle management

  Scenario: Run a background worker
    Given a logging worker
    When the worker controller starts and then stops it
    Then the log should contain at least one entry
