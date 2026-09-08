"""Verifica que el resultado sobrevive a recargar la página / reiniciar Streamlit.

Corre app.py en el runner headless de Streamlit (AppTest): cada instancia es una sesión
nueva, igual que abrir la pestaña de cero, así que si el resumen aparece sin volver a
subir el ZIP es que el estado se está leyendo de .sesion/estado.json.
"""
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

from streamlit.testing.v1 import AppTest

HERE = Path(__file__).parent
APP = HERE.parent / "app.py"
# Sesión de usar y tirar: la de verdad vive en .sesion/ y puede tener medio trimestre
# clasificado a mano.
WORK_DIR = Path(tempfile.mkdtemp(prefix="test_sesion_"))
os.environ["ORGANIZADOR_SESION"] = str(WORK_DIR)

sys.path.insert(0, str(HERE.parent))
import core  # noqa: E402


def main():
    output_dir = WORK_DIR / "salida"
    detalle, properties = core.process_invoices(
        HERE / "facturas_test.zip", core.load_properties(HERE / "propiedades_test.csv"), output_dir
    )
    pendiente = next(d["archivo"] for d in detalle if d["propiedad"] == "Sin identificar")
    # Dos propiedades: es el caso de la factura de mantenimiento de piscinas, que cubre
    # varias villas en la misma hoja.
    marcada = [p["nombre"] for p in properties[:2]]
    (WORK_DIR / "estado.json").write_text(
        json.dumps(
            {
                "detalle": detalle,
                "properties": properties,
                "work_dir": str(WORK_DIR),
                "output_dir": str(output_dir),
                "identificadores_nuevos": [],
                "elecciones": {pendiente: marcada},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    try:
        at = AppTest.from_file(str(APP), default_timeout=120).run()
        restaurado = at.session_state["resultado"] if "resultado" in at.session_state else None
        checks = [
            ("La app arranca sin excepción", not at.exception),
            ("Restaura el resultado sin volver a subir el ZIP", restaurado is not None),
            ("Muestra el resumen por propiedad",
             any("Resumen por propiedad" in s.value for s in at.subheader)),
            ("Restaura las mismas facturas que se procesaron",
             len((restaurado or {}).get("detalle", [])) == len(detalle)),
            (f"Recuerda las {len(marcada)} propiedades marcadas de {pendiente}",
             any(m.label == "Pertenece a" and m.value == marcada for m in at.multiselect)),
        ]

        # Reparto: el documento va a la carpeta de cada propiedad y el gasto se parte, que
        # es lo que suma después el resumen.
        importe_original = next(d["importe"] for d in detalle if d["archivo"] == pendiente)
        next(b for b in at.button if b.label == "Asignar").click().run()

        filas = [d for d in at.session_state["resultado"]["detalle"] if d["archivo"] == pendiente]
        carpetas = [next(p["carpeta"] for p in properties if p["nombre"] == n) for n in marcada]
        checks += [
            ("Repartir entre 2 propiedades deja una fila de gasto por propiedad",
             len(filas) == 2 and {f["propiedad"] for f in filas} == set(marcada)),
            ("El importe se reparte sin perderse por el camino",
             round(sum(f["importe"] for f in filas), 2) == round(importe_original or 0.0, 2)),
            ("El PDF queda archivado en la carpeta de las dos propiedades",
             all(list((output_dir / c).rglob(pendiente)) for c in carpetas)),
            ("No queda la factura en Sin_identificar",
             not list((output_dir / "Sin_identificar").rglob(pendiente))),
        ]
    finally:
        shutil.rmtree(WORK_DIR, ignore_errors=True)

    print("\n=== CHECKS ===")
    for desc, cond in checks:
        print(f"[{'OK' if cond else 'FALLO'}] {desc}")
    if not all(cond for _, cond in checks):
        sys.exit(1)


if __name__ == "__main__":
    main()
