"""chat/service.py 完整测试。"""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestChatServiceModels:
    def test_get_chat_model(self):
        from app.features.chat.service import get_chat_model
        get_chat_model.cache_clear()
        with patch("app.features.chat.service.get_chat_model_config", return_value={"api": "x", "key": "y", "model": "z"}):
            m = get_chat_model()
        assert m is not None

    def test_get_embeddings_model(self):
        from app.features.chat.service import get_embeddings_model
        get_embeddings_model.cache_clear()
        with patch("app.features.chat.service.get_embedding_model_config", return_value={"api": "x", "key": "y", "model": "z"}):
            with patch("app.infra.llm.client.TrackingOpenAIEmbeddings"):
                m = get_embeddings_model()
        assert m is not None

    def test_db_search_by_vector(self):
        from app.features.chat.service import _db_search_by_vector
        mock_session = MagicMock()
        mock_result = MagicMock()
        mock_result.__getitem__ = MagicMock(side_effect=lambda i: [1, "file.txt", "desc", "text/plain", 0.1][i])
        mock_session.execute.return_value.fetchall.return_value = [mock_result]
        with patch("app.features.chat.service.SessionLocal", return_value=mock_session):
            docs = _db_search_by_vector([0.1] * 1024, "test", 1, 10)
        assert len(docs) == 1

    def test_db_search_by_vector_empty(self):
        from app.features.chat.service import _db_search_by_vector
        mock_session = MagicMock()
        mock_session.execute.return_value.fetchall.return_value = []
        with patch("app.features.chat.service.SessionLocal", return_value=mock_session):
            docs = _db_search_by_vector([0.1] * 1024, "test", 1, 10)
        assert docs == []

    def test_vector_search_docs(self):
        from app.features.chat.service import _vector_search_docs
        mock_emb = MagicMock()
        mock_emb.embed_query.return_value = [0.1] * 2048
        with patch("app.features.chat.service._db_search_by_vector", return_value=[]):
            docs = _vector_search_docs("test", 1, mock_emb, 10)
        assert docs == []

    def test_custom_db_retriever(self):
        from app.features.chat.service import custom_db_retriever
        with patch("app.features.chat.service.get_embeddings_model"), \
             patch("app.features.chat.service._vector_search_docs", return_value=[]), \
             patch("app.features.chat.rerank.rerank_documents", new_callable=AsyncMock, return_value=[]):
            result = asyncio.run(custom_db_retriever("test", 1))
        assert result == []


class TestChatServiceMultiQuery:
    def test_multi_query_db_retriever(self):
        from app.features.chat.service import multi_query_db_retriever
        from app.features.chat.query_rewrite import RewriteKeywordDimensions
        dims = RewriteKeywordDimensions(
            topic_terms=["test"], entity_terms=[], time_terms=[],
            file_type_terms=[], action_terms=[], synonym_terms=[]
        )
        mock_emb = MagicMock()
        mock_emb.embed_documents = MagicMock(return_value=[[0.1] * 1024])
        with patch("app.features.chat.service.get_embeddings_model", return_value=mock_emb), \
             patch("app.features.chat.service._db_search_by_vector", return_value=[]), \
             patch("app.features.chat.service.build_retrieval_query", return_value="test"), \
             patch("app.features.chat.rerank.rerank_documents", new_callable=AsyncMock, return_value=[]):
            result = asyncio.run(multi_query_db_retriever("test", 1, dims))
        assert result == []

    def test_multi_query_db_retriever_with_orig_vector(self):
        from app.features.chat.service import multi_query_db_retriever
        from app.features.chat.query_rewrite import RewriteKeywordDimensions
        dims = RewriteKeywordDimensions(
            topic_terms=["test"], entity_terms=[], time_terms=[],
            file_type_terms=[], action_terms=[], synonym_terms=[]
        )
        mock_emb = MagicMock()
        mock_emb.embed_documents = MagicMock(return_value=[[0.2] * 1024])
        with patch("app.features.chat.service.get_embeddings_model", return_value=mock_emb), \
             patch("app.features.chat.service._db_search_by_vector", return_value=[]), \
             patch("app.features.chat.service.build_retrieval_query", return_value="test"), \
             patch("app.features.chat.rerank.rerank_documents", new_callable=AsyncMock, return_value=[]):
            result = asyncio.run(multi_query_db_retriever("test", 1, dims, original_vector=[0.1] * 1024))
        assert result == []

    def test_embed_original_question(self):
        from app.features.chat.service import embed_original_question
        mock_emb = MagicMock()
        mock_emb.embed_query = MagicMock(return_value=[0.1] * 1024)
        with patch("app.features.chat.service.get_embeddings_model", return_value=mock_emb):
            result = asyncio.run(embed_original_question({"question": "test"}))
        assert len(result) == 1024

    def test_embed_original_question_empty(self):
        from app.features.chat.service import embed_original_question
        result = asyncio.run(embed_original_question({"question": ""}))
        assert result == []

    def test_retrieve_docs_with_rewrite(self):
        from app.features.chat.service import retrieve_docs_with_rewrite
        from app.features.chat.query_rewrite import RewriteKeywordDimensions
        dims = RewriteKeywordDimensions(
            topic_terms=["test"], entity_terms=[], time_terms=[],
            file_type_terms=[], action_terms=[], synonym_terms=[]
        )
        with patch("app.features.chat.service.require_keyword_dimensions", return_value=dims), \
             patch("app.features.chat.service.multi_query_db_retriever", new_callable=AsyncMock, return_value=[]):
            result = asyncio.run(retrieve_docs_with_rewrite({
                "question": "test", "workspace_id": 1, "rewrite_output": {}, "original_vector": None
            }))
        assert result == []


class TestChatServiceFormat:
    def _make_doc(self, content, metadata):
        """创建模拟 Document 对象（langchain_core 被 mock）。"""
        doc = MagicMock()
        doc.page_content = content
        doc.metadata = metadata
        return doc

    def test_format_docs(self):
        from app.features.chat.service import format_docs
        doc = self._make_doc("content", {"name": "file.txt", "id": 1, "mime_type": "text/plain"})
        result = format_docs([doc])
        assert "file.txt" in result

    def test_format_docs_image(self):
        from app.features.chat.service import format_docs
        doc = self._make_doc("content", {"name": "img.jpg", "id": 1, "mime_type": "image/jpeg"})
        result = format_docs([doc])
        assert "![图片名]" in result

    def test_format_history_list(self):
        from app.features.chat.service import format_history
        result = format_history([{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}])
        assert "user: hi" in result

    def test_format_history_string(self):
        from app.features.chat.service import format_history
        result = format_history("some history")
        assert "some history" in result

    def test_format_history_empty(self):
        from app.features.chat.service import format_history
        assert format_history(None) == ""

    def test_fuse_docs_with_rrf(self):
        from app.features.chat.service import _fuse_docs_with_rrf
        doc1 = self._make_doc("c1", {"id": 1, "distance": 0.1})
        doc2 = self._make_doc("c2", {"id": 2, "distance": 0.2})
        result = _fuse_docs_with_rrf([[doc1], [doc2, doc1]], rrf_k=60, top_k=10)
        assert len(result) == 2
        assert result[0].metadata["id"] == 1

    def test_fuse_docs_with_rrf_empty(self):
        from app.features.chat.service import _fuse_docs_with_rrf
        assert _fuse_docs_with_rrf([], 60, 10) == []
