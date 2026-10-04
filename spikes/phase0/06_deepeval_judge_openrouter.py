# /// script
# requires-python = ">=3.12"
# dependencies = ["deepeval", "pymupdf"]
# ///
"""Phase 0 check 6: can a DeepEval judge on OpenRouter detect a shift in answer quality, and what does it cost?

Assumption (DESIGN §11): evaluation runs score answers with DeepEval through a judge model on
OpenRouter, compare each score with a stored baseline, and treat a drop beyond a tolerance as a shift.
That only works if a real shift moves the score by more than the judge's own run-to-run noise.
Pass criterion, written before running:
  a. DeepEval's faithfulness and G-Eval metrics run with an OpenRouter judge;
  b. a wrong figure is detected: on both metrics the answer with the wrong figure scores at least
     0.3 below the correct answer;
  c. the noise is smaller than that signal: over three repeats of the correct answer, each metric's
     spread (highest minus lowest) is under half the gap measured in b;
  d. a case whose retrieved context is an image (a page of a public filing with a chart) is scored.
Reported, not gated: the spread itself, the seconds and the dollars the check used.
No TipRanks content is used: the text case is invented and the image is from a public SEC filing.
Run: OPENROUTER_API_KEY=... uv run 06_deepeval_judge_openrouter.py <path to a filing PDF> [page number]
"""
import importlib.metadata as md
import json
import os
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

os.environ.setdefault("DEEPEVAL_TELEMETRY_OPT_OUT", "1")

import fitz  # PyMuPDF
from deepeval.metrics import FaithfulnessMetric, GEval
from deepeval.models import OpenRouterModel
from deepeval.test_case import LLMTestCase, LLMTestCaseParams, MLLMImage

JUDGE = os.environ.get("JUDGE_MODEL", "google/gemini-2.5-flash-lite")
KEY = os.environ["OPENROUTER_API_KEY"]


def credits_used() -> float:
    request = urllib.request.Request("https://openrouter.ai/api/v1/key", headers={"Authorization": f"Bearer {KEY}"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return float(json.load(response)["data"]["usage"])


judge = OpenRouterModel(model=JUDGE, api_key=KEY, temperature=0)

QUESTION = "What were Example Corp's net sales in fiscal 2025, and how did they change?"
CONTEXT = [
    "In fiscal 2025, Example Corp reported total net sales of $412.5 million, up 8% from $381.9 million "
    "in fiscal 2024. Services net sales were $96.2 million."
]
EXPECTED = "Net sales were $412.5 million in fiscal 2025, up 8% from fiscal 2024."
CORRECT = "Example Corp's fiscal 2025 net sales were $412.5 million, an 8% increase over fiscal 2024."
WRONG_FIGURE = "Example Corp's fiscal 2025 net sales were $452.5 million, an 18% increase over fiscal 2024."


def faithfulness() -> FaithfulnessMetric:
    return FaithfulnessMetric(model=judge, threshold=0.7, async_mode=False, include_reason=False)


def correctness() -> GEval:
    return GEval(
        name="Correctness",
        criteria="Determine whether the actual output states the same figures and facts as the expected output.",
        evaluation_params=[LLMTestCaseParams.INPUT, LLMTestCaseParams.ACTUAL_OUTPUT, LLMTestCaseParams.EXPECTED_OUTPUT],
        model=judge,
        async_mode=False,
    )


def score(answer: str) -> dict[str, float]:
    case = LLMTestCase(input=QUESTION, actual_output=answer, expected_output=EXPECTED, retrieval_context=CONTEXT)
    out = {}
    for name, metric in (("faithfulness", faithfulness()), ("correctness", correctness())):
        metric.measure(case)
        out[name] = round(float(metric.score), 3)
    return out


print(f"deepeval {md.version('deepeval')}; judge {JUDGE}")
spent_before = credits_used()
t0 = time.perf_counter()

repeats = [score(CORRECT) for _ in range(3)]
wrong = score(WRONG_FIGURE)
report = {"correct_answer_three_times": repeats, "wrong_figure": wrong}

gap, spread = {}, {}
for name in ("faithfulness", "correctness"):
    values = [r[name] for r in repeats]
    spread[name] = round(max(values) - min(values), 3)
    gap[name] = round(sum(values) / len(values) - wrong[name], 3)
report["gap_correct_minus_wrong"] = gap
report["spread_over_three_repeats"] = spread

image_result = "not run: no PDF given"
if len(sys.argv) > 1:
    pdf = fitz.open(sys.argv[1])
    page = pdf[int(sys.argv[2]) - 1 if len(sys.argv) > 2 else 22]
    zoom = 1288 / max(page.rect.width, page.rect.height)
    png = Path(tempfile.mkdtemp()) / "filing_page.png"
    page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False).save(png)
    pdf.close()
    image = MLLMImage(url=str(png))
    image_scores = {}
    for label, statement in {
        "statement_that_matches_the_page": "The page compares the company's cumulative total shareholder return with market indexes over five years.",
        "statement_that_contradicts_it": "The page shows the company's net sales by product category for a single quarter.",
    }.items():
        case = LLMTestCase(
            input="What does the chart on this page of the filing show?",
            actual_output=statement,
            retrieval_context=[f"A page of the filing: {image}"],
        )
        metric = faithfulness()
        metric.measure(case)
        image_scores[label] = round(float(metric.score), 3)
        image_scores["test_case_flagged_multimodal"] = bool(case.multimodal)
    image_result = image_scores
report["image_case"] = image_result

report["seconds"] = round(time.perf_counter() - t0, 1)
time.sleep(4)  # OpenRouter's usage counter lags by a few seconds
report["dollars_spent"] = round(credits_used() - spent_before, 5)
print(json.dumps(report, indent=1))

checks = {
    "a. metrics ran with an OpenRouter judge": True,  # an error would have raised before this line
    "b. wrong figure scores at least 0.3 lower on both metrics": all(g >= 0.3 for g in gap.values()),
    "c. spread over three repeats is under half the gap": all(spread[n] < gap[n] / 2 for n in gap),
    "d. a case with an image as context was scored": isinstance(image_result, dict) and image_result.get("test_case_flagged_multimodal") is True,
}
for name, ok in checks.items():
    print(f"  {'PASS' if ok else 'FAIL'}  {name}")
print("RESULT:", "PASS" if all(checks.values()) else "FAIL")
