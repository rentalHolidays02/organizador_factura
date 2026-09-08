"""Organiza un lote de facturas por propiedad y genera el informe de gastos, sin UI.

    python organizar_facturas.py <origen> <propiedades.csv> [-s salida] [-i identificadores.csv]

<origen> puede ser el ZIP tal cual llega o la carpeta ya descomprimida.
"""
import argparse
from pathlib import Path

import core


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("origen", help="ZIP o carpeta con las facturas (PDF/JPG/PNG)")
    parser.add_argument("propiedades", help="CSV de propiedades (ref/direccion/nombre)")
    parser.add_argument("-s", "--salida", default="facturas_organizadas", help="carpeta de salida")
    parser.add_argument(
        "-i",
        "--identificadores",
        help="CSV identificador->propiedad (CUPS, nº de contrato, de contador). "
        "Es lo único que permite clasificar las facturas de suministros.",
    )
    parser.add_argument(
        "-u", "--umbral", type=int, default=core.DEFAULT_FUZZY_THRESHOLD, help="umbral de match difuso"
    )
    args = parser.parse_args()

    salida = Path(args.salida)
    procesadas = {"n": 0}

    def avisar(nombre: str) -> None:
        procesadas["n"] += 1
        print(f"  [{procesadas['n']:>3}] {nombre}")

    print(f"Procesando {args.origen} ...")
    detalle, _ = core.process_invoices(
        args.origen,
        core.load_properties(args.propiedades),
        salida,
        threshold=args.umbral,
        on_file_processed=avisar,
        identifier_csv=args.identificadores,
    )

    resumen = core.build_resumen(detalle)
    core.build_excel_report(detalle, resumen, salida / "informe_gastos.xlsx")

    sin_identificar = [d for d in detalle if d["propiedad"] == "Sin identificar"]
    sin_importe = [d for d in detalle if d["importe"] is None]

    print(f"\n{resumen.to_string(index=False)}")
    print(f"\n{len(detalle)} facturas procesadas.")
    if sin_identificar:
        print(f"{len(sin_identificar)} en 'Sin_identificar': revísalas y clasifícalas a mano.")
    if sin_importe:
        print(f"{len(sin_importe)} sin importe legible (cuentan como 0 en el resumen):")
        for d in sin_importe:
            print(f"    {d['archivo']}")
    print(f"\nSalida en {salida.resolve()}")


if __name__ == "__main__":
    main()
