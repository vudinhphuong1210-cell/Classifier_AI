#!/usr/bin/env python3
"""Generate a Qwen answer, then classify the interaction with APC 1.3."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from collections import deque
from pathlib import Path
from typing import Any

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

DEFAULT_BASE_URL = "https://fvscode-proxy.iahn.hanoi.vn/v1"
DEFAULT_MODEL = "qwen3-coder"
ALLOWED_LEVELS = {"L0", "L1", "L2", "L3", "L4", "L5", "L6", "Thieu context"}

class ApiError(RuntimeError):
    pass


class RateLimiter:
    """Sliding-window request limiter shared by all API calls."""

    def __init__(self, requests_per_minute: int) -> None:
        if requests_per_minute < 0:
            raise ValueError("requests_per_minute không được âm")
        self.limit = requests_per_minute
        self.timestamps: deque[float] = deque()

    def acquire(self) -> None:
        if self.limit == 0:
            return
        while True:
            now = time.monotonic()
            while self.timestamps and now - self.timestamps[0] >= 60:
                self.timestamps.popleft()
            if len(self.timestamps) < self.limit:
                self.timestamps.append(now)
                return
            wait_seconds = max(0.01, 60 - (now - self.timestamps[0]))
            print(
                f"[rate limit] Đã đạt {self.limit} req/min, "
                f"chờ {wait_seconds:.1f}s...",
                file=sys.stderr,
                flush=True,
            )
            time.sleep(wait_seconds)


def load_dotenv(path: Path) -> None:
    """Load simple KEY=VALUE entries without requiring python-dotenv."""
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if key:
            os.environ.setdefault(key, value)


def chat_completion(
    *,
    base_url: str,
    api_key: str,
    model: str,
    messages: list[dict[str, str]],
    temperature: float,
    timeout: float,
    rate_limiter: RateLimiter,
) -> str:
    rate_limiter.acquire()
    url = f"{base_url.rstrip('/')}/chat/completions"
    body = json.dumps(
        {"model": model, "messages": messages, "temperature": temperature},
        ensure_ascii=False,
    ).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise ApiError(f"API HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise ApiError(f"Không kết nối được API: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ApiError("API trả về JSON không hợp lệ") from exc

    try:
        return payload["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError, TypeError, AttributeError) as exc:
        raise ApiError(f"API response không đúng định dạng: {payload}") from exc


def parse_json_object(
    raw: str, expected_keys: set[str] | None = None
) -> dict[str, Any]:
    text = raw.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        text = "\n".join(lines[1:-1]).strip()

    try:
        value = json.loads(text)
        if isinstance(value, dict) and (
            expected_keys is None or set(value) == expected_keys
        ):
            return value
    except json.JSONDecodeError:
        pass

    # Một số proxy chèn __CLASSIFIER_RESULT__ hoặc nhiều JSON object vào response.
    # Quét từng object và chỉ lấy object khớp chính xác schema đang cần.
    decoder = json.JSONDecoder()
    candidates: list[dict[str, Any]] = []
    for index, character in enumerate(text):
        if character != "{":
            continue
        try:
            candidate, _ = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict):
            candidates.append(candidate)

    if expected_keys is not None:
        matches = [candidate for candidate in candidates if set(candidate) == expected_keys]
        if matches:
            return matches[-1]
        raise ApiError(
            f"Model không trả về JSON đúng schema {sorted(expected_keys)}: {raw}"
        )
    if candidates:
        return candidates[-1]
    raise ApiError(f"Model không trả về JSON object hợp lệ: {raw}")


def evaluate_in_one_request(
    config: argparse.Namespace, user_input: str
) -> dict[str, Any]:
    raw = chat_completion(
        base_url=config.base_url,
        api_key=config.api_key,
        model=config.model,
        messages=[
            {"role": "system", "content": config.apc_system_prompt},
            {
                "role": "user",
                "content": json.dumps({"student_prompt": user_input}, ensure_ascii=False),
            },
        ],
        temperature=config.temperature,
        timeout=config.timeout,
        rate_limiter=config.rate_limiter,
    )
    expected_keys = {
        "status",
        "prompt_injection",
        "missing_context",
        "prompt_level",
        "ambiguous",
        "candidate_levels",
        "ai_answer",
        "answer_behavior_level",
        "answer_matches_request",
        "reason",
    }
    result = parse_json_object(raw, expected_keys)
    status = result.get("status")
    injection = result.get("prompt_injection")
    missing = result.get("missing_context")
    level = result.get("prompt_level")
    ambiguous = result.get("ambiguous")
    candidates = result.get("candidate_levels")

    if status not in {"answered", "blocked"}:
        raise ApiError(f"One-request evaluator trả về status không hợp lệ: {raw}")
    if not isinstance(injection, bool) or not isinstance(missing, bool):
        raise ApiError(f"One-request evaluator trả về guard không hợp lệ: {raw}")
    if not isinstance(ambiguous, bool) or not isinstance(candidates, list):
        raise ApiError(f"One-request evaluator trả về ambiguity không hợp lệ: {raw}")

    if status == "blocked":
        valid_injection = (
            injection is True
            and missing is False
            and result.get("reason") == "prompt_injection"
            and level is None
            and candidates == []
        )
        valid_missing = (
            injection is False
            and missing is True
            and result.get("reason") == "missing_context"
            and level == "Thieu context"
            and candidates == ["Thieu context"]
        )
        if not (valid_injection or valid_missing):
            raise ApiError(f"One-request evaluator trả về blocked result sai: {raw}")
        if (
            ambiguous
            or result.get("ai_answer") is not None
            or result.get("answer_behavior_level") is not None
            or result.get("answer_matches_request") is not None
        ):
            raise ApiError(f"Blocked result không được chứa answer/evaluation: {raw}")
        return result

    if injection or missing or result.get("reason") is not None:
        raise ApiError(f"Answered result có guard/reason không hợp lệ: {raw}")
    if level not in ALLOWED_LEVELS - {"Thieu context"}:
        raise ApiError(f"One-request evaluator trả về prompt_level không hợp lệ: {raw}")
    if (
        not all(candidate in ALLOWED_LEVELS - {"Thieu context"} for candidate in candidates)
        or len(candidates) != len(set(candidates))
        or len(candidates) not in {1, 2}
        or candidates[0] != level
        or (ambiguous and len(candidates) != 2)
        or (not ambiguous and len(candidates) != 1)
    ):
        raise ApiError(f"One-request evaluator trả về candidate_levels sai: {raw}")
    if not isinstance(result.get("ai_answer"), str) or not result["ai_answer"].strip():
        raise ApiError(f"One-request evaluator không trả về ai_answer hợp lệ: {raw}")
    if result.get("answer_behavior_level") not in ALLOWED_LEVELS - {"Thieu context"}:
        raise ApiError(f"One-request evaluator trả về answer level không hợp lệ: {raw}")
    if not isinstance(result.get("answer_matches_request"), bool):
        raise ApiError(f"One-request evaluator trả về answer match không hợp lệ: {raw}")
    return result


def resolve_final_level(
    prompt_result: dict[str, Any], answer_result: dict[str, Any]
) -> dict[str, Any]:
    prompt_level = str(prompt_result["prompt_level"])
    candidates = list(prompt_result["candidate_levels"])
    answer_level = str(answer_result["answer_behavior_level"])
    ambiguous = bool(prompt_result["ambiguous"])

    if not ambiguous:
        final_level = prompt_level
        decision_source = "student_prompt"
        needs_review = False
    elif answer_level in candidates:
        final_level = answer_level
        decision_source = "ai_answer_fallback"
        needs_review = False
    else:
        final_level = prompt_level
        decision_source = "student_prompt_fallback"
        needs_review = True

    return {
        "final_level": final_level,
        "decision_source": decision_source,
        "needs_review": needs_review,
        "action_mismatch": not bool(answer_result["answer_matches_request"]),
    }


def process(config: argparse.Namespace, user_input: str) -> dict[str, Any]:
    evaluated = evaluate_in_one_request(config, user_input)
    if evaluated["status"] == "blocked" and evaluated["prompt_injection"]:
        return {"status": "blocked", "reason": "prompt_injection"}
    if evaluated["status"] == "blocked" and evaluated["missing_context"]:
        return {
            "status": "blocked",
            "reason": "missing_context",
            "level": "Thieu context",
            "prompt_level": "Thieu context",
            "prompt_ambiguous": False,
            "candidate_levels": ["Thieu context"],
            "final_level": "Thieu context",
            "decision_source": "guard",
            "needs_review": False,
        }

    prompt_result = {
        "prompt_level": evaluated["prompt_level"],
        "ambiguous": evaluated["ambiguous"],
        "candidate_levels": evaluated["candidate_levels"],
    }
    answer_result = {
        "answer_behavior_level": evaluated["answer_behavior_level"],
        "answer_matches_request": evaluated["answer_matches_request"],
    }
    resolution = resolve_final_level(prompt_result, answer_result)
    return {
        "status": "answered",
        "user_input": user_input,
        "ai_answer": evaluated["ai_answer"],
        "prompt_level": prompt_result["prompt_level"],
        "prompt_ambiguous": prompt_result["ambiguous"],
        "candidate_levels": prompt_result["candidate_levels"],
        "answer_behavior_level": answer_result["answer_behavior_level"],
        "answer_matches_request": answer_result["answer_matches_request"],
        **resolution,
        "level": resolution["final_level"],
    }


def append_jsonl(path: str, result: dict[str, Any]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(result, ensure_ascii=False) + "\n")


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    
    # Try writing to a temp file first and replacing
    temporary = path.with_suffix(path.suffix + ".tmp")
    try:
        temporary.write_text(content, encoding="utf-8")
        for i in range(5):
            try:
                if path.exists() and i > 0:
                    try:
                        path.unlink()
                    except OSError:
                        pass
                temporary.replace(path)
                return
            except OSError:
                time.sleep(0.1)
    except OSError:
        pass

    # Fallback to direct write if replace fails
    for i in range(5):
        try:
            path.write_text(content, encoding="utf-8")
            return
        except OSError:
            time.sleep(0.1)


def load_test_cases(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    try:
        document = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError as exc:
        raise ApiError(f"Không tìm thấy input JSON: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ApiError(f"Input JSON không hợp lệ: {exc}") from exc

    if not isinstance(document, dict) or not isinstance(document.get("test_cases"), list):
        raise ApiError("Input JSON phải là object có mảng test_cases")

    cases = document["test_cases"]
    for index, case in enumerate(cases, start=1):
        if not isinstance(case, dict):
            raise ApiError(f"test_cases[{index - 1}] phải là object")
        if not isinstance(case.get("user_input"), str):
            raise ApiError(f"test_cases[{index - 1}].user_input phải là string")

    metadata = {key: value for key, value in document.items() if key != "test_cases"}
    return metadata, cases


def calculate_summary(results: list[dict[str, Any]]) -> dict[str, Any]:
    scored = [
        item for item in results
        if isinstance(item.get("is_correct"), bool) and item.get("status") == "answered"
    ]
    correct = sum(item["is_correct"] for item in scored)
    prompt_scored = [
        item for item in results
        if isinstance(item.get("prompt_is_correct"), bool) and item.get("status") == "answered"
    ]
    prompt_correct = sum(item["prompt_is_correct"] for item in prompt_scored)
    answered = sum(item.get("status") == "answered" for item in results)
    blocked = sum(item.get("status") == "blocked" for item in results)
    errors = sum(item.get("status") == "error" for item in results)
    fallback_used = sum(
        item.get("decision_source") == "ai_answer_fallback" for item in results
    )
    fallback_improved = sum(
        item.get("prompt_is_correct") is False and item.get("is_correct") is True
        for item in results
    )
    fallback_harmed = sum(
        item.get("prompt_is_correct") is True and item.get("is_correct") is False
        for item in results
    )
    return {
        "total": len(results),
        "answered": answered,
        "blocked": blocked,
        "errors": errors,
        "scored": len(scored),
        "correct": correct,
        "accuracy": round(correct / len(scored), 4) if scored else None,
        "prompt_only_scored": len(prompt_scored),
        "prompt_only_correct": prompt_correct,
        "prompt_only_accuracy": (
            round(prompt_correct / len(prompt_scored), 4) if prompt_scored else None
        ),
        "fallback_used": fallback_used,
        "fallback_improved": fallback_improved,
        "fallback_harmed": fallback_harmed,
        "action_mismatch_count": sum(
            item.get("action_mismatch") is True for item in results
        ),
        "needs_review_count": sum(item.get("needs_review") is True for item in results),
    }


def print_progress(completed: int, total: int, elapsed: float, status_text: str) -> None:
    width = 30
    if total <= 0:
        percent = 100.0
        filled_len = width
    else:
        percent = (completed / total) * 100
        filled_len = int(width * completed // total)
    bar = "█" * filled_len + "░" * (width - filled_len)
    msg = f"[{bar}] {percent:5.1f}% | {completed}/{total} | {status_text} | {elapsed:.1f}s"
    if sys.stderr.isatty():
        sys.stderr.write(f"\r{msg}\033[K")
        sys.stderr.flush()
    else:
        sys.stderr.write(f"{msg}\n")
        sys.stderr.flush()


def run_batch(config: argparse.Namespace) -> dict[str, Any]:
    started_at = time.time()
    input_path = Path(config.input_json).resolve()
    output_path = (
        Path(config.output_json).resolve()
        if config.output_json
        else input_path.with_name(f"{input_path.stem}_results.json")
    )
    progress_path = (
        Path(config.progress_json).resolve()
        if config.progress_json
        else output_path.with_name(f"{output_path.stem}_progress.json")
    )
    metadata, cases = load_test_cases(input_path)
    output: dict[str, Any] = {
        "source": str(input_path),
        "model": config.model,
        "base_url": config.base_url,
        "dataset": metadata,
        "results": [],
        "summary": {},
    }

    existing_results: dict[str, dict[str, Any]] = {}
    if not getattr(config, "no_resume", False) and output_path.is_file():
        try:
            existing_data = json.loads(output_path.read_text(encoding="utf-8"))
            if isinstance(existing_data, dict) and isinstance(existing_data.get("results"), list):
                for item in existing_data["results"]:
                    if isinstance(item, dict) and "id" in item:
                        if item.get("status") != "error":
                            existing_results[item["id"]] = item
                print(f"Đã tìm thấy {len(existing_results)} kết quả hợp lệ để resume.", file=sys.stderr)
        except Exception as exc:
            print(f"Không thể đọc file kết quả để resume: {exc}", file=sys.stderr)

    for index, case in enumerate(cases, start=1):
        case_id = case.get("id", f"test_{index:03d}")
        elapsed = round(time.time() - started_at, 1)

        if case_id in existing_results:
            item = existing_results[case_id]
            output["results"].append(item)
            output["summary"] = calculate_summary(output["results"])
            progress = {
                **output["summary"],
                "status": "running",
                "current_case": case_id,
                "completed": index,
                "total": len(cases),
                "percent": round(index * 100 / len(cases), 2) if cases else 100,
                "elapsed_seconds": elapsed,
            }
            write_json(progress_path, progress)
            print_progress(index, len(cases), elapsed, f"Resume {case_id}: {item['status']}")
            continue

        progress = {
            **calculate_summary(output["results"]),
            "status": "running",
            "current_case": case_id,
            "completed": index - 1,
            "total": len(cases),
            "percent": round((index - 1) * 100 / len(cases), 2) if cases else 100,
            "elapsed_seconds": elapsed,
        }
        write_json(progress_path, progress)
        print_progress(index - 1, len(cases), elapsed, f"Đang chạy {case_id}")
        try:
            processed = process(config, case["user_input"])
            item = {
                "id": case_id,
                "user_input": case["user_input"],
                "ai_answer": processed.get("ai_answer"),
                "prompt_level": processed.get("prompt_level"),
                "prompt_ambiguous": processed.get("prompt_ambiguous"),
                "candidate_levels": processed.get("candidate_levels"),
                "answer_behavior_level": processed.get("answer_behavior_level"),
                "answer_matches_request": processed.get("answer_matches_request"),
                "final_level": processed.get("final_level", processed.get("level")),
                "predicted_level": processed.get("final_level", processed.get("level")),
                "decision_source": processed.get("decision_source"),
                "action_mismatch": processed.get("action_mismatch"),
                "needs_review": processed.get("needs_review", False),
                "expected_level": case.get("expected_level"),
                "status": processed["status"],
                "reason": processed.get("reason"),
                "note": case.get("note", ""),
            }
            expected_status = case.get("expected_status")
            if isinstance(case.get("expected_level"), str):
                item["is_correct"] = item["predicted_level"] == case["expected_level"]
                item["prompt_is_correct"] = (
                    item["prompt_level"] == case["expected_level"]
                )
            elif isinstance(expected_status, str):
                item["is_correct"] = (
                    item["status"] == expected_status
                    and item["reason"] == case.get("expected_reason")
                )
                item["prompt_is_correct"] = None
            else:
                item["is_correct"] = None
                item["prompt_is_correct"] = None
        except ApiError as exc:
            item = {
                "id": case_id,
                "user_input": case["user_input"],
                "ai_answer": None,
                "prompt_level": None,
                "prompt_ambiguous": None,
                "candidate_levels": None,
                "answer_behavior_level": None,
                "answer_matches_request": None,
                "final_level": None,
                "predicted_level": None,
                "decision_source": None,
                "action_mismatch": None,
                "needs_review": True,
                "expected_level": case.get("expected_level"),
                "status": "error",
                "error": str(exc),
                "is_correct": False if isinstance(case.get("expected_level"), str) else None,
                "prompt_is_correct": None,
                "note": case.get("note", ""),
            }

        output["results"].append(item)
        output["summary"] = calculate_summary(output["results"])
        write_json(output_path, output)
        print_progress(index, len(cases), elapsed, f"Xong {case_id}: {item['status']}")

    if sys.stderr.isatty():
        sys.stderr.write("\n")

    final_progress = {
        "status": "completed",
        "current_case": None,
        "completed": len(cases),
        "total": len(cases),
        "percent": 100,
        "elapsed_seconds": round(time.time() - started_at, 1),
        **output["summary"],
        "output_json": str(output_path),
    }
    write_json(progress_path, final_progress)
    return {
        "status": "completed",
        "output_json": str(output_path),
        "progress_json": str(progress_path),
        **output["summary"],
    }


def build_parser() -> argparse.ArgumentParser:
    default_prompt_file = Path(__file__).resolve().with_name("system_prompt_ver22.md")
    parser = argparse.ArgumentParser(
        description="Gọi Qwen trả lời và phân loại tương tác theo APC 1.3."
    )
    parser.add_argument("prompt", nargs="?", help="Prompt người dùng")
    parser.add_argument("--model", default=os.getenv("QWEN_MODEL", DEFAULT_MODEL))
    parser.add_argument("--base-url", default=os.getenv("QWEN_BASE_URL", DEFAULT_BASE_URL))
    parser.add_argument("--api-key", default=os.getenv("QWEN_API_KEY"))
    parser.add_argument("--temperature", type=float, default=0.2)
    parser.add_argument("--timeout", type=float, default=120)
    parser.add_argument(
        "--requests-per-minute",
        type=int,
        default=int(os.getenv("QWEN_REQUESTS_PER_MINUTE", "0")),
        help="Giới hạn tổng API request mỗi phút; 0 để tắt giới hạn (mặc định: 0)",
    )
    parser.add_argument("--output-jsonl", help="Ghi nối kết quả vào file JSONL")
    parser.add_argument("--input-json", help="Chạy toàn bộ test_cases trong file JSON")
    parser.add_argument("--output-json", help="File JSON kết quả cho chế độ batch")
    parser.add_argument("--progress-json", help="File JSON theo dõi tiến trình batch")
    parser.add_argument(
        "--no-resume",
        action="store_true",
        help="Không chạy tiếp (resume) từ kết quả đã có mà chạy lại từ đầu",
    )
    parser.add_argument(
        "--apc-prompt-file",
        default=os.getenv("APC_PROMPT_FILE", str(default_prompt_file)),
        help="System prompt one-request dùng để trả lời và đánh giá APC",
    )
    return parser


def main() -> int:
    load_dotenv(Path(__file__).resolve().with_name(".env"))
    config = build_parser().parse_args()
    if config.requests_per_minute < 0:
        print("--requests-per-minute không được âm.", file=sys.stderr)
        return 2
    config.rate_limiter = RateLimiter(config.requests_per_minute)
    prompt_files = {"apc_system_prompt": Path(config.apc_prompt_file).resolve()}
    for attribute, prompt_path in prompt_files.items():
        try:
            prompt_text = prompt_path.read_text(encoding="utf-8-sig").strip()
        except OSError as exc:
            print(f"Không đọc được system prompt {prompt_path}: {exc}", file=sys.stderr)
            return 2
        if not prompt_text:
            print(f"System prompt đang rỗng: {prompt_path}", file=sys.stderr)
            return 2
        setattr(config, attribute, prompt_text)
    if not config.api_key:
        print("Thiếu QWEN_API_KEY. Hãy đặt biến môi trường trước khi chạy.", file=sys.stderr)
        return 2

    if config.input_json:
        if config.prompt:
            print("Không dùng prompt positional cùng --input-json.", file=sys.stderr)
            return 2
        try:
            batch_result = run_batch(config)
        except ApiError as exc:
            print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False))
            return 1
        print(json.dumps(batch_result, ensure_ascii=False, indent=2))
        return 0

    user_input = config.prompt
    if user_input is None:
        user_input = input("User input: ") if sys.stdin.isatty() else sys.stdin.read()

    try:
        result = process(config, user_input)
    except ApiError as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False))
        return 1

    print(json.dumps(result, ensure_ascii=False, indent=2))
    if config.output_jsonl:
        append_jsonl(config.output_jsonl, result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
