import unittest
from automatic_stratagems.scan_session import ScanSession

CATALOG = dict.fromkeys('ABCD')
COLORS = {'A':'red','B':'blue','C':'green','D':'yellow'}

def report(*ids):
    return {'status':'partial' if None in ids else 'matched', 'rows':[{'id':x} for x in ids]}

class IncrementalTests(unittest.TestCase):
    def setUp(self):
        self.session=ScanSession()

    def scan(self, ids, slots=None, replace=False):
        token=self.session.begin(slots or {1:'any',2:'any',3:'any'}, replace=replace)
        self.session.finish(token,report(*ids),CATALOG,COLORS)
        return self.session.snapshot()

    def test_missing_is_badged_and_reconfirmed_without_moving(self):
        self.scan(['A','B'])
        s=self.scan(['B',None,'C'])
        self.assertEqual(dict(s.assignments),{1:'A',2:'B',3:'C'})
        self.assertEqual(s.unconfirmed_slots,frozenset({1}))
        self.assertFalse(s.unknown_slots)
        s=self.scan(['C','A','B'])
        self.assertFalse(s.unconfirmed_slots)
        self.assertEqual(dict(s.assignments),{1:'A',2:'B',3:'C'})

    def test_specific_filters_before_any_and_no_duplicates(self):
        s=self.scan(['A','B','A','C'],{1:'any',2:'red',3:'blue',4:'yellow'})
        self.assertEqual(dict(s.assignments),{1:'C',2:'A',3:'B',4:None})
        self.assertEqual(s.overflow,0)

    def test_unknowns_do_not_displace_recognized_or_guess_color(self):
        s=self.scan([None,'A'],{1:'red',2:'blue',3:'any'})
        self.assertEqual(dict(s.assignments),{1:'A',2:None,3:None})
        self.assertEqual(s.unknown_slots,frozenset({3}))

    def test_filter_exhaustion_reports_overflow_without_wrong_color(self):
        s=self.scan(['A','B'],{1:'red',2:'yellow'})
        self.assertEqual(dict(s.assignments),{1:'A',2:None})
        self.assertEqual(s.overflow,1)

    def test_hold_replaces_only_after_success(self):
        self.scan(['A','B'])
        token=self.session.begin({1:'any',2:'any',3:'any'},replace=True)
        self.assertEqual(self.session.snapshot().assignments[1],'A')
        self.assertTrue(self.session.snapshot().replacing)
        self.session.finish(token,report('C',None),CATALOG,COLORS)
        s=self.session.snapshot()
        self.assertEqual(dict(s.assignments),{1:'C',2:None,3:None})
        self.assertFalse(s.unconfirmed_slots)
        self.assertEqual(s.unknown_slots,frozenset({2}))

    def test_failed_replacement_preserves_icons_and_badges(self):
        self.scan(['A','B'])
        before=self.scan(['A',None])
        token=self.session.begin({1:'any',2:'any',3:'any'},replace=True)
        self.session.fail(token,'capture failed')
        after=self.session.snapshot()
        self.assertEqual(after.assignments,before.assignments)
        self.assertEqual(after.unconfirmed_slots,before.unconfirmed_slots)
        self.assertEqual(after.unknown_slots,before.unknown_slots)

    def test_changed_filter_clears_ineligible_assignment_on_success(self):
        self.scan(['A'])
        s=self.scan(['A','B'],{1:'blue',2:'red'})
        self.assertEqual(dict(s.assignments),{1:'B',2:'A'})

    def test_incremental_unknowns_do_not_add_duplicate_unknown_buttons(self):
        self.scan(['A','B'])
        s=self.scan(['A',None])
        self.assertEqual(s.unconfirmed_slots,frozenset({2}))
        self.assertFalse(s.unknown_slots)

class ReplacementPriorityTests(unittest.TestCase):
    catalog = dict.fromkeys(['red1', 'red2', 'red3', 'blue1', 'blue2', 'green1'])
    colors = {key: key.rstrip('123') for key in catalog}

    def setUp(self):
        self.session = ScanSession()
        self.slots = {1: 'any', 2: 'any', 3: 'any'}

    def scan(self, *ids, replace=False):
        token = self.session.begin(self.slots, replace=replace)
        self.session.finish(token, report(*ids), self.catalog, self.colors)
        return self.session.snapshot()

    def test_empty_slot_precedes_same_color_replacement(self):
        self.scan('red1', 'blue1')
        s = self.scan('blue1', 'red2')
        self.assertEqual(dict(s.assignments), {1: 'red1', 2: 'blue1', 3: 'red2'})
        self.assertEqual(s.unconfirmed_slots, frozenset({1}))

    def test_same_color_precedes_older_different_color(self):
        self.scan('blue1', 'red1', 'green1')
        self.scan('red1', 'green1')
        s = self.scan('green1', 'red2')
        self.assertEqual(dict(s.assignments), {1: 'blue1', 2: 'red2', 3: 'green1'})
        self.assertEqual(s.unconfirmed_slots, frozenset({1}))
        self.assertEqual(s.overflow, 0)

    def test_oldest_same_color_survives_repeated_miss_and_capture_failure(self):
        self.scan('red1', 'red2', 'green1')
        self.scan('red1', 'green1')
        self.scan('green1')
        token = self.session.begin(self.slots)
        self.session.fail(token, 'capture failed')
        s = self.scan('green1', 'red3')
        self.assertEqual(dict(s.assignments), {1: 'red1', 2: 'red3', 3: 'green1'})

    def test_reconfirmation_resets_age(self):
        self.scan('red1', 'red2', 'green1')
        self.scan('red2', 'green1')
        self.scan('green1')
        self.scan('red1', 'green1')
        s = self.scan('green1', 'red3')
        self.assertEqual(dict(s.assignments), {1: 'red1', 2: 'red3', 3: 'green1'})

    def test_cross_color_fallback_uses_oldest_any_slot(self):
        self.scan('red1', 'blue1', 'green1')
        self.scan('red1', 'green1')
        s = self.scan('green1', 'red2', 'red3')
        self.assertEqual(dict(s.assignments), {1: 'red2', 2: 'red3', 3: 'green1'})
        self.assertFalse(s.unconfirmed_slots)
        self.assertEqual(s.overflow, 0)

    def test_same_color_matches_reserved_before_cross_color_fallbacks(self):
        self.scan('red1', 'blue1', 'green1')
        s = self.scan('green1', 'blue2', 'red2')
        self.assertEqual(dict(s.assignments), {1: 'red2', 2: 'blue2', 3: 'green1'})

    def test_filters_and_confirmed_slots_cannot_be_evicted(self):
        self.slots = {1: 'red', 2: 'any'}
        self.scan('red1', 'green1')
        s = self.scan('green1', 'blue1')
        self.assertEqual(dict(s.assignments), {1: 'red1', 2: 'green1'})
        self.assertEqual(s.overflow, 1)
        self.assertEqual(s.unconfirmed_slots, frozenset({1}))

    def test_unknown_alone_does_not_evict(self):
        self.scan('red1', 'blue1', 'green1')
        s = self.scan('green1', None, None)
        self.assertEqual(dict(s.assignments), {1: 'red1', 2: 'blue1', 3: 'green1'})
        self.assertEqual(s.unconfirmed_slots, frozenset({1, 2}))

    def test_cross_color_fallback_chooses_age_not_slot_number(self):
        self.slots = {1: 'any', 2: 'any'}
        self.scan('red1', 'blue1')
        self.scan('red1')
        s = self.scan('green1')
        self.assertEqual(dict(s.assignments), {1: 'red1', 2: 'green1'})
        self.assertEqual(s.unconfirmed_slots, frozenset({1}))

    def test_later_same_color_item_is_not_displaced_by_earlier_fallback(self):
        self.slots = {1: 'any', 2: 'any'}
        self.scan('red1', 'blue1')
        s = self.scan('green1', 'red2')
        self.assertEqual(dict(s.assignments), {1: 'red2', 2: 'green1'})

    def test_hold_rebuild_drops_old_unconfirmed_age(self):
        self.scan('red1', 'blue1', 'green1')
        self.scan('blue1', 'green1')
        self.scan('red1', 'red2', 'green1', replace=True)
        self.scan('red1', 'green1')
        s = self.scan('green1', 'red3')
        self.assertEqual(dict(s.assignments), {1: 'red1', 2: 'red3', 3: 'green1'})
