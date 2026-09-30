Feature: Safe payload diagnostics
  Framework diagnostics omit payload values while trusted applications retain raw access.

  Scenario: malformed post-accept authentication frame
    Given an accepted websocket with a short client.hello token frame
    When the malformed authentication frame is decoded
    Then the token is absent from diagnostic errors and trace summaries
    And trusted raw access retains the original authentication frame

  Scenario: explicitly sanitized local sample
    Given a local diagnostic sample with nested tokens and an application key
    When an explicitly configured sanitizer formats the sample
    Then sensitive values are redacted and ordinary values remain visible
