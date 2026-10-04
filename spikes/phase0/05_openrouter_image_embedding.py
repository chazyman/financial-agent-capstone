# /// script
# requires-python = ">=3.12"
# dependencies = ["pymupdf"]
# ///
"""Phase 0 check 5: does OpenRouter embed an image with Gemini Embedding 2, in the same space as text?

Assumption (DESIGN §3, §5, §10): `embed` sends text, a crop, or a crop and its text together to
google/gemini-embedding-2 on OpenRouter and stores one vector of `embedding.dimensions` (1536) per chunk,
so a text question can find a chart or a table crop.
Pass criterion, written before running:
  a. a text item returns a vector of 1536 values when `dimensions: 1536` is sent;
  b. an image item (a PNG sent as a data URL) returns a vector of the same size;
  c. an item holding a crop and its text together returns one vector of that size;
  d. the vectors share one space: the chart page's image is closer to a sentence describing it than to
     a sentence about another subject, and a question about the chart ranks the chart page's image
     above another page's image;
  e. the request is accepted with `provider.data_collection: deny`, the design's setting.
Reported, not gated: the cosine values, whether the vectors arrive normalised, the seconds and the dollars.
No TipRanks content is used: both images are pages of a public SEC filing.
Run: OPENROUTER_API_KEY=... uv run 05_openrouter_image_embedding.py <path to a filing PDF> [chart page] [other page]
"""
import base64
import importlib.metadata as md
import json
import math
import os
import sys
import time
import urllib.error
import urllib.request

import fitz  # PyMuPDF

MODEL = os.environ.get("EMBEDDING_MODEL", "google/gemini-embedding-2")
DIMENSIONS = int(os.environ.get("EMBEDDING_DIMENSIONS", "1536"))
KEY = os.environ["OPENROUTER_API_KEY"]
API = "https://openrouter.ai/api/v1"


def call(path: str, body: dict | None = None) -> dict:
    request = urllib.request.Request(
        f"{API}/{path}",
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        print(f"HTTP {error.code}: {error.read().decode(errors='replace')[:600]}")
        print("RESULT: FAIL")
        sys.exit(1)


def page_png(pdf: fitz.Document, page_no: int) -> str:
    page = pdf[page_no - 1]
    zoom = 1288 / max(page.rect.width, page.rect.height)
    png = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False).tobytes("png")
    return "data:image/png;base64," + base64.b64encode(png).decode()


def text(value: str) -> dict:
    return {"content": [{"type": "text", "text": value}]}


def image(url: str, caption: str | None = None) -> dict:
    parts = [{"type": "text", "text": caption}] if caption else []
    return {"content": parts + [{"type": "image_url", "image_url": {"url": url}}]}


def cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b)) / (math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b)))


pdf = fitz.open(sys.argv[1])
chart_page = int(sys.argv[2]) if len(sys.argv) > 2 else 23
other_page = int(sys.argv[3]) if len(sys.argv) > 3 else 25
chart_png, other_png = page_png(pdf, chart_page), page_png(pdf, other_page)
pdf.close()

DESCRIBES_CHART = "A line chart comparing the company's five-year cumulative total shareholder return with two market indexes."
OTHER_SUBJECT = "A schedule of lease liabilities and their maturities by fiscal year."
QUESTION = "How did the company's stock perform against the S&P 500 over five years?"
CAPTION = "Company stock performance: comparison of five-year cumulative total return."

items = {
    "describes_chart": text(DESCRIBES_CHART),
    "other_subject": text(OTHER_SUBJECT),
    "question": text(QUESTION),
    "chart_image": image(chart_png),
    "other_image": image(other_png),
    "chart_image_with_caption": image(chart_png, CAPTION),
}

print(f"pymupdf {md.version('pymupdf')}; model {MODEL}; dimensions {DIMENSIONS}")
t0 = time.perf_counter()
answer = call("embeddings", {
    "model": MODEL,
    "input": list(items.values()),
    "dimensions": DIMENSIONS,
    "encoding_format": "float",
    "provider": {"data_collection": "deny"},
})
seconds = time.perf_counter() - t0
vectors = {name: row["embedding"] for name, row in zip(items, sorted(answer["data"], key=lambda r: r["index"]))}

sizes = {name: len(v) for name, v in vectors.items()}
norms = {name: round(math.sqrt(sum(x * x for x in v)), 4) for name, v in vectors.items()}
cos = {
    "chart_image~describes_chart": cosine(vectors["chart_image"], vectors["describes_chart"]),
    "chart_image~other_subject": cosine(vectors["chart_image"], vectors["other_subject"]),
    "question~chart_image": cosine(vectors["question"], vectors["chart_image"]),
    "question~other_image": cosine(vectors["question"], vectors["other_image"]),
    "question~chart_image_with_caption": cosine(vectors["question"], vectors["chart_image_with_caption"]),
    "question~describes_chart (text to text)": cosine(vectors["question"], vectors["describes_chart"]),
    "chart_image~other_image": cosine(vectors["chart_image"], vectors["other_image"]),
}
print(json.dumps({
    "model_answered": answer.get("model"),
    "provider": answer.get("provider"),
    "usage": answer.get("usage"),
    "sizes": sizes,
    "norms": norms,
    "cosine": {k: round(v, 4) for k, v in cos.items()},
    "image_bytes_sent": {"chart": len(chart_png) * 3 // 4, "other": len(other_png) * 3 // 4},
    "seconds": round(seconds, 1),
    "dollars_spent": (answer.get("usage") or {}).get("cost"),  # as the answer reports it
}, indent=1))

checks = {
    f"a. a text item returns {DIMENSIONS} values": sizes["describes_chart"] == DIMENSIONS,
    "b. an image item returns a vector of the same size": sizes["chart_image"] == DIMENSIONS,
    "c. a crop and its text together return one vector of that size": sizes["chart_image_with_caption"] == DIMENSIONS,
    "d. one space: the image is closer to its description, and the question ranks the right image first": (
        cos["chart_image~describes_chart"] > cos["chart_image~other_subject"]
        and cos["question~chart_image"] > cos["question~other_image"]
    ),
    "e. accepted with data_collection: deny": True,  # a refusal would have exited above
}
for name, ok in checks.items():
    print(f"  {'PASS' if ok else 'FAIL'}  {name}")
print("RESULT:", "PASS" if all(checks.values()) else "FAIL")
