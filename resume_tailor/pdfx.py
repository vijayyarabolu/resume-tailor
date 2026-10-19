"""PDF facts through poppler's command-line tools (pdfinfo, pdffonts, pdftotext, pdftoppm). Stdlib otherwise.

lines()  -> text lines with positions (points, origin top-left) grouped by block (one block ~ one paragraph).
raster() -> page 1 as 8-bit grey rows, parsed from pdftoppm's PGM output.
"""
import re
import shutil
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

TIMEOUT_S = 60
XHTML = "{http://www.w3.org/1999/xhtml}"
_SUBSET = re.compile(r"^[A-Z]{6}\+")


class PdfError(RuntimeError):
    pass


@dataclass(frozen=True)
class Info:
    pages: int
    width: float
    height: float
    producer: str


@dataclass(frozen=True)
class Line:
    text: str
    x0: float
    y0: float
    x1: float
    y1: float
    words: tuple = ()        # (text, x0, x1)


@dataclass
class Block:
    lines: list = field(default_factory=list)

    @property
    def text(self) -> str:
        return " ".join(ln.text for ln in self.lines)


def tools_missing() -> list[str]:
    return [t for t in ("pdfinfo", "pdffonts", "pdftotext", "pdftoppm") if shutil.which(t) is None]


def _run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, capture_output=True, timeout=TIMEOUT_S, check=True, **kw)
    except FileNotFoundError as err:
        raise PdfError(f"{cmd[0]} not installed (brew install poppler)") from err
    except subprocess.CalledProcessError as err:
        raise PdfError(f"{cmd[0]} failed: {err.stderr.decode(errors='replace')[-300:]}") from err
    except subprocess.TimeoutExpired as err:
        raise PdfError(f"{cmd[0]} timed out") from err


def info(pdf: Path) -> Info:
    out = _run(["pdfinfo", str(pdf)]).stdout.decode(errors="replace")
    pages = re.search(r"^Pages:\s+(\d+)", out, re.M)
    size = re.search(r"^Page size:\s+([\d.]+) x ([\d.]+)", out, re.M)
    producer = re.search(r"^Producer:\s+(.*)$", out, re.M)
    return Info(int(pages.group(1)) if pages else 0, float(size.group(1)) if size else 0.0,
                float(size.group(2)) if size else 0.0, producer.group(1).strip() if producer else "")


def fonts(pdf: Path) -> set[str]:
    """Base font names without the subset prefix: {'Cambria', 'Cambria-Bold', 'Calibri', 'ArialMT'}."""
    out = _run(["pdffonts", str(pdf)]).stdout.decode(errors="replace").splitlines()[2:]
    return {_SUBSET.sub("", ln.split()[0]) for ln in out if ln.strip()}


def text_fonts(pdf: Path, page: int = 1) -> set[str]:
    """Font families that carry visible text (whitespace-only runs, which converters map to Times/Calibri,
    are ignored): from `pdftohtml -xml`."""
    out = _run(["pdftohtml", "-xml", "-i", "-stdout", "-f", str(page), "-l", str(page), str(pdf)]).stdout
    text = out.decode(errors="replace")
    specs = dict(re.findall(r'<fontspec id="(\d+)" size="\d+" family="(?:[A-Z]{6}\+)?([^"]+)"', text))
    used = set()
    for font, body in re.findall(r'<text[^>]*font="(\d+)"[^>]*>(.*?)</text>', text, re.S):
        if re.sub(r"<[^>]+>", "", body).strip():
            used.add(specs.get(font, ""))
    return families({u for u in used if u})


def families(names: set[str]) -> set[str]:
    """'Cambria-BoldItalic' -> 'cambria'; 'ArialMT' -> 'arial'."""
    out = set()
    for n in names:
        base = re.sub(r"[^a-z]", "", n.split("-")[0].split(",")[0].lower())
        out.add(re.sub(r"(psmt|mt)$", "", base))
    return out


def lines(pdf: Path, page: int = 1) -> list[Block]:
    """Blocks of lines with bounding boxes from `pdftotext -bbox-layout` (points, y down)."""
    out = _run(["pdftotext", "-bbox-layout", "-f", str(page), "-l", str(page), str(pdf), "-"]).stdout
    root = ET.fromstring(out)
    blocks = []
    for block in root.iter(XHTML + "block"):
        b = Block()
        for ln in block.findall(XHTML + "line"):
            words = tuple((w.text or "", float(w.get("xMin")), float(w.get("xMax")))
                          for w in ln.findall(XHTML + "word"))
            text = " ".join(w[0] for w in words)
            b.lines.append(Line(text, float(ln.get("xMin")), float(ln.get("yMin")), float(ln.get("xMax")),
                                float(ln.get("yMax")), words))
        if b.lines:
            blocks.append(b)
    return blocks


def flat_lines(blocks: list[Block]) -> list[Line]:
    return [ln for b in blocks for ln in b.lines]


def raster(pdf: Path, dpi: int = 72, page: int = 1) -> tuple[int, int, list[bytes]]:
    """(width, height, rows of grey bytes) of one page via pdftoppm -gray (PGM P5)."""
    with tempfile.TemporaryDirectory(prefix="resume_tailor-pgm-") as tmp:
        stem = Path(tmp) / "page"
        _run(["pdftoppm", "-gray", "-r", str(dpi), "-f", str(page), "-l", str(page), "-singlefile", str(pdf),
              str(stem)])
        data = stem.with_suffix(".pgm").read_bytes()
    return parse_pgm(data)


def parse_pgm(data: bytes) -> tuple[int, int, list[bytes]]:
    if not data.startswith(b"P5"):
        raise PdfError("pdftoppm did not produce a P5 PGM")
    fields, pos = [], 2
    while len(fields) < 3:
        while data[pos:pos + 1].isspace():
            pos += 1
        if data[pos:pos + 1] == b"#":
            pos = data.index(b"\n", pos) + 1
            continue
        end = pos
        while not data[end:end + 1].isspace():
            end += 1
        fields.append(int(data[pos:end]))
        pos = end
    pos += 1
    width, height, maxval = fields
    if maxval > 255:
        raise PdfError("16-bit PGM not supported")
    body = data[pos:pos + width * height]
    return width, height, [body[y * width:(y + 1) * width] for y in range(height)]


def png(pdf: Path, out_stem: Path, dpi: int = 110, page: int = 1) -> Path:
    """Page image for the phone view: <out_stem>.png"""
    _run(["pdftoppm", "-png", "-r", str(dpi), "-f", str(page), "-l", str(page), "-singlefile", str(pdf),
          str(out_stem)])
    return out_stem.with_suffix(".png")


# ---------- ink geometry ----------

def ink_bbox(width: int, height: int, rows: list[bytes], threshold: int = 160):
    """(left, top, right, bottom) pixel box of everything darker than threshold; None for a blank page."""
    left = top = None
    right = bottom = -1
    for y, row in enumerate(rows):
        xs = [x for x, v in enumerate(row) if v < threshold]
        if not xs:
            continue
        top = y if top is None else top
        bottom = y
        left = xs[0] if left is None else min(left, xs[0])
        right = max(right, xs[-1])
    return None if top is None else (left, top, right, bottom)


@dataclass(frozen=True)
class Band:
    top: int
    bottom: int
    left: int
    right: int

    @property
    def height(self) -> int:
        return self.bottom - self.top


def bands(width: int, height: int, rows: list[bytes], threshold: int = 160) -> list[Band]:
    """Horizontal runs of rows that contain ink (text lines; a heading's underline merges with its text)."""
    out, start = [], None
    for y in range(height + 1):
        inked = y < height and any(v < threshold for v in rows[y])
        if inked and start is None:
            start = y
        elif not inked and start is not None:
            xs = [x for r in rows[start:y] for x, v in enumerate(r) if v < threshold]
            band = Band(start, y, min(xs), max(xs))
            if out and band.height <= 2 and band.top - out[-1].bottom <= 2:     # a 1-px rule under a heading
                prev = out[-1]
                out[-1] = Band(prev.top, band.bottom, min(prev.left, band.left), max(prev.right, band.right))
            else:
                out.append(band)
            start = None
    return out
