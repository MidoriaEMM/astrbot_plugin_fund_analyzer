"""打板选股引擎单元测试（纯函数，无 Tushare 网络）。"""

from __future__ import annotations

import unittest

from tushare_client.daban_pick_engine import (
    DabanPickConfig,
    DabanPickMode,
    apply_dragon_dedup,
    enrich_limit_pool,
    passes_mode_filter,
    percentile_ranks,
    run_daban_pick_pipeline,
    score_candidates,
)
from tushare_client.daban_sector import extract_concept_key


class TestPercentileRanks(unittest.TestCase):
    def test_order(self):
        ranks = percentile_ranks([1.0, 2.0, 3.0])
        self.assertEqual(ranks[0], 0.0)
        self.assertEqual(ranks[2], 1.0)

    def test_single(self):
        self.assertEqual(percentile_ranks([5.0]), [0.5])


class TestModeFilter(unittest.TestCase):
    def setUp(self):
        self.cfg = DabanPickConfig()

    def test_shouban(self):
        row = {"nums": 2, "open_times": 0, "sector_limit_count": 3}
        self.assertFalse(passes_mode_filter(row, DabanPickMode.SHOUBAN, self.cfg))
        row2 = {"nums": 1, "open_times": 3, "sector_limit_count": 1}
        self.assertFalse(passes_mode_filter(row2, DabanPickMode.SHOUBAN, self.cfg))
        row3 = {"nums": 1, "open_times": 1, "sector_limit_count": 1}
        self.assertTrue(passes_mode_filter(row3, DabanPickMode.SHOUBAN, self.cfg))

    def test_relay(self):
        self.assertFalse(
            passes_mode_filter(
                {"nums": 1, "open_times": 0, "sector_limit_count": 1},
                DabanPickMode.RELAY,
                self.cfg,
            )
        )
        self.assertTrue(
            passes_mode_filter(
                {"nums": 3, "open_times": 1, "sector_limit_count": 1},
                DabanPickMode.RELAY,
                self.cfg,
            )
        )

    def test_dragon_sector(self):
        self.assertFalse(
            passes_mode_filter(
                {"nums": 2, "open_times": 0, "sector_limit_count": 1},
                DabanPickMode.DRAGON,
                self.cfg,
            )
        )


class TestDragonDedup(unittest.TestCase):
    def test_one_per_concept(self):
        rows = [
            {"ts_code": "A.SZ", "concept_key": "光伏", "score": 0.9},
            {"ts_code": "B.SZ", "concept_key": "光伏", "score": 0.7},
            {"ts_code": "C.SZ", "concept_key": "锂电", "score": 0.8},
        ]
        out = apply_dragon_dedup(rows)
        codes = {r["ts_code"] for r in out}
        self.assertEqual(codes, {"A.SZ", "C.SZ"})


class TestConceptKey(unittest.TestCase):
    def test_from_desc(self):
        k = extract_concept_key(ths_lu_desc="光伏+涨停", ths_tag=None, name="测试")
        self.assertEqual(k, "光伏")

    def test_noise_stripped(self):
        k = extract_concept_key(ths_lu_desc="人工智能概念", ths_tag=None, name="")
        self.assertIn("人工", k)


class TestPipeline(unittest.TestCase):
    def _sample_pool(self) -> list[dict]:
        lu = [
            {
                "ts_code": "000001.SZ",
                "name": "平安",
                "open_times": 0,
                "fd_amount": 5e7,
                "limit_times": 1,
                "first_time": "09:35:00",
                "pct_chg": 10.0,
            },
            {
                "ts_code": "600000.SH",
                "name": "浦发",
                "open_times": 2,
                "fd_amount": 1e7,
                "limit_times": 2,
                "first_time": "14:00:00",
                "pct_chg": 10.0,
            },
        ]
        mf = [
            {"ts_code": "000001.SZ", "net_amount": 5000, "buy_elg_amount": 3000},
            {"ts_code": "600000.SH", "net_amount": -1000, "buy_elg_amount": 500},
        ]
        ths = [
            {"ts_code": "000001.SZ", "lu_desc": "银行", "limit_up_suc_rate": 60},
            {"ts_code": "600000.SH", "lu_desc": "银行", "limit_up_suc_rate": 40},
        ]
        step = [
            {"ts_code": "000001.SZ", "nums": 1},
            {"ts_code": "600000.SH", "nums": 3},
        ]
        return enrich_limit_pool(lu, mf, ths_rows=ths, step_rows=step)

    def test_scoring_produces_tiers(self):
        cands = self._sample_pool()
        cfg = DabanPickConfig(top_n=10, mode=DabanPickMode.MIXED, score_min=0.0)
        stats: dict = {}
        out = run_daban_pick_pipeline(cands, cfg, n_limit=2, has_ths=True, stats_out=stats)
        self.assertGreaterEqual(len(out), 1)
        self.assertIn("tier", out[0])
        self.assertIn("score", out[0])

    def test_relay_filters_shouban(self):
        cands = self._sample_pool()
        cfg = DabanPickConfig(top_n=10, mode=DabanPickMode.RELAY, score_min=0.0)
        out = run_daban_pick_pipeline(cands, cfg, n_limit=2, has_ths=True)
        codes = [r["ts_code"] for r in out]
        self.assertIn("600000.SH", codes)
        self.assertNotIn("000001.SZ", codes)


if __name__ == "__main__":
    unittest.main()
