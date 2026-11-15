"""
Build the evidence figures for the final documentation from YOUR runs.

1. Run every portfolio's ./run_all.sh first, so each evidence/ folder holds your
   own outputs.
2. Add the two images that cannot be generated from text:
     portfolio-1-database-design/evidence/er-diagram.png        (export from mermaid.live)
     portfolio-3-transactions-concurrency/evidence/manual_terminals.png  (your screenshot)
3. Run, from this folder:
     pip install pillow python-docx
     python build_figures.py

It renders each evidence excerpt as a terminal-style PNG in ./figures/ and writes
MIT8103_Final_Documentation_with_evidence.docx, with every [[FIGURE:...]] slot
replaced by its image. Missing evidence is reported and its slot left in place.
"""
import re
import sys
from pathlib import Path

from docx import Document
from docx.oxml.ns import qn
from docx.shared import Inches
from PIL import Image, ImageDraw, ImageFont

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
TEMPLATE = HERE / "MIT8103_Final_Documentation.docx"
OUTPUT = HERE / "MIT8103_Final_Documentation_with_evidence.docx"
FIG_DIR = HERE / "figures"

P1 = REPO / "portfolio-1-database-design" / "evidence"
P2 = REPO / "portfolio-2-query-optimisation" / "evidence"
P3 = REPO / "portfolio-3-transactions-concurrency" / "evidence"
P4 = REPO / "portfolio-4-nosql" / "evidence"
P5 = REPO / "portfolio-5-distributed-cloud" / "evidence"

MAX_LINES = 42
MAX_WIDTH = 112


# ---------------------------------------------------------------------------
# Extraction helpers
# ---------------------------------------------------------------------------
def read(path):
    return path.read_text(encoding="utf-8", errors="replace").splitlines()


def region(lines, start, end=None):
    """Lines from the first match of `start` up to (not including) `end`."""
    out, on = [], False
    for ln in lines:
        if not on and re.search(start, ln):
            on = True
        elif on and end and re.search(end, ln):
            break
        if on:
            out.append(ln)
    return out


def keep(lines, pattern):
    return [ln for ln in lines if re.search(pattern, ln)]


def no_rules(lines):
    return [ln for ln in lines if not re.fullmatch(r"\s*[=\-]{10,}\s*", ln)]


def p1_constraints():
    lines = read(P1 / "constraint_tests_output.txt")
    lines = keep(lines, r"^--- T|ERROR:")
    return [re.sub(r"^psql:[^:]+:\d+:\s*", "", ln) for ln in lines]


def p2_timings():
    out = []
    for label, name in (("BASELINE", "02_baseline_plans.txt"), ("OPTIMISED", "04_optimised_plans.txt")):
        out.append(f"### {label}  ({name})")
        lines = read(P2 / name)
        write_times, in_w1 = [], False
        for ln in lines:
            if re.match(r"^(Q\d[a-z]?|W1)\s", ln):
                in_w1 = ln.startswith("W1")
                if not in_w1:
                    out.append(ln.strip())
            elif "Execution Time" in ln:
                out.append("      " + ln.strip())
            elif in_w1 and ln.startswith("Time:"):
                write_times.append(float(ln.split()[1]))
        if write_times:
            out.append(f"W1  Insert 50,000 payments (rolled back)\n      Time: {max(write_times):.1f} ms")
        out.append("")
    return "\n".join(out).splitlines()


def p2_app_patterns():
    return read(P2 / "05_app_patterns_2ms_latency.txt")


def p3_double_credit():
    lines = read(P3 / "03_concurrency.txt")
    return no_rules(region(lines, r"^Scenario 2a", r"^Scenario 2c"))


def p3_isolation():
    lines = region(read(P3 / "03_concurrency.txt"), r"^Scenario 3a", r"^Scenario 4a")
    return keep(lines, r"^Scenario 3|RESULT|ERROR|-> |retrying")


def p4_validation():
    lines = region(read(P4 / "03_validation_tests.txt"), r"^=== Enforced", r"^\(cleanup")
    return [ln for ln in lines if ln.strip()]


def p4_concurrency():
    lines = read(P4 / "05_concurrency_mongo.txt")
    return keep(lines, r"^C\d|RESULT|-> |claimed|WRITE FAILED|read (status|remaining)")


def p4_explain():
    lines = region(read(P4 / "04_operations.txt"), r"O9")
    return [ln for ln in no_rules(lines) if ln.strip()]


def p5_consistency():
    return no_rules(region(read(P5 / "02_consistency.txt"), r"^C2 Lagging"))


def p5_failover():
    lines = read(P5 / "03_failover.txt")
    return keep(lines, r"^F\d|RESULT|elected|killing|stepping|acknowledged|longest|reads ok|"
                       r"present on|FAILED|back as|waited")


def p5_outbox():
    lines = no_rules(read(P5 / "04_outbox.txt"))
    return [ln for ln in lines if ln.strip()]


# key -> (title shown in the image, extractor) ; image-only keys -> path
TEXT_FIGURES = {
    "p1_constraints":   ("psql -f 03_constraint_tests.sql", p1_constraints),
    "p2_timings":       ("EXPLAIN (ANALYZE) execution times", p2_timings),
    "p2_app_patterns":  ("python 05_app_patterns.py --latency-ms 2", p2_app_patterns),
    "p3_double_credit": ("python 03_concurrency_scenarios.py  (scenario 2)", p3_double_credit),
    "p3_isolation":     ("python 03_concurrency_scenarios.py  (scenario 3)", p3_isolation),
    "p4_validation":    ("mongosh 03_validation_tests.js", p4_validation),
    "p4_concurrency":   ("python 05_concurrency_mongo.py", p4_concurrency),
    "p4_explain":       ("mongosh 04_operations.js  (O9 explain)", p4_explain),
    "p5_consistency":   ("python 02_consistency.py", p5_consistency),
    "p5_failover":      ("python 03_failover.py", p5_failover),
    "p5_outbox":        ("python 04_outbox_change_stream.py", p5_outbox),
}
IMAGE_FIGURES = {
    "p1_er_diagram":       P1 / "er-diagram.png",
    "p3_manual_terminals": P3 / "manual_terminals.png",
}


# ---------------------------------------------------------------------------
# Terminal-style rendering
# ---------------------------------------------------------------------------
FONT_CANDIDATES = [
    "C:/Windows/Fonts/consola.ttf",
    "/System/Library/Fonts/Menlo.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf",
    "DejaVuSansMono.ttf",
]


def load_font(size):
    for f in FONT_CANDIDATES:
        try:
            return ImageFont.truetype(f, size)
        except OSError:
            continue
    return ImageFont.load_default()


def colour_for(line):
    u = line.upper()
    if re.match(r"^(###|Q\d|W1|Scenario|C\d|F\d|E\d|O9|===|---)", line.strip()):
        return (229, 192, 123)                        # section headers: amber
    if re.search(r"->\s*(REJECTED|ACCEPTED)", u):
        return (212, 212, 212)                        # expected test outcomes: neutral
    if re.search(r"WRONG|FAILED|ERROR|DEADLOCK|NOT FOUND|STALE|DATA LOST|DROPPED|UNEXPECTED", u):
        return (241, 108, 108)                        # problems: red
    if re.search(r"CORRECT|NO DATA LOSS|DELIVERED|SUCCEEDED|AS EXPECTED|APPROVED", u):
        return (125, 211, 132)                        # good outcomes: green
    return (212, 212, 212)


def render(title, lines, path, scale=2):
    wrapped = []
    for ln in lines:
        ln = ln.replace("\t", "    ").rstrip()
        while len(ln) > MAX_WIDTH:
            wrapped.append(ln[:MAX_WIDTH])
            ln = "    " + ln[MAX_WIDTH:]
        wrapped.append(ln)
    if len(wrapped) > MAX_LINES:
        wrapped = wrapped[:MAX_LINES - 1] + [f"... ({len(wrapped) - MAX_LINES + 1} more lines in the evidence file)"]

    font = load_font(13 * scale)
    bbox = font.getbbox("M")
    cw, lh = bbox[2] - bbox[0], int((bbox[3] - bbox[1]) * 1.65)
    pad, bar = 14 * scale, 26 * scale
    width = max(len(ln) for ln in wrapped + [title]) * cw + 2 * pad
    width = max(width, 600 * scale)
    height = bar + pad + len(wrapped) * lh + pad

    img = Image.new("RGB", (width, height), (30, 30, 30))
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, width, bar], fill=(58, 58, 58))
    for i, c in enumerate([(255, 95, 86), (255, 189, 46), (39, 201, 63)]):
        x = pad + i * 18 * scale
        d.ellipse([x, bar // 2 - 5 * scale, x + 10 * scale, bar // 2 + 5 * scale], fill=c)
    d.text((pad + 64 * scale, bar // 2 - lh // 3), "$ " + title, font=font, fill=(200, 200, 200))
    y = bar + pad
    for ln in wrapped:
        d.text((pad, y), ln, font=font, fill=colour_for(ln))
        y += lh
    img.save(path)


# ---------------------------------------------------------------------------
# Insert into the Word document
# ---------------------------------------------------------------------------
def replace_slot(doc, key, image_path, text_figure=False):
    marker = f"[[FIGURE:{key}]]"
    for p in doc.paragraphs:
        if p.text.strip() == marker:
            pPr = p._p.pPr
            if pPr is not None:                       # drop the placeholder box styling
                for tag in ("w:shd", "w:pBdr"):
                    el = pPr.find(qn(tag))
                    if el is not None:
                        pPr.remove(el)
            for r in p.runs:
                r.text = ""
            with Image.open(image_path) as im:
                w_px, h_px = im.size
            inches = 6.4
            if text_figure:                           # same text size in every terminal figure
                inches = min(6.4, w_px / 2 / 145)
            if h_px / w_px * inches > 8.5:            # keep very tall images on one page
                inches = 8.5 * w_px / h_px
            width = Inches(inches)
            p.alignment = 1                           # centre
            p.add_run().add_picture(str(image_path), width=width)
            return True
    return False


def main():
    if not TEMPLATE.exists():
        sys.exit(f"Template not found: {TEMPLATE}")
    FIG_DIR.mkdir(exist_ok=True)
    doc = Document(str(TEMPLATE))
    done, missing = [], []

    for key, (title, extractor) in TEXT_FIGURES.items():
        try:
            lines = extractor()
        except FileNotFoundError as e:
            missing.append(f"{key}: {Path(e.filename).relative_to(REPO)} not found (run that portfolio's run_all.sh)")
            continue
        if not any(ln.strip() for ln in lines):
            missing.append(f"{key}: evidence file found but nothing matched; check its contents")
            continue
        out = FIG_DIR / f"{key}.png"
        render(title, lines, out)
        replace_slot(doc, key, out, text_figure=True) and done.append(key)

    for key, path in IMAGE_FIGURES.items():
        if path.exists():
            replace_slot(doc, key, path) and done.append(key)
        else:
            missing.append(f"{key}: add your image at {path.relative_to(REPO)}")

    doc.save(str(OUTPUT))
    print(f"Inserted {len(done)} figures -> {OUTPUT.name}")
    for m in missing:
        print("  MISSING", m)


if __name__ == "__main__":
    main()
