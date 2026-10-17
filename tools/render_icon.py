"""Render the editable SVG into a multi-resolution Windows ICO using Qt."""

from pathlib import Path
import struct

from PySide6.QtCore import QBuffer, QIODevice, Qt
from PySide6.QtGui import QImage, QPainter
from PySide6.QtSvg import QSvgRenderer

ROOT = Path(__file__).resolve().parent.parent


def main():
    assets = ROOT / "checkin" / "assets"
    renderer = QSvgRenderer(str(assets / "app.svg"))
    if not renderer.isValid():
        raise ValueError("Invalid application SVG")
    sizes = (16, 20, 24, 32, 40, 48, 64, 96, 128, 256)
    frames = []
    for size in sizes:
        image = QImage(size, size, QImage.Format.Format_ARGB32)
        image.fill(Qt.GlobalColor.transparent)
        painter = QPainter(image)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        renderer.render(painter)
        painter.end()
        buffer = QBuffer()
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        if not image.save(buffer, "PNG"):
            raise RuntimeError("Icon PNG encoding failed")
        frames.append(bytes(buffer.data()))
    offset = 6 + len(sizes) * 16
    directory = bytearray(struct.pack("<HHH", 0, 1, len(sizes)))
    for size, frame in zip(sizes, frames):
        directory.extend(
            struct.pack(
                "<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32, len(frame), offset
            )
        )
        offset += len(frame)
    (assets / "app.ico").write_bytes(directory + b"".join(frames))
    preview = ROOT / "artifacts" / "icon.png"
    preview.parent.mkdir(parents=True, exist_ok=True)
    image = QImage(512, 512, QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    renderer.render(painter)
    painter.end()
    image.save(str(preview))
    print("Generated app.ico (16–256 px) and artifacts/icon.png")


if __name__ == "__main__":
    main()
