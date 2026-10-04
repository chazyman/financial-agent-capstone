# /// script
# requires-python = ">=3.12"
# dependencies = ["docling", "torch", "torchvision"]
#
# [tool.uv.sources]
# torch = { index = "pytorch-cpu" }
# torchvision = { index = "pytorch-cpu" }
#
# [[tool.uv.index]]
# name = "pytorch-cpu"
# url = "https://download.pytorch.org/whl/cpu"
# explicit = true
# ///
"""Phase 0 check 4: how large is a parse response that carries every crop of a long filing?

Assumption (DESIGN §3, §9.1): the models service returns the Docling JSON, the Markdown and every
table, chart and picture crop in its answer, and the engine writes them. That only works if the
answer stays a workable size.
Pass criterion, written before running: for a filing of 60 pages or more, parsed with the design's
settings (crops at images_scale 2.0), the answer is under 100 MB in total and under 1 MB per page.
The answer is: Docling JSON with its embedded page and picture images removed + Markdown + every
crop as PNG. The first run showed that Docling's JSON embeds every page image even in placeholder
mode, so the script now measures the JSON both ways; the criterion is unchanged.
Runs Docling on CPU, so no GPU and no models service is needed.
Run: uv run 04_parse_response_size.py <path to a long filing PDF>
"""
import importlib.metadata as md
import io
import json
import sys
import tempfile
import time
from pathlib import Path

from docling.datamodel.base_models import InputFormat
from docling.datamodel.pipeline_options import PdfPipelineOptions, TableFormerMode
from docling.document_converter import DocumentConverter, PdfFormatOption
from docling_core.types.doc import ImageRefMode, PictureItem, TableItem

pdf = Path(sys.argv[1])
print(f"docling {md.version('docling')}; input {pdf.name}, {pdf.stat().st_size / 1e6:.2f} MB")

options = PdfPipelineOptions()
options.images_scale = 2.0
options.generate_page_images = True      # tables are cropped from the page image
options.generate_picture_images = True
options.do_ocr = False
options.do_table_structure = True
options.table_structure_options.mode = TableFormerMode.ACCURATE

t0 = time.perf_counter()
converter = DocumentConverter(format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=options)})
doc = converter.convert(pdf).document
seconds = time.perf_counter() - t0
pages = len(doc.pages)

crops = {"table": [], "picture": []}
for item, _level in doc.iterate_items():
    kind = "table" if isinstance(item, TableItem) else "picture" if isinstance(item, PictureItem) else None
    if kind is None:
        continue
    image = item.get_image(doc)
    if image is None:
        continue
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    crops[kind].append(buffer.tell())

with tempfile.TemporaryDirectory() as tmp:
    as_written = Path(tmp) / "as_written.json"
    doc.save_as_json(as_written, image_mode=ImageRefMode.PLACEHOLDER)
    json_as_written = as_written.stat().st_size

# What the engine would send: the crops travel as files, so the JSON carries no image.
for page in doc.pages.values():
    page.image = None
for item, _level in doc.iterate_items():
    if isinstance(item, PictureItem):
        item.image = None
json_slim = len(json.dumps(doc.export_to_dict()).encode())
markdown = len(doc.export_to_markdown().encode())

crop_bytes = sum(crops["table"]) + sum(crops["picture"])
total = json_slim + markdown + crop_bytes
report = {
    "pages": pages,
    "parse_seconds_cpu": round(seconds, 1),
    "tables": len(crops["table"]),
    "pictures": len(crops["picture"]),
    "largest_crop_mb": round(max(crops["table"] + crops["picture"] + [0]) / 1e6, 2),
    "crops_mb": round(crop_bytes / 1e6, 2),
    "json_images_removed_mb": round(json_slim / 1e6, 2),
    "markdown_mb": round(markdown / 1e6, 2),
    "answer_total_mb": round(total / 1e6, 2),
    "answer_mb_per_page": round(total / 1e6 / max(pages, 1), 3),
    "answer_total_if_base64_mb": round((json_slim + markdown + crop_bytes * 4 / 3) / 1e6, 2),
    "for_information_json_as_docling_writes_it_mb": round(json_as_written / 1e6, 2),
}
print(json.dumps(report, indent=1))
ok = pages >= 60 and total < 100e6 and total / max(pages, 1) < 1e6
print("RESULT:", "PASS" if ok else ("NOT APPLICABLE: fewer than 60 pages" if pages < 60 else "FAIL"))
