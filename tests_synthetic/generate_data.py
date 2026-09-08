"""Genera facturas sintéticas y el CSV de propiedades para probar core.py."""
import zipfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

HERE = Path(__file__).parent
DATA_DIR = HERE / "data"


def make_pdf_invoice(path: Path, lines: list[str]) -> None:
    c = canvas.Canvas(str(path), pagesize=A4)
    width, height = A4
    y = height - 80
    for line in lines:
        c.drawString(60, y, line)
        y -= 22
    c.save()


def make_scanned_image_invoice(path: Path, lines: list[str]) -> None:
    """Simula una factura escaneada: texto renderizado sobre una imagen (no PDF con texto)."""
    img = Image.new("RGB", (1000, 700), color="white")
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("arial.ttf", 28)
    except OSError:
        font = ImageFont.load_default()
    y = 40
    for line in lines:
        draw.text((40, y), line, fill="black", font=font)
        y += 45
    img.save(path)


def build():
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    # 1) Factura con la referencia de la propiedad en el NOMBRE del archivo (match por nombre)
    make_pdf_invoice(
        DATA_DIR / "Factura_APT-CENTRO_junio.pdf",
        [
            "Suministros Electricos SL",
            "Factura N. 2026-0111",
            "Cliente: Comunidad propietarios",
            "Concepto: Electricidad junio",
            "Total a pagar: 128,45 EUR",
        ],
    )

    # 2) Factura sin referencia en el nombre, pero con la DIRECCION en el contenido (PDF, match por contenido exacto)
    make_pdf_invoice(
        DATA_DIR / "scan0042.pdf",
        [
            "Fontaneria Rodriguez e Hijos",
            "Factura N. F-8821",
            "Direccion de la obra: Avenida del Mar 45 Valencia",
            "Concepto: Reparacion grifo cocina",
            "Importe total: 1.234,56 EUR",
        ],
    )

    # 3) Factura como IMAGEN escaneada, con la referencia en el contenido (match por OCR + contenido)
    make_scanned_image_invoice(
        DATA_DIR / "IMG_20260610_093015.png",
        [
            "Limpiezas Norte SL",
            "Factura numero 559",
            "Propiedad ref: CASA-PLAYA",
            "Concepto: Limpieza fin de estancia",
            "Total factura: 90,00 EUR",
        ],
    )

    # 4) Factura que no coincide con ninguna propiedad (debe caer en Sin_identificar)
    make_pdf_invoice(
        DATA_DIR / "recibo_varios_123.pdf",
        [
            "Papeleria Central",
            "Ticket de compra",
            "Material de oficina",
            "Total: 15,20 EUR",
        ],
    )

    zip_path = HERE / "facturas_test.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in DATA_DIR.iterdir():
            zf.write(f, f.name)

    csv_path = HERE / "propiedades_test.csv"
    csv_path.write_text(
        "ref,direccion\n"
        "APT-CENTRO,Calle Mayor 12 3B Madrid\n"
        "CASA-PLAYA,Avenida del Mar Chico 8 Alicante\n"
        "CASA-VALENCIA,Avenida del Mar 45 Valencia\n",
        encoding="utf-8",
    )

    print(f"ZIP generado: {zip_path}")
    print(f"CSV generado: {csv_path}")


if __name__ == "__main__":
    build()
