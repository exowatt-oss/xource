# PDF rendering and extraction notices

Xource uses **pypdfium2 5.14.0** (resolved in `uv.lock`) for text extraction and
first-page thumbnails. This replaces PyMuPDF, whose
[AGPL/commercial licensing](https://pymupdf.readthedocs.io/en/latest/about.html#license)
was not suitable for an otherwise independently licensed runtime.

- The pypdfium2 Python bindings are licensed **Apache-2.0 OR BSD-3-Clause**:
  [upstream licensing statement](https://github.com/pypdfium2-team/pypdfium2/blob/5.14.0/README.md#licensing),
  [Apache-2.0 text](https://github.com/pypdfium2-team/pypdfium2/blob/5.14.0/LICENSES/Apache-2.0.txt),
  [BSD-3-Clause text](https://github.com/pypdfium2-team/pypdfium2/blob/5.14.0/LICENSES/BSD-3-Clause.txt).
- Bundled [PDFium](https://pdfium.googlesource.com/pdfium/+/refs/heads/main/LICENSE)
  is BSD-style licensed, **with separate licenses for components in each binary
  build**. Read and retain the notices for the deployed wheel's platform.
  Upstream tracks these in
  [`BUILD_LICENSES/`](https://github.com/pypdfium2-team/pypdfium2/tree/5.14.0/BUILD_LICENSES).
  For example, a Linux ARM64 5.14.0 wheel may ship them at
  `pypdfium2-5.14.0.dist-info/licenses/data/linux_arm64/BUILD_LICENSES/`;
  the bindings' license texts are in its `.dist-info/licenses/LICENSES/`.
  Other platform wheels carry their own build-specific notices. A macOS ARM64
  installation also retains build notice files under
  `data/darwin_arm64/BUILD_LICENSES/`, plus the bindings license texts. In
  particular, portions of this software use the FreeType Project
  (www.freetype.org); all rights reserved. Retain its FreeType Project License
  credit/disclaimer along with the other shipped notices.

Keep the wheel's `.dist-info/licenses/` directory when packaging or
redistributing the app; do not assume the bundled native components are all
covered by the bindings' dual license. This inventory is not legal advice.
