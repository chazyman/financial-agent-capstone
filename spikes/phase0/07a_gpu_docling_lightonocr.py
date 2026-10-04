# /// script
# requires-python = ">=3.12"
# dependencies = ["docling", "torch", "torchvision", "transformers>=5.0.0", "pymupdf", "pillow"]
# ///
"""Phase 0 check 7a: do Docling and LightOnOCR run together on the laptop GPU without saturating it?

Part of assumption 7 (the four GPU models fit in 16 GB, DESIGN §9.1). This covers two of the four:
Docling's layout and table models, and LightOnOCR-2-1B, kept loaded together as the models service
keeps them. The reranker and the sentiment classifier are not covered here.
Pass criterion, written before running:
  a. with both loaded, peak GPU memory stays under 10 GB, leaving 6 GB for the other two models;
  b. no out-of-memory error on an 80-page filing (Docling) or on rasterised pages (LightOnOCR);
  c. LightOnOCR recovers at least 95% of the words of rasterised born-digital pages, measured
     against the PDF's own text layer (the OCR sample of DESIGN §11).
Reported, not gated: seconds per page for each model.
Run: uv run 07a_gpu_docling_lightonocr.py <path to a long filing PDF>
PyTorch 2.14 needs a C compiler for its Triton kernels; without one, set TORCH_DISABLE_NATIVE_JIT=1.
In a container the image also needs fonts and OpenCV's system libraries (README, "Check 7a").
"""
import importlib.metadata as md
import json
import re
import sys
import time
from collections import Counter
from pathlib import Path

import fitz  # PyMuPDF
import torch
from PIL import Image

pdf_path = Path(sys.argv[1])
assert torch.cuda.is_available(), "no CUDA device visible"
DEVICE = "cuda"
MB = 1e6


def device_used_mb() -> float:
    free, total = torch.cuda.mem_get_info()
    return (total - free) / MB


report: dict = {
    "versions": {name: md.version(name) for name in ("torch", "docling", "transformers", "pymupdf")},
    "gpu": torch.cuda.get_device_name(0),
    "gpu_total_mb": round(torch.cuda.get_device_properties(0).total_memory / MB),
    "device_used_before_mb": round(device_used_mb()),
}

# ---- Docling on the GPU, with the design's settings (as in check 4)
from docling.datamodel.base_models import InputFormat
from docling.datamodel.pipeline_options import PdfPipelineOptions, TableFormerMode
from docling.document_converter import DocumentConverter, PdfFormatOption
from docling_core.types.doc import PictureItem, TableItem

try:
    from docling.datamodel.accelerator_options import AcceleratorDevice, AcceleratorOptions
except ImportError:
    from docling.datamodel.pipeline_options import AcceleratorDevice, AcceleratorOptions

options = PdfPipelineOptions()
options.images_scale = 2.0
options.generate_page_images = True
options.generate_picture_images = True
options.do_ocr = False
options.do_table_structure = True
options.table_structure_options.mode = TableFormerMode.ACCURATE
options.accelerator_options = AcceleratorOptions(device=AcceleratorDevice.CUDA)

torch.cuda.reset_peak_memory_stats()
t0 = time.perf_counter()
converter = DocumentConverter(format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=options)})
doc = converter.convert(pdf_path).document
docling_seconds = time.perf_counter() - t0
pages = len(doc.pages)
table_pages = sorted({item.prov[0].page_no for item, _ in doc.iterate_items() if isinstance(item, TableItem) and item.prov})
report["docling"] = {
    "pages": pages,
    "tables": sum(isinstance(i, TableItem) for i, _ in doc.iterate_items()),
    "pictures": sum(isinstance(i, PictureItem) for i, _ in doc.iterate_items()),
    "seconds_including_model_load": round(docling_seconds, 1),
    "seconds_per_page": round(docling_seconds / pages, 2),
    "peak_allocated_mb": round(torch.cuda.max_memory_allocated() / MB),
    "peak_reserved_mb": round(torch.cuda.max_memory_reserved() / MB),
    "held_after_run_mb": round(torch.cuda.memory_reserved() / MB),
}

# ---- LightOnOCR, loaded while Docling's models stay in memory (loading code as in the Day 2 notebook)
from transformers import LightOnOcrForConditionalGeneration, LightOnOcrProcessor

OCR_ID = "lightonai/LightOnOCR-2-1B"
t0 = time.perf_counter()
try:
    ocr_model = LightOnOcrForConditionalGeneration.from_pretrained(OCR_ID, dtype=torch.bfloat16).to(DEVICE)
except TypeError:
    ocr_model = LightOnOcrForConditionalGeneration.from_pretrained(OCR_ID, torch_dtype=torch.bfloat16).to(DEVICE)
ocr_processor = LightOnOcrProcessor.from_pretrained(OCR_ID)
load_seconds = time.perf_counter() - t0
held_with_both = torch.cuda.memory_reserved() / MB


@torch.inference_mode()
def ocr_pages(images: list[Image.Image], max_new_tokens: int = 4096) -> tuple[list[str], int]:
    conversations = [{"role": "user", "content": [{"type": "image", "image": im}]} for im in images]
    inputs = ocr_processor.apply_chat_template(
        conversations, add_generation_prompt=True, tokenize=True, return_dict=True, return_tensors="pt", padding=True
    )
    inputs = {
        k: (v.to(device=DEVICE, dtype=torch.bfloat16) if v.is_floating_point() else v.to(DEVICE)) for k, v in inputs.items()
    }
    out = ocr_model.generate(**inputs, max_new_tokens=max_new_tokens)
    new_tokens = out[:, inputs["input_ids"].shape[1]:]
    return ocr_processor.batch_decode(new_tokens, skip_special_tokens=True), int(new_tokens.shape[1])


WORD = re.compile(r"[a-z0-9]+(?:[.,][0-9]+)*")


def words(text: str) -> Counter:
    text = re.sub(r"<[^>]+>", " ", text)          # HTML table tags in the OCR output
    text = re.sub(r"[*_#|`$\\]", " ", text.lower())  # Markdown marks
    return Counter(WORD.findall(text))


def recall(reference: Counter, found: Counter) -> float:
    total = sum(reference.values())
    return sum((reference & found).values()) / total if total else 1.0


# Two prose pages and two table pages, rendered the way a scanned page would arrive: one image each.
pdf = fitz.open(pdf_path)
prose = [p for p in (5, 20) if p <= pages]
chosen = prose + [p for p in table_pages if p not in prose and p > 20][:2]
images, references = [], []
for page_no in chosen:
    page = pdf[page_no - 1]
    zoom = 1288 / max(page.rect.width, page.rect.height)  # the resolution the Day 2 notebook uses
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
    images.append(Image.frombytes("RGB", (pix.width, pix.height), pix.samples))
    references.append(page.get_text("text"))
pdf.close()

ocr_rows = []
for page_no, image, reference in zip(chosen, images, references):
    t0 = time.perf_counter()
    (text,), n_tokens = ocr_pages([image])
    seconds = time.perf_counter() - t0
    ref, got = words(reference), words(text)
    numbers = Counter({w: c for w, c in ref.items() if any(ch.isdigit() for ch in w)})
    ocr_rows.append({
        "page": page_no,
        "has_table": page_no in table_pages,
        "seconds": round(seconds, 1),
        "tokens": n_tokens,
        "tokens_per_second": round(n_tokens / seconds, 1),
        "hit_token_cap": n_tokens >= 4096,
        "reference_words": sum(ref.values()),
        "word_recall": round(recall(ref, got), 4),
        "number_recall": round(recall(numbers, got), 4),
        "word_precision": round(recall(got, ref), 4),
        "missing_from_ocr": [w for w, _ in (ref - got).most_common(12)],
        "extra_in_ocr": [w for w, _ in (got - ref).most_common(12)],
    })

t0 = time.perf_counter()
_, _ = ocr_pages(images)  # all pages in one batch, to see the memory a batch needs
batch_seconds = time.perf_counter() - t0

total_ref = sum(r["reference_words"] for r in ocr_rows)
overall_recall = sum(r["word_recall"] * r["reference_words"] for r in ocr_rows) / total_ref
report["lightonocr"] = {
    "load_seconds": round(load_seconds, 1),
    "held_with_docling_also_loaded_mb": round(held_with_both),
    "pages": ocr_rows,
    "mean_seconds_per_page_one_at_a_time": round(sum(r["seconds"] for r in ocr_rows) / len(ocr_rows), 1),
    "seconds_for_all_pages_in_one_batch": round(batch_seconds, 1),
    "overall_word_recall": round(overall_recall, 4),
}
report["both_models"] = {
    "peak_allocated_mb": round(torch.cuda.max_memory_allocated() / MB),
    "peak_reserved_mb": round(torch.cuda.max_memory_reserved() / MB),
    "device_used_at_end_mb": round(device_used_mb()),
}
print(json.dumps(report, indent=1))

checks = {
    "a. peak GPU memory with both loaded under 10 GB": torch.cuda.max_memory_reserved() < 10_000 * MB,
    "b. no out-of-memory error": True,  # an OOM would have raised before this line
    "c. OCR word recall at least 95%": overall_recall >= 0.95,
}
for name, ok in checks.items():
    print(f"  {'PASS' if ok else 'FAIL'}  {name}")
print("RESULT:", "PASS" if all(checks.values()) else "FAIL")
