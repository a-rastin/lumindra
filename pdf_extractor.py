"""
pdf_extractor.py
================

Parse a true (text-based) PDF and extract its content into a structured
folder of Markdown files, using:

    * Docling                  -> layout analysis + lossless DoclingDocument JSON
    * PyMuPDF (`pymupdf`)      -> plain text, image cropping, page geometry
    * Camelot (Stream flavor)  -> table re-extraction guided by Docling bboxes

The pipeline:

    1. Subset the input PDF to the user-selected pages (PyMuPDF).
    2. Run Docling layout analysis on that subset and save:
            <out>/_layout/layout.md
            <out>/_layout/layout.json     (lossless DoclingDocument)
    3. Walk the DoclingDocument in reading order and, for every element,
       use the original PDF + bbox to extract:
            text     -> appended to the main file
            tables   -> separate .md (Camelot Stream first, Docling fallback)
            pictures -> .png + .md sidecar
            code     -> fenced block in main file
            formula  -> math block in main file
    4. Build a `00_contents.md` (table of contents) and a `01_main.md`
       (plain text with placeholders pointing to the sidecar files).
    5. Zip the output folder.

Designed to run on Google Colab but works locally too.
"""

from __future__ import annotations

import json
import re
import shutil
import sys
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pymupdf

try:
    import camelot  # type: ignore

    HAS_CAMELOT = True
except Exception:
    HAS_CAMELOT = False

from docling.document_converter import DocumentConverter


# ---------------------------------------------------------------------------
# User input helpers
# ---------------------------------------------------------------------------

def parse_page_spec(spec: str, total: int) -> List[int]:
    """Parse user page input into a sorted, deduplicated list of 1-indexed pages.

    Accepted forms: ``"all"``, ``"*"``, ``"1"``, ``"1,3,5"``, ``"2-7"``,
    ``"1, 3-5, 9"``. Values outside ``1..total`` are silently dropped.
    """
    s = (spec or "").strip().lower()
    if not s or s in ("all", "*"):
        return list(range(1, total + 1))

    pages: set[int] = set()
    for token in s.split(","):
        token = token.strip()
        if not token:
            continue
        if "-" in token:
            a, b = token.split("-", 1)
            try:
                ai, bi = int(a.strip()), int(b.strip())
            except ValueError:
                continue
            for p in range(min(ai, bi), max(ai, bi) + 1):
                if 1 <= p <= total:
                    pages.add(p)
        else:
            try:
                p = int(token)
            except ValueError:
                continue
            if 1 <= p <= total:
                pages.add(p)
    return sorted(pages)


def make_subset_pdf(src_path: str | Path, pages: List[int], dst_path: str | Path) -> List[int]:
    """Write a new PDF containing only ``pages`` (1-indexed) from ``src_path``.

    Returns the same ``pages`` list so the caller can map
    ``subset_page_no -> original_page_no`` via ``pages[subset_idx-1]``.
    """
    src = pymupdf.open(str(src_path))
    dst = pymupdf.open()
    try:
        for p in pages:
            dst.insert_pdf(src, from_page=p - 1, to_page=p - 1)
        dst.save(str(dst_path))
    finally:
        dst.close()
        src.close()
    return list(pages)


def slugify(text: str, maxlen: int = 40) -> str:
    text = re.sub(r"[^\w\-]+", "_", (text or "").strip()).strip("_")
    return (text[:maxlen] or "item").lower()


# ---------------------------------------------------------------------------
# Coordinate / bbox helpers
# ---------------------------------------------------------------------------

def bbox_to_pymu_rect(bbox: dict, page_height: float) -> pymupdf.Rect:
    """Docling bbox -> PyMuPDF Rect (top-left origin, points)."""
    l, t, r, b = float(bbox["l"]), float(bbox["t"]), float(bbox["r"]), float(bbox["b"])
    origin = (bbox.get("coord_origin") or "BOTTOMLEFT").upper()
    if origin == "BOTTOMLEFT":
        # In BOTTOMLEFT: t = top edge (larger y), b = bottom edge (smaller y).
        return pymupdf.Rect(l, page_height - t, r, page_height - b)
    return pymupdf.Rect(l, t, r, b)


def bbox_to_camelot_area(bbox: dict, page_height: float) -> str:
    """Camelot ``table_areas`` string ``"x1,y1,x2,y2"`` in PDF default
    coordinates (bottom-left origin) where (x1,y1)=top-left, (x2,y2)=bot-right.
    """
    l, t, r, b = float(bbox["l"]), float(bbox["t"]), float(bbox["r"]), float(bbox["b"])
    origin = (bbox.get("coord_origin") or "BOTTOMLEFT").upper()
    if origin == "BOTTOMLEFT":
        return f"{l},{t},{r},{b}"
    # TOPLEFT -> convert
    return f"{l},{page_height - t},{r},{page_height - b}"


# ---------------------------------------------------------------------------
# Table conversion helpers
# ---------------------------------------------------------------------------

def docling_table_to_markdown(table_data: dict) -> str:
    """Convert a Docling ``TableData`` dict to a GitHub-style Markdown table."""
    rows: List[List[str]] = []

    grid = table_data.get("grid")
    if grid:
        for row in grid:
            out_row = []
            for cell in row:
                txt = (cell.get("text") or "")
                out_row.append(txt.replace("|", "\\|").replace("\n", " ").strip())
            rows.append(out_row)
    else:
        cells = table_data.get("table_cells") or []
        nrows = int(table_data.get("num_rows") or 0)
        ncols = int(table_data.get("num_cols") or 0)
        if nrows and ncols:
            rows = [["" for _ in range(ncols)] for _ in range(nrows)]
            for c in cells:
                i = int(c.get("start_row_offset_idx", 0))
                j = int(c.get("start_col_offset_idx", 0))
                if 0 <= i < nrows and 0 <= j < ncols:
                    txt = (c.get("text") or "")
                    rows[i][j] = txt.replace("|", "\\|").replace("\n", " ").strip()

    if not rows:
        return ""
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    header, body = rows[0], rows[1:]
    out = ["| " + " | ".join(header) + " |",
           "| " + " | ".join(["---"] * width) + " |"]
    for r in body:
        out.append("| " + " | ".join(r) + " |")
    return "\n".join(out)


def dataframe_to_markdown(df) -> str:
    """Pandas DataFrame -> Markdown table (no external dep on tabulate)."""
    cols = [str(c).replace("|", "\\|") for c in df.columns]
    out = ["| " + " | ".join(cols) + " |",
           "| " + " | ".join(["---"] * len(cols)) + " |"]
    for _, row in df.iterrows():
        cells = ["" if v is None else str(v).replace("|", "\\|").replace("\n", " ").strip()
                 for v in row.tolist()]
        out.append("| " + " | ".join(cells) + " |")
    return "\n".join(out)


def extract_table_with_camelot(
    pdf_path: str, page_in_subset: int, area: str
) -> Optional[str]:
    """Try to extract a single table with Camelot Stream. Returns Markdown or None."""
    if not HAS_CAMELOT:
        return None
    try:
        tables = camelot.read_pdf(
            pdf_path,
            pages=str(page_in_subset),
            flavor="stream",
            table_areas=[area],
            suppress_stdout=True,
        )
        if tables.n == 0:
            return None
        df = tables[0].df
        df.columns = [f"col_{i}" if not str(c).strip() else str(c) for i, c in enumerate(df.iloc[0])]
        df = df.iloc[1:].reset_index(drop=True)
        return dataframe_to_markdown(df)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Layout analysis
# ---------------------------------------------------------------------------

@dataclass
class LayoutResult:
    subset_pdf: Path
    page_map: List[int]            # subset_page_no (1-indexed) -> original page
    layout_md: Path
    layout_json: Path
    docling_dict: Dict[str, Any]   # the lossless DoclingDocument dict


def run_layout(
    src_pdf: str | Path,
    pages: List[int],
    work_root: Path,
    use_granite: bool = False,
) -> LayoutResult:
    """Stage 2 of the pipeline: subset + layout analysis."""
    work_root.mkdir(parents=True, exist_ok=True)
    layout_dir = work_root / "_layout"
    layout_dir.mkdir(parents=True, exist_ok=True)

    subset_pdf = work_root / "_subset.pdf"
    page_map = make_subset_pdf(src_pdf, pages, subset_pdf)

    if use_granite:
        # Lazy import so a plain Docling install also works.
        from docling.datamodel.base_models import InputFormat
        from docling.datamodel.pipeline_options import VlmPipelineOptions
        from docling.datamodel import vlm_model_specs
        from docling.document_converter import PdfFormatOption
        from docling.pipeline.vlm_pipeline import VlmPipeline

        opts = VlmPipelineOptions(vlm_options=vlm_model_specs.GRANITEDOCLING_TRANSFORMERS)
        converter = DocumentConverter(
            format_options={
                InputFormat.PDF: PdfFormatOption(
                    pipeline_cls=VlmPipeline, pipeline_options=opts
                )
            }
        )
    else:
        converter = DocumentConverter()

    result = converter.convert(str(subset_pdf))
    doc = result.document

    layout_md = layout_dir / "layout.md"
    layout_json = layout_dir / "layout.json"
    doc.save_as_markdown(layout_md)
    doc_dict = doc.export_to_dict()
    layout_json.write_text(
        json.dumps(doc_dict, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    return LayoutResult(
        subset_pdf=subset_pdf,
        page_map=page_map,
        layout_md=layout_md,
        layout_json=layout_json,
        docling_dict=doc_dict,
    )


# ---------------------------------------------------------------------------
# Walk the DoclingDocument & extract data from the PDF
# ---------------------------------------------------------------------------

@dataclass
class ContentItem:
    idx: int
    kind: str                    # "heading" | "text" | "list" | "table" | "picture" | "code" | "formula" | "caption"
    label: str                   # docling label (e.g. "section_header", "paragraph")
    page: int                    # ORIGINAL page number in the source PDF
    preview: str                 # short preview (for the TOC)
    sidecar: Optional[str] = None  # relative path to a sidecar file if any


def _resolve_ref(doc: dict, ref: str) -> Optional[dict]:
    """Resolve a '#/texts/0' style reference inside the DoclingDocument dict."""
    if not ref or not ref.startswith("#/"):
        return None
    parts = ref[2:].split("/")
    cur: Any = doc
    for p in parts:
        if isinstance(cur, list):
            try:
                cur = cur[int(p)]
            except (ValueError, IndexError):
                return None
        elif isinstance(cur, dict):
            cur = cur.get(p)
            if cur is None:
                return None
        else:
            return None
    return cur if isinstance(cur, dict) else None


def _iter_reading_order(doc: dict):
    """Yield `(ref_str, item_dict)` in reading order using `body.children`."""
    body = doc.get("body") or {}
    children = body.get("children") or []
    for child in children:
        ref = child.get("$ref") or child.get("cref")
        if not ref:
            continue
        item = _resolve_ref(doc, ref)
        if item is not None:
            yield ref, item


def _item_kind(ref: str, item: dict) -> Tuple[str, str]:
    """Return ``(kind, label)`` for a DoclingDocument item."""
    label = (item.get("label") or "").lower()
    section = ref.split("/")[1] if ref.startswith("#/") else ""
    if section == "tables":
        return "table", label or "table"
    if section == "pictures":
        return "picture", label or "picture"
    if label in {"section_header", "title", "page_header", "subtitle_level_1"}:
        return "heading", label
    if label in {"list_item"}:
        return "list", label
    if label in {"caption"}:
        return "caption", label
    if label in {"code"}:
        return "code", label
    if label in {"formula"}:
        return "formula", label
    return "text", label or "paragraph"


def _heading_level(label: str) -> int:
    if label == "title":
        return 1
    if label == "section_header":
        return 2
    if label == "subtitle_level_1":
        return 3
    return 3


def _prov_page_and_bbox(item: dict) -> Tuple[Optional[int], Optional[dict]]:
    prov = item.get("prov") or []
    if not prov:
        return None, None
    p0 = prov[0]
    return p0.get("page_no"), p0.get("bbox")


def _picture_caption(doc: dict, item: dict) -> str:
    caps = []
    for c in item.get("captions") or []:
        ref = c.get("$ref") or c.get("cref")
        cap = _resolve_ref(doc, ref) if ref else None
        if cap and cap.get("text"):
            caps.append(cap["text"])
    return " ".join(caps).strip()


# ---------------------------------------------------------------------------
# Main extraction
# ---------------------------------------------------------------------------

def extract_to_folder(
    src_pdf: str | Path,
    layout: LayoutResult,
    out_root: Path,
    image_dpi: int = 200,
) -> Path:
    """Stage 3+4: walk Docling JSON, pull data from the *original* PDF, write
    markdown files into ``out_root``. Returns ``out_root``.
    """
    out_root.mkdir(parents=True, exist_ok=True)
    tables_dir = out_root / "tables"
    images_dir = out_root / "images"
    codes_dir = out_root / "code"
    formulas_dir = out_root / "formulas"
    layout_copy_dir = out_root / "layout"
    for d in (tables_dir, images_dir, codes_dir, formulas_dir, layout_copy_dir):
        d.mkdir(parents=True, exist_ok=True)

    # Also publish the layout artifacts inside the bundle.
    shutil.copy2(layout.layout_md, layout_copy_dir / "layout.md")
    shutil.copy2(layout.layout_json, layout_copy_dir / "layout.json")

    doc = layout.docling_dict
    page_map = layout.page_map        # subset page -> original page

    src_doc = pymupdf.open(str(src_pdf))
    sub_doc = pymupdf.open(str(layout.subset_pdf))

    contents: List[ContentItem] = []
    main_lines: List[str] = []
    current_orig_page: Optional[int] = None
    table_no = 0
    image_no = 0
    code_no = 0
    formula_no = 0
    item_idx = 0

    try:
        for ref, item in _iter_reading_order(doc):
            item_idx += 1
            kind, label = _item_kind(ref, item)
            page_in_subset, bbox = _prov_page_and_bbox(item)
            if page_in_subset is None or not (1 <= page_in_subset <= len(page_map)):
                # No provenance -> skip silently (rare; mostly group nodes).
                continue
            orig_page = page_map[page_in_subset - 1]

            if orig_page != current_orig_page:
                if current_orig_page is not None:
                    main_lines.append("")
                main_lines.append(f"---\n\n## Page {orig_page}\n")
                current_orig_page = orig_page

            sub_page = sub_doc[page_in_subset - 1]
            src_page = src_doc[orig_page - 1]
            page_h_sub = sub_page.rect.height

            # ---------- TEXT-LIKE ELEMENTS ----------
            if kind in ("text", "heading", "list", "caption"):
                # Re-extract the actual glyph text from the ORIGINAL PDF using
                # the bbox; falls back to whatever Docling already parsed.
                pdf_text = ""
                if bbox:
                    rect = bbox_to_pymu_rect(bbox, page_h_sub)
                    try:
                        pdf_text = src_page.get_text("text", clip=rect).strip()
                    except Exception:
                        pdf_text = ""
                if not pdf_text:
                    pdf_text = (item.get("text") or "").strip()
                if not pdf_text:
                    continue

                if kind == "heading":
                    level = _heading_level(label)
                    main_lines.append(f"\n{'#' * level} {pdf_text}\n")
                    preview = pdf_text
                elif kind == "list":
                    main_lines.append(f"- {pdf_text}")
                    preview = pdf_text
                elif kind == "caption":
                    main_lines.append(f"\n*{pdf_text}*\n")
                    preview = pdf_text
                else:
                    main_lines.append(pdf_text + "\n")
                    preview = pdf_text

                contents.append(ContentItem(
                    idx=item_idx, kind=kind, label=label,
                    page=orig_page, preview=preview[:120],
                ))

            # ---------- TABLE ----------
            elif kind == "table":
                table_no += 1
                name = f"table_{table_no:03d}"
                rel_path = f"tables/{name}.md"

                # Try Camelot first against the SUBSET pdf (page numbering matches).
                md_table = None
                if bbox:
                    area = bbox_to_camelot_area(bbox, page_h_sub)
                    md_table = extract_table_with_camelot(
                        str(layout.subset_pdf), page_in_subset, area
                    )

                # Fallback to Docling's own parsed table.
                if not md_table:
                    md_table = docling_table_to_markdown(item.get("data") or {})

                caption = _picture_caption(doc, item)
                bbox_str = json.dumps(bbox) if bbox else "n/a"
                body = (
                    f"# Table {table_no:03d}\n\n"
                    f"- **Original page:** {orig_page}\n"
                    f"- **BBox (subset PDF):** {bbox_str}\n"
                    + (f"- **Caption:** {caption}\n" if caption else "")
                    + f"\n{md_table or '_(empty table)_'}\n"
                )
                (tables_dir / f"{name}.md").write_text(body, encoding="utf-8")

                placeholder = f"\n> **[TABLE {table_no:03d}]** -> [`{rel_path}`]({rel_path}){' - ' + caption if caption else ''}\n"
                main_lines.append(placeholder)
                contents.append(ContentItem(
                    idx=item_idx, kind="table", label=label,
                    page=orig_page,
                    preview=(caption or f"Table {table_no:03d}")[:120],
                    sidecar=rel_path,
                ))

            # ---------- PICTURE / IMAGE ----------
            elif kind == "picture":
                image_no += 1
                name = f"image_{image_no:03d}"
                png_rel = f"images/{name}.png"
                md_rel = f"images/{name}.md"

                if bbox:
                    # Render the cropped region from the ORIGINAL PDF for max
                    # fidelity (subset pdf shares geometry with src pages).
                    rect = bbox_to_pymu_rect(bbox, page_h_sub)
                    try:
                        pix = src_page.get_pixmap(
                            clip=rect, dpi=image_dpi, alpha=False
                        )
                        pix.save(str(images_dir / f"{name}.png"))
                    except Exception:
                        # Fallback to full-page render
                        pix = src_page.get_pixmap(dpi=image_dpi, alpha=False)
                        pix.save(str(images_dir / f"{name}.png"))

                caption = _picture_caption(doc, item)
                bbox_str = json.dumps(bbox) if bbox else "n/a"
                md_body = (
                    f"# Image {image_no:03d}\n\n"
                    f"- **Original page:** {orig_page}\n"
                    f"- **BBox (subset PDF):** {bbox_str}\n"
                    + (f"- **Caption:** {caption}\n" if caption else "")
                    + f"\n![{name}]({name}.png)\n"
                )
                (images_dir / f"{name}.md").write_text(md_body, encoding="utf-8")

                placeholder = (
                    f"\n> **[IMAGE {image_no:03d}]** -> [`{png_rel}`]({png_rel}) "
                    f"| sidecar: [`{md_rel}`]({md_rel})"
                    + (f" - {caption}" if caption else "")
                    + "\n"
                )
                main_lines.append(placeholder)
                contents.append(ContentItem(
                    idx=item_idx, kind="picture", label=label,
                    page=orig_page,
                    preview=(caption or f"Image {image_no:03d}")[:120],
                    sidecar=png_rel,
                ))

            # ---------- CODE BLOCK ----------
            elif kind == "code":
                code_no += 1
                name = f"code_{code_no:03d}"
                rel_path = f"code/{name}.md"
                code_text = (item.get("text") or "").rstrip()
                if bbox and not code_text:
                    rect = bbox_to_pymu_rect(bbox, page_h_sub)
                    try:
                        code_text = src_page.get_text("text", clip=rect).rstrip()
                    except Exception:
                        pass
                lang = (item.get("code_language") or "").strip()
                fence = f"```{lang}\n{code_text}\n```"
                (codes_dir / f"{name}.md").write_text(
                    f"# Code {code_no:03d}\n\n"
                    f"- **Original page:** {orig_page}\n\n{fence}\n",
                    encoding="utf-8",
                )
                main_lines.append(f"\n{fence}\n")
                contents.append(ContentItem(
                    idx=item_idx, kind="code", label=label,
                    page=orig_page,
                    preview=(code_text.splitlines()[0] if code_text else "")[:120],
                    sidecar=rel_path,
                ))

            # ---------- FORMULA ----------
            elif kind == "formula":
                formula_no += 1
                name = f"formula_{formula_no:03d}"
                rel_path = f"formulas/{name}.md"
                latex = (item.get("text") or "").strip()
                block = f"$$\n{latex}\n$$" if latex else "_(empty formula)_"
                (formulas_dir / f"{name}.md").write_text(
                    f"# Formula {formula_no:03d}\n\n"
                    f"- **Original page:** {orig_page}\n\n{block}\n",
                    encoding="utf-8",
                )
                main_lines.append(f"\n{block}\n")
                contents.append(ContentItem(
                    idx=item_idx, kind="formula", label=label,
                    page=orig_page, preview=latex[:120], sidecar=rel_path,
                ))
    finally:
        src_doc.close()
        sub_doc.close()

    # Main file
    main_path = out_root / "01_main.md"
    src_name = Path(str(src_pdf)).name
    header = (
        f"# {src_name}\n\n"
        f"_Extracted with Docling + PyMuPDF + Camelot. "
        f"Tables/images/etc. are referenced as sidecar files._\n"
    )
    main_path.write_text(header + "\n".join(main_lines).rstrip() + "\n",
                         encoding="utf-8")

    # Contents file
    toc_lines = [
        "# Document Contents",
        "",
        f"Source: `{src_name}`  ",
        f"Total elements: **{len(contents)}**  ",
        f"Tables: **{table_no}** | Images: **{image_no}** | "
        f"Code blocks: **{code_no}** | Formulas: **{formula_no}**",
        "",
        "| # | Type | Page | Preview / File |",
        "|---|------|------|----------------|",
    ]
    for c in contents:
        if c.sidecar:
            ref = f"[`{c.sidecar}`]({c.sidecar})"
            if c.preview:
                ref = f"{c.preview} - {ref}"
        else:
            ref = c.preview.replace("|", "\\|") if c.preview else ""
        toc_lines.append(f"| {c.idx} | {c.kind} | {c.page} | {ref} |")
    (out_root / "00_contents.md").write_text(
        "\n".join(toc_lines) + "\n", encoding="utf-8"
    )

    return out_root


# ---------------------------------------------------------------------------
# Packaging
# ---------------------------------------------------------------------------

def zip_folder(folder: Path, zip_path: Path) -> Path:
    """Zip ``folder`` (recursively) into ``zip_path``."""
    folder = Path(folder)
    zip_path = Path(zip_path)
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in folder.rglob("*"):
            if path.is_file():
                zf.write(path, arcname=path.relative_to(folder.parent))
    return zip_path


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def run(
    pdf_path: str | Path,
    page_spec: str = "all",
    out_dir: str | Path = "output",
    use_granite: bool = False,
    image_dpi: int = 200,
) -> Path:
    """End-to-end run. Returns the path of the created ZIP file."""
    pdf_path = Path(pdf_path).expanduser().resolve()
    if not pdf_path.is_file():
        raise FileNotFoundError(pdf_path)

    out_dir = Path(out_dir).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    with pymupdf.open(str(pdf_path)) as src:
        total_pages = src.page_count
    pages = parse_page_spec(page_spec, total_pages)
    if not pages:
        raise ValueError(f"No valid pages in spec={page_spec!r} (PDF has {total_pages} pages)")

    bundle_name = f"{pdf_path.stem}_pages_{pages[0]}-{pages[-1]}"
    work_root = out_dir / bundle_name
    if work_root.exists():
        shutil.rmtree(work_root)
    work_root.mkdir(parents=True, exist_ok=True)

    print(f"[1/3] Layout analysis on {len(pages)} page(s) (Docling{' + Granite VLM' if use_granite else ''})...")
    layout = run_layout(pdf_path, pages, work_root, use_granite=use_granite)
    print(f"      -> {layout.layout_md.relative_to(out_dir)}")
    print(f"      -> {layout.layout_json.relative_to(out_dir)}")

    print("[2/3] Extracting text / tables / images from PDF using JSON map...")
    extracted_dir = work_root / "extracted"
    extract_to_folder(pdf_path, layout, extracted_dir, image_dpi=image_dpi)
    n_files = sum(1 for _ in extracted_dir.rglob("*") if _.is_file())
    print(f"      -> {extracted_dir.relative_to(out_dir)} ({n_files} files)")

    print("[3/3] Zipping bundle...")
    zip_path = out_dir / f"{bundle_name}.zip"
    zip_folder(extracted_dir, zip_path)
    print(f"      -> {zip_path}")
    return zip_path


def _cli_entrypoint() -> int:
    import argparse

    ap = argparse.ArgumentParser(description="PDF extractor (Docling + PyMuPDF + Camelot Stream).")
    ap.add_argument("pdf", help="Path to a (true) PDF file.")
    ap.add_argument("-p", "--pages", default="all",
                    help="Pages to extract, e.g. 'all', '1,3,5', '2-7' (default: all).")
    ap.add_argument("-o", "--out", default="output", help="Output directory (default: ./output).")
    ap.add_argument("--granite", action="store_true",
                    help="Use Granite-Docling VLM pipeline (needs GPU; slower).")
    ap.add_argument("--dpi", type=int, default=200, help="DPI for rendered image crops.")
    args = ap.parse_args()

    try:
        zip_path = run(
            pdf_path=args.pdf,
            page_spec=args.pages,
            out_dir=args.out,
            use_granite=args.granite,
            image_dpi=args.dpi,
        )
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    print(f"\nDone. Bundle: {zip_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli_entrypoint())
