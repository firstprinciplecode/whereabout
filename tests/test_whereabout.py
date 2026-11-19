"""Unit tests for `whereabout` protocol, place matching, and SQLite place memory."""

from __future__ import annotations

import json
import os
import tempfile
import unittest

from whereabout.cache import match_place
from whereabout.cli import main as cli_main
from whereabout.engine import snapshot
from whereabout.memory import (
    REVISIT_GAP_SECONDS,
    add_note,
    delete_note,
    derive_keys,
    list_notes,
    list_places_and_visits,
    observe_and_recall,
    set_label,
)
from whereabout.protocol import SCHEMA_VERSION


class WhereaboutTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="whereabout-test-")
        self.db_path = os.path.join(self._tmp.name, "memory.db")
        self.cache_path = os.path.join(self._tmp.name, "cache.json")
        self.cfg_path = os.path.join(self._tmp.name, "config.json")
        self._old_env = {
            k: os.environ.get(k)
            for k in ("WHEREABOUT_MEMORY_DB", "WHEREABOUT_CACHE", "WHEREABOUT_CONFIG")
        }
        os.environ["WHEREABOUT_MEMORY_DB"] = self.db_path
        os.environ["WHEREABOUT_CACHE"] = self.cache_path
        os.environ["WHEREABOUT_CONFIG"] = self.cfg_path

    def tearDown(self) -> None:
        for k, v in self._old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self._tmp.cleanup()

    def test_place_specificity_scoring(self) -> None:
        cfg = {
            "places": [
                {"name": "Building", "gateway_mac": ["aa:bb:cc:dd:ee:01"], "ssid": ["CorpWiFi"]},
                {"name": "3rd Floor Lab", "bssid": ["11:22:33:44:55:66"]},
            ]
        }
        # Matching both router MAC and specific AP -> AP wins
        hit = match_place(
            cfg,
            ssid="CorpWiFi",
            bssid="11:22:33:44:55:66",
            gateway_mac="aa:bb:cc:dd:ee:01",
        )
        self.assertIsNotNone(hit)
        assert hit is not None
        self.assertEqual(hit["name"], "3rd Floor Lab")

        # Roaming to another AP on the same router -> Building wins
        hit2 = match_place(
            cfg,
            ssid="CorpWiFi",
            bssid="11:22:33:44:55:99",
            gateway_mac="aa:bb:cc:dd:ee:01",
        )
        self.assertIsNotNone(hit2)
        assert hit2 is not None
        self.assertEqual(hit2["name"], "Building")

    def test_memory_visits_labels_and_scoped_notes(self) -> None:
        net_a = {
            "online": True,
            "ssid": "StudioNet",
            "bssid": "aa:bb:cc:00:00:01",
            "gateway": "192.168.1.1",
            "gateway_mac": "70:00:00:00:00:01",
            "fingerprint": "fp_studio",
        }
        loc_a = {"lat": 40.72, "lon": -73.96, "city": "Brooklyn", "region": "NY"}
        dev = {"id": "laptop-1"}

        keys = derive_keys(net_a, loc_a)
        self.assertEqual(keys["ap"], "ap:aa:bb:cc:00:00:01")
        self.assertEqual(keys["place"], "gw:70:00:00:00:00:01")
        self.assertEqual(keys["area"], "area:40.72,-73.96")

        # First observation -> visit #1, status=new
        m1 = observe_and_recall(net_a, loc_a, dev, track=True)
        self.assertEqual(m1["visits"], 1)
        self.assertEqual(m1["status"], "new")

        # Immediate second observation on another extender of SAME router -> still visit #1!
        net_a_ext2 = dict(net_a, bssid="aa:bb:cc:00:00:02")
        m2 = observe_and_recall(net_a_ext2, loc_a, dev, track=True)
        self.assertEqual(m2["visits"], 1)
        self.assertGreaterEqual(REVISIT_GAP_SECONDS, 3600)

        # Label building, AP, and add scoped notes
        set_label(net_a, loc_a, "Studio", scope="place")
        set_label(net_a, loc_a, "North Desk AP", scope="ap")
        n_place = add_note(net_a, loc_a, "Laser cutter is on breaker #4", scope="place", kind="device")
        n_ap = add_note(net_a, loc_a, "Soldering fume extractor on smart plug 2", scope="ap", kind="tip")
        n_area = add_note(net_a, loc_a, "Hardware store on Grand St closes at 6pm", scope="area", kind="note")
        self.assertEqual(n_place["action"], "saved")
        self.assertEqual(n_ap["action"], "saved")
        self.assertEqual(n_area["action"], "saved")

        # Recall on AP #1 -> sees AP label + all 3 notes ordered ap -> place -> area
        m3 = observe_and_recall(net_a, loc_a, dev, track=True)
        self.assertEqual(m3["label"], "Studio")
        self.assertEqual(m3["subplace"], "North Desk AP")
        self.assertEqual([x["scope"] for x in m3["notes"]], ["ap", "place", "area"])

        # Roam to AP #2 -> sees Studio + area notes, NOT AP #1's desk note
        m4 = observe_and_recall(net_a_ext2, loc_a, dev, track=True)
        self.assertEqual(m4["label"], "Studio")
        self.assertNotIn("subplace", m4)
        self.assertEqual([x["scope"] for x in m4["notes"]], ["place", "area"])

        # Switch to a different building -> records transition (`previous` = Studio)
        net_b = {
            "online": True,
            "ssid": "CafeNet",
            "gateway": "10.0.0.1",
            "gateway_mac": "80:00:00:00:00:99",
            "fingerprint": "fp_cafe",
        }
        loc_b = {"lat": 40.72, "lon": -73.96, "city": "Brooklyn", "region": "NY"}  # same neighborhood!
        m5 = observe_and_recall(net_b, loc_b, dev, track=True)
        self.assertEqual(m5["visits"], 1)
        self.assertEqual(m5["previous"], "Studio")
        # Neighborhood area note STILL surfaces at the cafe down the block!
        self.assertEqual(len(m5["notes"]), 1)
        self.assertEqual(m5["notes"][0]["scope"], "area")

        # Return to Studio -> increments Studio to visit #2, previous = CafeNet
        m6 = observe_and_recall(net_a, loc_a, dev, track=True)
        self.assertEqual(m6["visits"], 2)
        self.assertIn("CafeNet", m6["previous"])

        # Delete note and verify
        del_res = delete_note(n_ap["id"])
        self.assertTrue(del_res["ok"])
        self.assertEqual(len(list_notes(net_a, loc_a)), 2)

        ledger = list_places_and_visits()
        self.assertGreaterEqual(len(ledger["places"]), 2)
        self.assertGreaterEqual(len(ledger["recent_visits"]), 3)

    def test_fast_snapshot_and_cli_json(self) -> None:
        snap = snapshot(fast=True, track=False)
        self.assertEqual(snap["schema"], SCHEMA_VERSION)
        self.assertIn("device", snap)
        self.assertIn("network", snap)

        rc = cli_main(["--fast", "--no-track", "--only", "device,time", "--json"])
        self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
