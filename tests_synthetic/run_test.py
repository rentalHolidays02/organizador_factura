"""Ejecuta core.py directamente sobre los datos sintéticos y verifica el resultado."""
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE.parent))

import pytesseract  # noqa: E402

import core  # noqa: E402

OUTPUT_DIR = HERE / "salida_test"


def tesseract_disponible() -> bool:
    try:
        pytesseract.get_tesseract_version()
        return True
    except Exception as exc:
        print(f"[AVISO] Tesseract no disponible en este entorno: {exc}")
        return False


def main():
    if OUTPUT_DIR.exists():
        shutil.rmtree(OUTPUT_DIR)

    ocr_ok = tesseract_disponible()

    detalle, properties = core.process_invoices(
        HERE / "facturas_test.zip",
        core.load_properties(HERE / "propiedades_test.csv"),
        OUTPUT_DIR,
    )
    resumen_df = core.build_resumen(detalle)
    core.build_excel_report(detalle, resumen_df, OUTPUT_DIR / "informe_gastos.xlsx")

    print("\n=== DETALLE ===")
    for d in detalle:
        print(d)

    print("\n=== RESUMEN ===")
    print(resumen_df.to_string(index=False))

    by_file = {d["archivo"]: d for d in detalle}
    checks = []

    def check(desc, condition):
        checks.append((desc, condition))

    check(
        "Match por NOMBRE de archivo (Factura_APT-CENTRO_junio.pdf -> APT-CENTRO)",
        by_file["Factura_APT-CENTRO_junio.pdf"]["propiedad"] == "APT-CENTRO"
        and by_file["Factura_APT-CENTRO_junio.pdf"]["metodo_match"] == "nombre_archivo (exacto)",
    )
    check(
        "Match por CONTENIDO de PDF (scan0042.pdf -> CASA-VALENCIA, via direccion)",
        by_file["scan0042.pdf"]["propiedad"] == "CASA-VALENCIA"
        and "contenido" in by_file["scan0042.pdf"]["metodo_match"],
    )
    check(
        "Factura sin coincidencia -> Sin_identificar (recibo_varios_123.pdf)",
        by_file["recibo_varios_123.pdf"]["propiedad"] == "Sin identificar",
    )
    check(
        "Importe extraido correctamente (Factura_APT-CENTRO_junio.pdf -> 128.45)",
        by_file["Factura_APT-CENTRO_junio.pdf"]["importe"] == 128.45,
    )
    check(
        "Importe extraido correctamente (scan0042.pdf -> 1234.56, formato miles+decimales es)",
        by_file["scan0042.pdf"]["importe"] == 1234.56,
    )

    if ocr_ok:
        check(
            "Match por CONTENIDO via OCR de imagen (IMG_...png -> CASA-PLAYA)",
            by_file["IMG_20260610_093015.png"]["propiedad"] == "CASA-PLAYA"
            and "contenido" in by_file["IMG_20260610_093015.png"]["metodo_match"],
        )
        check(
            "Importe extraido via OCR (IMG_...png -> 90.0)",
            by_file["IMG_20260610_093015.png"]["importe"] == 90.0,
        )
    else:
        print(
            "\n[SALTADO] Tesseract no está instalado en esta máquina de desarrollo "
            "(no se pudo instalar sin una sesión interactiva/UAC). Los checks de OCR "
            "no se pueden verificar aquí; el resto del pipeline (PDF, matching por "
            "nombre, Sin_identificar, importes) sí queda verificado arriba. "
            f"Resultado real para la imagen: {by_file['IMG_20260610_093015.png']}"
        )

    # Una factura de mantenimiento (piscinas, jardines) cubre varias propiedades en una hoja.
    texto_multi = core.normalize_text(
        "Trabajos realizados en (APT-CENTRO, CASA-PLAYA Y CASA-VALENCIA)"
    )
    detectadas = {p["nombre"] for p in core.detect_properties(texto_multi, properties)}
    check(
        "detect_properties encuentra las 3 propiedades que nombra una sola factura",
        detectadas == {"APT-CENTRO", "CASA-PLAYA", "CASA-VALENCIA"},
    )
    check(
        "detect_properties no inventa propiedades en una factura que no nombra ninguna",
        core.detect_properties(core.normalize_text("Factura de suministro electrico"), properties)
        == [],
    )

    # En Windows, abrir el PDF por ruta deja el handle vivo mientras exista la imagen
    # devuelta, y entonces falla el shutil.move con el que la UI archiva la factura revisada.
    muestra = next(OUTPUT_DIR.rglob("*.pdf"))
    core.render_preview(muestra)
    movido = muestra.with_name(muestra.name + ".movido")
    try:
        shutil.move(str(muestra), str(movido))
        shutil.move(str(movido), str(muestra))
        sin_bloqueo = True
    except PermissionError:
        sin_bloqueo = False
    check("render_preview no bloquea el archivo (se puede mover despues)", sin_bloqueo)

    print("\n=== CHECKS ===")
    all_ok = True
    for desc, cond in checks:
        status = "OK" if cond else "FALLO"
        if not cond:
            all_ok = False
        print(f"[{status}] {desc}")

    print(f"\nArchivos organizados en {OUTPUT_DIR}:")
    for p in sorted(OUTPUT_DIR.rglob("*")):
        if p.is_file():
            print(f"  {p.relative_to(OUTPUT_DIR)}")

    if not all_ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
