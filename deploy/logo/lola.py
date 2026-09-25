# Lola-Logo: python lola.py (schreibt lola.svg); python make_assets.py (icon.png, logo.png, Vorschau)
"""Lola-Logo als SVG (Stil: flach, dicke schwarze Kontur, Krone, Augen zu)."""
import cairosvg, sys

K = "#1d1d1b"        # Kontur
W = "#ffffff"        # Fell weiß
T = "#a39486"        # Tabby hell
TD = "#5f554c"       # Tabby Streifen
G = "#f4c21f"        # Gold
P = "#f2a7ae"        # Rosa (Ohr, Zunge)
N = "#b9786a"        # Nase
SW = 18              # Konturbreite

def cat(bg=None):
    head = "M 318 168 C 420 168 492 240 492 336 C 492 432 420 500 318 500 C 250 500 196 470 168 430 C 128 424 104 398 106 370 C 108 340 134 322 164 318 C 186 228 244 168 318 168 Z"
    body = "M 250 470 C 220 540 214 640 236 712 C 250 752 300 764 360 764 L 430 764 C 496 764 520 720 516 650 C 510 560 470 480 400 470 Z"
    ear_front = "M 196 262 C 190 200 194 138 214 96 C 256 122 292 160 314 196 Z"
    ear_back = "M 380 196 C 408 150 440 116 478 100 C 490 146 486 196 470 244 Z"
    leg = "M 262 560 C 250 620 246 680 252 724 L 318 724 C 322 680 322 620 318 560 Z"
    paw = "M 230 736 C 230 708 262 700 290 700 C 322 700 340 712 340 736 C 340 758 316 764 288 764 C 256 764 230 760 230 736 Z"
    hind = "M 392 700 C 392 660 430 640 470 646 C 512 652 530 690 522 724 C 516 754 490 764 452 764 C 412 764 392 740 392 700 Z"
    tail = "M 500 690 C 590 690 612 610 590 540 C 574 490 590 440 626 424"
    parts = []
    if bg:
        parts.append(bg)
    # Schwanz (hinter allem): Kontur, Fell, Ringe
    parts.append(f'<path d="{tail}" fill="none" stroke="{K}" stroke-width="{56+2*SW}" stroke-linecap="round"/>')
    parts.append(f'<path d="{tail}" fill="none" stroke="{T}" stroke-width="56" stroke-linecap="round"/>')
    parts.append(f'<path d="{tail}" fill="none" stroke="{TD}" stroke-width="56" stroke-dasharray="18 34" stroke-dashoffset="10"/>')
    parts.append(f'<path d="M 626 424 m -6 0" fill="none" stroke="{TD}" stroke-width="56" stroke-linecap="round"/>')
    # Kontur aller Körperteile (Vereinigung): erst dicke Striche, dann Füllungen
    shapes = [ear_back, ear_front, body, hind, leg, paw, head]
    for d in shapes:
        parts.append(f'<path d="{d}" fill="{K}" stroke="{K}" stroke-width="{2*SW}" stroke-linejoin="round"/>')
    # Füllungen
    parts.append(f'<defs><clipPath id="cb"><path d="{body}"/></clipPath><clipPath id="ch"><path d="{head}"/></clipPath>'
                 f'<clipPath id="ce"><path d="{ear_front}"/></clipPath><clipPath id="cb2"><path d="{ear_back}"/></clipPath>'
                 f'<clipPath id="chind"><path d="{hind}"/></clipPath></defs>')
    parts.append(f'<path d="{ear_back}" fill="{T}"/>')
    parts.append(f'<path d="M 402 200 C 424 166 448 142 470 128 C 476 160 474 196 464 226 Z" fill="{P}" clip-path="url(#cb2)"/>')
    parts.append(f'<path d="{body}" fill="{W}"/>')
    # Tabby-Rücken
    parts.append(f'<g clip-path="url(#cb)"><path d="M 356 470 C 404 520 428 580 462 624 C 486 654 512 664 560 664 L 560 440 Z" fill="{T}"/>'
                 f'<path d="M 420 516 C 450 526 486 532 530 530 M 446 576 C 474 588 500 594 530 594" '
                 f'fill="none" stroke="{TD}" stroke-width="16" stroke-linecap="round"/></g>')
    parts.append(f'<path d="{hind}" fill="{W}"/>')
    parts.append(f'<path d="{leg}" fill="{W}"/>')
    parts.append(f'<path d="{paw}" fill="{W}"/>')
    parts.append(f'<path d="{head}" fill="{W}"/>')
    # Tabby-Kappe + Augenfleck
    parts.append(f'<g clip-path="url(#ch)"><path d="M 150 250 C 230 250 250 300 262 340 C 280 392 330 400 360 380 C 400 356 430 380 470 420 L 520 420 L 520 120 L 150 120 Z" fill="{T}"/>'
                 f'<path d="M 300 214 C 312 240 318 262 318 290 M 350 206 C 366 234 372 256 372 282 M 404 222 C 418 246 426 268 428 296 M 452 270 C 462 290 466 306 468 330" '
                 f'fill="none" stroke="{TD}" stroke-width="16" stroke-linecap="round"/></g>')
    parts.append(f'<path d="{ear_front}" fill="{T}"/>')
    parts.append(f'<path d="M 214 250 C 212 204 216 162 228 130 C 252 150 272 174 286 200 Z" fill="{P}" clip-path="url(#ce)"/>')
    # Nase (braun-rosa), Nasenrücken-Fleck
    parts.append(f'<path d="M 130 356 C 124 344 132 334 146 336 C 158 338 164 346 160 358 C 156 368 146 374 140 374 C 134 372 132 364 130 356 Z" '
                 f'fill="{N}" stroke="{K}" stroke-width="10" stroke-linejoin="round"/>')
    # Zunge (Blep) + Mund
    parts.append(f'<path d="M 150 404 C 148 424 156 440 170 440 C 184 440 188 424 184 406 Z" fill="{P}" stroke="{K}" stroke-width="10" stroke-linejoin="round"/>')
    parts.append(f'<path d="M 128 392 C 140 406 160 408 176 398 C 188 408 204 408 214 398" fill="none" stroke="{K}" stroke-width="10" stroke-linecap="round"/>')
    # geschlossenes Auge (Sichel wie beim Vorbild)
    parts.append(f'<path d="M 214 318 C 226 352 262 362 290 344 C 262 354 236 346 222 312 Z" fill="{K}" stroke="{K}" stroke-width="8" stroke-linejoin="round"/>')
    # Schnurrhaare
    parts.append(f'<path d="M 204 388 C 240 384 272 388 300 398 M 202 404 C 234 408 262 418 286 432" fill="none" stroke="{K}" stroke-width="7" stroke-linecap="round"/>')
    # Brustfell-Linie
    parts.append(f'<path d="M 322 590 C 328 640 328 690 322 724" fill="none" stroke="{K}" stroke-width="10" stroke-linecap="round"/>')
    parts.append(f'<path d="M 404 740 C 400 690 430 660 474 664" fill="none" stroke="{K}" stroke-width="12" stroke-linecap="round"/>')
    # Zehen
    parts.append(f'<path d="M 268 740 L 268 758 M 300 740 L 300 758" fill="none" stroke="{K}" stroke-width="8" stroke-linecap="round"/>')
    # Krone (schräg auf dem Kopf)
    crown = (f'<g transform="translate(318 168) rotate(10)">'
             f'<path d="M -86 22 L -98 -64 L -48 -22 L 0 -92 L 48 -22 L 98 -64 L 86 22 Z" fill="{G}" stroke="{K}" stroke-width="{SW}" stroke-linejoin="round"/>'
             f'<rect x="-90" y="4" width="180" height="34" rx="12" fill="{G}" stroke="{K}" stroke-width="{SW}"/>'
             + "".join(f'<circle cx="{x}" cy="21" r="8" fill="{K}"/>' for x in (-54, -18, 18, 54))
             + "".join(f'<circle cx="{x}" cy="{y}" r="15" fill="{G}" stroke="{K}" stroke-width="12"/>' for x, y in ((-98, -64), (0, -92), (98, -64)))
             + f'<path d="M 0 -130 L 0 -104 M -12 -120 L 12 -120" stroke="{K}" stroke-width="10" stroke-linecap="round"/>'
             + '</g>')
    parts.append(crown)
    return "\n".join(parts)

def svg(w, h, view, bg=None):
    return f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="{view}">{cat(bg)}</svg>'

if __name__ == "__main__":
    s = svg(600, 800, "40 -10 640 820")
    open("lola.svg", "w").write(s)
    cairosvg.svg2png(bytestring=s.encode(), write_to="lola_preview.png", output_width=600)
