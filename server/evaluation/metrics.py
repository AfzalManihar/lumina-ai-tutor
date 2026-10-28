"""Deterministic, offline metrics for synthetic or human-labeled datasets."""
import re

def _terms(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]{3,}", str(text).lower()))

def evaluate_case(case: dict) -> dict:
    expected = set(case.get("expected_terms", []))
    answer = _terms(case.get("answer", ""))
    evidence = [_terms(x.get("text", "")) for x in case.get("retrieved", [])]
    labels = set(case.get("relevant_source_ids", []))
    retrieved_ids = {x.get("source_id") for x in case.get("retrieved", [])}
    expected_citations = set(case.get("expected_citations", []))
    actual_citations = set(case.get("citations", []))
    context_union = set().union(*evidence) if evidence else set()
    relevant_hits = len(labels & retrieved_ids)
    answerable = bool(case.get("answerable", True))
    query_terms = _terms(case.get("query", ""))
    faithfulness = len(answer & context_union) / max(1, len(answer))
    return {"case_id": case["id"],
            "answer_term_recall": round(len(expected & answer)/max(1,len(expected)),3),
            "answer_relevance": round(len(answer & query_terms)/max(1,len(query_terms)),3) if query_terms else None,
            "faithfulness_term_overlap": round(faithfulness,3),
            "context_recall": round(len(expected & context_union)/max(1,len(expected)),3),
            "context_precision": round(relevant_hits/max(1,len(retrieved_ids)),3),
            "citation_precision": round(len(expected_citations & actual_citations)/max(1,len(actual_citations)),3),
            "citation_recall": round(len(expected_citations & actual_citations)/max(1,len(expected_citations)),3) if expected_citations else (1.0 if not actual_citations else 0.0),
            "abstention_correct": answerable or bool(case.get("abstained",False)),
            "synthetic_fixture": bool(case.get("synthetic",True))}

def evaluate_dataset(dataset: dict) -> dict:
    results=[evaluate_case(c) for c in dataset["cases"]]
    metrics=["answer_term_recall","faithfulness_term_overlap","context_recall","context_precision","citation_precision","citation_recall","abstention_correct"]
    averages={m:round(sum(x[m] for x in results)/max(1,len(results)),3) for m in metrics}
    rel=[x["answer_relevance"] for x in results if x["answer_relevance"] is not None]
    if rel: averages["answer_relevance"]=round(sum(rel)/len(rel),3)
    return {"dataset_version":dataset["version"],"case_count":len(results),"synthetic_only":all(x["synthetic_fixture"] for x in results),"metrics":averages,"cases":results}
