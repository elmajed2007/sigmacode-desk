# SigmaCode Desk

Offline Arabic document assistant for Windows. Reads documents (including scanned
PDFs, using on-device Arabic OCR), lets a person review the text, splits it into
chunks, indexes it, and answers questions with citations to the document and page —
using a small open-source model running on the same computer. No internet is needed
after a model has been downloaded or imported once.

**Arabic documentation: [README.ar.md](README.ar.md)**

## Features (v1)

- **Import**: PDF (digital or scanned), images (PNG/JPG/TIFF…), Word (.docx), text files.
- **Arabic OCR on the device**: PaddleOCR PP-OCRv3 models running on ONNX Runtime,
  with Arabic post-processing (reading order, word spacing, letter forms).
  Digital PDFs are read directly from their text layer.
- **Review**: low-confidence OCR documents wait for a person to check and correct the
  text next to the page image before they are indexed.
- **Search**: Arabic-aware keyword search (SQLite FTS5 + Arabic stemming) combined with
  semantic search (local embedding model) by reciprocal-rank fusion.
- **Chat with citations**: every answer cites `[n]` sources; clicking a citation shows
  the approved text and the original page. Strict mode refuses to answer without sources.
- **AI providers**: local (llama.cpp, offline, default), Ollama, OpenAI, Claude, Gemini, or
  any OpenAI-compatible server. API keys are encrypted with the Windows account (DPAPI).
- **Local models**: one-click download of recommended Apache-2.0 models (Qwen3), or
  import a `.gguf` file from USB for machines with no internet.
- **Export**: JSONL dataset (one line per chunk with document, pages, confidence).

## Install

Download `SigmaCodeDesk-Setup-x.y.z.exe` from the
[Actions artifacts / Releases](../../releases), run it (no administrator rights needed),
and open **SigmaCode Desk** from the Start menu. Data is stored in
`%LOCALAPPDATA%\SigmaCodeDesk` and kept on uninstall.

System requirements: Windows 10/11 64-bit, Microsoft Edge (pre-installed), 8 GB RAM for
the recommended 4B model (4 GB for the 1.7B model).

## Architecture

```
SigmaCodeDesk.exe ── FastAPI on 127.0.0.1 (random port, per-launch token)
   │                    ├─ OCR: ONNX Runtime + PP-OCRv3 (Arabic)
   │                    ├─ SQLite (WAL) + FTS5 + float32 vectors
   │                    └─ llama-server.exe (chat / embeddings) as child processes
   └─ window: Edge in app mode (own profile); closes the app when the window closes
```

## Development

```bash
python -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
python -m pytest -q tests          # unit + API tests
python app/run.py                  # starts the app (llama-server optional: runtime/llama/)
```

The Windows installer is built by GitHub Actions (`.github/workflows/build-windows.yml`):
tests → llama.cpp runtime → local-AI integration test → PyInstaller → packaged self-test →
Inno Setup. Push a tag `v1.0.0` to publish a release.

## Licence notes

See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
