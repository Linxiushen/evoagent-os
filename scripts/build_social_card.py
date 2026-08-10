from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "docs" / "assets" / "evoagent-workflow.png"
OUTPUT = ROOT / "docs" / "assets" / "evoagent-social-card.png"


def load_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    names = (
        [
            Path("C:/Windows/Fonts/segoeuib.ttf"),
            Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
        ]
        if bold
        else [
            Path("C:/Windows/Fonts/segoeui.ttf"),
            Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
        ]
    )
    for name in names:
        if name.is_file():
            return ImageFont.truetype(str(name), size=size)
    return ImageFont.load_default()


def build() -> Path:
    if not SOURCE.is_file():
        raise FileNotFoundError(f"Capture the workflow console first: {SOURCE}")

    canvas = Image.new("RGB", (1280, 640), "#171d1c")
    draw = ImageDraw.Draw(canvas)
    draw.rectangle((328, 0, 1280, 640), fill="#eef2f0")

    screenshot = Image.open(SOURCE).convert("RGB")
    screenshot = ImageOps.fit(
        screenshot,
        (914, 574),
        method=Image.Resampling.LANCZOS,
        centering=(0.63, 0.42),
    )
    canvas.paste(screenshot, (346, 33))
    draw.rounded_rectangle((345, 32, 1261, 608), radius=8, outline="#b8c5c0", width=2)

    draw.rounded_rectangle((40, 42, 98, 100), radius=8, fill="#d9f1e8")
    draw.text((69, 70), "E", anchor="mm", fill="#0e624e", font=load_font(30, bold=True))
    draw.text((40, 136), "EvoAgent", fill="#f5f8f7", font=load_font(39, bold=True))
    draw.text((40, 181), "OS", fill="#65c8a9", font=load_font(39, bold=True))

    draw.text((40, 258), "Durable.", fill="#f5f8f7", font=load_font(24, bold=True))
    draw.text((40, 293), "Governed.", fill="#f5f8f7", font=load_font(24, bold=True))
    draw.text((40, 328), "Auditable.", fill="#f5f8f7", font=load_font(24, bold=True))
    draw.text(
        (40, 385),
        "Agent teams with leases,\nbudgets, approvals, artifacts,\nand regression gates.",
        fill="#aeb8b5",
        font=load_font(16),
        spacing=8,
    )

    labels = ["Runtime", "Fleet", "Forge", "Trace", "Realtime"]
    x, y = 40, 518
    for label in labels:
        bounds = draw.textbbox((0, 0), label, font=load_font(12, bold=True))
        width = bounds[2] - bounds[0] + 22
        if x + width > 308:
            x, y = 40, y + 38
        draw.rounded_rectangle((x, y, x + width, y + 28), radius=5, fill="#27312e")
        draw.text((x + 11, y + 7), label, fill="#d9e2df", font=load_font(12, bold=True))
        x += width + 8

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(OUTPUT, optimize=True)
    return OUTPUT


if __name__ == "__main__":
    print(build())
