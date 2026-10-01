"""事件信封与流校验测试（缺失/乱序/缺口/迟到/未知类型一律 fail-closed）。"""

import unittest

from prescription.events import (
    GAP,
    LATE,
    MALFORMED,
    MISSING_FIELD,
    OUT_OF_ORDER,
    UNKNOWN_TYPE,
    StreamValidator,
)

P = "PSEUDO-001"


def ev(seq, etype, ts="2026-10-02T07:00:00", **data):
    base = {"event_id": f"e{seq}", "patient_ref": P, "ts": ts, "seq": seq, "type": etype}
    base.update(data)
    return base


class SingleEventTest(unittest.TestCase):
    def test_valid_event_parses(self):
        e, flags = StreamValidator().check(
            ev(1, "symptom_check", symptoms=["none"]))
        self.assertIsNotNone(e)
        self.assertEqual(flags, [])

    def test_missing_envelope_fields(self):
        for raw in [{}, {"event_id": "x"}, {"event_id": "x", "patient_ref": P},
                    {"event_id": "x", "patient_ref": P, "ts": "not-a-date", "seq": 1,
                     "type": "hr_sample", "hr_bpm": 80}]:
            _, flags = StreamValidator().check(raw)
            self.assertTrue(flags, raw)

    def test_unknown_type_is_flagged(self):
        _, flags = StreamValidator().check(ev(1, "mystery"))
        self.assertTrue(any(f.code == UNKNOWN_TYPE for f in flags))

    def test_missing_payload_field(self):
        _, flags = StreamValidator().check(ev(1, "hr_sample"))
        self.assertTrue(any(f.code == MISSING_FIELD for f in flags))

    def test_non_positive_value_is_malformed(self):
        _, flags = StreamValidator().check(ev(1, "hr_sample", hr_bpm=0))
        self.assertTrue(any(f.code == MALFORMED for f in flags))

    def test_unknown_symptom_is_malformed(self):
        _, flags = StreamValidator().check(ev(1, "symptom_check", symptoms=["leg_it_ch"]))
        self.assertTrue(any(f.code == MALFORMED for f in flags))

    def test_symptom_onset_none_rejected(self):
        _, flags = StreamValidator().check(ev(1, "symptom_onset", symptom="none"))
        self.assertTrue(flags)


class StreamOrderingTest(unittest.TestCase):
    def test_in_order_stream_accepted(self):
        v = StreamValidator()
        for seq in range(1, 4):
            e, flags = v.check(ev(seq, "symptom_check", symptoms=["none"]))
            self.assertIsNotNone(e)
            self.assertEqual(flags, [])

    def test_out_of_order_event_rejected_not_consumed(self):
        v = StreamValidator()
        v.check(ev(1, "symptom_check", symptoms=["none"]))
        v.check(ev(3, "symptom_check", ts="2026-10-02T07:02:00", symptoms=["none"]))
        _, flags = v.check(ev(2, "symptom_check", ts="2026-10-02T07:01:00", symptoms=["none"]))
        self.assertTrue(any(f.code == OUT_OF_ORDER for f in flags))

    def test_gap_is_flagged(self):
        v = StreamValidator()
        v.check(ev(1, "symptom_check", symptoms=["none"]))
        _, flags = v.check(ev(3, "symptom_check", ts="2026-10-02T07:02:00", symptoms=["none"]))
        self.assertTrue(any(f.code == GAP for f in flags))

    def test_clock_goes_backwards_is_late(self):
        v = StreamValidator()
        v.check(ev(1, "symptom_check", ts="2026-10-02T07:05:00", symptoms=["none"]))
        _, flags = v.check(ev(2, "symptom_check", ts="2026-10-02T07:01:00", symptoms=["none"]))
        self.assertTrue(any(f.code == LATE for f in flags))

    def test_duplicate_event_rejected(self):
        v = StreamValidator()
        v.check(ev(1, "symptom_check", symptoms=["none"]))
        _, flags = v.check(ev(1, "symptom_check", symptoms=["none"]))
        self.assertTrue(flags)


if __name__ == "__main__":
    unittest.main()
