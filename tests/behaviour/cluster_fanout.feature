Feature: Cluster fan-out with process-local socket ownership

  Scenario: A room spans two independently mounted live servers
    Given clients A and B connect to separate live servers in room lobby
    Then lobby has both node memberships
    When client A publishes the first message
    Then both clients receive the first message
    And only serializable values cross the fan-out transport
    And each server uses only its own live socket objects
    When client B disconnects
    Then lobby retains only node A
    When client A publishes the second message
    Then client A receives the second message and client B receives nothing
