"""Rebuild frontend/fonts/*.woff2 so they hold the characters the translations use.

Run after adding text with new Japanese or other non-Latin characters:
    pip install fonttools brotli
    python tools/subset_fonts.py <folder with the source TTF files>

Source fonts (SIL OFL 1.1), from github.com/google/fonts/tree/main/ofl:
    mplus1/MPLUS1[wght].ttf, mplus1p/MPLUS1p-{Regular,Medium,Bold}.ttf, shipporimincho/ShipporiMincho-Bold.ttf
"""
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FONTS = ROOT / "frontend" / "fonts"
BASE = [*range(0x20, 0x7F), *range(0xA0, 0x180)]
KEEP = "階調色形壁層位置効果✓✗◎↺⟲ⓘ→←×·–—„”…°≥≤−+"
CYRILLIC = [*range(0x400, 0x460), 0x394, *map(ord, "✓✗◎↺ⓘ→←")]


def texts(value):
    if isinstance(value, dict):
        for v in value.values():
            yield from texts(v)
    else:
        yield value


def used(code: str) -> set[int]:
    data = json.loads((ROOT / "frontend" / "i18n" / f"{code}.json").read_text(encoding="utf-8"))
    return {ord(c) for k, v in data.items() for s in (k, *texts(v)) for c in s}


def subset(source: Path, target: str, codes):
    subprocess.run(["pyftsubset", str(source), "--unicodes=" + ",".join(f"{c:x}" for c in sorted(codes)),
                    "--flavor=woff2", "--layout-features=kern,liga,palt", f"--output-file={FONTS / target}"], check=True)


src = Path(sys.argv[1])
latin_ja = set(BASE) | {ord(c) for c in KEEP} | used("ja")
for code in ("pl", "es", "de", "fr"):
    latin_ja |= used(code)
subset(src / "MPLUS1[wght].ttf", "MPLUS1.woff2", latin_ja)
subset(src / "ShipporiMincho-Bold.ttf", "ShipporiMincho-Bold.woff2", latin_ja)
for weight in ("Regular", "Medium", "Bold"):
    subset(src / f"MPLUS1p-{weight}.ttf", f"MPLUS1p-{weight}.woff2", CYRILLIC)
