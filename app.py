"""UI Streamlit para organizador-facturas."""
import json
import os
import shutil
import threading
from pathlib import Path

import pandas as pd
import streamlit as st

import core

GASTO_EMPRESA = "Gastos_empresa"

# Carpeta de trabajo fija en vez de un tempdir: recargar la página abre una sesión nueva de
# Streamlit y reiniciarlo vacía la memoria, y volver a pasar el OCR por todo el lote cuesta
# minutos. Aquí quedan las facturas ya organizadas y el estado de la revisión a mano.
WORK_DIR = Path(os.environ.get("ORGANIZADOR_SESION") or Path(__file__).parent / ".sesion")
ESTADO_JSON = WORK_DIR / "estado.json"


def _guardar_estado(resultado: dict) -> None:
    # Escritura atomica: con varias personas revisando a la vez, un guardado a medias dejaria
    # el JSON ilegible para el siguiente arranque.
    tmp = ESTADO_JSON.with_suffix(".tmp")
    tmp.write_text(json.dumps(resultado, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, ESTADO_JSON)


@st.cache_resource
def _compartido() -> dict:
    """Streamlit da un session_state por pestana: con dos personas revisando el mismo lote eso
    son dos copias del mismo estado y la ultima en guardar pisa a la otra. El proceso es uno solo
    para todas las sesiones, asi que el estado vive aqui y el lock serializa los cambios."""
    return {"resultado": None, "lock": threading.Lock()}


@st.cache_data(show_spinner="Leyendo la factura...")
def _texto_factura(ruta: str) -> str:
    """Solo se lee la factura que estás mirando, y una sola vez: un escaneo cuesta ~5 s de OCR
    por página y no tiene sentido pagarlo por las 70 a la vez."""
    try:
        return core.extract_invoice_text(Path(ruta))
    except Exception:
        return ""

def _secreto(nombre: str) -> str:
    """En Streamlit Cloud las claves llegan por st.secrets, en local por variable de entorno."""
    try:
        return os.environ.get(nombre) or st.secrets[nombre]
    except Exception:
        return ""


@st.cache_data(ttl=3600, show_spinner="Cargando propiedades de Lodgify...")
def _propiedades_lodgify(api_key: str) -> list[dict]:
    return core.load_properties_from_api(api_key)


GROQ_API_KEY = _secreto("GROQ_API_KEY")
LODGIFY_API_KEY = _secreto("LODGIFY_API_KEY")
compartido = _compartido()

st.set_page_config(page_title="Organizador de facturas", layout="wide")
st.title("Organizador de facturas por propiedad")
if GROQ_API_KEY:
    st.caption(
        "Procesamiento local. Las facturas que no se identifican solas se mandan a Groq (IA) "
        "como último recurso — es lo único que sale de tu máquina."
    )
else:
    st.caption("Todo el procesamiento ocurre en local: nada se sube a servicios externos.")

properties = []
if not LODGIFY_API_KEY:
    st.error("Falta configurar el secreto LODGIFY_API_KEY.")
else:
    try:
        properties = _propiedades_lodgify(LODGIFY_API_KEY)
        st.caption(f"{len(properties)} propiedades cargadas de Lodgify.")
    except Exception as exc:
        st.error(f"No se pudieron cargar las propiedades de Lodgify: {exc}")

col1, col2 = st.columns(2)
with col1:
    zip_file = st.file_uploader("ZIP de facturas (PDF/JPG/PNG)", type=["zip"])
with col2:
    ident_file = st.file_uploader(
        "CSV de identificadores (opcional)",
        type=["csv"],
        help="Tabla identificador -> propiedad (CUPS, nº de contrato, de contador). "
        "Es lo único que permite clasificar las facturas de suministros.",
    )

threshold = st.slider(
    "Umbral de coincidencia difusa (fuzzy match)",
    min_value=50,
    max_value=100,
    value=core.DEFAULT_FUZZY_THRESHOLD,
    help="Por debajo de este porcentaje de similitud, una factura no se asigna por fuzzy match.",
)

ruta_carpeta = st.text_input(
    "...o ruta de una carpeta ya en disco",
    help="Sirve una unidad de Google Drive montada con Drive para escritorio "
    "(por ejemplo G:/Mi unidad/facturas/2025-Q4). Se lee de ahi; los originales no se tocan.",
)

ruta_salida = st.text_input(
    "Carpeta de Drive donde guardar el resultado (opcional)",
    help="Si la dejas vacía, el resultado solo queda en el ZIP de descarga. Rellénala "
    "(p.ej. G:/Mi unidad/facturas/salida/2025-2026) para que quede ya sincronizado en Drive.",
)

origen_ok = bool(zip_file) or bool(ruta_carpeta.strip())
procesar = st.button("Procesar facturas", disabled=not (origen_ok and properties))

if procesar and origen_ok and properties:
    fuente_carpeta = Path(ruta_carpeta.strip()) if ruta_carpeta.strip() else None
    if fuente_carpeta and not fuente_carpeta.is_dir():
        st.error(f"No existe la carpeta: {fuente_carpeta}")
        st.stop()
    with st.spinner("Procesando facturas..."):
        # Un lote nuevo reemplaza al anterior: si no, las facturas del trimestre pasado
        # seguirían en las carpetas y en el ZIP de descarga.
        shutil.rmtree(WORK_DIR, ignore_errors=True)
        work_dir = WORK_DIR
        work_dir.mkdir(parents=True, exist_ok=True)
        if fuente_carpeta:
            fuente = fuente_carpeta
        else:
            fuente = work_dir / "facturas.zip"
            fuente.write_bytes(zip_file.getvalue())
        output_dir = Path(ruta_salida.strip()) if ruta_salida.strip() else work_dir / "salida"

        progreso = st.empty()
        contador = {"n": 0}

        def _avisar_progreso(nombre_archivo: str) -> None:
            contador["n"] += 1
            progreso.text(f"Procesada factura {contador['n']}: {nombre_archivo}")

        try:
            detalle, properties = core.process_invoices(
                fuente,
                properties,
                output_dir,
                threshold=threshold,
                groq_api_key=GROQ_API_KEY,
                on_file_processed=_avisar_progreso,
                identifier_csv=ident_file,
            )
        except ValueError as exc:
            st.error(str(exc))
            st.stop()
        progreso.empty()

        compartido["resultado"] = {
            "detalle": detalle,
            "properties": properties,
            "work_dir": str(work_dir),
            "output_dir": str(output_dir),
            "identificadores_nuevos": [],
            "elecciones": {},
        }
        st.session_state["pos_revisar"] = 0
        _guardar_estado(compartido["resultado"])

if compartido["resultado"] is None and ESTADO_JSON.exists():
    guardado = json.loads(ESTADO_JSON.read_text(encoding="utf-8"))
    # Si alguien borró .sesion a mano queda el JSON apuntando a carpetas que ya no existen.
    if Path(guardado["output_dir"]).is_dir():
        compartido["resultado"] = guardado

resultado = compartido["resultado"]
if resultado:
    detalle = resultado["detalle"]
    output_dir = Path(resultado["output_dir"])
    resumen_df = core.build_resumen(detalle)

    sin_identificar = [d for d in detalle if d["propiedad"] == "Sin identificar"]
    if sin_identificar:
        st.warning(
            f"{len(sin_identificar)} factura(s) no se pudieron asociar a ninguna propiedad "
            "y quedaron en 'Sin_identificar'. Revísalas manualmente."
        )

    groq_matches = [d for d in detalle if d["metodo_match"].startswith("groq")]
    if groq_matches:
        st.warning(
            f"De esas, {len(groq_matches)} tienen una sugerencia de propiedad hecha por IA (Groq) — "
            "quedan igual dentro de 'Sin_identificar' (no se archivan ni se suman al gasto de ninguna "
            "propiedad), porque el modelo gratuito a veces se equivoca con confianza alta en facturas "
            "ambiguas. Mirá la columna 'metodo_match' y 'evidencia_ia' en el detalle para decidir si "
            "la sugerencia es correcta y mover el archivo a mano."
        )

    if sin_identificar:
        st.subheader(f"Revisar a mano ({len(sin_identificar)} pendientes)")
        nombres = [d["archivo"] for d in sin_identificar]
        elecciones = resultado.setdefault("elecciones", {})
        # Antes se marcaba una sola propiedad por factura y se guardaba como texto suelto.
        for archivo, marcado in elecciones.items():
            if not isinstance(marcado, list):
                elecciones[archivo] = [marcado] if marcado else []
        # Al asignar una factura desaparece de la lista, así que quedarse en la misma posición
        # ya deja seleccionada la siguiente pendiente.
        pos = st.session_state.get("pos_revisar", 0) % len(nombres)
        revisar = st.selectbox(
            "Factura",
            nombres,
            index=pos,
            format_func=lambda n: (
                f"{nombres.index(n) + 1}/{len(nombres)} — "
                f"{'✔ ' + ' + '.join(elecciones[n]) + ' — ' if elecciones.get(n) else ''}{n[:70]}"
            ),
        )
        pos = nombres.index(revisar)
        st.session_state["pos_revisar"] = pos

        col_ant, col_sig, _ = st.columns([1, 1, 6])
        if col_ant.button("◀ Anterior", use_container_width=True):
            st.session_state["pos_revisar"] = (pos - 1) % len(nombres)
            st.rerun()
        if col_sig.button("Siguiente ▶", use_container_width=True):
            st.session_state["pos_revisar"] = (pos + 1) % len(nombres)
            st.rerun()
        ruta = next(output_dir.rglob(revisar), None)
        if ruta is None:
            st.error(f"No se encuentra {revisar} en la salida.")
            st.stop()

        col_prev, col_form = st.columns([3, 2])
        with col_prev:
            try:
                st.image(core.render_preview(ruta), use_container_width=True)
            except Exception as exc:
                st.warning(f"No se puede previsualizar este archivo: {exc}")

        with col_form:
            props = resultado["properties"]
            # El nombre por sí solo no distingue: hay varias "Lucena del Cid" y varias
            # "Anclamar". La dirección es lo que se compara contra la factura que tienes delante.
            etiqueta = {p["nombre"]: f"{p['nombre']}  —  {p['direccion']}" for p in props}
            opciones = [GASTO_EMPRESA] + [p["nombre"] for p in props]
            texto = _texto_factura(str(ruta))
            sugeridas = [
                p["nombre"] for p in core.detect_properties(core.normalize_text(texto), props)
            ]
            # Lo marcado se guarda aunque no llegues a pulsar Asignar: con 72 facturas se
            # navega adelante y atrás, y volver a una ya mirada tiene que devolverte tu elección.
            marcado = elecciones.get(revisar, sugeridas)
            destinos = st.multiselect(
                "Pertenece a",
                opciones,
                default=[n for n in marcado if n in opciones],
                format_func=lambda n: etiqueta.get(n, n),
                help="Se pueden marcar varias: una factura de mantenimiento de piscinas cubre "
                "cuatro villas en la misma hoja.",
            )
            if destinos != marcado:
                with compartido["lock"]:
                    elecciones[revisar] = destinos
                    _guardar_estado(resultado)
            if len(sugeridas) > 1:
                st.info(
                    f"Esta factura nombra {len(sugeridas)} propiedades. Comprueba que no falte "
                    "ninguna: las que se nombran con palabras que comparten varias propiedades "
                    "(p.ej. 'Grao', 'Benicasim') no se detectan solas."
                )

            fila = next(d for d in detalle if d["archivo"] == revisar)
            importes = [fila["importe"]]
            if len(destinos) > 1:
                st.caption(
                    f"Reparto del importe de la factura ({fila['importe'] or 0:.2f} €). Viene "
                    "dividido a partes iguales; corrígelo si la factura detalla lo de cada una."
                )
                a_partes = (fila["importe"] or 0.0) / len(destinos)
                importes = [
                    st.number_input(
                        f"€ de {d}", min_value=0.0, value=round(a_partes, 2), step=1.0,
                        key=f"importe_{revisar}_{d}",
                    )
                    for d in destinos
                ]

            codigos = core.candidate_identifiers(texto)
            codigo = None
            if len(destinos) == 1 and destinos[0] != GASTO_EMPRESA:
                st.caption(
                    "Si es una factura de suministro, elige el código del punto de suministro "
                    "(CUPS, contrato o contador). Se guarda en identificadores.csv y a partir de "
                    "ahí esta factura se clasifica sola cada trimestre."
                )
                codigo = st.radio(
                    "Código que identifica el suministro",
                    ["(ninguno, es un gasto puntual)"] + codigos,
                    index=0,
                )
                if codigo.startswith("(ninguno"):
                    codigo = None

            if st.button("Asignar", disabled=not destinos, type="primary"):
                with compartido["lock"]:
                    # Otra persona pudo asignar esta misma factura mientras la mirabas: el
                    # archivo ya no esta donde lo dejo el proceso y shutil.move reventaria.
                    if fila["propiedad"] != "Sin identificar" or not ruta.exists():
                        st.warning(
                            f"{revisar} ya la asigno otra persona a {fila['propiedad']}. "
                            "Recarga la pagina para ver la lista al dia."
                        )
                        st.stop()
                    # Una factura repartida se archiva en la carpeta de cada propiedad: es el mismo
                    # documento, y quien abra la carpeta de una villa tiene que encontrarlo ahí.
                    # La última se lleva el original y las demás una copia.
                    metodo = (
                        "manual"
                        if len(destinos) == 1
                        else f"manual (repartida entre {len(destinos)})"
                    )
                    filas = [fila] + [dict(fila) for _ in destinos[1:]]
                    for i, (destino, importe, f) in enumerate(zip(destinos, importes, filas)):
                        carpeta = (
                            GASTO_EMPRESA
                            if destino == GASTO_EMPRESA
                            else next(p["carpeta"] for p in props if p["nombre"] == destino)
                        )
                        nuevo_dir = output_dir / carpeta / ruta.parent.name
                        nuevo_dir.mkdir(parents=True, exist_ok=True)
                        mover = shutil.move if i == len(destinos) - 1 else shutil.copy2
                        mover(str(ruta), str(nuevo_dir / ruta.name))

                        f["propiedad"] = destino
                        f["metodo_match"] = metodo
                        f["confianza"] = 100.0
                        f["importe"] = importe
                        if codigo:
                            resultado["identificadores_nuevos"].append(
                                {"identificador": codigo, "propiedad": destino}
                            )
                    detalle.extend(filas[1:])
                    _guardar_estado(resultado)
                    st.rerun()

        with st.expander("Texto leído de esta factura"):
            st.text(texto[:3000] or "(sin texto: ni capa de texto ni OCR legible)")

    st.subheader("Resumen por propiedad")
    st.dataframe(resumen_df, use_container_width=True)

    st.subheader("Detalle de facturas")
    st.dataframe(
        [{k: v for k, v in d.items() if k != "texto"} for d in detalle],
        use_container_width=True,
    )

    core.build_excel_report(detalle, resumen_df, output_dir / "informe_gastos.xlsx")
    zip_path = Path(resultado["work_dir"]) / "facturas_organizadas.zip"
    core.zip_folder(output_dir, zip_path)
    st.download_button(
        "Descargar ZIP organizado + informe Excel",
        data=zip_path.read_bytes(),
        file_name="facturas_organizadas.zip",
        mime="application/zip",
    )

    nuevos = resultado["identificadores_nuevos"]
    if nuevos:
        st.download_button(
            f"Descargar identificadores.csv con {len(nuevos)} fila(s) nueva(s)",
            data=pd.DataFrame(nuevos).to_csv(index=False, sep=";").encode("utf-8"),
            file_name="identificadores_nuevos.csv",
            mime="text/csv",
            help="Pégalas en tu identificadores.csv. El trimestre que viene esas facturas "
            "se clasifican solas.",
        )
