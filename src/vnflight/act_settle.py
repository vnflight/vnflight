"""Pure post-act settling policy.

``handle_act`` follows a successful submission with a bounded settle loop.
What that loop DOES is I/O — polling waits and state reads — but what it
CONCLUDES is a decision over a fixed evidence record: has the transaction
reached an outcome, does a positive verdict already hold, and is the story
still arriving fast enough that the agent should be handed back a wait().

Keeping the verdict rules here makes them testable on their own, and keeps a
future poll-scheduling change from quietly rewriting when an act is done.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any


# The bridge calls an applied transaction settled after _ACTION_SETTLE_GRACE
# (0.75 s) of quiet and holds trailing attribution open for
# _ACTION_POST_SETTLE_GRACE (5.0 s).  QUIET_SECONDS is the client-side mirror
# of the short grace, widened for fleet scheduling jitter: output newer than
# this is still the same burst, so the act is not finished.
_ACT_SETTLE_QUIET_SECONDS = 2.0


# WALL CLOCK, measured from the moment the tool call was ISSUED.
#
# "act() / wait() returns within this many seconds of being called whenever a
# story is still arriving at that point."  Nothing is consumed early: whatever
# has been rendered is returned with ``story_continues`` and the hint, and the
# caller's next wait() continues from the same cursor.
#
# The bound is approximate in ONE direction only: the hand-back fires at the
# first observation at-or-after the due time at which output is still flowing
# (see _ACT_SETTLE_QUIET_SECONDS), so a burst whose next line lands a second
# late returns a second late.  It is NOT "20 s of qualified story on top of
# however long the receipt took to settle" -- that reading is what put fleet
# R62/R63's hand-backs at a median ~32 s against this constant, because the
# clock only started once the act's scoped receipt drain had already returned.
# Both the observer's clock and wait()'s own hand-back are anchored to the
# call's issue time so the documented bound and the measured wall time agree.
_ACT_STORY_HANDBACK_SECONDS = 20.0


_ACT_SETTLE_POLL_CHUNK_SECONDS = 5.0


# While the handback clock is running the loop polls in short chunks.  The
# 5 s chunk above is a quiet-bridge economy; spending it here is what put
# fleet R62's handbacks at a median 31 s against a 20 s constant.
_ACT_SETTLE_HANDBACK_POLL_CHUNK_SECONDS = 1.0


# How long a story entry may sit in the pre-gameplay lull (the bridge's own
# gameplay_seen is still False) before the ordinary DONE rules are allowed to
# end it anyway.  A bound, not a target: the opening normally starts within a
# second or two, and this only stops "the title screen went away and the
# bridge is quiet" being mistaken for "the opening is over".
_ACT_PRE_GAMEPLAY_WAIT_SECONDS = 15.0


# Insurance only.  "The post-action scrape has landed" is proved by E2, not by
# a timer; this floor exists so a single mid-transition frame is not mistaken
# for the outcome when no provenance is available at all.
_ACT_POST_ACTION_MIN_WAIT_FLOOR = 1


_ACT_STORY_HANDBACK_HINT = "Story is still arriving; call wait() to continue."


_ACT_SETTLE_DONE = "done"


_ACT_SETTLE_STORY_HANDBACK = "story_handback"


_ACT_SETTLE_CONTINUE = "continue"


# Start-like entries: the click runs script and owns the whole opening.
_STORY_ENTRY_LABELS = frozenset({
    "start", "new game", "newgame", "continue", "load", "load game",
})


@dataclass
class _ActSettleEvidence:
    """Named evidence about one act's outcome.  See the table above."""

    bridge_settled: bool = False      # E1
    post_action_sample: bool = False  # E2
    decision: bool = False            # E3
    surface_changed: bool = False     # E4
    story_flowing: bool = False       # E5
    terminal: bool = False            # E6
    # E1': the receipt says the click was APPLIED -- weaker than E1, which
    # additionally requires the bridge's post-action quiet.
    action_applied: bool = False
    # Is a decision surface rendered at all, stale one included?  Only the
    # handback reads this; see _act_settle_positive_verdict.
    pending_surface: bool = False
    # The bridge says this run has not reached gameplay yet, and the act is
    # still inside the bounded pre-gameplay window.  Nothing that happens in
    # that lull is the act's outcome.
    pre_gameplay: bool = False
    # How long E1' and E5 have held together, for the story handback.
    settled_story_seconds: float = 0.0
    # Context the CONTINUE guard needs: what this act still expects.
    story_boundary: bool = False
    story_entry: bool = False
    rescrape_expected: bool = False
    empty_probe_pending: bool = False


def _act_settle_awaits_outcome(evidence: _ActSettleEvidence) -> bool:
    """Does this act still expect an observable outcome?  Pure.

    This is the CONTINUE guard, and it is deliberately NOT "nothing has
    arrived yet".  An act with no declared expectation and no evidence has
    already produced everything it is going to produce; polling it would spend
    the caller's whole budget on silence, which is exactly the R61 stall.
    """
    if evidence.decision or evidence.terminal:
        return False
    if evidence.story_flowing:
        # The story-entry / story-gap drains' stop condition, stated as
        # evidence: keep going until the story reaches a real boundary.
        return not evidence.story_boundary
    if evidence.story_entry:
        # A Start-like entry owns its whole opening.  Only the bridge's own
        # settle plus a surface that is NOT the menu we clicked ends it --
        # returning the pre-act surface would re-present a consumed menu.
        #
        # Both of those hold the INSTANT the title menu is replaced, which is
        # before the opening has emitted a line: fleet R62's act('Start')
        # returned the bare token "starting" in 7.6 s on all twelve agents and
        # moved the whole opening onto the caller's next wait().  While the
        # bridge itself still says the run has not reached gameplay, the pair
        # describes the start of the opening, not its end.
        if evidence.pre_gameplay:
            return True
        return not (evidence.bridge_settled and evidence.surface_changed)
    if evidence.rescrape_expected:
        # `_wait_after_action` means "re-scrape after the frame".  That is a
        # sample expectation (E2), never a story expectation.  It still needs
        # E1: without the bridge's own settle, a changed surface may be an
        # intermediate frame and the successor may still be rendering.
        return not (
            evidence.bridge_settled
            and (evidence.post_action_sample or evidence.surface_changed)
        )
    return evidence.empty_probe_pending


def _act_settle_positive_verdict(evidence: _ActSettleEvidence) -> str | None:
    """The three rules that POSITIVELY prove the act is done.  Pure.

    Separated from the CONTINUE guard because only positive evidence may cut a
    story drain short: "this act expects nothing more" is the drain's own
    knowledge, while "the bridge settled and the successor is on screen" is
    not.
    """
    if evidence.bridge_settled and (
        evidence.decision
        or evidence.terminal
        # A surface change during the pre-gameplay lull is the title screen
        # going away, not the act's outcome.  A decision or a terminal still
        # counts there: those ARE outcomes whenever they appear.
        or (evidence.surface_changed and not evidence.pre_gameplay)
    ):
        return _ACT_SETTLE_DONE
    if (
        evidence.bridge_settled
        and evidence.post_action_sample
        and not evidence.story_flowing
        # "Quiet after a post-action sample" means nothing more is coming.
        # In the boot lull it means the opening has not started yet, which is
        # the opposite claim.
        and not evidence.pre_gameplay
    ):
        return _ACT_SETTLE_DONE
    if (
        evidence.story_flowing
        and not evidence.decision
        and evidence.settled_story_seconds >= _ACT_STORY_HANDBACK_SECONDS
        and (
            evidence.bridge_settled
            # E1 is "the bridge went quiet for _ACTION_SETTLE_GRACE".  An
            # unbroken NVL page emits every ~2 s, so E1 is FALSE for exactly
            # as long as the burst lasts -- the handback could never fire in
            # the case it was written for (fleet R62: 54 acts at the 60 s
            # deadline, all 12 agents, deterministic on the "48 hours" beat).
            # The receipt's own "applied" is the honest evidence here: the
            # click has run, so the pre-act menu is provably consumed.
            #
            # Safe against re-presenting a stale successor because the
            # relaxed arm additionally requires that NO decision surface is
            # rendered at all: not merely "no fresh menu" (that is E3) but no
            # menu whatsoever.  The hand-back result therefore carries story
            # plus story_continues and no numbers for the agent to answer.
            or (evidence.action_applied and not evidence.pending_surface)
        )
    ):
        return _ACT_SETTLE_STORY_HANDBACK
    return None


def _act_settle_verdict(evidence: _ActSettleEvidence) -> str:
    """The one settle policy: evidence in, verdict out.  Pure."""
    positive = _act_settle_positive_verdict(evidence)
    if positive is not None:
        return positive
    if _act_settle_awaits_outcome(evidence):
        return _ACT_SETTLE_CONTINUE
    return _ACT_SETTLE_DONE


def _post_action_sample_is_after(
    sample: Any,
    *,
    source_id: str | None,
    source_seq: int | None,
    admission_seq: int | None,
) -> bool:
    """E2: is *sample* provably a scrape taken AFTER the act was accepted?

    Two independent proofs, both strict:

    * the shim's own queue order -- the same ``_source_id`` with a higher
      ``_source_seq`` than the coordinates the bridge stamped on the accepted
      (and, once applied, re-stamped) transaction;
    * the bridge's event counter -- ``_seq`` beyond the act's admission seq.

    A sample stamped at or before the anchor is the PRE-act scrape and is never
    evidence of the act's outcome; with no anchor at all there is no proof, so
    there is no evidence either.  Returning False here only costs a poll.
    """
    if not isinstance(sample, dict):
        return False
    sample_source_seq = sample.get("_source_seq")
    if (
        source_id
        and type(source_seq) is int
        and sample.get("_source_id") == source_id
        and type(sample_source_seq) is int
    ):
        return sample_source_seq > source_seq
    sample_seq = sample.get("_seq")
    if type(admission_seq) is int and type(sample_seq) is int:
        return sample_seq > admission_seq
    return False


def _mark_act_story_continues(result: dict) -> None:
    """Hand a still-running story back rather than hold the act open."""
    result["story_continues"] = True
    warning = str(result.get("warning") or "").strip()
    if _ACT_STORY_HANDBACK_HINT in warning:
        return
    result["warning"] = (
        "{} {}".format(warning, _ACT_STORY_HANDBACK_HINT).strip()
    )


def _act_settle_deadline(params: dict) -> float:
    """The settle loop's wall clock: the caller's own budget, nothing new."""
    followup_timeout = params.get("timeout", 60)
    try:
        followup_timeout = min(float(followup_timeout), 60.0)
    except (TypeError, ValueError):
        followup_timeout = 60.0
    deadline = time.time() + max(0.0, followup_timeout)
    result_deadline = params.get("_result_deadline")
    if isinstance(result_deadline, (int, float)):
        deadline = min(deadline, float(result_deadline))
    return deadline


