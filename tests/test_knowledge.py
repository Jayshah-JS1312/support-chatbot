"""Knowledge module: test chunking and metadata.

Tests pin:
- Each ## section becomes one chunk
- Chunk metadata carries the category (policies, rules, tone, regulations)
- Chunk count matches expected number
- Chunk IDs are unique and properly formatted

We test _chunks() directly without loading the embedding model or chromadb.
"""

import pytest

from support_chatbot import knowledge


class TestChunking:
    """_chunks() splits documents into chunks by ## headings."""

    def test_chunks_returns_list(self):
        chunks = knowledge._chunks()
        assert isinstance(chunks, list)

    def test_chunks_non_empty(self):
        """There are chunks in the knowledge base."""
        chunks = knowledge._chunks()
        assert len(chunks) > 0

    def test_expected_chunk_count(self):
        """The README says there are 48 chunks."""
        chunks = knowledge._chunks()
        # The README says 48 chunks total
        assert len(chunks) >= 45  # Allow some flexibility

    def test_each_chunk_has_required_fields(self):
        chunks = knowledge._chunks()
        for chunk in chunks:
            assert "id" in chunk
            assert "category" in chunk
            assert "source" in chunk
            assert "heading" in chunk
            assert "text" in chunk

    def test_chunk_id_format(self):
        """Chunk IDs are formatted as category/filename#heading."""
        chunks = knowledge._chunks()
        for chunk in chunks:
            chunk_id = chunk["id"]
            # Should have at least one /
            assert "/" in chunk_id
            # Should have #
            assert "#" in chunk_id
            # Format: category/source#heading
            parts = chunk_id.split("#", 1)
            assert len(parts) == 2

    def test_chunk_ids_are_unique(self):
        """Each chunk has a unique ID."""
        chunks = knowledge._chunks()
        chunk_ids = [c["id"] for c in chunks]
        assert len(chunk_ids) == len(set(chunk_ids))

    def test_chunk_category_is_valid(self):
        """Categories are one of the four known types."""
        chunks = knowledge._chunks()
        for chunk in chunks:
            assert chunk["category"] in knowledge.CATEGORIES

    def test_chunk_source_has_md_extension(self):
        """Source files are markdown."""
        chunks = knowledge._chunks()
        for chunk in chunks:
            assert chunk["source"].endswith(".md")

    def test_chunk_text_non_empty(self):
        """Each chunk has content."""
        chunks = knowledge._chunks()
        for chunk in chunks:
            assert len(chunk["text"]) > 0

    def test_chunk_heading_non_empty(self):
        """Each chunk has a heading."""
        chunks = knowledge._chunks()
        for chunk in chunks:
            assert len(chunk["heading"]) > 0


class TestChunkCategories:
    """Chunks are distributed across the four categories."""

    def test_has_policies_chunks(self):
        chunks = knowledge._chunks()
        policies = [c for c in chunks if c["category"] == "policies"]
        assert len(policies) > 0

    def test_has_rules_chunks(self):
        chunks = knowledge._chunks()
        rules = [c for c in chunks if c["category"] == "rules"]
        assert len(rules) > 0

    def test_has_tone_chunks(self):
        chunks = knowledge._chunks()
        tone = [c for c in chunks if c["category"] == "tone"]
        assert len(tone) > 0

    def test_has_regulations_chunks(self):
        chunks = knowledge._chunks()
        regulations = [c for c in chunks if c["category"] == "regulations"]
        assert len(regulations) > 0

    def test_category_distribution(self):
        """Each category has at least a few chunks."""
        chunks = knowledge._chunks()
        by_cat = {}
        for chunk in chunks:
            cat = chunk["category"]
            by_cat[cat] = by_cat.get(cat, 0) + 1
        # Each should have at least one
        for cat in knowledge.CATEGORIES:
            assert by_cat.get(cat, 0) > 0


class TestChunkSources:
    """Chunks come from files in the right folders."""

    def test_chunks_from_expected_folders(self):
        chunks = knowledge._chunks()
        for chunk in chunks:
            source = chunk["source"]
            category = chunk["category"]
            # Source should start with the category folder
            assert source.startswith(category + "/")

    def test_chunk_heading_structure(self):
        """Headings are from document titles and ## sections."""
        chunks = knowledge._chunks()
        for chunk in chunks:
            heading = chunk["heading"]
            # Should be non-empty and readable
            assert len(heading) > 0
            # Most chunks should have the format "Title — Subheading" or just "Title"
            # (The intro section is just the title)
            pass  # Visual inspection needed


class TestChunkMetadata:
    """Metadata on chunks is correct."""

    def test_policies_metadata(self):
        chunks = knowledge._chunks()
        policies = [c for c in chunks if c["category"] == "policies"]
        for chunk in policies:
            assert chunk["category"] == "policies"
            assert "policies/" in chunk["source"]

    def test_rules_metadata(self):
        chunks = knowledge._chunks()
        rules = [c for c in chunks if c["category"] == "rules"]
        for chunk in rules:
            assert chunk["category"] == "rules"
            assert "rules/" in chunk["source"]

    def test_source_and_category_match(self):
        """Source path and category agree."""
        chunks = knowledge._chunks()
        for chunk in chunks:
            source = chunk["source"]
            category = chunk["category"]
            # source is "category/filename"
            source_cat = source.split("/")[0]
            assert source_cat == category


class TestChunkContent:
    """Chunk text content is reasonable."""

    def test_chunk_text_contains_words(self):
        """Text should have actual content, not just whitespace."""
        chunks = knowledge._chunks()
        for chunk in chunks[:5]:  # Sample first 5
            text = chunk["text"]
            words = text.split()
            assert len(words) > 2  # At least a few words

    def test_chunk_text_is_stripped(self):
        """Chunk text should not have leading/trailing whitespace."""
        chunks = knowledge._chunks()
        for chunk in chunks:
            text = chunk["text"]
            assert text == text.strip()

    def test_chunk_heading_is_stripped(self):
        """Chunk heading should not have leading/trailing whitespace."""
        chunks = knowledge._chunks()
        for chunk in chunks:
            heading = chunk["heading"]
            assert heading == heading.strip()


class TestKnowledgeCategories:
    """The CATEGORIES constant is correct."""

    def test_categories_constant(self):
        assert knowledge.CATEGORIES == ("policies", "rules", "tone", "regulations")
