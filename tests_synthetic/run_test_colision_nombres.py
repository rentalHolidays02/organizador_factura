"""Verifica que _nombre_sin_colision no pisa archivos con el mismo nombre."""
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE.parent))

import core  # noqa: E402


def main():
    with tempfile.TemporaryDirectory() as tmp:
        dest_dir = Path(tmp)

        libre = core._nombre_sin_colision(dest_dir, "10.pdf")
        assert libre == "10.pdf", f"primer archivo debería quedarse igual, dio {libre!r}"

        (dest_dir / "10.pdf").write_text("factura A")
        segundo = core._nombre_sin_colision(dest_dir, "10.pdf")
        assert segundo == "10 (2).pdf", f"esperaba '10 (2).pdf', dio {segundo!r}"

        (dest_dir / "10 (2).pdf").write_text("factura B")
        tercero = core._nombre_sin_colision(dest_dir, "10.pdf")
        assert tercero == "10 (3).pdf", f"esperaba '10 (3).pdf', dio {tercero!r}"

        assert (dest_dir / "10.pdf").read_text() == "factura A", "no debe pisar el original"

    print("OK: nombres duplicados no se pisan entre sí.")


if __name__ == "__main__":
    main()
