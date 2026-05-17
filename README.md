# PDF Extractor (Colab-ready)

Parses a **true (text-based) PDF** and produces a ZIP bundle of structured Markdown files using:

- [**Docling**](https://github.com/docling-project/docling) (optionally the [**Granite-Docling 258M**](https://huggingface.co/ibm-granite/granite-docling-258M) VLM) for layout analysis and the lossless `DoclingDocument` JSON.
- [**PyMuPDF**](https://github.com/pymupdf/PyMuPDF) for plain text (`get_text(clip=bbox)`) and image cropping (`get_pixmap(clip=bbox)`).
- [**Camelot**](https://github.com/atlanhq/camelot) (**Stream** flavor) for table extraction guided by the Docling bboxes.

## Files

| File | Purpose |
|---|---|
| `pdf_extractor_colab.ipynb` | Self-contained Google Colab notebook (interactive). |
| `pdf_extractor.py` | Same logic, importable Python module **and** standalone CLI. |
| `README.md` | This file. |

## Pipeline

```
PDF -> [PyMuPDF: subset to chosen pages] -> subset.pdf
     -> [Docling: layout + reading order] -> layout.md + layout.json (lossless)
     -> [walk JSON in reading order] for each element:
            text     -> PyMuPDF text (clip = bbox)  ----> main file
            table    -> Camelot Stream (areas = bbox) | Docling fallback -> tables/*.md
            picture  -> PyMuPDF pixmap (clip = bbox)  ----> images/*.png + .md
            code     -> PyMuPDF text                  ----> code/*.md  +  inline
            formula  -> Docling LaTeX                 ----> formulas/*.md + inline
     -> 00_contents.md (TOC) + 01_main.md (plain text with placeholders)
     -> zip everything
```

## Output bundle layout

```
<pdf_stem>_pages_<first>-<last>/
  00_contents.md              # table of contents (one row per element)
  01_main.md                  # plain text of the PDF with placeholders
  layout/
    layout.md                 # Docling markdown export
    layout.json               # lossless DoclingDocument JSON
  tables/
    table_001.md ...          # one Markdown file per detected table
  images/
    image_001.png             # cropped picture from the original PDF
    image_001.md              # sidecar with bbox, caption, etc.
  code/
    code_001.md ...           # detected code blocks
  formulas/
    formula_001.md ...        # detected formulas (LaTeX)
```

## Use it on Google Colab

1. Upload `pdf_extractor_colab.ipynb` to [colab.research.google.com](https://colab.research.google.com).
2. Run the cells **top to bottom**. The notebook will prompt you for:
   - the PDF (upload widget / path / URL),
   - the pages to extract (`all`, `1,3,5`, `2-7`, ...),
   - whether to use Granite-Docling VLM (slower, needs a GPU runtime).
3. The last cell zips the output folder and triggers a browser download.

> For Granite-Docling on Colab, pick **Runtime -> Change runtime type -> GPU**. On a T4 you may need the `dtype=float32` workaround documented on the [model card](https://huggingface.co/ibm-granite/granite-docling-258M).

## Use it locally / from another script

```bash
pip install pymupdf docling "camelot-py[base]" opencv-python-headless
# Linux/macOS only - Ghostscript is required by Camelot:
sudo apt-get install -y ghostscript    # or:  brew install ghostscript
```

```bash
# CLI
python pdf_extractor.py path/to/file.pdf -p "1-5" -o ./output
python pdf_extractor.py path/to/file.pdf -p all --granite      # use Granite-Docling VLM
```

```python
# Python API
from pdf_extractor import run

zip_path = run(
    pdf_path="path/to/file.pdf",
    page_spec="1,3,5-7",
    out_dir="./output",
    use_granite=False,
    image_dpi=200,
)
print(zip_path)
```

## Notes / limitations

- Built for **true PDFs**. Scanned PDFs work too if you switch Docling to its OCR pipeline (or pre-OCR with PyMuPDF's `get_textpage_ocr`).
- Camelot Stream is the default table extractor (per the spec). When it can't find a table inside the bbox the script silently falls back to Docling's own parsed table cells.
- Coordinate handling assumes Docling reports bboxes in `BOTTOMLEFT` origin (the default since Docling 2.x); `TOPLEFT` is handled too, in case a future model emits it.
- Image bboxes are rendered against the *original* PDF, so picture fidelity is independent of the chosen DPI used by Docling internally.
