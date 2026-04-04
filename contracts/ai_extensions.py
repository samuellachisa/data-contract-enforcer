#!/usr/bin/env python3
"""
AI Contract Extensions (Week 7)

Implements:
1) Embedding Drift Detection for Week 3 extracted_facts[*].text
2) Prompt Input Schema Validation for Week 3 prompt inputs
3) Structured LLM Output Enforcement for Week 2 verdict records

Outputs:
- validation_reports/ai_metrics.json (or --output)
- validation_reports/ai_monitoring_metrics.json for dashboards (or CONTRACT_AI_MONITORING_OUTPUT_PATH / --monitoring-output; skip if CONTRACT_AI_MONITORING_DISABLE=1)
- violation_log/violations_with_blame.jsonl is produced by attributor; here we append to violations.jsonl
"""
from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import os
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from collections.abc import Callable
from typing import Any

import numpy as np
from jsonschema import validate
from sklearn.feature_extraction.text import HashingVectorizer

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from contracts.common import load_repo_dotenv

load_repo_dotenv()


@dataclass(frozen=True)
class AIExtensionConfig:
    """
    Tunable thresholds for AI contract checks.

    Environment (optional overrides; loaded by load_ai_extension_config_from_env):
      CONTRACT_AI_EMBEDDING_DRIFT_THRESHOLD
      CONTRACT_AI_EMBEDDING_SAMPLE_SIZE
      CONTRACT_AI_LLM_VIOLATION_TREND_MULTIPLIER
      CONTRACT_AI_LLM_VIOLATION_TREND_MIN_DELTA
      CONTRACT_AI_PROMPT_PREVIEW_MAX_LENGTH
      CONTRACT_AI_HASHING_N_FEATURES

    Monitoring (file + hooks only; does not change check outcomes):
      CONTRACT_AI_MONITORING_OUTPUT_PATH — default validation_reports/ai_monitoring_metrics.json
      CONTRACT_AI_MONITORING_DISABLE=1 — skip writing monitoring JSON and hook emission
    """

    embedding_drift_threshold: float = 0.15
    embedding_sample_size: int = 200
    llm_violation_trend_multiplier: float = 1.5
    llm_violation_trend_min_delta: float = 0.001
    prompt_preview_max_length: int = 8000
    hashing_n_features: int = 384


def _env_float(key: str, default: float) -> float:
    raw = os.environ.get(key, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(key: str, default: int) -> int:
    raw = os.environ.get(key, "").strip()
    if not raw:
        return default
    try:
        return int(raw, 10)
    except ValueError:
        return default


def load_ai_extension_config_from_env() -> AIExtensionConfig:
    """Defaults + CONTRACT_AI_* environment overrides (safe parse; bad values fall back)."""
    return AIExtensionConfig(
        embedding_drift_threshold=_env_float("CONTRACT_AI_EMBEDDING_DRIFT_THRESHOLD", 0.15),
        embedding_sample_size=max(1, _env_int("CONTRACT_AI_EMBEDDING_SAMPLE_SIZE", 200)),
        llm_violation_trend_multiplier=_env_float("CONTRACT_AI_LLM_VIOLATION_TREND_MULTIPLIER", 1.5),
        llm_violation_trend_min_delta=_env_float("CONTRACT_AI_LLM_VIOLATION_TREND_MIN_DELTA", 0.001),
        prompt_preview_max_length=max(1, _env_int("CONTRACT_AI_PROMPT_PREVIEW_MAX_LENGTH", 8000)),
        hashing_n_features=max(16, _env_int("CONTRACT_AI_HASHING_N_FEATURES", 384)),
    )


def merge_ai_extension_config(
    base: AIExtensionConfig,
    *,
    embedding_drift_threshold: float | None = None,
    embedding_sample_size: int | None = None,
    llm_violation_trend_multiplier: float | None = None,
    llm_violation_trend_min_delta: float | None = None,
    prompt_preview_max_length: int | None = None,
    hashing_n_features: int | None = None,
) -> AIExtensionConfig:
    """CLI / programmatic overrides (None = keep base)."""
    kwargs: dict[str, Any] = {}
    if embedding_drift_threshold is not None:
        kwargs["embedding_drift_threshold"] = embedding_drift_threshold
    if embedding_sample_size is not None:
        kwargs["embedding_sample_size"] = max(1, int(embedding_sample_size))
    if llm_violation_trend_multiplier is not None:
        kwargs["llm_violation_trend_multiplier"] = llm_violation_trend_multiplier
    if llm_violation_trend_min_delta is not None:
        kwargs["llm_violation_trend_min_delta"] = llm_violation_trend_min_delta
    if prompt_preview_max_length is not None:
        kwargs["prompt_preview_max_length"] = max(1, int(prompt_preview_max_length))
    if hashing_n_features is not None:
        kwargs["hashing_n_features"] = max(16, int(hashing_n_features))
    return dataclasses.replace(base, **kwargs)


# Optional callbacks for dashboards / APM (no core check logic changes; invoked after metrics are computed).
MONITORING_HOOKS: list[Callable[[dict[str, Any]], None]] = []


def register_ai_monitoring_hook(fn: Callable[[dict[str, Any]], None]) -> None:
    """Register a sink; receives the same payload written to ai_monitoring_metrics.json."""
    MONITORING_HOOKS.append(fn)


def _emit_ai_monitoring_hooks(payload: dict[str, Any]) -> None:
    for fn in MONITORING_HOOKS:
        try:
            fn(payload)
        except Exception:
            continue


def build_ai_monitoring_snapshot(
    *,
    embedding: dict[str, Any],
    prompt: dict[str, Any],
    llm: dict[str, Any],
    traces: dict[str, Any],
    config: AIExtensionConfig,
) -> dict[str, Any]:
    """
    Stable, dashboard-friendly metrics (gauges + numeric state codes + text states).
    Prometheus-style scrapers can map gauges_*; states_numeric are 0=PASS,1=WARN,2=FAIL,3=ERROR,-1=unknown.
    """

    def _status_code(s: Any) -> float:
        m = {"PASS": 0.0, "WARN": 1.0, "FAIL": 2.0, "ERROR": 3.0}
        return float(m.get(str(s).upper(), -1.0))

    return {
        "schema_version": "1",
        "kind": "ai_contract_extensions",
        "timestamp": _now_iso(),
        "config_echo": {
            "embedding_drift_threshold": config.embedding_drift_threshold,
            "embedding_sample_size": config.embedding_sample_size,
            "llm_violation_trend_multiplier": config.llm_violation_trend_multiplier,
            "llm_violation_trend_min_delta": config.llm_violation_trend_min_delta,
            "prompt_preview_max_length": config.prompt_preview_max_length,
            "hashing_n_features": config.hashing_n_features,
        },
        "gauges": {
            "ai_embedding_drift_score": float(embedding.get("drift_score") or 0.0),
            "ai_embedding_drift_threshold": float(embedding.get("threshold") or config.embedding_drift_threshold),
            "ai_llm_violation_rate": float(llm.get("violation_rate") or 0.0),
            "ai_llm_baseline_violation_rate": float(llm.get("baseline_violation_rate") or 0.0),
            "ai_llm_schema_violations_total": float(llm.get("schema_violations") or 0),
            "ai_llm_total_outputs": float(llm.get("total_outputs") or 0),
            "ai_langsmith_checks_failed": float(traces.get("checks_failed") or 0),
            "ai_langsmith_total_traces": float(traces.get("total_traces") or 0),
            "ai_prompt_quarantined_total": float(prompt.get("quarantined_count") or 0),
        },
        "states_numeric": {
            "ai_embedding_drift": _status_code(embedding.get("status")),
            "ai_llm_output_schema": _status_code(llm.get("status")),
            "ai_langsmith_traces": _status_code(traces.get("status")),
            "ai_prompt_input": _status_code(prompt.get("status")),
        },
        "states_text": {
            "ai_embedding_drift": str(embedding.get("status", "")),
            "ai_llm_output_schema": str(llm.get("status", "")),
            "ai_langsmith_traces": str(traces.get("status", "")),
            "ai_prompt_input": str(prompt.get("status", "")),
            "ai_llm_trend": str(llm.get("trend", "")),
        },
    }


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ai_monitoring_disabled() -> bool:
    return os.environ.get("CONTRACT_AI_MONITORING_DISABLE", "").strip().lower() in ("1", "true", "yes")


def _resolve_ai_monitoring_output_path(cli_path: Path | None) -> Path:
    if cli_path is not None:
        return cli_path.expanduser().resolve()
    raw = os.environ.get("CONTRACT_AI_MONITORING_OUTPUT_PATH", "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    return (_REPO / "validation_reports" / "ai_monitoring_metrics.json").resolve()


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            if line.startswith("#") or line.startswith("//"):
                continue
            rows.append(json.loads(line))
    return rows


def _violation_dedupe_key(violation: dict[str, Any]) -> tuple[Any, ...]:
    return (
        violation.get("type"),
        violation.get("check_id"),
        violation.get("verdict_id"),
    )


def _violation_already_logged(path: Path, key: tuple[Any, ...]) -> bool:
    if not path.exists():
        return False
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or line.startswith("//"):
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if _violation_dedupe_key(obj) == key:
                return True
    return False


def _append_violation(path: Path, violation: dict[str, Any], *, dedupe: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if dedupe and _violation_already_logged(path, _violation_dedupe_key(violation)):
        return
    # If file doesn't exist, add injection comment.
    if not path.exists():
        path.write_text("# Auto-appended violations by ai_extensions.\n", encoding="utf-8")
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(violation, ensure_ascii=False) + "\n")


def _embed_texts_hashing(texts: list[str], n_features: int) -> np.ndarray:
    vec = HashingVectorizer(
        n_features=n_features,
        alternate_sign=False,
        norm="l2",
        stop_words=None,
    )
    x = vec.transform(texts)
    return x.toarray().astype(np.float32)


def _embed_texts_openai(texts: list[str], model: str = "text-embedding-3-small") -> np.ndarray | None:
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        return None
    try:
        from openai import OpenAI
    except Exception:
        return None
    client = OpenAI(api_key=api_key)
    vecs: list[list[float]] = []
    batch = 100
    for i in range(0, len(texts), batch):
        chunk = texts[i : i + batch]
        resp = client.embeddings.create(model=model, input=chunk)
        ordered = sorted(resp.data, key=lambda d: d.index)
        vecs.extend([list(d.embedding) for d in ordered])
    return np.array(vecs, dtype=np.float32)


def _openrouter_client() -> Any | None:
    """OpenAI-compatible client for https://openrouter.ai (chat + embeddings)."""
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        return None
    try:
        from openai import OpenAI
    except Exception:
        return None
    base = os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1").rstrip("/")
    headers: dict[str, str] = {
        "X-Title": os.environ.get("OPENROUTER_X_TITLE", "Week7-DataContractEnforcer"),
    }
    ref = os.environ.get("OPENROUTER_HTTP_REFERER", "").strip()
    if ref:
        headers["HTTP-Referer"] = ref
    return OpenAI(api_key=api_key, base_url=base, default_headers=headers)


def _embed_texts_openrouter(texts: list[str]) -> np.ndarray | None:
    client = _openrouter_client()
    if client is None:
        return None
    model = os.environ.get("OPENROUTER_EMBEDDING_MODEL", "openai/text-embedding-3-small")
    vecs: list[list[float]] = []
    batch = 100
    for i in range(0, len(texts), batch):
        chunk = texts[i : i + batch]
        try:
            resp = client.embeddings.create(model=model, input=chunk)
        except Exception:
            return None
        ordered = sorted(resp.data, key=lambda d: d.index)
        vecs.extend([list(d.embedding) for d in ordered])
    return np.array(vecs, dtype=np.float32)


def _embedding_backend_choice() -> str:
    if os.environ.get("EMBEDDING_OFF", "").strip().lower() in ("1", "true", "yes"):
        return "hashing"
    use_or = os.environ.get("USE_OPENROUTER_EMBEDDINGS", "").strip().lower() in ("1", "true", "yes")
    if use_or and os.environ.get("OPENROUTER_API_KEY"):
        return "openrouter"
    if os.environ.get("OPENAI_API_KEY"):
        return "openai"
    return "hashing"


def _embedding_meta_path() -> Path:
    return _REPO / "schema_snapshots" / "embedding_baseline_meta.json"


def check_embedding_drift(
    extractions: list[dict[str, Any]],
    *,
    config: AIExtensionConfig | None = None,
    threshold: float | None = None,
) -> dict[str, Any]:
    cfg = config or load_ai_extension_config_from_env()
    eff_threshold = float(threshold) if threshold is not None else cfg.embedding_drift_threshold

    texts: list[str] = []
    for r in extractions:
        for f in r.get("extracted_facts") or []:
            t = f.get("text")
            if isinstance(t, str) and t.strip():
                texts.append(t.strip())
    if not texts:
        return {"drift_score": 0.0, "status": "WARN", "threshold": eff_threshold, "reason": "no texts"}

    rng = np.random.default_rng(42)
    sample_n = min(cfg.embedding_sample_size, len(texts))
    idx = rng.choice(len(texts), size=sample_n, replace=False)
    sample_texts = [texts[i] for i in idx]

    backend = _embedding_backend_choice()
    meta_path = _embedding_meta_path()
    baseline_path = _REPO / "schema_snapshots" / "embedding_baselines.npz"

    def _api_drift_result(
        backend_id: str,
        model_name: str,
        embed_fn: Callable[[list[str]], np.ndarray | None],
    ) -> dict[str, Any] | None:
        emb = embed_fn(sample_texts)
        if emb is None:
            return None
        current = emb
        current_centroid = np.mean(current, axis=0)
        current_centroid = current_centroid / (np.linalg.norm(current_centroid) + 1e-9)

        if baseline_path.exists() and meta_path.exists():
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            if meta.get("backend") != backend_id:
                baseline_path.unlink(missing_ok=True)
                meta_path.unlink(missing_ok=True)

        if not baseline_path.exists():
            np.savez(baseline_path, centroid=current_centroid.astype(np.float64))
            meta_path.write_text(json.dumps({"backend": backend_id, "model": model_name}), encoding="utf-8")
            return {
                "drift_score": 0.0,
                "status": "PASS",
                "threshold": eff_threshold,
                "baseline_created": True,
                "backend": backend_id,
                "model": model_name,
            }

        base = np.load(baseline_path)
        baseline_centroid = base["centroid"].astype(np.float64)
        baseline_centroid = baseline_centroid / (np.linalg.norm(baseline_centroid) + 1e-9)
        c64 = current_centroid.astype(np.float64)
        cosine_sim = float(np.dot(c64, baseline_centroid))
        drift = 1.0 - cosine_sim
        drift = round(float(drift), 4)
        status = "FAIL" if drift > eff_threshold else "PASS"
        return {
            "drift_score": drift,
            "status": status,
            "threshold": eff_threshold,
            "backend": backend_id,
            "model": model_name,
        }

    if backend == "openrouter":
        model_or = os.environ.get("OPENROUTER_EMBEDDING_MODEL", "openai/text-embedding-3-small")
        out = _api_drift_result("openrouter", model_or, _embed_texts_openrouter)
        if out is not None:
            return out
        backend = "hashing"

    if backend == "openai":
        model_oa = "text-embedding-3-small"
        out = _api_drift_result(
            "openai",
            model_oa,
            lambda tx: _embed_texts_openai(tx, model=model_oa),
        )
        if out is not None:
            return out
        backend = "hashing"

    # HashingVectorizer fallback (offline, deterministic)
    current = _embed_texts_hashing(sample_texts, n_features=cfg.hashing_n_features)
    current_centroid = np.mean(current, axis=0)
    current_centroid = current_centroid / (np.linalg.norm(current_centroid) + 1e-9)

    if baseline_path.exists() and meta_path.exists():
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        if meta.get("backend") != "hashing":
            baseline_path.unlink(missing_ok=True)
            meta_path.unlink(missing_ok=True)

    if not baseline_path.exists():
        np.savez(baseline_path, centroid=current_centroid.astype(np.float64))
        meta_path.write_text(
            json.dumps({"backend": "hashing", "model": f"HashingVectorizer-{cfg.hashing_n_features}"}),
            encoding="utf-8",
        )
        return {
            "drift_score": 0.0,
            "status": "PASS",
            "threshold": eff_threshold,
            "baseline_created": True,
            "backend": "hashing",
            "model": f"HashingVectorizer-{cfg.hashing_n_features}",
        }

    base = np.load(baseline_path)
    baseline_centroid = base["centroid"].astype(np.float64)
    baseline_centroid = baseline_centroid / (np.linalg.norm(baseline_centroid) + 1e-9)
    c64 = current_centroid.astype(np.float64)
    cosine_sim = float(np.dot(c64, baseline_centroid))
    drift = 1.0 - cosine_sim
    drift = round(float(drift), 4)
    status = "FAIL" if drift > eff_threshold else "PASS"
    return {
        "drift_score": drift,
        "status": status,
        "threshold": eff_threshold,
        "backend": "hashing",
        "model": f"HashingVectorizer-{cfg.hashing_n_features}",
    }


def check_prompt_input_schema(
    extractions: list[dict[str, Any]],
    *,
    config: AIExtensionConfig | None = None,
) -> dict[str, Any]:
    cfg = config or load_ai_extension_config_from_env()
    max_len = cfg.prompt_preview_max_length
    schema = {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "type": "object",
        "required": ["doc_id", "source_path", "content_preview"],
        "properties": {
            "doc_id": {"type": "string", "minLength": 1},
            "source_path": {"type": "string", "minLength": 1},
            "content_preview": {"type": "string", "maxLength": max_len},
        },
        "additionalProperties": False,
    }

    quarantined: list[dict[str, Any]] = []
    for r in extractions:
        doc_id = r.get("doc_id")
        source_path = r.get("source_path")
        # content_preview: first fact text (or placeholder)
        facts = r.get("extracted_facts") or []
        preview = ""
        if facts and isinstance(facts, list) and isinstance(facts[0], dict):
            preview = str(facts[0].get("text") or "")
        record = {"doc_id": doc_id, "source_path": source_path, "content_preview": preview[:max_len]}

        try:
            validate(instance=record, schema=schema)
        except Exception:
            quarantined.append({"prompt_input": record, "source_doc_id": doc_id})

    status = "PASS" if not quarantined else "FAIL"
    if quarantined:
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        out = _REPO / "outputs" / "quarantine" / f"prompt_inputs_quarantine_{ts}.jsonl"
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("w", encoding="utf-8") as f:
            for q in quarantined:
                f.write(json.dumps(q, ensure_ascii=False) + "\n")
    return {
        "quarantined_count": len(quarantined),
        "status": status,
        "content_preview_max_length": max_len,
    }


def _write_prompt_input_schema_file(config: AIExtensionConfig | None = None) -> None:
    cfg = config or load_ai_extension_config_from_env()
    max_len = cfg.prompt_preview_max_length
    schema = {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "type": "object",
        "required": ["doc_id", "source_path", "content_preview"],
        "properties": {
            "doc_id": {"type": "string", "minLength": 1},
            "source_path": {"type": "string", "minLength": 1},
            "content_preview": {"type": "string", "maxLength": max_len},
        },
        "additionalProperties": False,
    }
    out = _REPO / "generated_contracts" / "prompt_inputs" / "week3_extraction_prompt_input.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(schema, indent=2), encoding="utf-8")


def _verdict_json_schema() -> dict[str, Any]:
    # Targets the schema in the prompt. We enforce the contract-critical fields.
    return {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "type": "object",
        "required": [
            "verdict_id",
            "target_ref",
            "rubric_id",
            "rubric_version",
            "scores",
            "overall_verdict",
            "overall_score",
            "confidence",
            "evaluated_at",
        ],
        "properties": {
            "verdict_id": {"type": "string", "minLength": 1},
            "target_ref": {"type": "string", "minLength": 1},
            "rubric_id": {"type": "string", "minLength": 1},
            "rubric_version": {"type": "string", "minLength": 1},
            "scores": {
                "type": "object",
                "minProperties": 1,
                "additionalProperties": {
                    "type": "object",
                    "required": ["score", "evidence", "notes"],
                    "properties": {
                        "score": {"type": "integer", "minimum": 1, "maximum": 5},
                        "evidence": {"type": "array", "items": {"type": "string"}},
                        "notes": {"type": "string"},
                    },
                },
            },
            "overall_verdict": {"type": "string", "enum": ["PASS", "FAIL", "WARN"]},
            "overall_score": {"type": "number"},
            "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
            "evaluated_at": {"type": "string"},
        },
        "additionalProperties": True,
    }


def validate_llm_output_schema(
    verdicts: list[dict[str, Any]],
    *,
    config: AIExtensionConfig | None = None,
) -> dict[str, Any]:
    cfg = config or load_ai_extension_config_from_env()
    schema = _verdict_json_schema()
    failures: list[dict[str, Any]] = []

    for v in verdicts:
        try:
            validate(instance=v, schema=schema)
        except Exception as e:
            failures.append({"verdict_id": v.get("verdict_id"), "error": str(e)})

    violation_rate = len(failures) / max(1, len(verdicts))

    baseline_path = _REPO / "schema_snapshots" / "llm_violation_baseline.json"
    if baseline_path.exists():
        baseline_violation_rate = float(json.loads(baseline_path.read_text(encoding="utf-8")).get("baseline_violation_rate", 0.0))
    else:
        # Deterministic baseline for the demo/evaluation run:
        # we want the seeded Week 2 violations to register as a rising violation rate.
        baseline_violation_rate = 0.0
        baseline_path.write_text(json.dumps({"baseline_violation_rate": baseline_violation_rate}), encoding="utf-8")

    mult = cfg.llm_violation_trend_multiplier
    floor = cfg.llm_violation_trend_min_delta
    trend = "stable"
    if violation_rate > baseline_violation_rate * mult and violation_rate > baseline_violation_rate + floor:
        trend = "rising"
    status = "WARN" if trend == "rising" else "PASS"

    vlog = _REPO / "violation_log" / "violations.jsonl"
    # Append violation records for each failed item (bounded to keep output small).
    for f in failures[:50]:
        _append_violation(
            vlog,
            {
                "violation_id": str(uuid.uuid4()),
                "type": "llm_output_schema",
                "check_id": "week2.verdict_record.schema",
                "detected_at": _now_iso(),
                "message": f"Week 2 verdict record failed structured output schema validation: {f.get('error')}",
                "source_contract_id": "week2-digital-courtroom-verdicts",
                "verdict_id": f.get("verdict_id"),
                "records_failing": 1,
                "severity": "CRITICAL",
                "blame_hint": {"file": "src/week3/extractor.py", "line_start": 1, "line_end": 40},
            },
            dedupe=True,
        )

    if status == "WARN" and trend == "rising":
        _append_violation(
            vlog,
            {
                "violation_id": str(uuid.uuid4()),
                "type": "llm_output_schema_trend",
                "check_id": "week2.verdict_record.violation_rate",
                "detected_at": _now_iso(),
                "message": (
                    f"LLM output schema violation rate rising vs baseline: "
                    f"rate={violation_rate:.4f}, baseline={baseline_violation_rate:.4f}"
                ),
                "source_contract_id": "week2-digital-courtroom-verdicts",
                "records_failing": len(failures),
                "severity": "WARNING",
            },
            dedupe=True,
        )

    return {
        "total_outputs": len(verdicts),
        "schema_violations": len(failures),
        "violation_rate": round(float(violation_rate), 6),
        "baseline_violation_rate": round(float(baseline_violation_rate), 6),
        "trend": trend,
        "status": status,
        "failures_sample": failures[:5],
        "trend_threshold_multiplier": mult,
        "trend_min_delta": floor,
    }


def check_langsmith_traces(traces: list[dict[str, Any]]) -> dict[str, Any]:
    """LangSmith trace_record contract (Phase 4); mirrors runner checks and logs FAIL rows."""
    from contracts.validation_checks import validate_langsmith_runs

    contract = {"id": "langsmith-trace-runs"}
    checks = validate_langsmith_runs(traces, contract, _REPO)
    fail_rows = [c for c in checks if c.status == "FAIL"]
    for c in fail_rows[:30]:
        _append_violation(
            _REPO / "violation_log" / "violations.jsonl",
            {
                "violation_id": str(uuid.uuid4()),
                "type": "langsmith_trace_schema",
                "check_id": c.check_id,
                "detected_at": _now_iso(),
                "severity": c.severity,
                "message": c.message,
                "source_contract_id": "langsmith-trace-runs",
                "records_failing": c.records_failing,
            },
        )
    return {
        "total_traces": len(traces),
        "checks_failed": len(fail_rows),
        "status": "FAIL" if fail_rows else "PASS",
        "failed_check_ids": [c.check_id for c in fail_rows[:10]],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="AI Contract Extensions (Week 7)")
    parser.add_argument(
        "--extractions",
        type=Path,
        default=None,
        help="Week 3 extractions JSONL (default: outputs/week3/extractions.jsonl).",
    )
    parser.add_argument(
        "--verdicts",
        type=Path,
        default=None,
        help="Week 2 verdicts JSONL (default: outputs/week2/verdicts.jsonl).",
    )
    parser.add_argument(
        "--traces",
        type=Path,
        default=None,
        help="LangSmith runs JSONL (default: outputs/traces/runs.jsonl if present).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Write metrics JSON (default: validation_reports/ai_metrics.json). Practitioner manual also names validation_reports/ai_extensions.json.",
    )
    parser.add_argument(
        "--also-write-ai-extensions-name",
        action="store_true",
        help="Also write validation_reports/ai_extensions.json (duplicate of metrics for rubric filenames).",
    )
    parser.add_argument(
        "--embedding-drift-threshold",
        type=float,
        default=None,
        help="Override CONTRACT_AI_EMBEDDING_DRIFT_THRESHOLD.",
    )
    parser.add_argument(
        "--embedding-sample-size",
        type=int,
        default=None,
        help="Override CONTRACT_AI_EMBEDDING_SAMPLE_SIZE.",
    )
    parser.add_argument(
        "--llm-trend-multiplier",
        type=float,
        default=None,
        help="Override CONTRACT_AI_LLM_VIOLATION_TREND_MULTIPLIER.",
    )
    parser.add_argument(
        "--llm-trend-min-delta",
        type=float,
        default=None,
        help="Override CONTRACT_AI_LLM_VIOLATION_TREND_MIN_DELTA.",
    )
    parser.add_argument(
        "--prompt-preview-max-length",
        type=int,
        default=None,
        help="Override CONTRACT_AI_PROMPT_PREVIEW_MAX_LENGTH.",
    )
    parser.add_argument(
        "--hashing-n-features",
        type=int,
        default=None,
        help="Override CONTRACT_AI_HASHING_N_FEATURES (hashing backend only).",
    )
    parser.add_argument(
        "--monitoring-output",
        type=Path,
        default=None,
        help="Write dashboard metrics JSON (default: validation_reports/ai_monitoring_metrics.json or CONTRACT_AI_MONITORING_OUTPUT_PATH).",
    )
    args = parser.parse_args()

    extractions_path = (args.extractions or _REPO / "outputs" / "week3" / "extractions.jsonl").expanduser().resolve()
    verdicts_path = (args.verdicts or _REPO / "outputs" / "week2" / "verdicts.jsonl").expanduser().resolve()
    traces_path = (args.traces or _REPO / "outputs" / "traces" / "runs.jsonl").expanduser().resolve()

    extractions = _load_jsonl(extractions_path)
    verdicts = _load_jsonl(verdicts_path)
    traces = _load_jsonl(traces_path) if traces_path.exists() else []

    base_cfg = load_ai_extension_config_from_env()
    cfg = merge_ai_extension_config(
        base_cfg,
        embedding_drift_threshold=args.embedding_drift_threshold,
        embedding_sample_size=args.embedding_sample_size,
        llm_violation_trend_multiplier=args.llm_trend_multiplier,
        llm_violation_trend_min_delta=args.llm_trend_min_delta,
        prompt_preview_max_length=args.prompt_preview_max_length,
        hashing_n_features=args.hashing_n_features,
    )

    _write_prompt_input_schema_file(cfg)

    embedding = check_embedding_drift(extractions, config=cfg)
    vlog_main = _REPO / "violation_log" / "violations.jsonl"
    if embedding.get("status") == "FAIL":
        _append_violation(
            vlog_main,
            {
                "violation_id": str(uuid.uuid4()),
                "type": "embedding_drift",
                "check_id": "week3.extracted_facts.text.embedding_drift",
                "detected_at": _now_iso(),
                "message": (
                    f"Embedding centroid drift exceeds threshold: drift_score={embedding.get('drift_score')}, "
                    f"threshold={embedding.get('threshold')}, backend={embedding.get('backend')}"
                ),
                "source_contract_id": "week3-document-refinery-extractions",
                "records_failing": 1,
                "severity": "HIGH",
                "blame_hint": {"file": "src/week3/extractor.py", "line_start": 1, "line_end": 80},
            },
            dedupe=True,
        )
    prompt = check_prompt_input_schema(extractions, config=cfg)
    llm = validate_llm_output_schema(verdicts, config=cfg)
    traces_report = check_langsmith_traces(traces)

    metrics = {
        "run_date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "prompt_hash": hashlib.sha256(b"week3-prompt-input-schema-v1").hexdigest()[:12],
        "embedding_drift": embedding,
        "prompt_input_validation": prompt,
        "langsmith_traces": traces_report,
        "total_outputs": llm["total_outputs"],
        "schema_violations": llm["schema_violations"],
        "violation_rate": llm["violation_rate"],
        "trend": llm["trend"],
        "baseline_violation_rate": llm["baseline_violation_rate"],
        "status": llm["status"],
        "timestamp": _now_iso(),
        "ai_extension_config": {
            "embedding_drift_threshold": cfg.embedding_drift_threshold,
            "embedding_sample_size": cfg.embedding_sample_size,
            "llm_violation_trend_multiplier": cfg.llm_violation_trend_multiplier,
            "llm_violation_trend_min_delta": cfg.llm_violation_trend_min_delta,
            "prompt_preview_max_length": cfg.prompt_preview_max_length,
            "hashing_n_features": cfg.hashing_n_features,
        },
    }

    out = (args.output or _REPO / "validation_reports" / "ai_metrics.json").expanduser().resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(metrics, indent=2)
    out.write_text(payload, encoding="utf-8")
    print(f"Wrote {out}")
    if args.also_write_ai_extensions_name:
        alt = _REPO / "validation_reports" / "ai_extensions.json"
        alt.write_text(payload, encoding="utf-8")
        print(f"Wrote {alt}")

    if not _ai_monitoring_disabled():
        monitoring = build_ai_monitoring_snapshot(
            embedding=embedding,
            prompt=prompt,
            llm=llm,
            traces=traces_report,
            config=cfg,
        )
        mon_path = _resolve_ai_monitoring_output_path(args.monitoring_output)
        mon_path.parent.mkdir(parents=True, exist_ok=True)
        mon_path.write_text(json.dumps(monitoring, indent=2), encoding="utf-8")
        print(f"Wrote {mon_path}")
        _emit_ai_monitoring_hooks(monitoring)


if __name__ == "__main__":
    main()

