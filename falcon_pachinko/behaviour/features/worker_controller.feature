Feature: Worker lifecycle management

  Scenario: Run a background worker
    Given a logging worker
    When the worker controller starts and then stops it
    Then the log should contain at least one entry

  Scenario: Roll back partial startup and permit retry
    Given a logging worker and a failing worker factory
    When both workers are started
    Then startup propagates the original error and leaves no worker running
    When the logging worker is restarted
    Then the logging worker runs after retry
