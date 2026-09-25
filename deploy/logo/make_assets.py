"""Erzeugt icon.png (128×128), logo.png (250×100) und Varianten aus lola.svg."""
import cairosvg, io
from PIL import Image, ImageDraw, ImageFont, ImageFilter

FONT = "/usr/share/fonts/truetype/google-fonts/Poppins-Bold.ttf"

def render():
    svg = open("lola.svg").read()
    big = Image.open(io.BytesIO(cairosvg.svg2png(bytestring=svg.encode(), output_width=1360))).convert("RGBA")
    return big.crop(big.getbbox())

def halo(img, px):
    pad = px + 2
    canvas = Image.new("RGBA", (img.size[0] + 2 * pad, img.size[1] + 2 * pad), (0, 0, 0, 0))
    canvas.paste(img, (pad, pad), img)
    a = canvas.split()[3].point(lambda v: 255 if v > 20 else 0)
    grown = a.filter(ImageFilter.MaxFilter(2 * px + 1)).filter(ImageFilter.GaussianBlur(1.2))
    white = Image.new("RGBA", canvas.size, (255, 255, 255, 255))
    white.putalpha(grown)
    white.alpha_composite(canvas)
    return white

def fit(img, box, pad):
    w, h = img.size
    s = min((box[0] - 2 * pad) / w, (box[1] - 2 * pad) / h)
    im = img.resize((max(1, round(w * s)), max(1, round(h * s))), Image.LANCZOS)
    out = Image.new("RGBA", box, (0, 0, 0, 0))
    out.paste(im, ((box[0] - im.size[0]) // 2, (box[1] - im.size[1]) // 2), im)
    return out

def logo(fig, scale):
    W, H = 250 * scale, 100 * scale
    out = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    f = fit(fig, (round(H * 0.84), H), scale)
    out.paste(f, (0, 0), f)
    d = ImageDraw.Draw(out)
    font = ImageFont.truetype(FONT, 42 * scale)
    x, y, sw = f.size[0] + 3 * scale, H // 2 - 32 * scale, 4 * scale
    d.text((x, y), "Tri", font=font, fill="#1d1d1b", stroke_width=sw, stroke_fill="#ffffff")
    d.text((x + d.textlength("Tri", font=font), y), "Lola", font=font, fill="#3f7a6e", stroke_width=sw,
           stroke_fill="#ffffff")
    return out

if __name__ == "__main__":
    fig = halo(render(), 34)
    fig.save("figure_sticker.png")
    fit(fig, (512, 512), 8).save("icon_512.png")
    fit(fig, (128, 128), 2).save("icon.png")
    fit(fig, (64, 64), 1).save("favicon_64.png")
    logo(fig, 1).save("logo.png")
    logo(fig, 2).save("logo_500.png")
    sheet = Image.new("RGBA", (620, 520), "#ffffff")
    sheet.paste(Image.new("RGBA", (620, 260), "#1c1c1c"), (0, 260))
    for y0 in (0, 260):
        for path, pos, size in (("logo_500.png", (20, 30), None), ("icon.png", (500, 70), None), ("icon.png", (540, 200), 48)):
            im = Image.open(path)
            if size:
                im = im.resize((size, size), Image.LANCZOS)
            sheet.paste(im, (pos[0], pos[1] + y0), im)
    sheet.save("preview_assets.png")
