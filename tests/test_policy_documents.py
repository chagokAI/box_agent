"""DB 문서 저장으로 바뀐 뒤에도 검색 청크의 출처와 문서명이 보존된다."""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "mcp_servers"))
import knowledge_server as knowledge


class PolicyDocumentTests(unittest.TestCase):
    def test_database_document_keeps_source_and_filename(self):
        docs = {"POL-02": {"name": "POL-02_order.md", "text": "# 주문 규정 (POL-02)\n\n## 1. 주문\n승인 뒤 주문한다.\n"}}
        with patch.object(knowledge, "_policy_docs", return_value=docs):
            chunks = knowledge.load_policies()
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0]["sourceId"], "POL-02 §1")
        self.assertEqual(chunks[0]["meta"]["file"], "POL-02_order.md")
        self.assertEqual(chunks[0]["content"], "승인 뒤 주문한다.")


if __name__ == "__main__":
    unittest.main()
