import sys
import unittest
from pathlib import Path

SERVICES_DIR = Path(__file__).resolve().parents[1] / "app" / "services"
if str(SERVICES_DIR) not in sys.path:
    sys.path.insert(0, str(SERVICES_DIR))

from change_log_summary import summarize_events  # type: ignore


class ChangeLogSummaryTestCase(unittest.TestCase):
    def test_summarize_events_collects_scope_and_breakdown(self):
        events = [
            {
                "id": 101,
                "entity_type": "file",
                "entity_id": 11,
                "action": "move",
                "old_parent_id": 1,
                "new_parent_id": 2,
                "old_name": "draft.md",
                "new_name": "draft.md",
            },
            {
                "id": 102,
                "entity_type": "folder",
                "entity_id": 2,
                "action": "rename",
                "old_parent_id": 0,
                "new_parent_id": 0,
                "old_name": "tmp",
                "new_name": "project",
            },
            {
                "id": 103,
                "entity_type": "file",
                "entity_id": 12,
                "action": "create",
                "old_parent_id": None,
                "new_parent_id": 2,
                "old_name": None,
                "new_name": "notes.txt",
            },
        ]

        result = summarize_events(
            events,
            total_count=3,
            from_event_id=100,
            to_event_id=103,
        )

        self.assertEqual(result["changed_file_ids"], [11, 12])
        self.assertEqual(sorted(result["changed_folder_ids"]), [0, 1, 2])
        self.assertEqual(result["action_breakdown"]["file:move"], 1)
        self.assertEqual(result["action_breakdown"]["folder:rename"], 1)
        self.assertIn("Event range: (100, 103]", result["summary_text"])
        self.assertIn("file:move", result["summary_text"])


if __name__ == "__main__":
    unittest.main()
