r"""Properties of the coverage-ownership contracts, over generated steps.

The contracts beside this file each pin one workflow and one case. They say
nothing about the space between the cases, and the helpers they rest on are
asked *arbitrary* parsed YAML: an action path is a string a contributor can
spell almost any way, and a step list is whatever a workflow happens to hold.

They are stated in pairs, because each rule has an opposite defect that a
fixture cannot see. Refusing every action would satisfy "no look-alike is the
real action" while breaking the real workflow, and accepting every action
would satisfy the mirror. Only the pair says anything.

Seven invariants hold:

* repinning an action to a different ref never changes whether it is
  recognised, because the ref is a pin and not part of the action's identity;
* extending a real path never satisfies it, so a look-alike action cannot
  stand in for the genuine one, while the loose sweep still sees the real path
  inside that look-alike;
* a genuine ratcheting step is still recognised at any pin, alongside
  arbitrary other steps;
* a ratchet input on some other action is not coverage, so the ratchet can
  never be satisfied by an action that does not generate coverage;
* one ratcheting step is always enough, regardless of what surrounds it; and
* a lane that does not run on main is never a CodeScene publisher, whatever
  steps it declares.

The last is the ownership rule itself (CV-005): CodeScene publication is
main's alone, and a pull request must not be able to reach it. The first six
are what keep that rule from being satisfied by accident.

Run with:

    uv run pytest tests/workflow_contracts
"""

from __future__ import annotations

import typing as typ

import hypothesis as hyp
import hypothesis.strategies as st

from .test_main_owned_codescene_coverage import (
    RATCHET_ACTION_PATH,
    UPLOAD_ACTION_PATH,
    _has_ratcheted_coverage,
    _is_main_publisher,
    _ratcheting_coverage,
    _uses,
    _uses_fragment,
)

#: Action paths that are never one of the real ones. The ``own/`` prefix keeps
#: a generated path away from ``actions/checkout`` and the shared actions, so a
#: generated step cannot accidentally satisfy an identity check.
foreign_action_paths = st.text(
    alphabet=st.sampled_from("abcdefghijklmnopqrstuvwxyz-_/."), min_size=1, max_size=20
).map(lambda tail: f"own/{tail}")

#: Paths built from a real action path, the way a look-alike would be: another
#: owner's action that embeds the real path, or the real path under a longer
#: name. Drawing these from the real paths is what makes the tightness
#: properties below capable of failing: an arbitrary string essentially never
#: happens to spell ``generate-coverage``.
look_alike_paths = st.sampled_from([RATCHET_ACTION_PATH, UPLOAD_ACTION_PATH]).flatmap(
    lambda real: st.sampled_from(
        [
            f"evil/{real}",
            f"own/{real}-extra",
            f"{real}-extra",
            f"{real}/nested",
        ]
    )
)

#: Every path a step's ``uses`` might carry. Look-alikes are mixed in beside
#: the arbitrary ones so a property that depends on them can actually fail.
non_matching_action_paths = foreign_action_paths | look_alike_paths

#: Pinning refs, drawn as plausible SHA fragments.
pinning_refs = st.text(
    alphabet=st.sampled_from("0123456789abcdef"), min_size=1, max_size=40
)

#: Values a step can carry under any of its keys. Mappings appear here so a
#: generated ``with`` can be a mapping, a scalar, or nothing at all.
step_values = (
    st.none()
    | st.booleans()
    | st.integers(min_value=-9, max_value=9)
    | st.text(max_size=12)
    | st.dictionaries(st.text(max_size=6), st.text(max_size=6), max_size=3)
)

arbitrary_steps = st.dictionaries(
    st.sampled_from(["uses", "with", "run", "name"]), step_values, max_size=4
)

#: Steps whose action is never the coverage action, even when they carry a
#: ``with-ratchet`` input that looks exactly like the real one.
foreign_steps = st.builds(
    lambda uses, inputs: {"uses": uses, "with": inputs},
    non_matching_action_paths,
    st.one_of(
        st.none(),
        st.text(max_size=6),
        st.fixed_dictionaries({"with-ratchet": st.sampled_from([True, "true"])}),
    ),
)

#: A genuine ratcheting coverage step, as the shared action declares it.
ratcheted_step = {
    "uses": f"{RATCHET_ACTION_PATH}@abc123",
    "with": {"with-ratchet": "true"},
}

#: Triggers that are not "a push to main and nothing else". Every one of these
#: must fail the main-only test, including the ones that do run on main: a
#: pull-request lane and a matrix of other branches are the cases that matter.
non_main_triggers = st.sampled_from(
    [
        {},
        "push",
        {"pull_request": None},
        {"pull_request_target": {"branches": ["main"]}},
        {"pull_request": None, "push": {"branches": ["main"]}},
        {"workflow_dispatch": None},
        {"push": {"branches": ["release"]}},
        {"push": {"branches": ["main", "release"]}},
        {"push": {"tags": ["v*"]}},
    ]
)


@hyp.given(path=foreign_action_paths, ref=pinning_refs)
def test_pinning_a_ref_never_changes_recognition(path: str, ref: str) -> None:
    """Recognise an action whatever ref it is pinned to.

    The ref is a supply-chain pin, not part of the action's name. A check that
    compared the whole reference would fail every time the pin was bumped,
    which turns a routine update into a broken contract.
    """
    assert _uses({"uses": f"{path}@{ref}"}, path), (
        f"{path} pinned to {ref} must still be recognised"
    )


@hyp.given(
    real=st.sampled_from([RATCHET_ACTION_PATH, UPLOAD_ACTION_PATH]),
    shape=st.sampled_from(
        ["evil/{real}", "own/{real}-extra", "{real}-extra", "{real}/nested"]
    ),
)
def test_an_extension_of_a_real_path_is_not_that_path(real: str, shape: str) -> None:
    """Refuse a path that embeds a real one without being it.

    This is the defect the substring check had: any action whose name contains
    the real path was accepted, so an action vendored under another owner could
    stand in for the genuine one while every real workflow still passed. The
    vendor scan depends on this, since a look-alike it mistook for the real
    action would also be mistaken for a CodeScene reference.
    """
    look_alike = shape.format(real=real)

    assert not _uses({"uses": look_alike}, real), (
        f"{look_alike} must not be mistaken for {real}"
    )
    assert _uses_fragment({"uses": look_alike}, real), (
        f"the loose sweep must still see {real} inside {look_alike}"
    )


@hyp.given(path=foreign_action_paths, ref=pinning_refs)
def test_the_exact_check_never_outruns_the_loose_sweep(path: str, ref: str) -> None:
    """Admit nothing the deliberately loose vendor sweep would miss.

    The sweep is intentionally broader than the identity check, because it must
    catch a vendor reference spelled any way at all. Tightening identity must
    not narrow it: anything recognised exactly is still recognised loosely.
    """
    step = {"uses": f"{path}@{ref}"}
    hyp.assume(_uses(step, path))

    assert _uses_fragment(step, path), (
        f"{path} matched exactly but not loosely, so the sweep is narrower"
    )


@hyp.given(steps=st.lists(foreign_steps, max_size=4))
def test_coverage_is_never_claimed_by_another_action(
    steps: list[dict[typ.Any, typ.Any]],
) -> None:
    """Refuse a ratchet input offered by an action that is not coverage.

    The ratchet flag alone is not coverage. An unrelated action carrying a
    ``with-ratchet`` input, or an action whose path merely resembles the real
    one, must not satisfy the pull-request coverage contract.
    """
    assert not _has_ratcheted_coverage(steps), (
        f"no step here generates coverage, yet the ratchet was satisfied: {steps}"
    )
    assert not any(_ratcheting_coverage(step) for step in steps), (
        f"a non-coverage action was read as ratcheting: {steps}"
    )


@hyp.given(steps=st.lists(foreign_steps, max_size=4), ref=pinning_refs)
def test_a_genuine_ratcheting_step_is_recognised(
    steps: list[dict[typ.Any, typ.Any]], ref: str
) -> None:
    """Recognise the real action at any pin, alongside arbitrary other steps.

    This is the mirror of the property above, and the reason the two must be
    read together: a check that refused everything would satisfy "no foreign
    action is coverage" while breaking the contract on the real workflow.
    """
    ratcheting = {
        "uses": f"{RATCHET_ACTION_PATH}@{ref}",
        "with": {"with-ratchet": "true"},
    }

    assert _ratcheting_coverage(ratcheting), (
        f"the real action pinned to {ref} must read as ratcheting"
    )
    assert _has_ratcheted_coverage([*steps, ratcheting]), (
        f"a genuine ratcheting step among {steps} must satisfy the contract"
    )


@hyp.given(steps=st.lists(arbitrary_steps, max_size=4))
def test_one_ratcheted_step_always_suffices(
    steps: list[dict[typ.Any, typ.Any]],
) -> None:
    """Accept any step list once it holds one ratcheting coverage step.

    The check is an existence test, so what surrounds the ratcheting step must
    not be able to veto it. This is the opposite defect to the one above, and
    equally invisible to a fixture: a check that demanded *every* coverage step
    ratchet would pass a single-step workflow and fail a real one.
    """
    assert _has_ratcheted_coverage([*steps, ratcheted_step]), (
        f"a ratcheting step among {steps} must satisfy the contract"
    )


@hyp.given(trigger=non_main_triggers, steps=st.lists(arbitrary_steps, max_size=4))
def test_a_lane_off_main_is_never_a_publisher(
    trigger: dict[typ.Any, typ.Any] | str, steps: list[dict[typ.Any, typ.Any]]
) -> None:
    """Keep CodeScene publication reachable only from main.

    This is the ownership rule. A workflow that runs for a pull request, for
    another branch, or for a dispatch must not be read as a publisher even when
    it declares a ratcheting step and an uploading step, because publication is
    what main alone owns and a credential on any other lane is the exposure.
    """
    workflow = {"on": trigger, "jobs": {"job": {"steps": steps}}}

    assert not _is_main_publisher(workflow), (
        f"a lane with trigger {trigger!r} was read as a CodeScene publisher"
    )
