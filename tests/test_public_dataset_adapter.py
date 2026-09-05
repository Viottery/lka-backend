import json

from evals.lka_evals.public_datasets import from_hotpotqa, load_normalized_jsonl


def test_hotpot_adapter_preserves_multi_hop_evidence():
    dataset = from_hotpotqa(
        [{"_id": "q1", "question": "Who?", "answer": "Ada", "supporting_facts": [["A", 0], ["B", 1]]}],
        corpus={"A": "first", "B": "second", "C": "distractor"},
    )
    assert dataset.queries[0].hop_count == 2
    assert dataset.queries[0].supporting_document_ids == ["hotpot_A", "hotpot_B"]
    assert [item.title for item in dataset.documents] == ["A", "B"]


def test_normalized_jsonl_round_trip(tmp_path):
    path = tmp_path / "set.jsonl"
    path.write_text(json.dumps({"kind": "document", "document_id": "d1", "title": "D", "text": "text"}) + "\n" + json.dumps({"kind": "query", "query_id": "q1", "question": "Q", "answer": "A", "supporting_document_ids": ["d1"], "hop_count": 1}) + "\n", encoding="utf-8")
    loaded = load_normalized_jsonl(path)
    assert loaded.documents[0].document_id == "d1"
    assert loaded.queries[0].supporting_document_ids == ["d1"]
