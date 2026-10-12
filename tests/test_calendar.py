import unittest
from datetime import date

from evo.calendar_de import easter_sunday, holiday_name, is_bridge_day, locate


class CalendarTests(unittest.TestCase):
    def test_easter(self):
        self.assertEqual(easter_sunday(2026), date(2026, 4, 5))
        self.assertEqual(easter_sunday(2027), date(2027, 3, 28))
        self.assertEqual(easter_sunday(2025), date(2025, 4, 20))

    def test_state_specific_holidays(self):
        self.assertEqual(holiday_name(date(2026, 5, 14), "NW"), "Christi Himmelfahrt")
        self.assertEqual(holiday_name(date(2026, 6, 4), "NW"), "Fronleichnam")
        self.assertIsNone(holiday_name(date(2026, 6, 4), "HH"))
        self.assertEqual(holiday_name(date(2026, 10, 31), "HH"), "Reformationstag")
        self.assertEqual(holiday_name(date(2026, 11, 18), "SN"), "Buß- und Bettag")
        self.assertEqual(holiday_name(date(2026, 3, 8), "BE"), "Internationaler Frauentag")

    def test_bridge_days(self):
        self.assertTrue(is_bridge_day(date(2026, 5, 15), "NW"))   # Freitag nach Himmelfahrt
        self.assertTrue(is_bridge_day(date(2026, 6, 5), "NW"))    # Freitag nach Fronleichnam
        self.assertFalse(is_bridge_day(date(2026, 6, 5), "HH"))
        self.assertTrue(is_bridge_day(date(2026, 12, 28), "HH"))  # zwischen den Jahren
        self.assertFalse(is_bridge_day(date(2026, 10, 12), "NW"))

    def test_locate(self):
        self.assertEqual(locate("WIESBADEN DERN'SCHE HÖFE", "Düsseldorf").state, "HE")
        self.assertEqual(locate("Köln Ehrenfeld", "Düsseldorf").key, "köln")
        self.assertEqual(locate("Demo Nord", "Düsseldorf").key, "düsseldorf")
        self.assertEqual(locate("Demo Nord", "Düsseldorf", {"Demo Nord": "Hamburg"}).state, "HH")


if __name__ == "__main__":
    unittest.main()
