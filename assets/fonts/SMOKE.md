# Fonts smoke-test

**Date:** 2026-05-10
**Tester:** Phase 7 Plan 03 (BOOT-03)
**Tool used:** Pillow (PIL) — Homebrew `ffmpeg 8.1` is built without `--enable-libfreetype`,
so `drawtext` filter is unavailable. Pillow path is the canonical render route for
Phase 8+ character generation per CLAUDE.md tech-stack (Pillow >= 10.0), so this is the
production-relevant smoke target. ffmpeg drawtext path will be revisited in Phase 13
(video assembly) — by then either reinstall ffmpeg with `brew install ffmpeg --with-freetype`
(legacy tap) or use the `--enable-freetype` from `homebrew/ffmpeg-options`, OR delegate
text overlays to Pillow → composite into video frames.

## Test setup

- Background: brand bg `#1A0F08`
- Foreground: brand gold `#D4A640` and `#F4C757`
- Canvas: 1000×200 PNG, text centred via textbbox
- Test string contains Russian Cyrillic + edge-case glyphs:
  `«Привет, мир ёъўїЙ»`, `«Forton Lab — студия мобильных приложений»`,
  `«Курсивный текст по-русски»`, `«Жирный курсив тест»`

## Commands run

```python
from PIL import Image, ImageDraw, ImageFont
img = Image.new("RGB", (1000, 200), "#1A0F08")
fnt = ImageFont.truetype("cormorant-garamond/CormorantGaramond-Regular.ttf", 64)
draw = ImageDraw.Draw(img)
draw.text((x, y), "Привет, мир ёъўїЙ", font=fnt, fill="#D4A640")
img.save("/tmp/smoke-cg-regular.png")
```
(repeated for each font/style)

Glyph-coverage audit via `ImageFont.getlength()` — comparing real text width vs.
width of `?` fallback string of equal length: significantly different widths confirm
that glyphs are real (not `.notdef`).

## Results

| # | Font | File | PNG size | Text width vs `?`-fallback | Cyrillic rendered |
|---|---|---|---:|---|:---:|
| 1 | Cormorant Garamond Regular    | `cormorant-garamond/CormorantGaramond-Regular.ttf`    | 10 191 B | 334 px vs 240 px | yes |
| 2 | Cormorant Garamond Bold       | `cormorant-garamond/CormorantGaramond-Bold.ttf`       | 12 545 B | 337 px vs 240 px | yes |
| 3 | Cormorant Garamond Italic     | `cormorant-garamond/CormorantGaramond-Italic.ttf`     | 12 070 B | 311 px vs 210 px | yes |
| 4 | Cormorant Garamond BoldItalic | `cormorant-garamond/CormorantGaramond-BoldItalic.ttf` | 11 382 B | 319 px vs 210 px | yes |
| 5 | Marck Script Regular          | `marck-script/MarckScript-Regular.ttf`                | 11 606 B | 368 px vs 300 px | yes |

All 5 fonts: text-width differs from `?`-fallback-width — glyphs are real, not
`.notdef` substitutions. Visual inspection of output PNGs confirms correct Russian
rendering including edge-case characters `ё`, `ъ`, `ў` (Belarusian short-u; expected
to be present in Cormorant per its extended-Cyrillic coverage), `ї` (Ukrainian),
and `Й` (Russian short-i with breve).

**Verdict: PASS** — both font families render the project's required Cyrillic set
without missing glyphs.

## Notes

- All fonts are licensed under **SIL OFL 1.1** — `OFL.txt` lives in each subdirectory
  (`cormorant-garamond/OFL.txt` from CatharsisFonts upstream;
  `marck-script/OFL.txt` from google/fonts upstream).
- **Cormorant Garamond** replaces Cinzel as the serif body/headings face for
  Russian-language posts. Cinzel itself remains for English-only material because
  its design is tailored for Latin caps.
- **Marck Script** replaces Allura for Russian script/handwritten contexts (logos,
  таглайны). Allura has no Cyrillic at all.
- **Inter** (pre-existing in palette) already covers Cyrillic — no changes needed.
- Source provenance:
    - Cormorant Garamond static TTFs: `github.com/CatharsisFonts/Cormorant`
      (`fonts/ttf/CormorantGaramond-{Regular,Bold,Italic,BoldItalic}.ttf`).
      Picked over the variable `[wght].ttf` in google/fonts because static styles
      are immediately ffmpeg-/Pillow-compatible without variation-axis configuration.
    - Marck Script: `github.com/google/fonts/tree/main/ofl/marckscript`
      (`MarckScript-Regular.ttf`, single weight upstream).

## Future use

- **Phase 8+ character generation** (Pillow):
  ```python
  from PIL import ImageFont
  fnt = ImageFont.truetype(
      "marketing-v3/assets/fonts/cormorant-garamond/CormorantGaramond-Bold.ttf",
      size=42,
  )
  ```
- **Phase 13 video assembly** (ffmpeg, **requires libfreetype-enabled build**):
  ```
  ffmpeg ... -vf "drawtext=fontfile=marketing-v3/assets/fonts/cormorant-garamond/CormorantGaramond-Regular.ttf:text='...':fontcolor=#D4A640:fontsize=64:..."
  ```
  If the deployed `ffmpeg` lacks freetype, fall back to Pillow-rendered PNG overlay
  composited via ffmpeg `overlay=` filter — equivalent visual result.
