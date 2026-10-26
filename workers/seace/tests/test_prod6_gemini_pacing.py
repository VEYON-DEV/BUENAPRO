"""Token pace of the paid Gemini calls and the quota window of a 429. Offline."""
from types import SimpleNamespace

import pytest

from buenapro_worker.matching.substantive_policy import checklist_omitted_proposal_documents
from buenapro_worker.prod6.gemini_pacing import GeminiPacer
from buenapro_worker.prod6.reading import quota_window


class Clock:
    def __init__(self):
        self.now, self.slept = 100.0, []

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


def pacer(clock, rate=150_000):
    return GeminiPacer(rate=lambda: rate, clock=lambda: clock.now, sleep=clock.sleep)


def test_the_first_send_never_waits_and_each_answer_delays_the_next_by_its_tokens():
    clock = Clock()
    pace = pacer(clock)
    assert pace.wait() == 0
    pace.spent(57_000, 1_000)  # A DIN evaluation: 58 000 tokens at 150 000/min = 23.2 s.
    assert pace.wait() == pytest.approx(23.2)
    pace.spent(3_500, 500)
    assert pace.wait() == pytest.approx(1.6)


def test_time_already_passed_counts_and_debts_add_up():
    clock = Clock()
    pace = pacer(clock)
    pace.spent(75_000, 0)  # 30 s
    clock.now += 10
    assert pace.wait() == pytest.approx(20)
    pace.spent(75_000, 0)
    pace.spent(75_000, 0)  # Two answers in a row before the next send: 60 s.
    assert pace.wait() == pytest.approx(60)


def test_the_nine_evaluations_of_26_10_now_stay_under_the_minute_budget():
    clock = Clock()
    pace = pacer(clock)
    sent = []
    for tokens in [3_900, 58_900, 4_800, 58_900, 58_600, 3_500, 4_300, 3_600, 58_200]:
        pace.wait()
        sent.append((clock.now, tokens))
        pace.spent(tokens, 0)
    for start, _ in sent:
        window = sum(tokens for at, tokens in sent if start <= at < start + 60)
        assert window <= 150_000 + 58_900  # At most one call beyond the budget starts in any minute.


def test_zero_disables_and_unknown_usage_spends_nothing():
    clock = Clock()
    off = pacer(clock, rate=0)
    off.spent(1_000_000, 0)
    assert off.wait() == 0
    on = pacer(clock)
    on.spent(None, None)
    assert on.wait() == 0 and clock.slept == []


def rejected(*quota_ids):
    return SimpleNamespace(details={"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "details": [
        {"@type": "type.googleapis.com/google.rpc.QuotaFailure",
         "violations": [{"quotaMetric": "m", "quotaId": quota} for quota in quota_ids]},
        {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "37s"}]}})


@pytest.mark.parametrize("quota_ids, window", [
    (["GenerateContentInputTokensPerModelPerMinute-FreeTier"], "minute"),
    (["GenerateRequestsPerMinutePerProjectPerModel"], "minute"),
    (["GenerateRequestsPerDayPerProjectPerModel-FreeTier"], "day"),
    (["GenerateRequestsPerMinutePerProjectPerModel", "GenerateRequestsPerDayPerProjectPerModel"], "day"),
    ([], None),
])
def test_the_quota_window_comes_from_the_quota_ids_and_a_daily_quota_rules(quota_ids, window):
    assert quota_window(rejected(*quota_ids)) == window


def test_an_error_without_details_has_no_window():
    assert quota_window(SimpleNamespace(details=None)) is None
    assert quota_window(ValueError("x")) is None


def requirement(index, kind):
    return {"source_index": index, "tipo": kind, "requisito": f"R{index}", "obligatorio": True, "detalle": {}}


def test_only_omitted_proposal_documents_move_to_the_checklist():
    expected = [requirement(2, "key_personnel"), requirement(7, "proposal_document"),
                requirement(8, "proposal_document"), requirement(13, "specific_experience")]
    judged = [{"source_index": 2}, {"source_index": 8}, {"source_index": 13}]
    checklist = [{"tipo": "rnp", "requisito": "RNP", "estado": "preparar", "score_impact": False}]
    kept = checklist_omitted_proposal_documents(judged, expected, checklist)
    # A proposal document the model judged (8) stays a requirement; another omission is not hidden.
    assert [r["source_index"] for r in kept] == [2, 8, 13]
    assert checklist[1:] == [{"tipo": "proposal_document", "requisito": "R7", "estado": "preparar",
                              "score_impact": False}]
    other = checklist_omitted_proposal_documents([{"source_index": 7}], expected[:2], [])
    assert [r["source_index"] for r in other] == [2, 7]
