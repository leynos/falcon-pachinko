# Developer Guide

This guide captures maintainer-facing conventions that are not part of the
public user guide.

## WorkerController lifecycle

`WorkerController.start()` rejects a second start while tasks remain registered
and schedules each worker with the same keyword context. If a worker factory or
task creation fails, it closes any coroutine rejected by
`asyncio.create_task()`, cancels and awaits tasks already scheduled, and resets
the controller so the caller can retry. The original startup exception remains
primary; rollback failures or cancellation are attached to it as exception
notes. Rollback continues cleanup if the caller is cancelled. The controller
does not keep an async exit stack; the application lifespan handler owns
external resources.

`_schedule_worker()` creates the worker task and closes a rejected coroutine.
`_rollback_start()` waits for `_finish_startup_rollback()` despite caller
cancellation; that helper cancels and awaits scheduled tasks, then clears state.
`_rollback_start_preserving_error()` keeps the startup exception primary and
attaches rollback failures or cancellation as notes.

During `stop()`, the controller cancels all tasks, gathers them, and selects
the first non-cancellation exception in registration order. It then clears the
task list and re-raises that exception, if present, leaving the controller
ready to restart. The controller emits neither startup logs nor metrics and
remains telemetry-agnostic. It propagates the original startup exception to the
application startup caller, leaving application-specific logging and metrics to
the lifespan owner.

## WebSocket receive-loop lifecycle

`WebSocketRouter(receive_loop=True)` opts into a router-owned session. The
default remains `False`, where the application owns later receives and
disconnect cleanup. For the opt-in path, `_session_driver.run_session()` uses
the final child resource, prepared socket, and already-bound hook manager for
the entire lifecycle:

1. Run `before_connect`, then `on_connect`, then `after_connect`.
2. Establish the cleanup promise as soon as `on_connect` approves or the
   resource accepts the socket. Router acceptance is skipped if the resource
   already accepted.
3. Receive one frame at a time and await dispatch before receiving the next.
4. On disconnect, run cleanup once and mark the session closed.

The state moves through `CONNECTING`, `ESTABLISHED`, `DISCONNECTING`, and
`CLOSED`. A falsey `on_connect` result on a never-accepted socket is a
rejection. If setup fails after approval or acceptance, the receive loop is
skipped and disconnect cleanup still runs. If approval succeeds but the socket
is never accepted, the driver skips reception and cleans up with `1011`. An
`on_connect` failure before approval or acceptance is outside the cleanup
promise; resources must roll back partial registrations from that failure.

The receive adapter preserves `str` and `bytes` from `receive_media()` and
encodes other decoded media as JSON for `WebSocketResource.dispatch()`. Native
Falcon sockets use the private raw receive seam because Falcon's public media
receivers decode or filter frames. Malformed media or a handler failure is an
unexpected session failure and selects close code `1011`.

Falcon's `WebSocketDisconnected.code` is preserved when available; missing
transport codes use `1006`. A clean peer close does not send a second close. A
resource or server close uses Falcon's reported code when available.
Cancellation selects `1001`; an unexpected setup, receive, or handler failure
selects `1011`. Both policy closes are best effort and cannot replace the
primary session error.

Cleanup runs in a separate shielded task. The driver keeps awaiting that same
task through repeated cancellation so accepted connection-manager membership
cannot be abandoned when the session task exits. There is no cleanup timeout;
`on_disconnect` must terminate. `before_disconnect` receives the request,
prepared socket, route parameters, effective close code, and primary error. It
runs before `on_disconnect`, which is attempted even if notification fails.
Receive hooks and dispatch check the bound session phase and socket closure, so
no frame begins resource work after disconnect starts.

A lone primary or cleanup failure is re-raised unchanged. Multiple ordinary
failures form an `ExceptionGroup`, with the primary failure first. Cancellation
stays a bare `CancelledError`; failures suppressed by cancellation are sent to
the event loop's exception handler with a short message. This keeps resource
rollback failures visible without replacing cancellation.

## Cluster fan-out maintenance

Keep `ConnectionBackend` and `WebSocketConnectionManager` as process-local
registries for live sockets. `ClusterFanout` coordinates distributed room
membership through a `FanoutTransport` that exchanges string identifiers and
floating-point lease durations as metadata, and serialized envelopes as bytes;
it never transfers live sockets. A lease is per node and room: subscribe on the
first local join, unsubscribe on the last local leave, refresh by heartbeat,
release during graceful shutdown, and let `lease_ttl` expire membership after a
crash. Resources should call `add_connection`, `join_room`, `publish` and
`remove_connection` through the coordinator, with `fanout.run` started and
stopped by `WorkerController` in the ASGI lifespan. See the
[user guide's clustered deployment section](users-guide.md#clustered-deployments),
the
[fan-out design](falcon-websocket-extension-design.md#371-cluster-membership-and-fan-out),
and the
[scalability discussion](falcon-websocket-extension-design.md#64-scalability-for-distributed-systems).

`ClusterFanout.__init__` has a method-scoped CodeScene *Excess Number of
Function Arguments* exception because its explicit, typed collaborators and
timing options form the public wiring contract; replacing them with a
configuration object or opaque keyword arguments would change that contract.
Keep this exception limited to that method, without a cohesion suppression or
project threshold change.

Delivery is best effort, with no durability or replay to down or expired nodes.
Per-socket ordering depends on the transport preserving per-publisher, per-node
order; there is no global ordering, and duplicates are possible with
at-least-once transports. The coordinator does not deduplicate; `message_id` is
available in the envelope but is not added to WebSocket payloads, so
applications needing deduplication must supply their own payload identifier.
Serialization failures occur before publication, transport publish failures
propagate without retry, and local send failures do not stop the listener,
evict connections or roll back publication. The in-memory test transport is
`InMemoryFanoutTransport`; the real two-server BDD lives in
`tests/behaviour/cluster_fanout.feature` and
`tests/behaviour/test_cluster_fanout_steps.py`.

`WorkerController` remains telemetry-agnostic. `ClusterFanout` and session
cleanup may emit standard DEBUG events with fixed fields `operation`, `outcome`,
`duration_seconds` measured with a monotonic clock, and `exception_type`; use
fixed operation and outcome values for publication, membership maintenance,
listener termination and disconnect cleanup. Keep successful message decoding
and local delivery silent, distinguish decode failures from delivery failures,
and emit no unconditional per-frame events. Exclude identifiers, payloads,
exception values and tracebacks. Host logging handlers may aggregate metrics or
start and end spans using task or context; broker decorators may measure queue
waits and operations, while existing lifecycle hooks support resource
instrumentation. Filter, sample or aggregate to bound event volume because
publication and lease renewal are frequent and DEBUG is off by default. Metrics
must use fixed operation and outcome dimensions, never room, message,
connection or node IDs as labels. Keep instrumentation optional and
host-configured, with no mandatory SDK, public constructor changes or
`WorkerController` changes.

## Dependency bounds

The package supports Falcon 4.x through `falcon>=4,<5`. Falcon follows
[semantic versioning](https://falcon.readthedocs.io/en/stable/community/releases.html#semantic-versioning),
so an incompatible API change can arrive with a new major release. This
package uses Falcon's ASGI application and WebSocket APIs in
`falcon_pachinko/router.py`, `falcon_pachinko/websocket.py`, and
`falcon_pachinko/testing/harness.py`; the exclusive upper bound keeps a future
major release out until compatibility is verified.

The lower bound was checked on 2026-10-08 UTC (2026-10-09 in Europe/Berlin) by
installing Falcon 4.0.0 with `uv pip install falcon==4.0.0` and running
`uv run --no-sync pytest -v`: 770 tests passed and 2 were skipped. Run
`make build` afterwards to restore the normal environment. The latest CI build
checked for this change resolved Falcon 4.4.0.

`uv.lock` is committed and `.gitignore` no longer excludes it. The dependency
bounds above are deliberately wide, so the lockfile is what pins an actual
release. See "Dependency management with uv" below for how it is kept current.
Lower-bound coverage in CI stays tracked separately from this support policy.

## Dependency management with uv

The project is uv-managed. `pyproject.toml` declares runtime dependencies under
`[project]`, development tooling under `[dependency-groups]`, and the
`examples` and `testing` sets as `[project.optional-dependencies]` extras. The
lockfile `uv.lock` resolves all three, including every interpreter version in
`requires-python`, and is committed alongside them.

`make build` runs:

```sh
uv sync --group dev --locked
```

`--locked` is what makes the committed lockfile load-bearing. A plain `uv sync`
re-resolves whenever `pyproject.toml` and `uv.lock` disagree, so a branch that
edited one but not the other would install a dependency set nobody reviewed and
report success. With `--locked` the same disagreement fails the build instead,
which is the behaviour CI needs: `ci.yml` installs the project by running
`make build`, so the lockfile CI installs is the lockfile the branch committed.

### Changing a dependency

- After editing any dependency in `pyproject.toml`, run `uv lock` and commit
  the regenerated `uv.lock` in the same change. `make build` fails until it is
  refreshed.
- To upgrade one dependency without disturbing the rest, use
  `uv lock --upgrade-package <name>`. A bare `uv lock --upgrade` re-resolves
  everything and produces a diff no reviewer can attribute to a reason.
- Bump the `ruff` and `ty` pins in the Makefile and in `ci.yml` together, not
  through the lockfile: they run as `uv tool run --from <tool>==<pin>` outside
  the project environment, and `tests/test_toolchain_versions.py` holds the two
  sites to one another.

### Dependabot

`.github/dependabot.yml` reads Python dependencies through the `uv` ecosystem,
which is the ecosystem that understands `uv.lock`; `pip` reads
`requirements*.txt` and the older `pyproject.toml` layout and would leave the
lockfile unmaintained. Dependabot's lockfile updates and the entry's grouping,
limits, labels and cooldown are held by `tests/test_uv_toolchain.py`, which
also asserts that the lockfile is committed, not ignored, and that `make build`
installs it with `--locked`.

The auto-merge workflow needs no change for this: `dependabot-automerge.yml`
keys on the actor `dependabot[bot]`, not on the ecosystem that opened the pull
request.

## WebSocket lifecycle acceptance tests

Every production WebSocket lifecycle change requires a pytest-bdd scenario
against a real Falcon ASGI responder, mounted with `WebSocketRouter.attach()`
and exercised through `WebSocketTestClient`. Use the public
`live_websocket_server` fixture for ordinary exchanges. The lifecycle suite
also keeps `tests/behaviour/_live_server.py`, a focused Uvicorn harness that
exposes server shutdown and active-task state for cancellation scenarios.
Simulator scenarios remain useful for fast behavioural coverage, but they do
not establish the production responder contract or replace live coverage.

The public harness captures failures from Falcon's `Exception` handler and from
exceptions escaping the ASGI callable. Tests can inspect the read-only
`server.errors` snapshot or acknowledge asserted failures with
`server.pop_errors()`. Applications that register their own `Exception` handler
must register it after `live_websocket_server.start(app)` and call
`server.record_error(ex)`; otherwise Falcon may consume the exception before
the harness can report it. Unacknowledged server failures fail context teardown.

The `testing` extra and `dev` dependency group include `uvicorn>=0.34,<1`. The
test server imports Uvicorn lazily and uses its lifespan readiness and
graceful-shutdown APIs; the behavioural contract belongs to Falcon-Pachinko's
adapter protocol, not to Uvicorn.

## WebSocket responder and live-harness boundaries

`WebSocketRouter.attach(app, prefix)` compiles relative paths, registers the
exact mount and descendant catch-all routes, and installs raw-frame media
handlers on the app. `WebSocketRouter(receive_loop=True)` opts into the
router-owned lifecycle described above. The default `False` returns after
connection setup and leaves subsequent receives and cleanup to the application.

`install_raw_websocket_media_handlers()` wraps the app-wide TEXT and BINARY
handlers once. Inbound deserialization passes frames through as raw `str` or
`bytes` for resource dispatch; outbound serializers remain the configured
Falcon serializers. `configure_raw_frame_media(app)` provides the same
idempotent setup for manually mounted routers and other Falcon WebSocket
routes. Both entry points warn when customized inbound handlers will be
replaced, and repeated installation does not wrap a handler again.

The opted-in session adapter preserves `str` and `bytes` returned by
`receive_media()` and encodes other decoded values as JSON before dispatch.
Native Falcon sockets use the private raw receive seam; malformed media is an
unexpected session failure. Falcon reports peer endings with
`falcon.WebSocketDisconnected`; transports without an effective code use
`1006`. Unexpected setup, receive, or handler failures select `1011`, while
task cancellation selects `1001`. Disconnect cleanup runs once in a shielded
task, calls `before_disconnect` before `on_disconnect`, and preserves ordinary
failures with primary-first `ExceptionGroup` ordering. Cancellation remains a
bare `CancelledError`, with cleanup failures reported through the loop's
exception handler.

The `ServerAdapter` protocol isolates the ASGI server. Its `task` property is
the serve-loop task, `start(app, sock)` returns only after lifespan startup and
listener readiness, and `stop()` requests graceful shutdown. The harness binds
the ephemeral socket before passing it to the adapter, then joins the task
during teardown. The default Uvicorn adapter is imported lazily and implements
that contract using Uvicorn's lifespan and graceful-stop support.

`live_websocket_server` provides a per-test runner with a fresh event loop. Its
`run()` method executes awaitables on that loop; `start()` tracks each harness
and `close()` exits servers in reverse order, cancels leftover tasks, shuts
down async generators and the default executor, and closes the loop. Startup
timeouts or failures trigger partial-startup cleanup and are reported as
`LiveServerStartupError`. A shutdown deadline records
`LiveServerShutdownError`. Unacknowledged captured application or server-task
failures are grouped into `LiveServerError` at teardown; `server.pop_errors()`
acknowledges failures a scenario has asserted. The capture handler is installed
during startup, so a custom Falcon `Exception` handler must be registered
afterwards and call `server.record_error(ex)`.

## Validation error import boundary

Use `from falcon_pachinko.utils import ValidationError` as the stable import
path for validation exceptions. New package code must raise through
`falcon_pachinko.utils.ValidationError`, keeping raise sites behind the
package's stable path. It is the same class as `msgspec.ValidationError`,
preserving existing exception handlers. The dispatcher routes validation
failures to `on_unhandled`; a `ValidationError` raised inside a handler still
propagates.

The typed alias is a direct assignment to preserve the original class object.
Ruff bans `from msgspec import ...`; PEP 695 syntax would not preserve class
identity. The line-local Pylint `prefer-type-statement` and Ruff
`non-pep695-type-alias` suppressions document those constraints.

A malformed `schema` declared on a `WebSocketResource` subclass is a different
kind of failure, and raises `TypeError` at class creation through
`validate_schema_types`. That check runs from `__init_subclass__`, so it only
sees a schema present at class creation. A `schema` assigned afterwards
bypasses it: `msgspec` reports a union mixing integer and string tags when it
first derives that union at decode time, and that configuration `TypeError`
propagates instead of becoming an `on_unhandled` fallback. Reject the
temptation to catch it at the decode site: widening the handler would leave the
invalid schema in place and disguise a schema bug as a malformed message.

## Safe diagnostics

Framework diagnostics must not call `repr()` or `str()` on raw payloads,
decoded caller values, or other caller-controlled objects. Use the shared
bounded metadata helpers in `falcon_pachinko.diagnostics` for identifiers,
types, frame kind and length, and safe scalar fields. Payload values stay
available to runtime code; their default representations, errors, logs and
trace summaries omit them.

When translating a vendor decode failure, capture only its exception class in
the `except` suite and raise the framework error after leaving that suite.
`raise ... from None` suppresses traceback display of the preceding exception,
but it does not remove the implicit `__context__` reference. Raising afterwards
avoids retaining the vendor exception and its message in either chain
attribute. Preserve application exceptions when they are propagated; diagnostic
display suppression does not sanitize an exception object retained or logged by
application code.

Application hooks and handlers that read `HookContext.raw`, fallback raw
arguments, or `TraceEvent.payload` cross a trusted boundary. Do not use payload
`repr()` or `str()` for diagnostics there; UTF-8 decoding changes
representation but does not remove secrets.

## Spelling policy

Run the spelling gate with:

```bash
make spelling
```

`TYPOS_CONFIG_BUILDER_VERSION` in the `Makefile` pins the
`typos-config-builder` release the gate runs (currently `v0.1.3`). Raise it
together with the regenerated `typos.toml`, never on its own.

The tracked `typos.toml` is regenerated on every run from the live shared
dictionary and the repository-specific `typos.local.toml` overlay. Never edit
generated entries by hand; add only narrow repository terminology to the
overlay. Because the dictionary is live, `typos.toml` must never be drift
checked in continuous integration (CI).

The shared `typos-config-builder` CLI refreshes the estate dictionary into an
untracked local cache only when the authoritative copy is newer. A valid cache
remains usable when the network is unavailable. Quoted APIs and identifiers
retain their upstream spelling; put them in backticks or fenced code blocks
where practical rather than adding broad word-level exceptions.

## Router Request Boundary

`WebSocketRouter` is mounted as a Falcon resource, but its internal dispatch
pipeline only needs a small request surface before handing control to resource
callbacks:

- `path`: the concrete request path used for route matching.
- `path_template`: the Falcon mount template used to verify that the request
  belongs to the mounted router prefix.

The router models that surface with the private `_RequestLike` protocol in
`falcon_pachinko/router.py`. Internal routing helpers should accept
`_RequestLike` instead of broad `object` parameters. This keeps request-shaped
test doubles type-checkable while documenting the attributes the router
actually consumes.

Keep casts to `falcon.Request` at the edge where the router calls APIs whose
public contract is Falcon-specific, such as hook notification and
`WebSocketResource.on_connect()`. Do not widen the whole router pipeline back to
`falcon.Request` unless those internals start depending on Falcon-only
attributes.

Test request doubles should expose both `path` and `path_template`. Use an empty
`path_template` for root-mounted router tests, matching the runtime default
used by Falcon-style request objects that do not provide a template.

### URI-template compiler boundary

`falcon_pachinko/_uri_template.py` is the private shared compiler for full-path
and prefix matchers. The router uses both forms for top-level routes, while
nested subroutes use the prefix form. The compiler escapes every literal run
before building the regular expression, and validates placeholder braces,
unique Python-identifier names, and parameter layouts first. Placeholders
capture non-empty path segments; a segment may contain one placeholder or a
terminal adjacent pair, while other multi-placeholder layouts are ambiguous.
Compilation happens before router registration state is mutated, so invalid
templates fail at registration or mount time with `ValueError` rather than
being deferred to connection dispatch.

`uvicorn>=0.34,<1` is in the development dependency group for
`tests/test_live_routing.py`, which exercises the router through a live Falcon
ASGI server and real WebSocket connections.

### `_RouteMatch` and `_Dispatch`

`_RouteMatch` holds what the prefix match produced: `params: dict[str, str]` and
`remaining: str`, the path segment still to be consumed. As
`_resolve_subroutes` walks a resource's nested subroutes, it updates the same
`_RouteMatch` in place, consuming `remaining` and merging each subroute's
captured parameters into `params`.

`_Dispatch` is a mutable, per-connection-attempt bundle of `route`, `req`,
`ws`, and `match`. It is built once in `_try_route` and threaded through
`_execute_route_with_error_handling`, resource and subroute resolution, hook
notification (`_prepare_connection_context` fires `before_connect`;
`_finalize_connection`, and `_execute_resource_handler` on error, fire
`after_connect`), the resource's `on_connect` handler
(`_execute_resource_handler`), and finalization (`_finalize_connection`).
Bundling these fields means a step that changes route state — subroute
resolution, or a hook that rewrites `params` — is visible to every later step,
rather than requiring `route`, `req`, `ws`, `params`, and `remaining` to be
threaded positionally through each helper.

`req` is cast to `falcon.Request` at the point `_Dispatch` is constructed in
`_try_route`, consistent with the cast-at-the-edge policy described above.

## Reserved lifecycle dispatch

The `on_connect`, `on_disconnect` and `on_unhandled` callbacks are driven by
the connection lifecycle, never by an envelope `type`, so the dispatcher
refuses to resolve them by convention.

The conventional allowlist is `_conventional_handler_names` in
`falcon_pachinko/resource.py`, computed once per class by
`_init_conventional_handler_names`. Class creation inspects each `on_*`
attribute with `inspect.getattr_static` rather than `getattr`, so a descriptor
named `on_*` cannot run, or raise, while the class is being created; the raw
attribute it returns means only a plain coroutine function is admitted. A
`@staticmethod` is not: it has no `self` for the dispatcher to pass. A
`@classmethod` is not either: it would receive the class in that position.
`functools.partialmethod` is excluded explicitly, because from Python 3.13
`inspect.iscoroutinefunction` unwraps it, so the raw descriptor would be
admitted on some interpreters and not others, and because class access yields a
generated wrapper rather than the function the test saw. The predicate still
unwraps it for the reserved-name check, so a callback cannot hide behind one.

`LIFECYCLE_CALLBACK_NAMES` and `is_lifecycle_callback`, both in
`falcon_pachinko/handlers.py`, are the single source of truth: registration
(`WebSocketResource.add_handler`, `@handles_message`) calls the predicate
directly, and the conventional allowlist that
`dispatcher.find_conventional_handler` consults is built from it, so the two
paths cannot drift. `is_lifecycle_callback` unwraps both sides of every
comparison with `_unwrap_handler`, then checks the callable's own `__name__`
and, failing that, identity against the reserved entries in the owner's MRO,
which is what catches a neutral alias such as `on_bye = on_disconnect` or a
function copied from a parent resource under a new name.

`_unwrap_handler` peels `functools.partial`, a `_HandlesMessageDescriptor` (the
object `@handles_message` returns), the standard-library descriptor families
(`functools.partialmethod`, `functools.singledispatchmethod`,
`functools.cached_property`, `property`), `classmethod` and `staticmethod`
objects, and bound methods. For the generated class-view of a `partialmethod` or
`singledispatchmethod`, `_descriptor_view_owner` follows the descriptor
backlink the view carries. A `functools.wraps` wrapper is deliberately *not*
unwrapped: it is a distinct function that only borrows metadata, so following
`__wrapped__` would over-reject an unrelated handler.

Explicit registration takes precedence: dispatch looks up the exact envelope
`type` first and falls back to convention only on a miss, so a reserved tag
stays legal for a distinct method; see §3.6 of the
[design document](falcon-websocket-extension-design.md).

## Opt-in router-owned WebSocket sessions

`WebSocketRouter(receive_loop=True)` opts into a router-owned session. The
default, `receive_loop=False`, returns after connection setup and acceptance;
the application then owns reception and cleanup. In the opt-in path,
`WebSocketRouter.on_websocket()` awaits
`falcon_pachinko._session_driver.run_session()` for setup, frame dispatch, and
completed disconnect cleanup. Route selection and pre-establishment factory
error handling remain in the router.

The session moves through `CONNECTING`, `ESTABLISHED`, `DISCONNECTING`, and
`CLOSED`. A truthy `on_connect` result establishes the cleanup promise before
the `after_connect` hooks and router acceptance. Resource-owned acceptance
establishes it as well, including when `on_connect` then raises or returns a
falsey result. A rejection that remains unaccepted has no post-accept cleanup
promise. If approval succeeds but setup never accepts the socket, the router
skips frame reception and cleans up with `1011`.

Setup remains ordered as `before_connect`, resource `on_connect`,
`after_connect`, then router acceptance when the resource has not accepted. A
resource may accept during `on_connect` when it must send setup data first; the
router detects that state and skips duplicate acceptance. Native Falcon sockets
use a forwarding `SessionWebSocket` adapter to preserve acceptance history,
since Falcon's `unaccepted` property also becomes false after a rejected
handshake is closed. Custom transports with an `unaccepted` property retain
that probe as authoritative; a transport without it is wrapped so a completed
`accept()` call records acceptance. This keeps rejected-close and
accepted-then-closed setup paths distinct without changing `WebSocketLike`.

After setup, the router receives raw frames on the final resource in the
resolved child chain and dispatches each frame through the existing resource
and hook manager. The native adapter reads Falcon's low-level TEXT or BINARY
event so frames reach dispatch unchanged; Falcon's public receivers otherwise
decode media or accept only one frame type. For injected transports,
`receive_media()` values that are already `str` or `bytes` pass through, while
other values are encoded as JSON with `msgspec`. Values that cannot be encoded
fail as unexpected errors. A native ASGI receive event without a TEXT or BINARY
payload is malformed transport input; it raises `falcon.PayloadTypeError` and
follows the unexpected-failure path with `1011`.

The receive loop ends on Falcon's `WebSocketDisconnected`, retaining its code
when present and using `1006` when it is absent. A resource- or server-initiated
`ws.close(code)` sends the requested code to the client, but the code Falcon
reports to the router may be the peer's close response. An ordinary setup,
receive, or handler failure triggers a best-effort `1011` close; task
cancellation triggers a best-effort `1001` close. `WebSocketDisconnected`
observed during accepted setup or by a resource handler is a transport ending,
not an unexpected handler failure.

Cleanup first changes the phase to `DISCONNECTING`, which guards router
dispatch and receive hooks so no later frame enters the resource. If a handler
closes the socket, the remaining `after_receive` hooks for that frame are
skipped. The router then runs `before_disconnect` with the request, socket,
route parameters, effective close code, and primary error, followed by
`on_disconnect(ws, close_code)` even if the before-disconnect hook fails. This
is where resources remove connection-manager membership established during
`on_connect`; setup that registers membership and fails before approval or
acceptance must roll it back itself.

Cleanup runs in a separate shielded task, and the router keeps awaiting that
same task through repeated cancellation. There is no cleanup timeout, so
`on_disconnect` must terminate. A lone session or cleanup failure is re-raised
unchanged. Multiple ordinary failures are raised in an `ExceptionGroup`, with
the session failure first. Cancellation remains a bare `CancelledError`;
cleanup failures that cannot be grouped with it are reported through the
running loop's exception handler. The primary-first rule also preserves an
accepted `on_connect` failure if an `after_connect` hook fails.

### Session test harnesses

Test sockets must terminate receive loops. `DummyWS`, `RecordingWS`, and the
simulator deliver seeded frames in order and then report a Falcon disconnect.
The simulator records its first close code and wakes pending receives.
`SimulatorRouterHarness.connect()` runs `router.on_websocket()` as a task,
waits for acceptance, rejection, or early failure, and joins cleanup on exit.

`falcon_pachinko.testing.LiveWebSocketServer` and the `live_websocket_server`
fixture provide reusable real-socket tests. The lifecycle BDD harness runs the
production router under Uvicorn and waits for shutdown and session cleanup. It
counts `on_disconnect` invocations at hook entry separately from completed
cleanup records, so an invocation that returns early still counts toward the
exactly-once assertion. The router-session fixture shares the lifecycle
harness's `LiveServerTransport` for readiness, socket discovery, and shutdown
on an ephemeral loopback port. Both use one event loop and leave process signal
ownership with the test runner.

Cleanup reports preserve the original application exception object when they
call the loop exception handler. This keeps failures visible without replacing
them with type-only summaries.

Router-owned sessions also emit bounded DEBUG observations for session, setup,
acceptance, receive, and cleanup. Records include the lifecycle phase,
effective close code when known, elapsed time, resource type, and exception
categories; they omit route parameters, frame contents, and exception messages.
Cleanup failures reported through the loop exception handler carry the
operation, closed phase, effective close code, and exception type while
retaining the original exception object for the handler. These events support
application log-to-metric or tracing adapters without adding a metrics API to
the router.

## Lint and Typecheck Toolchain

`make lint` provisions nose, then runs Ruff, both Pylint passes, ambrleaks and
the blocking nose duplication gate. `make typecheck` runs
`ty check falcon_pachinko tests tools`. These are the complete local and CI
checks; they need no wrapper or second manual step.

Ruff is pinned at 0.16.4 via `RUFF_VERSION` in the Makefile, and the same
version is installed in `.github/workflows/ci.yml` with
`uv tool install ruff==0.16.4`. `make lint` runs `ruff check .`, covering the
repository's Python sources, scripts and tests. Ruff runs in preview mode,
targets py312, and also formats Python code blocks embedded in Markdown.

`ty` is pinned at 0.0.74 via `TY_VERSION` in the Makefile, with a matching
`uv tool install ty==0.0.74` step in CI. `ty` is pre-1.0 and its diagnostics
shift between releases, which is why the version is pinned rather than left
floating.

`tests/test_toolchain_versions.py` is a contract test asserting that the
Makefile pins and the CI pins name the same version, without hard-coding a
version itself. Bump both sites together when upgrading either tool. The same
file pins the structure of the Pylint passes described below.

### Import conventions

Import `msgspec.inspect` as `msinspect`:

```python
import msgspec as ms
import msgspec.inspect as msinspect
import msgspec.json as msjson

msinspect.type_info(int)
```

Ruff rejects member imports such as `from msgspec.inspect import type_info` with
`ICN003`; it rejects a missing or different `msgspec.inspect` alias with
`ICN001`. The `[tool.ruff.lint.flake8-import-conventions]` table in
`pyproject.toml` is the authoritative source for aliases and `banned-from`.
Change that table, not this guide, to alter enforcement. When the configured
convention changes, update this guide to match.

### Pylint passes

Pylint runs twice, in two isolated `uv tool run` environments. Neither pass
touches the project `.venv`, whose interpreter, supported Python versions, and
test matrix are unaffected.

Table 1. Pylint passes run by `make lint`.

| Target             | Interpreter                      | Configuration        | Checks                                     |
| ------------------ | -------------------------------- | -------------------- | ------------------------------------------ |
| `make lint-pylint` | PyPy 8.0.0, Python 3.12.14 build | `pyproject.toml`     | Classic, built-in Pylint messages          |
| `make lint-df12`   | CPython 3.14                     | `pylintrc-df12.toml` | df12-python-lints checkers, then ambrleaks |

The linter's interpreter and the source's Python baseline are separate
settings. Both configuration files pin `py-version = "3.12"`, so
version-sensitive checks follow the project's 3.12 baseline rather than the
host interpreter. In particular, the df12 pass runs on 3.14 without flagging the
`from __future__ import annotations` imports that baseline-3.12 modules need:
`redundant-future-annotations` (C9112) stays dormant below a 3.14 baseline.

Both passes run one Pylint worker (`--jobs=1`) and pin Pylint 4.0.8 and
`astroid` 4.0.4 through `PYLINT_VERSION` and `ASTROID_VERSION`. Pylint 4.0.8 is
the newest release, and it declares `astroid>=4.0.2,<=4.1.dev0`, which excludes
`astroid` 4.3.x. The upgrade to `astroid` 4.3.1 is therefore deferred until a
Pylint release accepts it, and a test asserts that
`make lint-pylint ASTROID_VERSION=4.3.1` fails dependency resolution.

The df12 pass additionally pins `df12-python-lints` 0.3.0 through
`DF12_PYTHON_LINTS_VERSION`, installed from the `v0.3.0` tag of the
`df12-python-lints` repository. The runtime check refuses to run if any other
version of `df12-python-lints` is installed.

#### The classic pass on PyPy

PyPy 8.0.0 is the first PyPy release with a Python 3.12 build, and its
maintainers label that build beta quality. The pass establishes lint-toolchain
compatibility only; it says nothing about running the application on PyPy.

The pass needs no shim. PyPy 7.3.22 raised `TypeError` on
`types.FunctionType.__text_signature__`
([pypy/pypy#5458](https://github.com/pypy/pypy/issues/5458)), which crashed the
`astroid` bootstrap on every run; the estate's pylint-pypy-shim existed to
work around that. The bug was fixed twice: PyPy stopped raising from 7.3.23, and
`astroid` 4.3.0 learned to tolerate it. This repository relies on the PyPy
fix, so vanilla `astroid` 4.0.4 bootstraps and inspects live objects on PyPy
8.0.0. The `astroid` fix, and its related `__class_getitem__` robustness fix,
arrive with the deferred 4.3.1 upgrade. This repository never used the shim, so
the PyPy pass is an initial adoption rather than a shim removal.

uv's built-in interpreter catalogue does not yet list PyPy 3.12, so
`tools/pypy-downloads.json` names the official PyPy 8.0.0 tarballs and their
published SHA-256 sums for Linux (x86-64 and AArch64, glibc 2.28 or newer) and
macOS (Apple silicon and Intel). `make lint-pylint` passes that manifest to uv,
which refuses any download that does not match, installs the interpreter into
`.uv-python` rather than uv's shared store, and keeps it off `PATH` with
`--no-bin`. Other platforms, including Windows and musl-based Linux, fail with
uv's "No download found" error.

To use another interpreter, set `PYLINT_PYTHON` to its path. Before Pylint runs,
`tools/check_lint_runtime.py` inspects the interpreter inside the tool
environment and rejects anything other than PyPy 8.0.0 on Python 3.12 with the
pinned Pylint and `astroid`, whether or not the variable was overridden. The
df12 pass runs the same check for CPython 3.14.

#### Failed analysis fails the gate

Both configuration files enable `syntax-error` and the fatal diagnostics
(`fatal`, `astroid-error`, `parse-error`, `config-parse-error`, and
`method-check-failed`) by name. Pylint reports these even under
`disable = ["all"]`, but an explicit disable of any of them lets an unparsable
or unanalysable module pass with exit status 0. The contract tests reject any
configuration or Makefile command that disables them.

#### Plugin isolation and pragma validation

Only `pylintrc-df12.toml` loads `df12_python_lints`, so the PyPy pass cannot
inherit the plugin through shared configuration, and its tool environment does
not contain the plugin at all. Inline pragmas such as
`# pylint: disable=trivial-attribute-wrapper` name df12 messages that the
classic pass never registers, so that pass disables `unknown-option-value`
(W0012). The df12 pass registers both the core and the df12 messages and
enables W0012, so every pragma name is still checked for typos exactly once.

#### State and caches

Each pass keeps its persisted Pylint state in its own directory under
`.cache/pylint`, named for its runtime, so neither reads the other's
statistics. CI keys `.uv-python` by runner OS and architecture plus the
Makefile and `tools/pypy-downloads.json`. It keys `.uv-cache` by runner OS and
architecture plus the Makefile, `pylintrc-df12.toml`,
`tools/pypy-downloads.json`, `pyproject.toml` and `scripts/**`. The
`.tools/nose` cache key also includes runner OS and architecture,
`pyproject.toml`, `scripts/install_nose.py` and
`tools/nose-release-digests.json`; `make install-nose` verifies the cached
binary's pinned version before use.

#### Coverage and tests

`PYLINT_TARGETS` covers `falcon_pachinko` (listing its `unittests` and
`behaviour` directories explicitly, because they carry no `__init__.py`),
`tests`, `examples`, `tools`, every top-level `scripts/*.py` module, and every
`scripts/tests/*.py` helper-test module. Both Pylint passes use this same
target list, so the scripts and tests receive classic and df12 checks.
Recursive directory discovery covers nested test and tool packages. None of
these Python files needs syntax newer than Python 3.12, so no separate CPython
classic pass exists. The three inline script blocks under `examples` declare
dependencies but no Python version.

`tests/test_lint_toolchain_integration.py` (marker `lint_toolchain`) runs the
Makefile's own commands against the real interpreters. The tests provision PyPy
on their first run, so they need network access once, and they never skip. CI
runs them as a dedicated step after `make lint`.

`ambrleaks`, also from df12-python-lints, runs at the end of `make lint-df12`
and scans syrupy `.ambr` snapshots for unredacted values.

### Suppression policy

Every `noqa`, `pylint: disable`, or `type: ignore` pragma must carry a reason
in the same comment. The df12-python-lints C9106 and C9107 checkers reject bare
pragmas.

### Nose code duplication gate

`make lint` provisions and runs a blocking duplication gate. Its implementation
and focused tests adapt the approach merged in
[Episodic PR #276](https://github.com/leynos/episodic/pull/276), at immutable
revision
[`d9e5ac0d254f375e2986f52d91a3b88c117c833b`](https://github.com/leynos/episodic/commit/d9e5ac0d254f375e2986f52d91a3b88c117c833b).
The detector is `corca-ai/nose` v0.20.0: the Rust-built `nose` executable, not
the unrelated Python package of the same name. Adapted material retains its
upstream attribution and is used under this repository's ISC licence. This
repository downloads only official release archives and verifies their
platform-specific SHA-256 digests; it does not use cargo-binstall. The wrapper
validates that configured roots exist and contain Python candidates after the
explicit exclusions. Before its single combined JSON query, the wrapper asks
Nose for JSON stats on each root and fails with status 2 if any root has no
effective Python sources. This uses the pinned detector's actual handling of
`.ignore`, `.gitignore`, hidden paths, `.git/info/exclude`, and global Git
ignore rules without trying to duplicate its ignore precedence in Python. The
query's own warning remains a final check for a wholly empty scan. Stale
allow-rule diagnostics say “unmatched in this capped scan” because absence from
a ranked report does not prove that duplication has disappeared.

The scan covers production modules under `falcon_pachinko/`. Setuptools uses
package discovery with the `falcon_pachinko*` include pattern and excludes
`falcon_pachinko.behaviour*` and `falcon_pachinko.unittests*`. The installed
distribution therefore includes `falcon_pachinko.testing`, but omits the
behaviour and unit-test support packages. These package-discovery rules are
separate from the duplication gate's source exclusions for `testing/`,
`unittests/`, and `behaviour/`. The gate also omits `examples/`, top-level
`tests/`, `tools/`, and maintenance `scripts/`: these are examples, tests,
tooling, and the gate itself, rather than shipped production modules. The
configured roots are passed together in one detector invocation so cross-root
families remain visible. The initial report selected only `__init__.py`,
`exceptions.py`, `router.py`, and `websocket.py`, with no excluded paths.

`make build` installs the `dev` dependency group with `uv sync --group dev`; it
does not install the optional `examples` extra. Run
`uv sync --group dev --extra examples` before running example programs or tests
that require example-only dependencies such as `aiosqlite`.

The detector runs `syntax,semantic,near`, with a minimum of 24 intermediate-
language tokens, `surface = "all"`, and a top-30 ranked-family limit. `all`
widens the report surface but does not make the ranking exhaustive: allowed
families still use positions in the top 30, and lower-ranked families are not
enforced by that scan. The initial selected surface contained three families
and did not reach the cap. The semantic channel supplies a limited,
witness-backed check; it is not a guarantee of finding every Type-4 clone.

The gate and its tests use an isolated, explicitly selected CPython 3.14
tooling environment with `--no-project`; the package's Python `>=3.12` floor,
platform support, and runtime dependencies remain unchanged. The pinned
official binaries cover Linux glibc and macOS on x86-64 and AArch64. The gate
uses POSIX file locking and directory operations, so native Windows gate use is
unsupported; use POSIX or WSL. Missing trusted binaries are provisioning
errors, with no source-build fallback. `NOSE_BIN` may select another binary;
relative paths resolve from the repository root, and the expected version is
checked before every scan and after installation.

Detector and installer operations receive explicit runtime contexts. The
`DetectorContext` carries the repository root, environment, binary discoverer,
and command runner. The `InstallerContext` carries repository, configuration,
and manifest paths; environment and process runner; system, machine, and libc
metadata; downloader; and output callback. The CLI composition boundaries
assemble these contexts and snapshot process state, so detector and installer
operations use supplied dependencies rather than reading ambient globals.

The isolated helper-test environment pins Syrupy 6.1.1 for exact snapshots of
the CLI report and detector argument vectors. It also installs nose before
running the acceptance suite; real-detector canaries fail if the binary is
missing or has drifted. Review snapshot changes with the test contract that
produced them. The gate and test target both pin `pathspec` 1.1.1, and a
toolchain contract test checks that they stay in sync. This matcher validates
configured exclusion globs and is a tool-only dependency; it does not enter the
application environment.

Use these Make targets:

```text
make install-nose
make duplication
make duplication-test
make duplication-allow FIRST='path[::name]' [SECOND='path[::name]'] REASON='...'
```

The allow target reads `FIRST`, `SECOND`, and `REASON` only when supplied on
the Make command line. Each TOML entry must have a non-blank reason and cover
every location in one family. Every key in a `members` entry must match at
least one location, so unused keys cannot widen an exception. Partial entries
cannot be combined to allow a family. A named key matches only the exact
reported unit name, never an unnamed fragment. The CLI accepts repeated
`--second` values for families with more than two members. Updates preserve
TOML comments, are idempotent, and use an atomic writer protected by a stable
sidecar advisory lock. That lock coordinates participating allow commands, not
unrelated editors.

Review an unmatched stale entry against source or a sufficiently widened
relevant scan before removing it; do not delete it automatically. In this
ranked report, “unmatched” can mean that a family fell below the cutoff.

The repository's initial families were adjudicated as follows:

| Family                                                                                    | Decision and reason                                                                                                                                                                                                                                                                                                                                                                                                                                           | Regression evidence                                                                                                                                               |
| ----------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `exceptions.py::__init__` and `websocket.py::__init__`                                    | Intentional. The constructors belong to independent exception base classes and preserve different parameter and message contracts. A shared base or formatter would couple separate error domains without shared validation logic.                                                                                                                                                                                                                            | `falcon_pachinko/unittests/test_exceptions.py`                                                                                                                    |
| Import fragments in `router.py` and `__init__.py`                                         | Intentional. The router imports implementation dependencies; the package initializer re-exports the public package surface. A separate helper would add an import-only module, while routing router imports through the initializer would create a cycle. Nose gives this fragment no unit name, so exact path-only keys are necessary; a future unnamed family must include locations in both files, and every listed path must match at least one location. | `tests/test_package_exports.py`                                                                                                                                   |
| `_uri_template.py::compile_uri_template` and `_uri_template.py::_compile_prefix_template` | Retained as separate thin variants of the existing `_compile_template_with_suffix`: complete URI matching permits an optional trailing slash, while prefix matching enforces segment boundaries. The main-branch extraction changed the module path but not these contracts.                                                                                                                                                                                  | `falcon_pachinko/unittests/test_router.py::test_template_compilers_keep_complete_and_prefix_match_semantics` and `falcon_pachinko/unittests/test_uri_template.py` |
| Unnamed fragments in `diagnostics.py`                                                     | The sequence and mapping code emits different opening delimiters. Sequence traversal preserves list or tuple shape; mapping traversal filters and normalizes keys and reports omitted entries. A shared renderer would couple their distinct output and budget rules. Nose supplies no unit names, so this exact path-only key also covers future fragments in the file; review them rather than treating the entry as blanket approval.                      | `tests/test_diagnostics_unit.py`                                                                                                                                  |
| `_session.py` and `websocket.py` unnamed guard fragments                                  | Intentional. Nose reports both empty-list guards as unnamed fragments, so path-only keys are required. Session cleanup raises the first failure and annotates later failures; room broadcasting re-raises one failure or groups several. Sharing the guard would couple distinct error contracts. The exception covers only families located in both named modules; review any future matching family.                                                        | `falcon_pachinko/unittests/test_router_session.py` and `tests/test_connection_manager_unit.py`                                                                    |

No application code was changed to clear the initial report. The gate modules
and tests are focused adoption evidence, not a detector benchmark; no benchmark
result or performance claim from Episodic applies to this repository.

## Build Environment

The `build` target owns the local virtual environment. It depends on the
`.venv` target, which runs:

```sh
uv venv --clear
```

This deliberately replaces an existing `.venv` before
`uv sync --group dev --locked`. The behaviour matches CI, where a previous step
may already have created the directory. Without `--clear`, modern `uv` exits
with an error when `.venv` exists, causing downstream gates such as
`make typecheck` to fail before they reach analysis.

`uv.lock` is a prerequisite of `build` for the same reason `pyproject.toml` is
one of `.venv`: a lockfile change should visibly invalidate the environment
rather than leave the build graph claiming nothing changed.

Prefer Makefile targets over invoking tools directly. When changing the
Makefile, run `mbake validate Makefile` and the relevant commit gates before
committing.

## Mutation testing

Mutation testing asks a different question from the rest of the suite. The
tests answer "does this behaviour work"; a mutation run answers "would the
suite notice if `falcon_pachinko` were wrong". It is informational, it is slow,
and it never gates a pull request.

Run it locally with:

```sh
make mutation           # mutate the package against the whole suite
make mutation-results   # list the last run's mutants and their statuses
```

`make mutation` runs `mutmut` through `uv run --with mutmut==3.6.0`, so the
tool is injected at call time and is deliberately not a project dependency.
`make mutation-results` prints one line per mutant, grouped by status. To see
what a surviving mutant actually changed, ask mutmut directly:

```sh
UV_PYTHON=3.13 uv run --with mutmut==3.6.0 mutmut show <mutant-name>
```

A run leaves a generated `mutants/` tree in the working directory. It is
untracked and ignored, and pytest is configured to skip it: mutmut runs the
suite from inside that tree, so the copy holds a second file for every test
module under the same import name, and collecting both aborts the repository's
own `make test` with an `ImportPathMismatchError`. The `norecursedirs` entry in
`[tool.pytest.ini_options]` is what prevents that; deleting `mutants/` is safe
whenever its results are no longer needed.

The `[tool.mutmut]` section in `pyproject.toml` is the whole configuration:

- **`source_paths`** is `falcon_pachinko/`, so the CLI, the harness in
  `falcon_pachinko/testing/` and the rest of the package are mutated.
- **`do_not_mutate`** excludes `falcon_pachinko/unittests/` and
  `falcon_pachinko/behaviour/`. Those are the package's own tests, and a
  mutation in a test module can never be killed: mutmut pairs a mutant with the
  tests that execute it, and a test module is not executed by the suite it
  belongs to. Left in, they were 1951 of 4431 mutants, 1909 of them reported
  `no tests`, and a mutation that raised on import — `test_app_install.py` is
  loaded as a pytest plugin — stopped collection outright rather than failing a
  test.
- **`also_copy`** carries `tools/` and `examples/` into the `mutants/` tree
  that mutmut runs in. mutmut copies only `tests/`, `pyproject.toml`,
  `setup.cfg` and root `test*.py` by default, and two modules in `tests/`
  import those trees through `sys.path`.
- **`pytest_add_cli_args_test_selection`** names the three test trees the run
  uses: `tests/`, `falcon_pachinko/unittests/` and `falcon_pachinko/behaviour/`.
- **`pytest_add_cli_args`** drops three modules and one marker.
  `--ignore=tests/workflow_contracts`,
  `--ignore=tests/test_toolchain_versions.py` and
  `--ignore=tests/test_uv_toolchain.py` are repository-tooling contracts that
  read `.github/workflows/`, `Makefile`, `uv.lock`, `pylintrc-df12.toml` and
  `.markdownlint-cli2.jsonc` — none of which mutmut copies into `mutants/`, so
  all three fail the baseline there. They say nothing about
  `falcon_pachinko/`'s behaviour, so excluding them costs the mutation score
  nothing. `-m "not lint_toolchain"` drops the tests that provision a PyPy
  interpreter and reach the network; `-p no:cacheprovider` keeps the run from
  writing a cache into the copied tree. Mutmut has no `runner` key in 3.6.0, so
  these arguments are the whole lever.

`.github/workflows/mutation.yml` runs the estate's reusable workflow
(`leynos/shared-actions/.github/workflows/mutation-mutmut.yml`) weekly and on
manual dispatch. There is no pull-request trigger: a mutation run executes the
whole suite once per mutant, which is not a per-commit cost. The caller passes
`paths: "falcon_pachinko/"`, an empty `module-prefix-strip` (the package is
top-level, not `src/`-layout) and `python-version: "3.13"` to match
`.github/cv005.toml` and `ci.yml`. The scheduled run is scoped to files changed
in the last week, so a quiet week finishes in seconds; a dispatch runs
everything. Results land in the job summary and as the `mutation-report-mutmut`
artefact.

## Coverage and CodeScene

`coverage-main.yml` owns both persistent coverage outputs: the CodeScene upload
and the ratchet baseline. `ci.yml` generates coverage on a pull request only,
for its own ratchet check, and no workflow a pull request can reach names a
CodeScene action, runs a `cs-coverage` command, calls the service's host,
carries `CS_ACCESS_TOKEN`, or forwards every secret with `secrets: inherit`.
That separation is the estate rule CV-005.

It is not tidiness. Between 2026-09-16 and 2026-09-18 an unpinned `cs-coverage`
could not parse its own cobertura output. Because the check ran as a step of
the pull-request lane, every pull request in this repository was blocked on a
failure that had nothing to say about the change under review and that no pull
request could fix. A lane that cannot contact CodeScene cannot be stopped by
CodeScene.

Both lanes must measure the same thing, or the baseline the trunk writes is not
the baseline a pull request should be compared against.

The shared CV-005 contract library holds that shape. `make check-cv005` runs
`cv005-contracts check`, from the `leynos/shared-actions` package
`packages/cv005-contracts`, at the full commit named by `CV005_CONTRACTS_REF`
in the Makefile, and CI runs it as its own step. A fix to the rules is a pin
bump. The target needs `uv`, which fetches the Python 3.13 the library runs
under, and `make test-workflow-contracts` runs it before this repository's own
contracts. The library's suite proves each rule in both directions, so this
repository keeps no copy of the readers or the refusal cases. What it holds:

- No workflow a pull request can reach names a CodeScene action or host, runs
  `cs-coverage`, carries `CS_ACCESS_TOKEN` at any scope, or forwards every
  secret with `secrets: inherit`. The reach is a closure through local calls,
  not a trigger list, and a call the reader cannot place is refused.
- The publisher uploads rather than checks, behind a ref guard read as a
  conjunction (a `||` is refused) and a check step whose one command is
  `echo "available=${{ secrets.CS_ACCESS_TOKEN != '' }}" >> "$GITHUB_OUTPUT"`.
  The token reaches `access-token` directly and is bound in no `env`, because
  the uploader is a composite action that hands a step's `env` to its nested
  steps. A guard on `env.CS_ACCESS_TOKEN != ''` would not do: with the binding
  deleted it is simply false, and the upload skips forever without failing
  anything.
- The publisher job declares `environment: codescene`, which admits deployments
  from `main` alone, and no other job does (`environment.placement`).
- The publisher's concurrency group is keyed on the ref and never cancels a
  running generation. The shared action saves a fresh baseline cache per
  successful push and later runs restore the newest match, so two overlapping
  pushes would let the older commit's baseline become the one every pull
  request is measured against. It is not a durable queue: GitHub keeps one
  pending run per group, so a newer push replaces an older pending one.
- Both lanes select the same `generate-coverage` inputs, both set
  `with-ratchet: 'true'`, and both use one shared-actions revision. The
  publisher's selection is pinned in `.github/cv005.toml` under `[selection]`,
  so a change made to both lanes at once is still a reviewed change. The
  pull-request lane declines the report artefact and the publisher keeps the
  action's default.
- The upload passes no checksum input. From shared-actions `f68e8e2e` the
  action pins `cs-coverage` through its own manifest and rejects a non-empty
  `installer-checksum`, and a workflow that reads or refreshes the retired
  `CODESCENE_CLI_SHA256` variable is refused. `get-codescene-sha.yml` is
  deleted for that reason, and the repository variable can be removed whenever
  convenient, because nothing reads it.

`.github/cv005.toml` carries this repository's parameters: `repository`, the
`[selection]` and `interpreter = "3.13"`. The interpreter makes the library
require `UV_PYTHON: '3.13'` on every `generate-coverage` step, set on the job
in both lanes, and hold that version inside `requires-python` (`>=3.12`),
because generate-coverage otherwise takes the `python3` that the latest
`setup-python` put on `PATH`, and `uv sync` refuses an interpreter outside the
range. The ratchet baseline key carries the interpreter
(`ratchet-baseline-<os>-py<major.minor>-`), so a lane on another Python would
miss its baseline rather than compare against the wrong one.

`tests/workflow_contracts/test_check_cv005_target.py` holds the target's wiring
(the pinned full commit, Python 3.13, the arguments, the parameters file and
failure propagation) without the network, and CI runs the real command as the
end-to-end check. The runner-label and lane-trigger contracts
(`test_runner_placement.py`, `test_lane_triggers.py` and their readers) stay
local too.

One fact remains this repository's own, in
`tests/workflow_contracts/test_coverage_lane_facts.py`. The pull-request lane
fetches no full Git history: the ratchet compares the measured percentage with
a stored baseline and reads no commits, and the full clone the lane once
requested dates from the CodeScene check step, which has left it. The library
has no clause for it, so the contract is proved over temporary sources as well
as this repository's workflow.

One known exception: a Dependabot pull request merged by the automerge workflow
uses `GITHUB_TOKEN`, whose merges fire no push event, so that commit publishes
no coverage until the next push or a dispatch on main.

`tests/workflow_contracts/` needs two development dependencies the library
itself does not. **PyYAML** (`pyyaml>=6.0.3`) parses the GitHub Actions
documents and **Hypothesis** (`hypothesis>=6.168.0`) generates the documents
the label readers are held to. Both are in the `dev` dependency group, so
`make build`, which runs `uv sync --group dev`, installs them;
`uv sync --group dev` on its own does as well. `make test` collects the
contracts, which is what makes them a gate rather than a convenience, and
`uv run pytest tests/workflow_contracts` runs them alone. Running them without
those dependencies fails at import.

## Markdown formatting and linting

`make fmt` rewrites Markdown and `make check-fmt` verifies it, both by calling
`mdtablefix` directly over `--git --include-untracked`. That selection is the
tracked Markdown set plus anything new, so a document is neither missed because
it has not been committed yet nor rewritten twice. Both targets also pass
`$(MDTABLEFIX_RULES)`, which is
`--wrap --renumber --breaks --ellipsis --fences`: without those flags
`check-fmt` would pass files that `fmt` still wraps or renumbers, and the
80-column rule would go unenforced. The workflow contract asserts every one of
the flags in both targets, in the Makefile and in what Make expands and runs.
`make fmt` also runs `markdownlint-cli2 --fix`, because the two tools fix
different things and a contributor who ran only one would learn the rest from
CI.

The rules and exclusions live in `.markdownlint-cli2.jsonc`, copied verbatim
from the estate canon. A repository may add rules and globs alongside them; it
may not weaken one. An alternate file name does not count, because
`markdownlint-cli2` reads several and a repository carrying two would enforce
whichever it found first.

CI lints Markdown through `DavidAnson/markdownlint-cli2-action` pinned to a
full commit SHA, and nothing else may. The action's release carries the
linter's whole dependency graph, so nothing is resolved from the npm registry
while the gate is running: a moved tag or a registry outage cannot change what
the gate enforces without changing this repository. A `run:` step that invokes
the linter, or drives `make markdownlint`, is the defect this replaces.

Before this, CI installed `markdownlint-cli2` and then linted no Markdown at
any point, and the Makefile named `markdownlint`, which is a different program.
It resolved to nothing, so the local target ran `xargs` with no command and
exited zero having linted the empty set. A gate that reports success while
checking nothing is worse than no gate, because it is believed.

`tests/workflow_contracts/test_markdown_baseline.py` holds each of those facts,
and each is proved by removing the thing it asserts. It reads the Makefile
rather than running it, expanding variable references first: a contract
matching the literal text `$(MDLINT)` would pass with that variable pointing
anywhere at all.

## GitHub Actions runner placement

Repository-owned Linux lanes run on Ubicloud managed runners. Scheduled,
dispatch-only and `pull_request_target` work stays GitHub-hosted, where
public-repository minutes are free and where this migration has nothing to gain.

| Job                                   | Trigger            | Ceiling |
| ------------------------------------- | ------------------ | ------- |
| `ci.yml` `lint-test`                  | pull request, push | 15 min  |
| `coverage-main.yml` `coverage-upload` | push to `main`     | 15 min  |
| `release.yml` `pure-wheel`            | tag push           | 30 min  |
| `release.yml` `release`               | tag push           | 30 min  |

*Table: the lanes that run on Ubicloud.*

Everything else stays GitHub-hosted. `build-wheels.yml`'s `build` is a
`workflow_call` matrix across Ubuntu, Windows and macOS, including aarch64
under emulation, and Ubicloud offers Linux only. `delayed-pr-comment.yml`'s
`delay_and_comment` is dispatch-only. `dependabot-automerge.yml`'s `automerge`
calls a reusable workflow and declares no runner of its own.

### The pull-request lane's label is an expression

A pull request from a fork cannot obtain an Ubicloud runner. `lint-test` serves
pull requests, so a fixed `ubicloud-standard-2` would leave such a pull request
queueing for a runner that never arrives. A pull request Dependabot opened also
takes the hosted arm: hosted minutes cost nothing on a public repository, and
Dependabot opens many pull requests, which therefore build cold on the hosted
pool. The guard tests the pull request's author, not `github.actor`, because
the actor is whoever triggered the run, so a person pushing to a Dependabot
branch would flip the runner. It resolves its label from the head repository
and the author instead:

```yaml
runs-on: >-
  ${{ (github.event.pull_request.head.repo.fork
  || github.event.pull_request.user.login == 'dependabot[bot]')
  && 'ubuntu-latest' || 'ubicloud-standard-2' }}
```

The fork field takes three values. A pull request from a fork sets it to `true`
and takes the GitHub-hosted arm. A pull request from a branch of this
repository sets it to `false`. Any other event, `push` to `main` included,
carries no `pull_request` object at all, and GitHub evaluates the missing
property as an empty string. `false` and the empty string are both falsy, so
both reach Ubicloud; the distinction matters only because a reader who believed
non-fork pull requests left the field empty would conclude the expression had a
case it does not handle.

Two rules follow, and a green run demonstrates neither:

- Keep the continuation at the same indent as the first line. A continuation
  indented one level deeper keeps its line break, so the folded scalar parses
  to a label with a newline inside the expression. The document still parses,
  `actionlint` still passes, and GitHub evaluates the newline-bearing
  expression anyway.
- Do not copy the expression onto the other three lanes. They trigger on a
  push or a tag, where `github.event.pull_request` is null, so the guard could
  never hold. A guard that cannot hold reads as protection and is not.

### Ceilings

An Ubicloud runner registers as a just-in-time self-hosted runner, so GitHub's
six-hour cap does not apply and an unbounded lane can run to the five-day
limit. Every lane that can reach Ubicloud therefore declares `timeout-minutes`.

The two measured ceilings are sized from real runs rather than chosen:

| Run                               | Job               | Queue | Run time |
| --------------------------------- | ----------------- | ----- | -------- |
| CI, pull request, 2026-09-17      | `lint-test`       | 2 s   | 36 s     |
| CI, push, 2026-09-16              | `lint-test`       | 3 s   | 19 s     |
| Coverage (main), push, 2026-09-16 | `coverage-upload` | 3 s   | 34 s     |
| Coverage (main), push, 2026-08-13 | `coverage-upload` | 588 s | 30 s     |
| CI, PR, 2026-09-17, Ubicloud      | `lint-test`       | 11 s  | 44 s     |

*Table: the runs the ceilings are sized from.*

Fifteen minutes is roughly twenty-five times the longest of these, which leaves
room for a cold cache and for Ubicloud's two vCPUs. The release lanes have no
recorded run at all, so their thirty minutes is not measured; it is set wide on
the principle that a release killed part-way is worse than one that runs long.
Re-size on the third Ubicloud run of each lane.

The last row is the first run on Ubicloud, and it is slower than the
GitHub-hosted run above it: 44 s against 36 s, on an 11 s queue against 2 s.
One sample decides nothing, but it is what the paragraph below predicts, and it
is recorded rather than quietly dropped.

The second coverage sample is the reason this repository is worth moving at
all. Its 588 s queue against a 30 s run is the queueing the migration targets;
the 19 s and 36 s lanes will be dominated by runner start-up either way.

### Contracts

`tests/workflow_contracts/` holds the rules above, and
`make test-workflow-contracts` runs them on their own. `make test` collects
them too, which is what makes them a gate rather than a convenience.

They need two development dependencies that the library itself does not.
**PyYAML** parses the GitHub Actions documents, and **Hypothesis** generates
the declarations the label readers are held to. Both are in the `dev`
dependency group, so `make build`, which runs `uv sync --group dev`, installs
them; `uv sync --group dev` on its own does as well. Running
`make test-workflow-contracts` or `make all` without them fails at import.

The readers take the directory they read rather than reaching for a module
global, and the rules that prove the readers pass a directory of their own. A
rule parametrized over `.github/workflows` can only show that the current files
pass, which they do whether or not the rule discriminates anything: a reader
tied to the wrong matrix key, or blind to a direct matrix axis, would leave
every such rule green. So the readers are driven over documents written for one
question each, and each of those documents is one the estate does not contain.

Every failure the reader can meet is translated into a named error: a directory
that cannot be listed, a file that cannot be read, one that is not UTF-8, one
that is not YAML, and one that is YAML but not a mapping. A contract that meets
any of them fails saying so, rather than surfacing an `OSError` from inside a
generator. A runner label the reader cannot parse is raised rather than
skipped, because a skipped label leaves the registry equality holding over a
smaller set than the estate uses, which is the one thing that equality exists
to refuse. The same holds one level up: a job that is not a mapping, and a
registry entry that is not a string, are refused rather than filtered out.

Workflows are parsed with `StrictLoader` from
`tests/workflow_contracts/strict_yaml.py`, a `SafeLoader` that refuses a
mapping repeating a key and a key that is itself a list or mapping. PyYAML
alone keeps the last of two equal keys without a word, so a job declaring
`runs-on` twice could carry a paid label in the discarded half and read as
hosted to every rule here.

`runs-on` is read by `tests/workflow_contracts/runs_on_forms.py` in all three
forms GitHub accepts: a label, a list of labels, and a mapping of `group` and
`labels`, where a runner group counts as a label because it is as billable and
selects a runner the same way. Any other shape is refused, not read as
declaring no runner, because a job that declares none drops out of the
placement, ceiling and registry rules at once. A job that calls a reusable
workflow declares nothing, since the called workflow places its own jobs.

A job's matrix is read only for the keys its `runs-on` names, and for each of
those both shapes are read: the axis the matrix declares as a list under the
key, and any `include` row carrying the same key. Reading every `os` in sight
would enter a test parameter named `os` into the labels in use and demand a
registration for a runner no job can request. Reading only `include` rows would
miss `matrix: {os: [ubuntu-latest, windows-latest]}` entirely, and leave its
labels unregistered. `tests/workflow_contracts/runner_matrix.py` holds that
reading. It follows a matrix reference only when the declaration is exactly one
`${{ matrix.<key> }}`: GitHub renders a composed declaration such as
`${{ matrix.os }}-${{ matrix.arch }}` into one label per combination, which is
none of the axis values, so the union of the axes would be the wrong answer and
the composed form is refused. An axis declared as an expression, an axis
nothing in the matrix supplies, and a value that is not a string are refused
for the same reason: the labels the job can run on would be unknown.

The ceilings and trigger filters in the tables above are restated in the
contracts rather than derived from the workflows. The rule that every Ubicloud
lane declares a ceiling says nothing about the number, so a ceiling widened to
six hours would pass it while leaving this guide's table wrong; the same holds
for `coverage-main.yml`'s `branches: [main]` and `release.yml`'s `v*.*.*` tags,
which carry the rest of the placement argument. Change a figure in a workflow
and this guide in the same commit, or the contract fails.

The registry in `.github/actionlint.yaml` is held to an equality with the
labels actually in use, in both directions. A subset assertion would miss a
registration that outlived its last use, which reads as a provider still in
play. The exemption is a named set of GitHub-hosted labels rather than a
provider prefix: a prefix rule would silently exempt a second paid provider's
labels, which is the question the registry exists to ask.

The in-use set accounts for every workflow that declares a label, including
`build-wheels.yml`, which is `workflow_call` and which nothing in this
repository calls. The alternative was a rule excluding callerless
`workflow_call` files, and it was rejected: such a file is one line away from
gaining a caller, and "callerless" is not even a local property, since a caller
may live in another repository. A rule that skipped it would quietly stop
asking the registry question for a workflow that could run tomorrow. A contract
names the workflows the traversal must reach, so dropping one fails there
rather than silently shrinking what the registry is held to.

Two contracts read the `on:` block rather than the jobs, because the placement
rules rest on claims about events: that a fork can reach `lint-test`, and that
no fork can reach the other three. A contract reading only `runs-on` asserts
the consequence and never the premise, so a silent change to a trigger would
leave the consequence looking deliberate. Those contracts read the trigger
block under the boolean key `True`, because YAML 1.1 parses the bare word `on`
that way; a contract reading the string `"on"` finds nothing and passes on an
empty mapping, which is itself asserted.
