from __future__ import annotations

from datetime import timedelta

import pytest

from skymonitor.analysis import Verdict
from skymonitor.decision import DecisionMachine, DecisionParams, fuse, fuse_coverage
from skymonitor_helpers import NIGHT

CLEAR, PARTLY, CLOUDY, UNKNOWN = Verdict.CLEAR, Verdict.PARTLY, Verdict.CLOUDY, Verdict.UNKNOWN
# A typical coverage for each verdict when a test does not say.
COVERAGE = {CLEAR: 0.92, PARTLY: 0.55, CLOUDY: 0.15, UNKNOWN: None}


class Night:
    """A decision machine fed one look a minute."""

    def __init__(self, params: DecisionParams = DecisionParams(), sun: float | None = -30.0) -> None:
        self.machine = DecisionMachine(params)
        self.sun = sun
        self.when = NIGHT

    def look(self, *verdicts: Verdict, minutes: float = 1.0, coverage: float | None = None):
        self.when += timedelta(minutes=minutes)
        coverages = [COVERAGE[verdict] if coverage is None else coverage for verdict in verdicts]
        return self.machine.update(self.when, verdicts, coverages, self.sun)

    def opened(self) -> Night:
        for _ in range(11):
            decision = self.look(CLEAR)
        assert decision.safe
        return self


def test_the_roof_opens_only_after_ten_good_minutes() -> None:
    night = Night()

    decisions = [night.look(CLEAR) for _ in range(11)]

    assert [decision.safe for decision in decisions] == [False] * 10 + [True]
    assert decisions[9].reason == "WAITING"
    assert decisions[9].held_seconds == 540
    assert decisions[9].held_coverage == pytest.approx(0.92)
    assert decisions[10].reason == "CLEAR"
    assert decisions[10].imaging_ok
    assert decisions[10].since == night.when


def test_a_few_clouds_during_the_wait_do_not_start_it_again() -> None:
    night = Night()

    # Stars over 92 % of the sky, then two looks with a cloud bank over a third of it.
    looks = [CLEAR] * 4 + [PARTLY, PARTLY] + [CLEAR] * 5
    decisions = [night.look(verdict, coverage=0.62 if verdict is PARTLY else 0.92) for verdict in looks]

    assert [decision.safe for decision in decisions] == [False] * 10 + [True]
    assert decisions[10].held_coverage == pytest.approx((9 * 0.92 + 2 * 0.62) / 11)


def test_a_sky_that_stays_half_covered_never_opens_the_roof() -> None:
    night = Night()

    decisions = [night.look(PARTLY, coverage=0.55) for _ in range(40)]

    assert not any(decision.safe for decision in decisions)
    assert decisions[-1].reason == "WAITING"
    assert decisions[-1].held_coverage == pytest.approx(0.55)


def test_a_lenient_setting_opens_under_more_cloud() -> None:
    lenient = Night(DecisionParams(open_coverage=0.55, hold_coverage=0.30))

    decisions = [lenient.look(PARTLY, coverage=0.6) for _ in range(11)]

    assert decisions[-1].safe and not decisions[-1].imaging_ok


@pytest.mark.parametrize("interruption", [CLOUDY, UNKNOWN])
def test_a_cloudy_or_blind_look_starts_the_wait_again(interruption: Verdict) -> None:
    night = Night()
    for _ in range(8):
        night.look(CLEAR)

    broken = night.look(interruption)
    decisions = [night.look(CLEAR) for _ in range(11)]

    assert broken.held_seconds == 0
    assert [decision.safe for decision in decisions] == [False] * 10 + [True]


def test_a_look_below_the_hold_share_starts_the_wait_again() -> None:
    night = Night()
    for _ in range(8):
        night.look(CLEAR)

    assert night.look(PARTLY, coverage=0.35).held_seconds == 0


def test_two_cloudy_looks_close_the_roof() -> None:
    night = Night().opened()

    first, second = night.look(CLOUDY), night.look(CLOUDY)

    assert (first.safe, first.reason, first.imaging_ok) == (True, "CLOUDY_HOLDING", False)
    assert (second.safe, second.reason) == (False, "CLOUDY")
    assert not night.look(CLEAR).safe


def test_one_cloudy_look_is_forgiven() -> None:
    night = Night().opened()

    night.look(CLOUDY)

    assert night.look(CLEAR).safe


def test_missing_data_closes_the_roof_after_the_grace_time() -> None:
    night = Night().opened()

    decisions = [night.look(UNKNOWN) for _ in range(3)]

    assert [(d.safe, d.reason) for d in decisions] == [
        (True, "NO_DATA_HOLDING"),
        (True, "NO_DATA_HOLDING"),
        (False, "NO_DATA"),
    ]


def test_cloud_and_missing_data_in_turn_still_close_the_roof() -> None:
    night = Night().opened()

    decisions = [night.look(verdict) for verdict in (CLOUDY, UNKNOWN, CLOUDY)]

    # Never two cloudy looks in a row, but two minutes without a sign of stars.
    assert [decision.safe for decision in decisions] == [True, True, False]


def test_no_grace_time_closes_at_once() -> None:
    night = Night(DecisionParams(unknown_grace_seconds=0.0)).opened()

    assert not night.look(UNKNOWN).safe


def test_a_partly_cloudy_sky_keeps_the_roof_open_for_a_while_without_imaging() -> None:
    night = Night().opened()

    decisions = [night.look(PARTLY) for _ in range(31)]

    assert all(decision.safe and not decision.imaging_ok for decision in decisions[:30])
    assert decisions[0].reason == "PARTLY_HOLDING"
    assert (decisions[30].safe, decisions[30].reason) == (False, "PARTLY_CLOUDY")


def test_a_sky_that_is_never_clear_again_closes_the_roof() -> None:
    night = Night().opened()

    # Partly cloudy with single cloudy looks in between: neither rule alone would close.
    decisions = [night.look(CLOUDY if minute % 3 == 2 else PARTLY) for minute in range(31)]

    assert decisions[29].safe
    assert not decisions[30].safe


def test_daylight_closes_the_roof_whatever_the_cameras_say() -> None:
    night = Night().opened()
    night.sun = -5.0

    decision = night.look(CLEAR)

    assert (decision.safe, decision.reason, decision.verdict) == (False, "DAYLIGHT", UNKNOWN)


def test_twilight_allows_the_roof_before_imaging() -> None:
    night = Night(sun=-10.0).opened()

    twilight = night.look(CLEAR)
    night.sun = -12.5
    dark = night.look(CLEAR)

    assert (twilight.safe, twilight.imaging_ok) == (True, False)
    assert (dark.safe, dark.imaging_ok) == (True, True)


def test_without_a_site_the_stars_alone_decide() -> None:
    night = Night(sun=None).opened()

    assert night.look(CLEAR).imaging_ok


def test_a_gap_in_the_monitors_own_time_closes_the_roof() -> None:
    night = Night().opened()

    after_sleep = night.look(CLEAR, minutes=45)

    # The computer slept: forty-five unwatched minutes are not forty-five good ones.
    assert (after_sleep.safe, after_sleep.reason) == (False, "WAITING")
    assert after_sleep.held_seconds == 0


def test_a_clock_that_runs_backwards_closes_the_roof() -> None:
    night = Night().opened()

    assert not night.look(CLEAR, minutes=-5).safe


def test_reset_starts_over_from_closed() -> None:
    night = Night().opened()

    night.machine.reset()

    assert not night.look(CLEAR).safe


def test_the_worst_camera_decides() -> None:
    assert fuse([CLEAR, PARTLY]) is PARTLY
    assert fuse([CLEAR, CLOUDY, PARTLY]) is CLOUDY
    assert fuse([CLEAR, UNKNOWN]) is CLEAR
    assert fuse([CLEAR, UNKNOWN], min_cameras=2) is UNKNOWN
    assert fuse([UNKNOWN]) is UNKNOWN
    assert fuse([]) is UNKNOWN
    assert fuse_coverage([CLEAR, PARTLY, UNKNOWN], [0.9, 0.6, 0.1]) == 0.6
    assert fuse_coverage([UNKNOWN], [0.9]) is None


def test_two_cameras_must_both_be_clear() -> None:
    night = Night()

    decisions = [night.look(CLEAR, PARTLY, coverage=0.6) for _ in range(12)]

    assert not any(decision.safe for decision in decisions)
