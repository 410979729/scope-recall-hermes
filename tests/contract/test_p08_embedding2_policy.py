"""P08 Gem2 embedding-space and admission contracts."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest

from scope_recall.contracts import ContractError
from scope_recall.core.recall_policy import (
    EMBEDDING_SPACE,
    SPACE_ID,
    VECTOR_SCORE_TOLERANCE,
    RecallPolicy,
    canonical_embedding_space,
    embedding_space_id,
    encode_embedding_text,
)
from scope_recall.core.retrieval import CandidateRef


def test_P08_gem2_descriptor_is_authorized_and_canonical():
    root = Path(__file__).resolve().parents[2]
    descriptor = json.loads((root / "tests/fixtures/p08-embedding-space.json").read_text(encoding="utf-8"))
    canonical = canonical_embedding_space(descriptor["embedding_space"])
    assert canonical == EMBEDDING_SPACE
    assert embedding_space_id(canonical) == descriptor["space_id"] == SPACE_ID
    assert hashlib.sha256(json.dumps(canonical, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest() == descriptor["canonical_json_sha256"]
    assert canonical["task_type"] is None
    assert canonical["request_encoding"]["task_type_field"] is None


def test_P08_gem2_encoding_keeps_raw_text_separate_and_nfkc_normalized():
    raw_query = "设计\u3000Ａ"
    raw_source = "标题\u3000Ａ"
    assert encode_embedding_text(raw_query, kind="query") == "task: question answering | query: 设计 A"
    assert encode_embedding_text(raw_source, kind="document") == "title: none | text: 标题 A"
    with pytest.raises(ContractError):
        encode_embedding_text(raw_query, kind="unknown")


def test_P08_gem2_old_space_rejected_and_explicit_threshold_tolerates_float_rounding():
    # A different model is a different space, not an invalid one: the descriptor
    # is configuration now, so naming another model has to be expressible. What
    # protects the store is that the digest changes with it — the vector
    # directory moves and vectors from the old space are refused admission
    # below, instead of being compared across incompatible geometries.
    old = copy.deepcopy(EMBEDDING_SPACE)
    old["model"] = "gemini-embedding-001"
    assert embedding_space_id(canonical_embedding_space(old)) != SPACE_ID

    # A malformed descriptor is still refused.
    for broken in ({**EMBEDDING_SPACE, "dimensions": 0},
                   {**EMBEDDING_SPACE, "endpoint": "http://insecure"},
                   {**EMBEDDING_SPACE, "metric": "dot"},
                   {**EMBEDDING_SPACE, "model": ""}):
        with pytest.raises(ContractError):
            canonical_embedding_space(broken)

    candidate = CandidateRef(
        "event",
        "event-gem2",
        1,
        "vector",
        vector_id="gem2-vector",
        embedding_space=SPACE_ID,
        vector_score=0.653189912,
    )
    policy = RecallPolicy(vector_threshold=0.653189984350642)
    assert VECTOR_SCORE_TOLERANCE == 1e-6
    assert policy.vector_admission(candidate) == (True, None)
    assert policy.vector_admission(CandidateRef(
        "event", "event-gem2-low", 1, "vector", vector_id="low",
        embedding_space=SPACE_ID, vector_score=0.653179984350642,
    ))[1] == "vector_below_threshold"
    assert policy.vector_admission(CandidateRef(
        "event", "event-old", 1, "vector", vector_id="old",
        embedding_space="old-gemini-space", vector_score=0.999,
    ))[1] == "embedding_space_mismatch"
    assert RecallPolicy(vector_threshold=None).vector_admission(candidate)[1] == "vector_threshold_unconfigured"


def test_P08_gem2_vector_rejects_scores_outside_cosine_domain():
    policy = RecallPolicy(vector_threshold=0.5)
    for score in (1.0 + 2 * VECTOR_SCORE_TOLERANCE, -1.0 - 2 * VECTOR_SCORE_TOLERANCE):
        accepted, reason = policy.vector_admission(CandidateRef(
            "event", "event-malformed", 1, "vector", vector_id="malformed",
            embedding_space=SPACE_ID, vector_score=score,
        ))
        assert accepted is False
        assert reason == "vector_score_invalid"


def test_P08_embedding_model_is_configuration_not_a_constant():
    """Another provider can be addressed, and the store moves when it is.

    The embedding model used to be a module constant that
    `canonical_embedding_space` enforced field by field, so a deployment could
    not choose its own provider. The digest-keyed vector directory already made
    swapping safe; only the hardcoded descriptor stopped anyone using it.
    """
    from scope_recall.adapters.models import EmbeddingRouteConfig
    from scope_recall.core.recall_policy import build_embedding_space

    default = EmbeddingRouteConfig(credential_env="TEST_EMBED_KEY")
    assert default.space() == EMBEDDING_SPACE
    assert embedding_space_id(default.space()) == SPACE_ID, "an existing install must keep its directory"
    assert default.wire_dialect() == "gemini"

    other = EmbeddingRouteConfig(
        credential_env="TEST_EMBED_KEY", model="MiniMax-embedding-01",
        endpoint="https://api.minimaxi.com/v1/embeddings", dimensions=1536, dialect="openai",
    )
    assert other.wire_dialect() == "openai"
    assert embedding_space_id(other.space()) != SPACE_ID, "a different model must move the store"
    assert other.space()["dimensions"] == 1536

    # Same inputs, same digest: the directory name has to be reproducible.
    assert embedding_space_id(other.space()) == embedding_space_id(build_embedding_space(
        model="MiniMax-embedding-01", endpoint="https://api.minimaxi.com/v1/embeddings",
        dimensions=1536, dialect="openai",
    ))

    # Half a descriptor would pair a new model with the old dimensionality and
    # the digest would not reveal the mix, so it is refused outright.
    with pytest.raises(ValueError):
        EmbeddingRouteConfig(credential_env="TEST_EMBED_KEY", model="only-a-model")
    with pytest.raises(ValueError):
        EmbeddingRouteConfig(
            credential_env="TEST_EMBED_KEY", model="m", endpoint="https://e",
            dimensions=1536, dialect="not-a-dialect",
        )


def test_P08_admission_is_bound_to_the_configured_space_not_the_shipped_one():
    """Moving the store is only half of a model switch; admission has to follow.

    The policy compared every vector with the shipped ``SPACE_ID``, so a named
    route -- even one stating the default Gemini values, whose request encoding
    gives it another digest -- had each of its own hits refused as a mismatch.
    """
    from scope_recall.core.recall_policy import build_embedding_space

    named = embedding_space_id(build_embedding_space(
        model="MiniMax-embedding-01", endpoint="https://api.minimaxi.com/v1/embeddings",
        dimensions=1536, dialect="openai",
    ))
    stated_default = embedding_space_id(build_embedding_space(
        model=EMBEDDING_SPACE["model"], endpoint=EMBEDDING_SPACE["endpoint"],
        dimensions=EMBEDDING_SPACE["dimensions"],
    ))
    assert SPACE_ID not in {named, stated_default}

    def hit(space: str) -> CandidateRef:
        return CandidateRef("event", "event-space", 1, "vector", vector_id="v",
                            embedding_space=space, vector_score=0.9)

    for space in (named, stated_default):
        bound = RecallPolicy(vector_threshold=0.5, embedding_space_id=space)
        assert bound.vector_admission(hit(space)) == (True, None)
        assert bound.vector_admission(hit(SPACE_ID)) == (False, "embedding_space_mismatch")

    # The default policy, which Core and an install naming no route use, is unchanged.
    default = RecallPolicy(vector_threshold=0.5)
    assert default.embedding_space_id == SPACE_ID
    assert default.vector_admission(hit(SPACE_ID)) == (True, None)
    assert default.vector_admission(hit(named)) == (False, "embedding_space_mismatch")

    for broken in ("", "  ", None, 7):
        with pytest.raises(ContractError):
            RecallPolicy(vector_threshold=0.5, embedding_space_id=broken)


def test_P08_embedding_wire_dialects_round_trip_both_shapes():
    """Both request shapes are built, and both responses parse, at any width."""
    from scope_recall.adapters.models import (
        AuxiliaryModelError,
        build_openai_embed_body,
        build_gemini_embed_body,
        parse_embedding_response,
    )

    gemini = json.loads(build_gemini_embed_body("t", model="m", dimensions=64))
    assert gemini["requests"][0]["model"] == "models/m"
    assert gemini["requests"][0]["embedContentConfig"]["outputDimensionality"] == 64

    openai = json.loads(build_openai_embed_body("t", model="m", dimensions=64))
    assert openai == {"model": "m", "input": ["t"], "dimensions": 64}

    vector = [0.5] * 64
    got, usage = parse_embedding_response(
        {"embeddings": [{"values": vector}], "usageMetadata": {"promptTokenCount": 7}},
        dialect="gemini", dimensions=64)
    assert len(got) == 64 and usage == {"promptTokenCount": 7}

    got, usage = parse_embedding_response(
        {"data": [{"embedding": vector}], "usage": {"prompt_tokens": 7}},
        dialect="openai", dimensions=64)
    assert len(got) == 64 and usage == {"promptTokenCount": 7}

    # A provider that ignores the requested width must not slip through: the
    # space digest commits to it, so a mismatched vector is unusable.
    with pytest.raises(AuxiliaryModelError):
        parse_embedding_response({"data": [{"embedding": [0.5] * 63}]}, dialect="openai", dimensions=64)
