# Final documentation

| File | Purpose |
|---|---|
| `MIT8103_Final_Documentation.docx` | The 2–3 page summary, reflection, AI declaration and references, with an evidence appendix of figure slots |
| `build_figures.py` | Turns your `evidence/` outputs into terminal-style figures and inserts them into the document |

## Steps

1. Run `./run_all.sh` in every portfolio folder so each `evidence/` folder holds **your** outputs.
2. Add the two images that can't be generated from text:
   - `portfolio-1-database-design/evidence/er-diagram.png` (paste `er-diagram.mmd` into mermaid.live and export PNG)
   - `portfolio-3-transactions-concurrency/evidence/manual_terminals.png` (your two-terminal screenshot)
3. Build the figures:
   ```bash
   pip install pillow python-docx
   cd final-documentation
   python build_figures.py
   ```
   This writes `MIT8103_Final_Documentation_with_evidence.docx` and the images in `figures/`. Any missing evidence is listed, and its slot is left in place.
4. Open the new document in Word: fill in your details, write the reflection, check the AI declaration, update numbers that differ from your run, and delete the highlighted notes.

If you edit the text first, edit `MIT8103_Final_Documentation.docx` itself and keep the `[[FIGURE:...]]` lines, so the script can still find them.
