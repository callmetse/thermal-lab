import copy
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from knowledge.experiment_query import RESTART_RUN, RESTART_IDS, query, restart_documents
from lab.knowledge_retrieval import build, retrieve


class ExperimentQueryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.index = Path(temporary.name) / "snapshot.sqlite3"
        build(ROOT / "knowledge/project_corpus.json", self.index, additional=restart_documents(ROOT.parents[1]))
        self.summary = {"batch": {"batch_id": RESTART_RUN, "run_id": RESTART_RUN, "channel": "D",
                                  "deleted_ms": None, "execution_status": "completed", "archive_status": "ok"},
                        "observation_count": 152}
        self.question = "D通道数据服务重启后最终归档多少条云端观测，怎样证明批次继续？"

    def test_answer_joins_typed_summary_and_scoped_citations(self):
        answer = query(self.index, self.question, RESTART_RUN, lambda _: self.summary)
        self.assertEqual(answer["status"], "answered")
        self.assertEqual(answer["facts"]["cloud_observations"], 152)
        self.assertEqual(answer["facts"]["before_stop"], 5)
        self.assertEqual([doc["id"] for doc in answer["citations"]], ["batch-summary", *RESTART_IDS])

    def test_deleted_batch_and_mismatched_counts_are_excluded(self):
        for field, value, reason in (("deleted_ms", 10, "batch_unavailable"),
                                     ("batch_id", "other", "batch_unavailable")):
            summary = copy.deepcopy(self.summary)
            summary["batch"][field] = value
            with self.subTest(field=field):
                result = query(self.index, self.question, RESTART_RUN, lambda _: summary)
                self.assertEqual(result["reason"], reason)
                self.assertEqual(result["citations"], [])
        self.summary["observation_count"] = 151
        result = query(self.index, self.question, RESTART_RUN, lambda _: self.summary)
        self.assertEqual(result["reason"], "summary_disagrees_with_reviewed_evidence")

    def test_no_support_and_no_batch_scope(self):
        self.assertEqual(query(self.index, "zzzzzzz")["status"], "insufficient_evidence")
        hits = retrieve(self.index, self.question, limit=20)
        self.assertTrue(all(doc["id"] not in RESTART_IDS for doc in hits))
        result = query(self.index, "重启之后新增的观测有多少条", RESTART_RUN, lambda _: self.summary)
        self.assertEqual(result["status"], "insufficient_evidence")

    def test_missed_detections_require_ground_truth(self):
        result = query(self.index, "D 批次一共漏检了多少条观测？", RESTART_RUN, lambda _: self.summary)
        self.assertEqual(result["status"], "insufficient_evidence")
        self.assertEqual(result["reason"], "ground_truth_required")
        self.assertNotIn("152", result["answer"])
        self.assertNotIn("facts", result)
        self.assertEqual(result["citations"], [])

    def test_question_channel_must_match_selected_batch(self):
        result = query(self.index, "A通道归档了多少条观测？", RESTART_RUN, lambda _: self.summary)
        self.assertEqual(result["status"], "insufficient_evidence")
        self.assertEqual(result["reason"], "batch_channel_mismatch")
        self.assertIn("D 通道", result["answer"])
        self.assertIn("请选择 A 通道", result["answer"])
        self.assertNotIn("facts", result)
        self.assertEqual(result["citations"], [])

    def test_archive_question_wording(self):
        cases = {
            "total": (
                "已归档了多少条观测？",
                "归档了多少条观测？",
                "这批次归档了多少条观测记录？",
                "这个批次总计归档了多少条观测？",
                "这个批次一共归档了多少条观测结果？",
                "所选实验批次已归档多少条观测记录？",
                "当前批次合计记录了几条观测？",
                "请问归档了几条观测？",
                "D通道归档了多少条观测？",
                "Ｄ通道归档了多少条观测？",
                "d 通道归档了几条观测记录？",
                "D路归档了多少条观测结果？",
                "D批次的观测已归档了多少条？",
                "归档的观测记录总数是多少？",
            ),
            "evidence": (
                self.question,
                "D 通道数据服务重启后，原批次最终归档了多少条云端观测？怎样证明还是同一批？",
                "D 通道数据服务重启后最终归档多少条云端观测？",
                "D通道归档了多少条云端观测？",
                "云端观测已归档了多少条？",
                "归档的云端观测记录数量是多少？",
                "数据服务重启之后原批次累计归档了多少条观测？",
            ),
            "batch_channel_mismatch": (
                "A通道归档了多少条观测？",
                "Ｂ通道归档了几条观测记录？",
                "c 通道观测已归档了多少条？",
                "A批次的归档观测数量是多少？",
            ),
            "ground_truth_required": (
                "D 批次一共漏检了多少条观测？",
                "这一批有多少个没检出的人？",
                "有多少目标漏识别了？",
                "没有被模型检出的目标有哪些，未检出多少？",
            ),
            "unsupported_batch_question": (
                "这批观测的平均置信度是多少？",
                "丢失了多少条归档观测？",
                "漏了几条观测没归档？",
                "重启之后新增的观测有多少条？",
                "数据服务重启后归档多少条云端观测？",
                "重启前归档了多少条观测？",
                "过去五分钟归档了多少条观测？",
                "昨天归档了多少条观测？",
                "归档观测每秒多少条？",
                "归档观测峰值是多少？",
                "A和D通道归档了多少条观测？",
                "B通道和C通道的观测数量差多少？",
                "归档了多少条观测？另外有多少个独立人员？",
                "归档了多少条有效观测？",
                "归档了多少条边端观测？",
                "归档了多少条观测和告警？",
                "没有归档的观测数量是多少？",
                "归档了多少条观测，哪一个模型更准确？",
                "全站所有批次归档了多少条观测？",
                "归档了多少条观测并且有多少批次？",
                "原批次沿用没？多少条观测？",
                "归档观测数量增长百分比是多少？",
            ),
        }
        for expected, questions in cases.items():
            for question in questions:
                with self.subTest(question=question):
                    result = query(self.index, question, RESTART_RUN, lambda _: self.summary)
                    if expected in ("total", "evidence"):
                        self.assertEqual(result["status"], "answered")
                        self.assertEqual(result["facts"]["batch_id"], RESTART_RUN)
                        self.assertEqual(result["facts"]["observation_count"], 152)
                        expected_citations = ["batch-summary"]
                        if expected == "evidence":
                            self.assertEqual(result["facts"]["cloud_observations"], 152)
                            expected_citations.extend(RESTART_IDS)
                        self.assertEqual([item["id"] for item in result["citations"]], expected_citations)
                    else:
                        self.assertEqual(result["status"], "insufficient_evidence")
                        self.assertEqual(result["reason"], expected)
                        self.assertNotIn("facts", result)
                        self.assertNotIn("152", result["answer"])
                        self.assertEqual(result["citations"], [])

    def test_batch_summaries_with_incomplete_scope(self):
        for batch_fields, summary_fields, reason in (
            ({"deleted_ms": 1}, {}, "batch_unavailable"),
            ({"batch_id": "different"}, {}, "batch_unavailable"),
            ({"channel": None}, {}, "batch_channel_unknown"),
            ({}, {"historical_missing": True}, "invalid_dataset_summary"),
            ({}, {"observation_count": True}, "invalid_dataset_summary"),
            ({}, {"observation_count": -1}, "invalid_dataset_summary"),
        ):
            with self.subTest(batch=batch_fields, summary=summary_fields):
                summary = {**self.summary, **summary_fields,
                           "batch": {**self.summary["batch"], **batch_fields}}
                result = query(self.index, "D通道归档了多少条观测？", RESTART_RUN, lambda _: summary)
                self.assertEqual(result["reason"], reason)
                self.assertEqual(result["citations"], [])
                self.assertNotIn("facts", result)

        self.summary["batch"]["archive_status"] = "partial"
        result = query(self.index, "D通道归档了多少条观测？", RESTART_RUN, lambda _: self.summary)
        self.assertEqual(result["status"], "answered")
        self.assertIn("归档状态未确认为完整", result["answer"])
        result = query(self.index, self.question, RESTART_RUN, lambda _: self.summary)
        self.assertEqual(result["status"], "insufficient_evidence")

    def test_live_query_omits_stale_values(self):
        fresh = {"present": True, "stale": False, "sensor_id": "sht4x", "data_time_ms": 900,
                 "quality": "ok", "age_ms": 100, "value": 21}
        stale = {**fresh, "sensor_id": "scd41", "stale": True, "value": 500}
        payload = {"zone_id": "lab", "now_ms": 1000, "fields": {"temperature_c": fresh, "co2_ppm": stale},
                   "stale_after_ms": {"temperature_c": 1000, "co2_ppm": 1000}}
        question = "现在实验室温度和 CO₂ 多少？"
        result = query(self.index, question, get_environment=lambda: payload)
        self.assertEqual(result["facts"]["values"]["temperature_c"]["value"], 21)
        self.assertIsNone(result["facts"]["values"]["co2_ppm"])
        self.assertNotIn("500", result["answer"])
        fresh["present"] = False
        self.assertEqual(query(self.index, question, get_environment=lambda: payload)["status"],
                         "insufficient_evidence")


if __name__ == "__main__":
    unittest.main()
