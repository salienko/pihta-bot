import unittest
from datetime import date, datetime, timedelta
from domain import TZ, RuleError, clean_name, horizon, normalize_phone, order_card, quote, visit_end
from settings import load


class DomainTests(unittest.TestCase):
    def setUp(self):
        self.settings = load()
        self.catalog = {item['code']: item for item in self.settings.content['services']}
        self.start = datetime(2026, 10, 15, 10, tzinfo=TZ)

    def data(self, **changes):
        d = dict(start=self.start.isoformat(), hours=2, guests=4, children=0, cart={},
                 name='Тест', phone='+79990000000', telegram_id=2, username='')
        return dict(d, **changes)

    def test_rates_and_evening_transition(self):
        for hour, hours, total in ((16, 2, 500000), (19, 2, 600000), (18, 3, 830000), (16, 4, 1010000)):
            with self.subTest(hour=hour, hours=hours):
                d = self.data(start=self.start.replace(hour=hour).isoformat(), hours=hours)
                self.assertEqual(quote(d, self.catalog, {})['total'], total)

    def test_holiday_and_weekend(self):
        d = self.data(start=self.start.replace(day=17).isoformat(), hours=3)
        self.assertEqual(quote(d, self.catalog, {})['total'], 880000)
        h = {'2026-10-17': {'base': 650000, 'extra': 300000, 'label': 'Праздник'}}
        self.assertEqual(quote(d, self.catalog, h)['total'], 950000)

    def test_children_and_services(self):
        d = self.data(guests=8, children=2, cart={'birch': 2, 'oak': 1})
        q = quote(d, self.catalog, {})
        self.assertEqual(q['guest_extra'], 100000)
        self.assertEqual(q['total'], 760000)
        self.assertEqual(quote(self.data(guests=8, children=4), self.catalog, {})['total'], 500000)

    def test_invalid_counts(self):
        for guests, children in ((0, 0), (9, 0), (2, 3), (2, -1), (True, 0)):
            with self.subTest(guests=guests, children=children), self.assertRaises(RuleError):
                quote(self.data(guests=guests, children=children), self.catalog, {})
        for cart in ({'birch': 0}, {'birch': 101}, {'unknown': 1}, {'birch': True}):
            with self.subTest(cart=cart), self.assertRaises(RuleError):
                quote(self.data(cart=cart), self.catalog, {})

    def test_windows(self):
        clock = self.start - timedelta(hours=1)
        self.assertEqual(visit_end(self.start, 5, clock), self.start + timedelta(hours=5))
        with self.assertRaises(RuleError):
            visit_end(self.start, 2, clock + timedelta(seconds=1))
        with self.assertRaises(RuleError):
            visit_end(self.start.replace(hour=11), 2, clock)
        with self.assertRaises(RuleError):
            visit_end(self.start.replace(hour=22), 3, clock)
        end = visit_end(self.start.replace(hour=22), 2, clock)
        self.assertEqual(end, self.start.replace(day=16, hour=0))
        with self.assertRaises(RuleError):
            visit_end(self.start.replace(tzinfo=None), 2, clock)

    def test_calendar_horizon(self):
        self.assertEqual(horizon(date(2026, 12, 31)), date(2027, 2, 28))

    def test_contacts(self):
        for value in ('+7 (981) 244-00-50', '89812440050', '9812440050'):
            self.assertEqual(normalize_phone(value), '+79812440050')
        for value in ('123', '+3809812440050', 'abc', '+89812440050'):
            with self.assertRaises(RuleError):
                normalize_phone(value)
        self.assertEqual(clean_name('  Иван   Иванов  '), 'Иван Иванов')
        with self.assertRaises(RuleError):
            clean_name('A')

    def test_admin_card_is_bounded_and_clearly_a_request(self):
        data = self.data(name='И' * 100, cart={code: 100 for code in self.catalog})
        text = order_card('A' * 12, data, quote(data, self.catalog, {}), True)
        self.assertLess(len(text), 4096)
        self.assertIn('не закрепляет время', text)
        self.assertIn('Телефон:', text)


if __name__ == '__main__':
    unittest.main()
