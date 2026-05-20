# Lumindra

**Structured PDF-to-Markdown Extraction Pipeline**

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.9%2B-blue)](https://www.python.org/)
[![Docling](https://img.shields.io/badge/Layout-Docling-green)](https://github.com/docling-project/docling)
[![PyMuPDF](https://img.shields.io/badge/Text%2FImages-PyMuPDF-orange)](https://github.com/pymupdf/PyMuPDF)
[![Camelot](https://img.shields.io/badge/Tables-Camelot--Stream-red)](https://github.com/atlanhq/camelot)

---

## Abstract

Lumindra is a document intelligence pipeline that converts true (text-based) PDF files into richly structured bundles of Markdown files. The pipeline combines three complementary libraries — **Docling** for layout analysis and reading-order recovery, **PyMuPDF** for high-fidelity text and image extraction, and **Camelot** for table re-extraction guided by detected bounding boxes — to produce a lossless, navigable representation of the source document. The system is designed as both a standalone CLI tool and a Google Colab-ready interactive notebook, targeting researchers, engineers, and practitioners who require machine-readable structured output from PDF corpora.

---

## Table of Contents

1. [Background and Motivation](#1-background-and-motivation)
2. [Architecture Overview](#2-architecture-overview)
3. [Pipeline Stages](#3-pipeline-stages)
4. [Output Bundle Layout](#4-output-bundle-layout)
5. [Repository Structure](#5-repository-structure)
6. [Dependencies](#6-dependencies)
7. [Installation](#7-installation)
8. [Usage](#8-usage)
   - [Command-Line Interface](#81-command-line-interface)
   - [Python API](#82-python-api)
   - [Google Colab Notebook](#83-google-colab-notebook)
9. [Configuration Reference](#9-configuration-reference)
10. [Implementation Notes](#10-implementation-notes)
11. [Limitations](#11-limitations)
12. [License](#12-license)

---

## 1. Background and Motivation

Portable Document Format (PDF) is the dominant container for academic papers, technical reports, legal documents, and enterprise data sheets. Yet PDF encodes visual presentation, not semantic structure: paragraphs, tables, figures, mathematical formulae, and code listings are flattened into a stream of positioned glyphs with no intrinsic hierarchy.

Downstream tasks such as retrieval-augmented generation, knowledge base construction, and data extraction require the inverse transformation — recovering structure from layout. Existing single-library approaches suffer characteristic weaknesses: pure text extractors lose table structure; OCR-centric tools discard vector text precision; general layout models do not always expose their bounding-box provenance for secondary extraction.

Lumindra addresses this by orchestrating three specialised tools in a staged pipeline, using each library only for the task at which it excels, and propagating geometric provenance (bounding boxes) across stages so that every extracted artefact can be traced back to its exact location in the source PDF.

---

## 2. Architecture Overview

```
┌─────────────────────────────────────────────────────────────────────┐
│                         LUMINDRA PIPELINE                           │
│                                                                     │
│  ┌─────────┐     ┌──────────────┐     ┌──────────────────────────┐ │
│  │  Input  │────▶│ Page Subset  │────▶│   Layout Analysis        │ │
│  │   PDF   │     │  (PyMuPDF)   │     │   (Docling / Granite VLM)│ │
│  └─────────┘     └──────────────┘     └────────────┬─────────────┘ │
│                                                    │                │
│                              DoclingDocument JSON  │                │
│                              (reading order + bbox)│                │
│                                                    ▼                │
│                         ┌──────────────────────────────────────┐   │
│                         │      Walk Reading Order               │   │
│                         │                                       │   │
│           ┌─────────────┼──────────────────────────┐           │   │
│           │             │                          │           │   │
│           ▼             ▼                          ▼           │   │
│     ┌──────────┐  ┌──────────┐  ┌────────────────────────┐    │   │
│     │  Text /  │  │  Tables  │  │  Pictures / Code /      │    │   │
│     │ Headings │  │ Camelot  │  │  Formulae              │    │   │
│     │ PyMuPDF  │  │ Stream + │  │  PyMuPDF pixmap /       │    │   │
│     │ clip=bbox│  │ Docling  │  │  raw text / LaTeX       │    │   │
│     │          │  │ fallback │  │                         │    │   │
│     └────┬─────┘  └────┬─────┘  └──────────┬─────────────┘    │   │
│          │             │                   │                   │   │
│          └─────────────┴───────────────────┘                   │   │
│                                  │                             │   │
│                                  ▼                             │   │
│                   ┌──────────────────────────┐                 │   │
│                   │ 00_contents.md (TOC)      │                 │   │
│                   │ 01_main.md (full text)    │                 │   │
│                   │ tables/, images/,         │                 │   │
│                   │ code/, formulas/,         │                 │   │
│                   │ layout/                   │                 │   │
│                   └──────────────────────────┘                 │   │
│                                  │                             │   │
│                                  ▼                             │   │
│                         ┌──────────────┐                       │   │
│                         │  ZIP Bundle  │                       │   │
│                         └──────────────┘                       │   │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 3. Pipeline Stages

### Stage 0 — Page Selection

The user specifies an arbitrary page range using a flexible specification string (`all`, comma-separated, hyphenated range, or a mix). PyMuPDF subsets the source PDF to only the requested pages. All subsequent stages operate on this subset, preserving original page number metadata for provenance tracking.

### Stage 1 — Layout Analysis

The page subset is passed to **Docling**, which performs:

- Visual layout segmentation (text blocks, tables, figures, formulae, code)
- Reading-order recovery
- Export of a lossless `DoclingDocument` JSON, including per-element bounding boxes with coordinate origin metadata (`BOTTOMLEFT` or `TOPLEFT`)
- Export of a Markdown rendering (`layout.md`)

An optional Granite-Docling VLM variant (`ibm-granite/granite-docling-258M`) is available for enhanced layout understanding on GPU-enabled environments.

### Stage 2 — Element-Level Extraction

The pipeline walks the `DoclingDocument` JSON in reading order. For each element, the bounding box is converted to the native coordinate system of PyMuPDF and/or Camelot, and the appropriate extractor is invoked:

| Element Type | Primary Extractor | Fallback |
|---|---|---|
| Text, Heading, List, Caption | PyMuPDF `get_text(clip=bbox)` | Docling `.text` field |
| Table | Camelot Stream (`table_areas=bbox`) | Docling parsed table cells |
| Picture / Figure | PyMuPDF `get_pixmap(clip=bbox, dpi=N)` | — |
| Code block | PyMuPDF `get_text(clip=bbox)` | Docling `.text` field |
| Formula | Docling LaTeX export | — |

Coordinate conversion handles both `BOTTOMLEFT` (Docling 2.x default) and `TOPLEFT` origins. Image crops are rendered against the **original** (pre-subset) PDF to preserve full raster fidelity independent of Docling's internal DPI.

### Stage 3 — Assembly and Packaging

A Table of Contents (`00_contents.md`) and a main narrative file (`01_main.md`) are assembled from all extracted elements, with cross-references (Markdown links) to sidecar files. The entire output directory is zipped for distribution or download.

---

## 4. Output Bundle Layout

```
<pdf_stem>_pages_<first>-<last>/
│
├── 00_contents.md          # Structured table of contents; one row per element
├── 01_main.md              # Full plain-text narrative with sidecar placeholders
│
├── layout/
│   ├── layout.md           # Docling Markdown export (direct rendering)
│   └── layout.json         # Lossless DoclingDocument JSON (full provenance)
│
├── tables/
│   ├── table_001.md        # One GitHub-flavoured Markdown table per detection
│   ├── table_002.md
│   └── ...
│
├── images/
│   ├── image_001.png       # Cropped raster from the original PDF
│   ├── image_001.md        # Sidecar: page number, bbox, caption
│   └── ...
│
├── code/
│   ├── code_001.md         # Fenced code block with language hint when available
│   └── ...
│
└── formulas/
    ├── formula_001.md      # LaTeX display-math block
    └── ...
```

---

## 5. Repository Structure

```
lumindra/
├── .github/
│   └── workflows/              # CI/CD workflows
├── pdf_extractor.py            # Core Python module and CLI entry point (742 lines)
├── pdf_extractor_colab.ipynb   # Self-contained Google Colab notebook
├── .gitignore
├── LICENSE                     # MIT License
└── README.md
```

**Language distribution:** Jupyter Notebook 60.1% · Python 39.9%

---

## 6. Dependencies

| Package | Role | Version guidance |
|---|---|---|
| `pymupdf` | PDF parsing, text extraction, image rendering | ≥ 1.23 |
| `docling` | Layout analysis, reading order, VLM option | ≥ 2.0 |
| `camelot-py[base]` | Stream-mode table extraction | latest |
| `opencv-python-headless` | Required by Camelot | any |
| `ghostscript` *(system)* | Required by Camelot on Linux/macOS | any |

Optional (Granite VLM):

| Package | Role |
|---|---|
| `transformers` | Required by Granite-Docling VLM pipeline |
| CUDA-capable GPU | Recommended; float32 fallback available on T4 |

---

## 7. Installation

### Local (Linux / macOS)

```bash
# System dependency for Camelot
sudo apt-get install -y ghostscript       # Debian/Ubuntu
# brew install ghostscript               # macOS

pip install pymupdf docling "camelot-py[base]" opencv-python-headless
```

### Google Colab

Upload `pdf_extractor_colab.ipynb` to [colab.research.google.com](https://colab.research.google.com). All dependencies are installed by the notebook's first cell. For Granite-Docling, select **Runtime → Change runtime type → GPU (T4 or better)** before running.

---

## 8. Usage

### 8.1 Command-Line Interface

```bash
# Extract all pages
python pdf_extractor.py path/to/document.pdf

# Extract a page range
python pdf_extractor.py path/to/document.pdf -p "1-5"

# Extract non-contiguous pages to a custom output directory
python pdf_extractor.py path/to/document.pdf -p "1,3,7-10" -o ./results

# Use Granite-Docling VLM (requires GPU)
python pdf_extractor.py path/to/document.pdf -p all --granite

# Custom image DPI for figure crops
python pdf_extractor.py path/to/document.pdf --dpi 300
```

**Options:**

| Flag | Default | Description |
|---|---|---|
| `pdf` *(positional)* | — | Path to the input PDF file |
| `-p`, `--pages` | `all` | Page specification: `all`, `1,3,5`, `2-7`, or mixed |
| `-o`, `--out` | `./output` | Output directory |
| `--granite` | `False` | Enable Granite-Docling VLM pipeline |
| `--dpi` | `200` | Raster DPI for image/figure crops |

### 8.2 Python API

```python
from pdf_extractor import run

zip_path = run(
    pdf_path="path/to/document.pdf",
    page_spec="1,3,5-7",
    out_dir="./output",
    use_granite=False,
    image_dpi=200,
)
print(f"Bundle written to: {zip_path}")
```

The `run()` function returns the `pathlib.Path` of the output ZIP archive. Intermediate artefacts are available in `out_dir/<pdf_stem>_pages_<first>-<last>/`.

For granular control, the two sub-stages can be called independently:

```python
from pdf_extractor import run_layout, extract_to_folder
from pathlib import Path

# Stage 1: layout only
layout = run_layout(
    src_pdf="document.pdf",
    pages=[1, 2, 3],
    work_root=Path("./work"),
    use_granite=False,
)

# Stage 2: element-level extraction
extract_to_folder(
    src_pdf="document.pdf",
    layout=layout,
    out_root=Path("./output"),
    image_dpi=300,
)
```

### 8.3 Google Colab Notebook

1. Navigate to [colab.research.google.com](https://colab.research.google.com) and upload `pdf_extractor_colab.ipynb`.
2. Execute cells sequentially from top to bottom.
3. When prompted, provide:
   - The PDF source (upload widget, local path, or URL)
   - The page specification string
   - Whether to enable the Granite-Docling VLM
4. The final cell produces and automatically downloads a ZIP archive of the output bundle.

> **Note:** On a Colab T4 GPU, the Granite-Docling VLM may require `dtype=float32`. Refer to the [model card](https://huggingface.co/ibm-granite/granite-docling-258M) for details.

---

## 9. Configuration Reference

### Page Specification Format

| Input | Meaning |
|---|---|
| `all` or `*` | All pages |
| `5` | Page 5 only |
| `1,3,7` | Pages 1, 3, and 7 |
| `2-8` | Pages 2 through 8 (inclusive) |
| `1,3-5,9` | Pages 1, 3, 4, 5, and 9 |

Values outside the valid page range are silently ignored. The resulting list is always sorted and deduplicated.

### Coordinate Systems

Docling 2.x reports bounding boxes in `BOTTOMLEFT` origin by default. The pipeline detects the `coord_origin` field and converts accordingly for both PyMuPDF (`TOPLEFT`) and Camelot (PDF native, `BOTTOMLEFT`). Documents emitting `TOPLEFT` coordinates are handled transparently.

---

## 10. Implementation Notes

- **Table extraction precedence.** Camelot Stream is attempted first on every detected table region. If Camelot returns no result within the supplied area constraint, the pipeline falls back to Docling's own parsed table cells, which are always available from the `DoclingDocument` JSON.

- **Image fidelity.** Figure crops are rendered from the **original** PDF (not the page subset) using PyMuPDF's `get_pixmap(clip=rect, dpi=N)`. This ensures raster output quality is decoupled from any internal rasterisation Docling performs during layout analysis.

- **Reading order.** The pipeline walks `body.children` in the `DoclingDocument` JSON, resolving JSON pointer references (`#/texts/0`, `#/tables/1`, etc.) to reconstruct the document's logical reading sequence rather than its visual top-to-bottom order.

- **Provenance tracking.** Every sidecar file records the original (pre-subset) page number and the bounding box from which the element was extracted, enabling round-trip verification.

- **Dual-mode entry point.** `pdf_extractor.py` is simultaneously a standalone CLI (invoked via `__main__`) and an importable module exposing `run()`, `run_layout()`, and `extract_to_folder()` for programmatic use.

---

## 11. Limitations

- **Scanned PDFs.** The default pipeline is designed for true (text-based) PDFs. Scanned or image-only PDFs can be processed by enabling Docling's OCR pipeline, or by pre-processing with PyMuPDF's `get_textpage_ocr()`. Neither path is enabled by default.

- **Camelot platform requirement.** Camelot requires Ghostscript, which is not available on Windows without additional configuration. On unsupported platforms, table extraction automatically falls back to Docling's parsed output.

- **Complex multi-column layouts.** Reading-order recovery in highly non-linear layouts (e.g., multi-column academic papers with side-bar annotations) depends entirely on Docling's layout model quality. The Granite-Docling VLM variant generally performs better on such documents.

- **Formula rendering.** Formulae are preserved as raw LaTeX strings from Docling's export. Rendering requires a Markdown processor with MathJax or KaTeX support; raw `.md` viewers may display unformatted LaTeX.

- **Concurrent writes.** The pipeline does not support concurrent execution against the same `out_dir` root. Parallel runs should use distinct output directories.

---

## 12. License

This project is released under the [MIT License](LICENSE).

---

*Lumindra — from Latin* lumen *(light) and* indra *(Sanskrit: perceiver). Illuminating the structure within documents.*
