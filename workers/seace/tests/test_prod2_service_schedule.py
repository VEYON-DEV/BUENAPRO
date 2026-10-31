"""Several cycles a day (``schedule``) and the national listing in the service; private synthetic contracts.

No source, PostgreSQL, Redis or paid call: the child process is replaced by the fakes of ``test_prod2_service``.
"""
from datetime import datetime, timedelta, timezone

import pytest

from buenapro_worker.prod2 import service
from test_prod2_service import (  # noqa: F401  (``paths`` is a fixture)
    Clock, Egress, NOW, ROOT, configuration, fake_children, paths, write,
)

SCHEDULE = [{"start_time": "01:00", "window_days": 3}, {"start_time": "08:00", "window_days": 1},
            {"start_time": "12:00", "window_days": 1}, {"start_time": "16:00", "window_days": 1},
            {"start_time": "20:00", "window_days": 1}]
EVENING = datetime(2026, 10, 20, 0, 0, tzinfo=timezone.utc)  # 19:00 on the 19th in Lima.


def utc(day, hour, minute=0):
    return datetime(2026, 10, day, hour, minute, tzinfo=timezone.utc)


def scheduled(max_cycles=4):
    value = configuration(max_cycles=max_cycles)
    value.update(schedule=SCHEDULE, interval_seconds=900)
    return value


def lima(moment):
    return moment.astimezone(service.LIMA).strftime("%d/%m %H:%M")


# --- Configuration ----------------------------------------------------------------------------------------------

def test_the_daily_schedule_the_hourly_one_and_the_extreme_slots_are_valid():
    service._check_schedule(SCHEDULE)
    service._check_schedule([{"start_time": "00:00", "window_days": 1}, {"start_time": "23:59", "window_days": 30}])
    service._check_schedule([{"start_time": f"{hour:02d}:00", "window_days": 1} for hour in range(24)])


@pytest.mark.parametrize("schedule", [
    None, "01:00", {}, [], ["01:00"], [None], [{}],
    [{"start_time": "01:00"}], [{"window_days": 1}],
    [{"start_time": "01:00", "window_days": 1, "extra": True}],
    [{"start_time": "25:00", "window_days": 1}], [{"start_time": "1:00", "window_days": 1}],
    [{"start_time": 100, "window_days": 1}], [{"start_time": "", "window_days": 1}],
    [{"start_time": "01:00", "window_days": 0}], [{"start_time": "01:00", "window_days": 31}],
    [{"start_time": "01:00", "window_days": True}], [{"start_time": "01:00", "window_days": "1"}],
    [{"start_time": "01:00", "window_days": 1.5}], [{"start_time": "01:00", "window_days": None}],
    [{"start_time": "01:00", "window_days": 1}, {"start_time": "01:00", "window_days": 2}],
    [{"start_time": "08:00", "window_days": 1}, {"start_time": "01:00", "window_days": 1}],
    [{"start_time": f"{hour:02d}:00", "window_days": 1} for hour in range(24)]
    + [{"start_time": "23:30", "window_days": 1}],
])
def test_an_invalid_schedule_never_starts_a_cycle(paths, monkeypatch, schedule):
    config, state = paths
    write(config, {**configuration(), "schedule": schedule})
    children = fake_children(monkeypatch)
    assert service.run(config, state, project_root=ROOT, now=lambda: NOW) == 2
    assert children == []
    assert service._read(state / "state.private.json")["reason"] == "invalid_schedule"


def test_a_schedule_excludes_the_single_start_time(paths, monkeypatch):
    config, state = paths
    write(config, {**configuration(), "schedule": SCHEDULE, "start_time": "01:00"})
    children = fake_children(monkeypatch)
    assert service.run(config, state, project_root=ROOT, now=lambda: NOW) == 2
    assert children == []
    assert service._read(state / "state.private.json")["reason"] == "schedule_excludes_start_time"


def test_a_disabled_service_may_carry_its_schedule(paths, monkeypatch):
    config, state = paths
    write(config, {"version": 1, "enabled": False, "schedule": SCHEDULE})
    children = fake_children(monkeypatch)
    assert service.run(config, state, project_root=ROOT, now=lambda: NOW) == 2
    assert children == [] and service._read(state / "state.private.json")["status"] == "prepared"


# --- The next slot ------------------------------------------------------------------------------------------------

CONFIG = {"interval_seconds": 900, "schedule": SCHEDULE}


@pytest.mark.parametrize("finished,expected,days", [
    (utc(20, 5, 40), utc(20, 6), 3),    # 00:40 in Lima: the 01:00 slot of the same day.
    (utc(20, 5, 45), utc(20, 6), 3),    # 00:45 + 15 min is exactly 01:00: the slot counts.
    (utc(20, 5, 46), utc(20, 13), 1),   # One minute later it is the 08:00 slot.
    (utc(20, 14, 30), utc(20, 17), 1),  # 09:30: the 08:00 slot is skipped, 12:00 follows.
    (utc(20, 21, 45), utc(21, 1), 1),   # 16:45: 17:00 has passed, 20:00 follows.
    (utc(21, 1, 50), utc(21, 6), 3),    # 20:50: no more slots today, 01:00 of the next day.
    (utc(21, 4, 50), utc(21, 6), 3),    # 23:50 Lima on the 20th: midnight is crossed, 01:00 of the 21st.
    (utc(21, 5, 0), utc(21, 6), 3),     # 00:00 on the 21st in Lima.
])
def test_the_next_cycle_is_the_first_slot_at_least_the_interval_after_the_cycle_ends(finished, expected, days):
    when, slot = service._next_slot(CONFIG, finished)
    assert when == expected and slot["window_days"] == days and when.tzinfo is not None
    assert service._next_cycle_at(CONFIG, finished) == expected
    assert lima(when).endswith(slot["start_time"])


def test_a_slot_with_minutes_and_a_single_slot_roll_over_the_day():
    single = {"interval_seconds": 60, "schedule": [{"start_time": "08:30", "window_days": 2}]}
    assert service._next_slot(single, utc(20, 13, 0))[0] == utc(20, 13, 30)   # 08:00 -> 08:30
    assert service._next_slot(single, utc(20, 13, 30))[0] == utc(21, 13, 30)  # 08:30 + 60 s -> tomorrow
    assert service._next_slot(single, utc(20, 13, 30))[1] == {"start_time": "08:30", "window_days": 2}


def test_the_returned_slot_is_a_copy_that_cannot_change_the_configuration():
    _, slot = service._next_slot(CONFIG, utc(20, 5, 40))
    slot["window_days"] = 99
    assert SCHEDULE[0]["window_days"] == 3


def test_without_a_schedule_the_next_cycle_is_computed_as_before():
    assert service._next_cycle_at({"interval_seconds": 900}, utc(20, 5)) == utc(20, 5, 15)
    assert service._next_cycle_at({"interval_seconds": 900, "start_time": "01:00"}, utc(20, 5, 40)) == utc(20, 6)


# --- Running with a schedule -----------------------------------------------------------------------------------------

def windows(children):
    plans = [service._read(child.folder / "plan.private.json") for child in children]
    return [(plan["published_from"], plan["published_to"], plan.get("schedule_slot")) for plan in plans]


def test_each_cycle_searches_the_window_of_its_slot_and_the_first_one_the_window_of_the_service(paths, monkeypatch):
    config, state = paths
    write(config, scheduled())
    children = fake_children(monkeypatch)
    egress = Egress(monkeypatch, [])
    clock = Clock()
    clock.current = EVENING  # 19:00 in Lima on the 19th.
    assert service.run(config, state, project_root=ROOT, now=clock.now, stop=clock) == 0
    assert windows(children) == [
        ("2026-10-13", "2026-10-19", None),                                                   # window.days = 7
        ("2026-10-19", "2026-10-19", {"start_time": "20:00", "window_days": 1}),              # 20:00 on the 19th
        ("2026-10-18", "2026-10-20", {"start_time": "01:00", "window_days": 3}),              # 01:00 on the 20th
        ("2026-10-20", "2026-10-20", {"start_time": "08:00", "window_days": 1})]              # 08:00 on the 20th
    assert clock.current == utc(20, 13)
    assert "Próximo ciclo: 19/10 20:00 (Lima)." in egress.alerts[0]
    assert "Próximo ciclo: 20/10 01:00 (Lima)." in egress.alerts[1]
    assert "Próximo ciclo: 20/10 08:00 (Lima)." in egress.alerts[2]
    assert "Servicio completo" in egress.alerts[3]
    assert service._read(state / "state.private.json")["next_slot"] == {"start_time": "12:00", "window_days": 1}


def test_a_cycle_started_after_midnight_in_utc_but_before_it_in_lima_searches_up_to_the_lima_day(paths, monkeypatch):
    config, state = paths
    write(config, scheduled())
    children = fake_children(monkeypatch)
    Egress(monkeypatch, [])
    clock = Clock()
    clock.current = EVENING
    assert service.run(config, state, project_root=ROOT, now=clock.now, stop=clock) == 0
    assert windows(children)[1][1] == "2026-10-19"  # 20:00 in Lima is already the 20th in UTC.


def test_the_slot_of_the_next_cycle_survives_a_restart(paths, monkeypatch):
    config, state = paths
    write(config, scheduled())
    children = fake_children(monkeypatch)
    Egress(monkeypatch, [])
    first = Clock()
    first.current = EVENING
    first.on_wait = first.set  # Cycle 1 completes; the stop arrives while waiting for the 20:00 slot.
    assert service.run(config, state, project_root=ROOT, now=first.now, stop=first) == 2
    state_file = state / "state.private.json"
    stopped = service._read(state_file)
    assert stopped["completed_cycles"] == 1 and stopped["next_slot"] == {"start_time": "20:00", "window_days": 1}
    assert datetime.fromisoformat(stopped["next_cycle_at"]) == utc(20, 1)
    service._write(state_file, {**stopped, "status": "waiting"})
    second = Clock()
    second.current = EVENING + timedelta(minutes=5)
    assert service.run(config, state, project_root=ROOT, now=second.now, stop=second) == 0
    assert [w[:2] for w in windows(children)[:2]] == [("2026-10-13", "2026-10-19"), ("2026-10-19", "2026-10-19")]
    assert windows(children)[1][2] == {"start_time": "20:00", "window_days": 1}
    assert len(children) == 4


@pytest.mark.parametrize("state_facts", [
    {"completed_cycles": 1},                                                    # Written without a schedule.
    {"completed_cycles": 1, "next_slot": {"start_time": "03:00", "window_days": 1}},  # No longer in the schedule.
])
def test_a_later_cycle_without_a_valid_slot_stops_for_review(paths, monkeypatch, state_facts):
    config, state = paths
    write(config, scheduled())
    children = fake_children(monkeypatch)
    Egress(monkeypatch, [])
    state.mkdir(mode=0o700)
    service._write(state / "state.private.json", {"version": 1, "status": "retry_wait", **state_facts})
    assert service.run(config, state, project_root=ROOT, now=lambda: NOW) == 2
    assert children == []
    assert service._read(state / "state.private.json")["reason"] == "schedule_slot_unavailable"


def test_a_retried_later_cycle_keeps_the_window_of_its_slot(paths, monkeypatch):
    config, state = paths
    write(config, scheduled(max_cycles=2))
    children = fake_children(monkeypatch)
    Egress(monkeypatch, [])
    state.mkdir(mode=0o700)
    service._write(state / "state.private.json", {"version": 1, "status": "retry_wait", "completed_cycles": 1,
                                                  "next_slot": SCHEDULE[0]})
    assert service.run(config, state, project_root=ROOT, now=lambda: NOW) == 0
    assert windows(children) == [("2026-10-17", "2026-10-19", SCHEDULE[0])]


def test_the_first_cycle_ignores_a_slot_left_in_the_state(paths, monkeypatch):
    config, state = paths
    write(config, scheduled(max_cycles=1))
    children = fake_children(monkeypatch)
    Egress(monkeypatch, [])
    state.mkdir(mode=0o700)
    service._write(state / "state.private.json", {"version": 1, "status": "egress_unavailable",
                                                  "next_slot": SCHEDULE[0]})
    assert service.run(config, state, project_root=ROOT, now=lambda: NOW) == 0
    assert windows(children) == [("2026-10-13", "2026-10-19", None)]


def test_the_schedule_is_part_of_the_active_configuration(paths, monkeypatch):
    config, state = paths
    write(config, scheduled())
    fake_children(monkeypatch)
    Egress(monkeypatch, [])
    first = Clock()
    first.current = EVENING
    first.on_wait = first.set
    assert service.run(config, state, project_root=ROOT, now=first.now, stop=first) == 2
    state_file = state / "state.private.json"
    service._write(state_file, {**service._read(state_file), "status": "waiting"})
    changed = scheduled()
    changed["schedule"] = [{"start_time": "20:00", "window_days": 5}]
    write(config, changed)
    assert service.run(config, state, project_root=ROOT, now=lambda: EVENING) == 2
    assert service._read(state_file)["reason"] == "active_configuration_changed"


def test_without_a_schedule_the_state_and_the_plans_have_no_slot(paths, monkeypatch):
    config, state = paths
    value = configuration(max_cycles=2)
    value.update(start_time="01:00", interval_seconds=900)
    write(config, value)
    children = fake_children(monkeypatch)
    Egress(monkeypatch, [])
    clock = Clock()
    assert service.run(config, state, project_root=ROOT, now=clock.now, stop=clock) == 0
    assert "next_slot" not in service._read(state / "state.private.json")
    assert all(window[2] is None for window in windows(children))
    assert [w[:2] for w in windows(children)] == [("2026-10-13", "2026-10-19"), ("2026-10-14", "2026-10-20")]


# --- National listing and reading queue in the service ----------------------------------------------------------------

def national(**changes):
    value = configuration(**changes)
    argv = value["base_argv"]
    argv[argv.index("--keyword"):argv.index("--keyword") + 2] = ["--national-listing"]
    argv[argv.index("--years") + 1] = "window"
    return value


def test_the_national_listing_is_a_valid_selector_that_plans_through_the_real_planner(paths, monkeypatch):
    config, state = paths
    value = national()
    value["base_argv"] += ["--llm1-min-affinity", "85", "--reading-queue"]
    write(config, value)
    children = fake_children(monkeypatch)
    Egress(monkeypatch, [])
    assert service.run(config, state, project_root=ROOT, now=lambda: NOW) == 0
    plan = service._read(children[0].folder / "completion.private.json")
    requested = plan["requested"]
    assert requested["discovery_policy"] == "prod2_national_listing_v1" and requested["keywords"] == [""]
    assert requested["reading_queue"] is True and requested["llm1_min_affinity"] == 85
    assert requested["from_date"] == "2026-10-13" and requested["to_date"] == "2026-10-19"
    assert "--keyword" not in plan["argv"] and "--national-listing" in plan["argv"]


def test_the_national_listing_with_a_schedule_searches_each_slot_window(paths, monkeypatch):
    config, state = paths
    value = national(max_cycles=2)
    value.update(schedule=SCHEDULE, interval_seconds=900)
    write(config, value)
    children = fake_children(monkeypatch)
    Egress(monkeypatch, [])
    clock = Clock()
    clock.current = EVENING
    assert service.run(config, state, project_root=ROOT, now=clock.now, stop=clock) == 0
    assert [w[:2] for w in windows(children)] == [("2026-10-13", "2026-10-19"), ("2026-10-19", "2026-10-19")]
    plan = service._read(children[1].folder / "completion.private.json")
    assert plan["requested"]["from_date"] == plan["requested"]["to_date"] == "2026-10-19"


@pytest.mark.parametrize("change,reason", [
    (lambda argv: argv.extend(["--keyword", "software"]), "ValueError"),  # The planner refuses two selectors.
    (lambda argv: argv.remove("--national-listing"), "explicit_scope_and_paid_policy_required")])
def test_a_keyword_next_to_the_listing_or_no_selector_at_all_refuses_the_service_plan(paths, monkeypatch, change, reason):
    config, state = paths
    value = national()
    change(value["base_argv"])
    write(config, value)
    children = fake_children(monkeypatch)
    assert service.run(config, state, project_root=ROOT, now=lambda: NOW) == 2
    assert children == []
    assert service._read(state / "state.private.json")["reason"] == reason


def test_a_window_over_the_national_listing_limit_never_starts(paths, monkeypatch):
    config, state = paths
    value = national()
    value["window"]["days"] = 40
    write(config, value)
    children = fake_children(monkeypatch)
    assert service.run(config, state, project_root=ROOT, now=lambda: NOW) == 2
    assert children == []


def test_the_cycle_alert_counts_the_readings_left_in_the_queue(paths, monkeypatch):
    def queued(report):
        report.update(accepted=4, llm1_complete=1, accepted_unread=3, reading_queued=2, processed=4,
                      records=[{"status": "llm1_complete"}, *({"status": "accepted_unread"},) * 3])

    config, state = paths
    write(config, configuration())
    fake_children(monkeypatch, mutate=queued)
    egress = Egress(monkeypatch, [])
    assert service.run(config, state, project_root=ROOT, now=lambda: NOW) == 0
    assert "1 leídas, 3 sin leer, 2 en cola de lectura, 0 en revisión" in egress.alerts[0]


def test_the_cycle_alert_is_unchanged_without_queued_readings(paths, monkeypatch):
    config, state = paths
    write(config, configuration())
    fake_children(monkeypatch)
    egress = Egress(monkeypatch, [])
    assert service.run(config, state, project_root=ROOT, now=lambda: NOW) == 0
    assert "1 leídas, 0 en revisión" in egress.alerts[0] and "cola" not in egress.alerts[0]
