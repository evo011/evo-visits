"""Die Erkennung muss mit unterschiedlichen, plausiblen Seitenformaten klarkommen."""

import json
import unittest

from evo.extract import best_from_html, best_from_json, embedded_json_blobs, single_from_json


def as_map(cand):
    return {r.gym: (r.count, r.capacity, r.pct) for r in cand.readings}


class JsonHeuristics(unittest.TestCase):
    def test_flat_list(self):
        data = [{"id": 1, "name": "HAMBURG ALTONA", "current": 42, "max": 120},
                {"id": 2, "name": "BERLIN MITTE", "current": 17, "max": 90}]
        c = best_from_json(data)
        self.assertEqual(as_map(c), {"HAMBURG ALTONA": (42, 120, None), "BERLIN MITTE": (17, 90, None)})

    def test_nested_objects(self):
        data = {"data": {"clubs": [
            {"id": "a1", "club": {"name": "Köln Ehrenfeld", "zip": "50823"}, "visitors": {"current": 12, "capacity": 80}},
            {"id": "a2", "club": {"name": "Düsseldorf Bilk", "zip": "40225"}, "visitors": {"current": 30, "capacity": 100}},
        ]}}
        c = best_from_json(data)
        self.assertEqual(as_map(c), {"Köln Ehrenfeld": (12, 80, None), "Düsseldorf Bilk": (30, 100, None)})

    def test_dict_of_dicts(self):
        data = {"hamburg-altona": {"current": 12, "max": 100}, "dresden-prager-strasse": {"current": 7, "max": 60}}
        c = best_from_json(data)
        self.assertEqual(as_map(c)["hamburg-altona"], (12, 100, None))
        self.assertEqual(len(c.readings), 2)

    def test_fraction_is_percent(self):
        data = {"locations": [{"title": "MÜNCHEN SCHWABING", "utilization": 0.45},
                              {"title": "WIESBADEN DERN'SCHE HÖFE", "utilization": 0.2}]}
        c = best_from_json(data)
        self.assertAlmostEqual(as_map(c)["MÜNCHEN SCHWABING"][2], 45.0)

    def test_numeric_strings_and_camel_case(self):
        data = {"items": [{"locationName": "Berlin Friedrichshain", "checkedInCount": "23", "maxCapacity": "150"},
                          {"locationName": "Berlin Wedding", "checkedInCount": "9", "maxCapacity": "120"}]}
        c = best_from_json(data)
        self.assertEqual(as_map(c)["Berlin Friedrichshain"], (23, 150, None))

    def test_club_list_beats_forecast_list(self):
        data = {
            "forecast": [{"label": "Mo", "value": 20}, {"label": "Di", "value": 25}, {"label": "Mi", "value": 30}],
            "clubs": [{"name": f"HAMBURG CLUB {i}", "visitors": i * 3} for i in range(1, 12)],
        }
        c = best_from_json(data)
        self.assertEqual(len(c.readings), 11)
        self.assertTrue(all(r.gym.startswith("HAMBURG") for r in c.readings))

    def test_ids_and_coordinates_are_not_counts(self):
        data = [{"id": 101, "name": "Bonn Zentrum", "lat": 50.7, "lng": 7.1, "zip": 53111},
                {"id": 102, "name": "Essen Rü", "lat": 51.4, "lng": 7.0, "zip": 45130}]
        self.assertIsNone(best_from_json(data))

    def test_single_object(self):
        r = single_from_json({"status": "ok", "data": {"currentVisitors": 33, "maxVisitors": 100}})
        self.assertEqual((r.count, r.capacity), (33, 100))


class HtmlHeuristics(unittest.TestCase):
    def test_cards_with_headings(self):
        page = """
        <html><head><title>Visits - EVO Fitness</title></head><body>
          <h1>Visits</h1>
          <div class="club-card"><h3>WIESBADEN DERN'SCHE HÖFE</h3><span class="count">42</span> <span>Personen</span></div>
          <div class="club-card"><h3>HAMBURG ALTONA</h3><p>17 Personen im Club</p></div>
          <div class="club-card"><h3>KÖLN EHRENFELD</h3><p>23 / 150</p></div>
        </body></html>"""
        c = best_from_html(page)
        m = as_map(c)
        self.assertEqual(m["WIESBADEN DERN'SCHE HÖFE"][0], 42)
        self.assertEqual(m["HAMBURG ALTONA"][0], 17)
        self.assertEqual(m["KÖLN EHRENFELD"][:2], (23, 150))
        self.assertNotIn("Visits", m)

    def test_percent_cards(self):
        page = """<ul>
          <li><span class="location-name">Berlin Mitte</span><span class="load">Auslastung 35 %</span></li>
          <li><span class="location-name">Berlin Wedding</span><span class="load">Auslastung 12 %</span></li></ul>"""
        m = as_map(best_from_html(page))
        self.assertEqual(m["Berlin Mitte"][2], 35)

    def test_embedded_next_data_and_inertia(self):
        payload = {"props": {"pageProps": {"clubs": [{"name": "DRESDEN PRAGER STRASSE", "current": 5},
                                                     {"name": "DRESDEN NEUSTADT", "current": 8}]}}}
        page = f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(payload)}</script>'
        blobs = embedded_json_blobs(page)
        c = best_from_json(blobs[0][1])
        self.assertEqual(len(c.readings), 2)

        inertia = json.dumps({"component": "Visits", "props": {"clubs": [
            {"title": "MÜNCHEN PASING", "people": 14}, {"title": "MÜNCHEN GIESING", "people": 21}]}})
        page2 = '<div id="app" data-page="' + inertia.replace('"', "&quot;") + '"></div>'
        c2 = best_from_json(embedded_json_blobs(page2)[0][1])
        self.assertEqual(as_map(c2)["MÜNCHEN GIESING"][0], 21)

    def test_window_state(self):
        page = """<script>window.__INITIAL_STATE__ = {"visits": {"list": [
            {"clubName": "Frankfurt Bockenheim", "count": 31}, {"clubName": "Frankfurt Ostend", "count": 4}]}};</script>"""
        blobs = embedded_json_blobs(page)
        c = best_from_json(blobs[0][1])
        self.assertEqual(as_map(c)["Frankfurt Ostend"][0], 4)


if __name__ == "__main__":
    unittest.main()
