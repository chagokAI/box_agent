import unittest
from unittest.mock import patch

import initialize_knowledge


class KnowledgeInitializationTests(unittest.TestCase):
    def test_existing_knowledge_is_preserved(self):
        with patch.object(initialize_knowledge.db, "query", side_effect=[[{"name": "box_agent.knowledge"}], [{"n": 10}]]), patch.object(initialize_knowledge.subprocess, "run") as ingest:
            initialize_knowledge.main()
        ingest.assert_not_called()

    def test_missing_table_is_initialized(self):
        with patch.object(initialize_knowledge.db, "query", return_value=[{"name": None}]), patch.object(initialize_knowledge.subprocess, "run") as ingest:
            initialize_knowledge.main()
        ingest.assert_called_once()
        self.assertTrue(ingest.call_args.kwargs["check"])

    def test_empty_knowledge_table_is_initialized(self):
        with patch.object(initialize_knowledge.db, "query", side_effect=[[{"name": "box_agent.knowledge"}], [{"n": 0}]]), patch.object(initialize_knowledge.subprocess, "run") as ingest:
            initialize_knowledge.main()
        ingest.assert_called_once()
