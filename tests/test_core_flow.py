import os
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from router import Router  # noqa: E402
from state import MAX_CLONE_LIMIT, new_state  # noqa: E402


class RouterFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        os.environ["KILLME_MOCK"] = "1"
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.router = Router(base_dir=ROOT, db_path=Path(self.tmp.name) / "killme.sqlite")

    def start(self) -> str:
        self.router.handle_line("/start 构建一个轻量多智能体审议 CLI")
        state = self.router.storage.load_latest_state()
        assert state is not None
        return state["session_id"]

    def test_start_bootstraps_topic_specific_major_question(self) -> None:
        output = self.router.handle_line("/start 多智能体下提示词对齐")
        state = self.router.storage.load_latest_state()
        assert state is not None

        self.assertIn("Question source：chair", output)
        self.assertIn("多智能体下提示词对齐", state["current_major_question"])
        self.assertNotEqual(state["current_major_question"], "这个想法最可能失败在哪里，最轻的验证边界是什么？")
        self.assertEqual(state["round"], 1)

        turns = self.router.storage.recent_turns(state["session_id"], limit=5)
        self.assertEqual(turns[-1]["speaker"], "chair")
        self.assertEqual(turns[-1]["metadata"]["type"], "initial_major_question")

    def test_manual_role_command_calls_single_instance(self) -> None:
        session_id = self.start()
        self.router.handle_line("/config executioner 3")

        output = self.router.handle_line("/exec")

        self.assertIn("### executioner result", output)
        self.assertNotIn("### Merger result", output)
        speakers = [turn["speaker"] for turn in self.router.storage.recent_turns(session_id, limit=20)]
        self.assertIn("executioner", speakers)
        self.assertNotIn("merger", speakers)
        self.assertNotIn("Executioner-1", speakers)

    def test_spawn_command_uses_independent_clones_and_merger(self) -> None:
        session_id = self.start()

        output = self.router.handle_line("/spawn executioner 2")

        self.assertIn("### Merger result", output)
        turns = self.router.storage.recent_turns(session_id, limit=20)
        clone_turns = [turn for turn in turns if turn["speaker"].startswith("Executioner-")]
        merger_turns = [turn for turn in turns if turn["speaker"] == "merger"]
        self.assertEqual(len(clone_turns), 2)
        self.assertEqual(len(merger_turns), 1)
        clone_group_ids = {turn["metadata"].get("clone_group_id") for turn in clone_turns}
        self.assertEqual(len(clone_group_ids), 1)
        self.assertNotIn(None, clone_group_ids)
        self.assertTrue(all(turn["metadata"].get("independent_snapshot") for turn in clone_turns))
        self.assertTrue(all(turn["metadata"].get("state_patch_applied") is False for turn in clone_turns))

        state = self.router.storage.load_latest_state()
        assert state is not None
        self.assertTrue(state["strongest_attack"])

    def test_loaded_clone_limits_are_clamped(self) -> None:
        state = new_state(ROOT, "测试被污染的 clone limit")
        state["clone_limits"]["executioner"] = 999
        self.router.storage.create_session(state["session_id"], state["core_claim"])
        self.router.storage.save_snapshot(state["session_id"], state)

        loaded = self.router._require_state()

        self.assertEqual(loaded["clone_limits"]["executioner"], MAX_CLONE_LIMIT)

    def test_summary_and_checkpoint_commands(self) -> None:
        self.start()
        before = self.router.storage.load_latest_state()
        assert before is not None

        summary = self.router.handle_line("/summary")
        checkpoint = self.router.handle_line("/checkpoint")
        after = self.router.storage.load_latest_state()
        assert after is not None

        self.assertIn("## Session summary", summary)
        self.assertIn("已保存 checkpoint", checkpoint)
        self.assertEqual(after["round"], before["round"] + 1)

    def test_contextual_followup_does_not_update_user_position(self) -> None:
        self.start()
        self.router.handle_line("/build")
        before = self.router.storage.load_latest_state()
        assert before is not None

        output = self.router.handle_line("这是什么意思，什么的方案")
        after = self.router.storage.load_latest_state()
        assert after is not None

        self.assertEqual(after["user_position"], before["user_position"])
        self.assertIn("Builder", output)
        self.assertIn("没有写入 user_position", output)

        turns = self.router.storage.recent_turns(after["session_id"], limit=5)
        assistant_turns = [turn for turn in turns if turn["speaker"] == "assistant"]
        self.assertEqual(assistant_turns[-1]["metadata"]["type"], "contextual_answer")

    def test_plain_user_message_does_not_update_user_position(self) -> None:
        self.start()
        before = self.router.storage.load_latest_state()
        assert before is not None

        output = self.router.handle_line("我只是记录一个临时想法")
        after = self.router.storage.load_latest_state()
        assert after is not None

        self.assertEqual(after["user_position"], before["user_position"])
        self.assertIn("没有写入 user_position", output)

        turns = self.router.storage.recent_turns(after["session_id"], limit=3)
        self.assertEqual(turns[-1]["metadata"]["type"], "user_message")
        self.assertTrue(turns[-1]["metadata"]["user_position_unchanged"])

    def test_position_command_can_show_set_add_and_clear(self) -> None:
        self.start()

        show_output = self.router.handle_line("/position")
        set_output = self.router.handle_line("/position set 用户只接受离线验证")
        add_output = self.router.handle_line("/position add 不能接真实写接口")
        clear_output = self.router.handle_line("/position clear")
        state = self.router.storage.load_latest_state()
        assert state is not None

        self.assertIn("## User position", show_output)
        self.assertIn("用户只接受离线验证", set_output)
        self.assertIn("不能接真实写接口", add_output)
        self.assertIn("尚未补充额外约束", clear_output)
        self.assertEqual(state["user_position"], "用户刚启动审议，尚未补充额外约束。")

    def test_spawn_refuses_to_exceed_configured_limit(self) -> None:
        self.start()
        self.router.handle_line("/config judge 1")

        output = self.router.handle_line("/spawn judge 2")

        self.assertIn("judge 当前 clone 上限是 1", output)

    def test_evidence_commands_add_import_and_list_items(self) -> None:
        self.start()

        add_output = self.router.handle_line("/evidence add ToolBench has public tool-use tasks")
        evidence_path = Path(self.tmp.name) / "evidence.json"
        evidence_path.write_text(
            json.dumps(
                {
                    "title": "ALCE benchmark",
                    "url": "https://example.test/alce",
                    "source_type": "benchmark",
                    "key_quote_or_summary": "Citation grounding benchmark notes.",
                    "reliability": "high",
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        import_output = self.router.handle_line(f"/evidence import {evidence_path}")
        list_output = self.router.handle_line("/evidence")
        state = self.router.storage.load_latest_state()
        assert state is not None

        self.assertIn("已导入 1 条 evidence item", add_output)
        self.assertIn("已导入 1 条 evidence item", import_output)
        self.assertEqual(len(state["evidence_items"]), 2)
        self.assertIn("ToolBench has public tool-use tasks", list_output)
        self.assertIn("ALCE benchmark", list_output)

    def test_evidence_request_command_records_search_need(self) -> None:
        self.start()

        output = self.router.handle_line("/evidence request ToolBench quickstart solution path")
        state = self.router.storage.load_latest_state()
        assert state is not None

        self.assertEqual(len(state["evidence_requests"]), 1)
        self.assertIn("ToolBench quickstart solution path", output)
        self.assertIn("Evidence requests", output)


if __name__ == "__main__":
    unittest.main()
