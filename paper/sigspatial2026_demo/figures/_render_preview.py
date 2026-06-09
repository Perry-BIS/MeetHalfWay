"""Render a PNG preview of each PPT slide using PIL.

Used for visual QA of the annotated PPT (no LibreOffice/PowerPoint required).
Keeps the same image-area and label-column geometry as _build_pptx.py.
"""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

FIG = Path(__file__).resolve().parent
COVER_PNG  = FIG / "cover.png"
RESULT_PNG = FIG / "result_map.png"

# slide geometry (must match _build_pptx.py)
SLIDE_W_IN, SLIDE_H_IN = 13.333, 7.5
IMG_X_IN, IMG_Y_IN     = 0.40, 0.95
IMG_W_IN, IMG_H_IN     = 7.50, 5.36
LABEL_X_IN, LABEL_W_IN = 8.10, 5.00
PX_W_SRC, PX_H_SRC     = 2800, 2000

DPI = 96
def in2px(v):  return int(round(v * DPI))

SLIDE_W = in2px(SLIDE_W_IN)
SLIDE_H = in2px(SLIDE_H_IN)
IMG_X   = in2px(IMG_X_IN);   IMG_Y = in2px(IMG_Y_IN)
IMG_W   = in2px(IMG_W_IN);   IMG_H = in2px(IMG_H_IN)
LABEL_X = in2px(LABEL_X_IN); LABEL_W = in2px(LABEL_W_IN)

ORANGE   = (0xE1, 0x5A, 0x1E, 255)
DARK     = (0x36, 0x45, 0x4F, 255)
SUB_GRAY = (0x55, 0x55, 0x55, 255)
WHITE    = (0xFF, 0xFF, 0xFF, 255)


def srcpx_to_slide(px, py):
    """Source image pixel -> slide pixel."""
    x = IMG_X + (px / PX_W_SRC) * IMG_W
    y = IMG_Y + (py / PX_H_SRC) * IMG_H
    return x, y


def try_font(name, size):
    try:
        return ImageFont.truetype(name, size)
    except Exception:
        return ImageFont.load_default()


FONT_TITLE   = try_font("calibrib.ttf", 26)
FONT_LABEL   = try_font("calibrib.ttf", 18)
FONT_SUB     = try_font("calibrii.ttf", 13)


def draw_dashed_rect(draw, x0, y0, x1, y1, color, width=2, dash=10, gap=5):
    """Draw a dashed rectangle."""
    # Top
    x = x0
    while x < x1:
        x_end = min(x + dash, x1)
        draw.line([(x, y0), (x_end, y0)], fill=color, width=width)
        x = x_end + gap
    # Bottom
    x = x0
    while x < x1:
        x_end = min(x + dash, x1)
        draw.line([(x, y1), (x_end, y1)], fill=color, width=width)
        x = x_end + gap
    # Left
    y = y0
    while y < y1:
        y_end = min(y + dash, y1)
        draw.line([(x0, y), (x0, y_end)], fill=color, width=width)
        y = y_end + gap
    # Right
    y = y0
    while y < y1:
        y_end = min(y + dash, y1)
        draw.line([(x1, y), (x1, y_end)], fill=color, width=width)
        y = y_end + gap


def text_wrap(text, font, max_w):
    """Greedy word wrap by pixel width using ImageDraw measurement."""
    if not text:
        return [""]
    # Create a temp draw for measurement
    tmp = Image.new("RGBA", (10, 10))
    d = ImageDraw.Draw(tmp)
    words = text.split()
    lines, cur = [], ""
    for w in words:
        trial = (cur + " " + w).strip()
        bbox = d.textbbox((0, 0), trial, font=font)
        if bbox[2] - bbox[0] <= max_w:
            cur = trial
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def render_slide(title, picture_path, annotations, out_path):
    """annotations: list of (px1,py1,px2,py2, label_y_in, head, sub)."""
    img = Image.new("RGBA", (SLIDE_W, SLIDE_H), WHITE)
    draw = ImageDraw.Draw(img)

    # Title
    draw.text((IMG_X, in2px(0.20)), title, fill=DARK, font=FONT_TITLE)

    # Picture
    pic = Image.open(picture_path).convert("RGBA")
    pic = pic.resize((IMG_W, IMG_H), Image.LANCZOS)
    img.paste(pic, (IMG_X, IMG_Y), pic)

    # Annotations
    for px1, py1, px2, py2, ly_in, head, sub in annotations:
        x0, y0 = srcpx_to_slide(px1, py1)
        x1, y1 = srcpx_to_slide(px2, py2)
        x0, y0, x1, y1 = map(int, (x0, y0, x1, y1))

        # Dashed callout rectangle on screenshot
        draw_dashed_rect(draw, x0, y0, x1, y1, ORANGE, width=3, dash=12, gap=6)

        # Label text and subtext
        label_x  = LABEL_X
        label_y  = in2px(ly_in)
        label_w  = LABEL_W

        # head (bold orange) — wrap if needed
        head_lines = text_wrap(head, FONT_LABEL, label_w)
        sub_lines  = text_wrap(sub,  FONT_SUB,   label_w)
        y_cursor = label_y
        for line in head_lines:
            draw.text((label_x, y_cursor), line, fill=ORANGE, font=FONT_LABEL)
            bbox = draw.textbbox((0, 0), line, font=FONT_LABEL)
            y_cursor += (bbox[3] - bbox[1]) + 2
        y_cursor += 4
        for line in sub_lines:
            draw.text((label_x, y_cursor), line, fill=SUB_GRAY, font=FONT_SUB)
            bbox = draw.textbbox((0, 0), line, font=FONT_SUB)
            y_cursor += (bbox[3] - bbox[1]) + 2

        # Connector line from box right-mid -> label left-mid
        box_rx, box_ry = x1, (y0 + y1) // 2
        lbl_lx, lbl_ly = label_x, label_y + (y_cursor - label_y) // 2
        draw.line([(box_rx, box_ry), (lbl_lx, lbl_ly)], fill=ORANGE, width=3)

    img.convert("RGB").save(out_path, "PNG")
    print(f"saved: {out_path}")


# ---------- annotation tables (same as PPT) ----------
ANNS_S1 = [
    (40, 30, 660, 500, 1.05,
     "Project title and positioning",
     "Project name and tagline frame the system as a privacy-first meeting-place planner."),
    (790, 30, 1280, 500, 2.20,
     "Design principles at a glance",
     "Core goal, privacy policy, recommendation target, and interaction model presented as summary cards."),
    (40, 540, 1300, 1090, 3.35,
     "Six-step privacy-separated workflow",
     "From private check-in to commute radius, overlap search, voting, time alignment, and final suggestion."),
    (40, 1100, 1300, 1450, 4.50,
     "Privacy guarantees",
     "Participants never exchange raw coordinates with one another; only outcomes are shown."),
    (1080, 1470, 1300, 1580, 5.65,
     "Entry point into the meeting query",
     "The Next button advances the user from the landing page to the participant input flow."),
]

ANNS_S2 = [
    (350, 150, 850, 450, 1.05,
     "Shared feasible region",
     "Green shaded polygon is the intersection of the two travel-time isochrones - the jointly reachable area."),
    (1380, 380, 1920, 650, 2.20,
     "Ranked candidate venues",
     "Green pins denote candidate POIs retrieved inside the shared region and ranked by the fairness score."),
    (2140, 470, 2270, 620, 3.35,
     "Participant B starting location",
     "Blue pin marks the second participant's geocoded starting point."),
    (1280, 800, 1410, 980, 4.50,
     "Geometric midpoint baseline",
     "Magenta pin shows the naive midpoint a non-fairness-aware tool would return, for contrast."),
    (170, 1100, 300, 1270, 5.65,
     "Participant A starting location",
     "Red pin marks the first participant's geocoded starting point."),
]

render_slide("MeetHalfway Demo Interface  -  Home Page",
             COVER_PNG, ANNS_S1, FIG / "_preview_slide1.png")
render_slide("MeetHalfway Demo Result  -  Shared Feasible Region & Candidates",
             RESULT_PNG, ANNS_S2, FIG / "_preview_slide2.png")
