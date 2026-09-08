"""UI Streamlit para organizador-facturas."""
import json
import os
import shutil
import threading
from pathlib import Path

import pandas as pd
import streamlit as st

import core

# Destinos que no son un piso. El texto es lo que ve quien revisa; la clave es el nombre de
# la carpeta y lo que se escribe en la tabla de códigos.
ETIQUETA_CATEGORIA = {
    "Gastos_empresa": "Gasto de la empresa (no es de ningún piso)",
    "Gasolina": "Gasolina / combustible",
}

# Carpeta de trabajo fija en vez de un tempdir: recargar la página abre una sesión nueva de
# Streamlit y reiniciarlo vacía la memoria, y volver a pasar el OCR por todo el lote cuesta
# minutos. Aquí quedan las facturas ya organizadas y el estado de la revisión a mano.
WORK_DIR = Path(os.environ.get("ORGANIZADOR_SESION") or Path(__file__).parent / ".sesion")
ESTADO_JSON = WORK_DIR / "estado.json"

# La tabla de códigos vive FUERA de la carpeta del lote: es lo único que se acumula trimestre a
# trimestre, y empezar un lote nuevo no puede borrarla. Cada código que se identifica a mano se
# añade aquí, así que el trimestre siguiente esa factura ya se coloca sola.
CODIGOS_CSV = Path(os.environ.get("ORGANIZADOR_CODIGOS") or Path(__file__).parent / "codigos_guardados.csv")

# Pisos que la empresa alquila pero que no están dados de alta en Lodgify (p.ej. porque el
# dueño no usa esa plataforma). Se guardan aparte, también fuera del contenedor, y se suman a
# los de Lodgify: mismo trato en el desplegable, en el emparejamiento por texto y en la tabla
# de códigos. A diferencia de Gastos_empresa/Gasolina, SÍ participan del matching automático.
PROPIEDADES_EXTRA_CSV = Path(
    os.environ.get("ORGANIZADOR_PROPIEDADES_EXTRA") or Path(__file__).parent / "propiedades_extra.csv"
)


def _codigos_guardados() -> pd.DataFrame | None:
    if not CODIGOS_CSV.exists():
        return None
    try:
        return pd.read_csv(CODIGOS_CSV, dtype=str, sep=None, engine="python").fillna("")
    except Exception:
        return None


def _anadir_codigos(nuevos: list[dict]) -> None:
    previo = _codigos_guardados()
    tabla = pd.concat([previo, pd.DataFrame(nuevos)], ignore_index=True) if previo is not None else pd.DataFrame(nuevos)
    tabla.to_csv(CODIGOS_CSV, index=False, sep=";")


def _propiedades_extra() -> list[dict]:
    if not PROPIEDADES_EXTRA_CSV.exists():
        return []
    try:
        return core.load_properties(PROPIEDADES_EXTRA_CSV)
    except Exception:
        return []


def _anadir_propiedad_extra(nombre: str, direccion: str) -> None:
    fila = pd.DataFrame([{"ref": nombre, "direccion": direccion, "nombre": nombre}])
    previo = pd.read_csv(PROPIEDADES_EXTRA_CSV, dtype=str, sep=";") if PROPIEDADES_EXTRA_CSV.exists() else None
    tabla = pd.concat([previo, fila], ignore_index=True) if previo is not None else fila
    tabla.to_csv(PROPIEDADES_EXTRA_CSV, index=False, sep=";")


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


@st.cache_data(ttl=3600, show_spinner="Cargando pisos de Lodgify...")
def _propiedades_lodgify(api_key: str) -> list[dict]:
    return core.load_properties_from_api(api_key)


GROQ_API_KEY = _secreto("GROQ_API_KEY")
LODGIFY_API_KEY = _secreto("LODGIFY_API_KEY")
compartido = _compartido()

st.set_page_config(page_title="Facturas por propiedad", layout="wide", page_icon="🗂️")

properties = []
error_pisos = ""
if not LODGIFY_API_KEY:
    error_pisos = "Falta configurar la clave LODGIFY_API_KEY para leer los pisos de Lodgify."
else:
    try:
        properties = _propiedades_lodgify(LODGIFY_API_KEY)
    except Exception as exc:
        error_pisos = f"Lodgify no respondió: {exc}"

extra = _propiedades_extra()
properties = properties + extra

with st.sidebar:
    st.subheader("Estado")
    if properties:
        texto_estado = f"{len(properties) - len(extra)} pisos leídos de Lodgify"
        if extra:
            texto_estado += f" + {len(extra)} piso(s) manual(es)"
        st.success(texto_estado)
    else:
        st.error(error_pisos)
    st.caption(
        "Las facturas se leen en este ordenador. Las que no se identifican solas se consultan "
        "a una IA (Groq)." if GROQ_API_KEY else
        "Las facturas se leen en este ordenador. No se sube nada a servicios externos."
    )
    if ESTADO_JSON.exists():
        st.divider()
        if st.button("Empezar un lote nuevo", width="stretch"):
            shutil.rmtree(WORK_DIR, ignore_errors=True)
            compartido["resultado"] = None
            st.rerun()
        st.caption("Borra el lote actual y su revisión. Descarga antes el resultado.")

    st.divider()
    with st.expander(f"Pisos que no están en Lodgify ({len(extra)})"):
        st.caption(
            "Se alquilan igual que los demás pero el dueño no los tiene dados de alta en "
            "Lodgify. Se tratan como un piso más: carpeta propia, y entran en la búsqueda "
            "automática del nombre dentro de la factura. Se puede añadir en cualquier "
            "momento, incluso a mitad de revisar un lote."
        )
        if extra:
            for p in extra:
                st.write(f"· {p['nombre']}" + (f" — {p['direccion']}" if p["direccion"] else ""))
        with st.form("nueva_propiedad_extra", clear_on_submit=True):
            nombre_nuevo = st.text_input("Nombre del piso")
            direccion_nueva = st.text_input("Dirección (opcional, ayuda a identificarlo)")
            if st.form_submit_button("Añadir piso") and nombre_nuevo.strip():
                _anadir_propiedad_extra(nombre_nuevo.strip(), direccion_nueva.strip())
                st.rerun()

if compartido["resultado"] is None and ESTADO_JSON.exists():
    guardado = json.loads(ESTADO_JSON.read_text(encoding="utf-8"))
    # Si alguien borró .sesion a mano queda el JSON apuntando a carpetas que ya no existen.
    if Path(guardado["output_dir"]).is_dir():
        compartido["resultado"] = guardado

resultado = compartido["resultado"]

# ---------------------------------------------------------------- paso 1: cargar el lote
if resultado is None:
    st.title("Facturas por propiedad")
    st.markdown(
        "Sube el ZIP con las facturas del trimestre. Te lo devuelve ordenado en una carpeta "
        "por piso y por mes, con el Excel de gastos hecho."
    )
    zip_file = st.file_uploader(
        "ZIP de facturas del trimestre", type=["zip"],
        help="Tal cual llega: PDF, JPG o PNG mezclados dentro.",
    )

    guardados = _codigos_guardados()
    if guardados is not None:
        # Una fila con el CUPS pero sin piso no sirve para nada: hay que contar las que de
        # verdad colocan una factura, no las filas del CSV.
        utiles = len(core.load_identifier_map(CODIGOS_CSV, properties)) if properties else 0
        sin_asignar = len(guardados) - utiles
        st.success(f"Tabla de códigos guardada: {utiles} código(s) listos para colocar facturas solos.")
        if sin_asignar > 0:
            st.info(
                f"Hay {sin_asignar} código(s) más en la tabla sin piso asignado: no sirven "
                "todavía. Se van rellenando solos según coloques esas facturas a mano aquí."
            )
        with st.expander("Cambiar la tabla de códigos"):
            nueva = st.file_uploader("Sustituirla por otro CSV", type=["csv"], key="sustituir")
            if nueva is not None:
                CODIGOS_CSV.write_bytes(nueva.getvalue())
                st.rerun()
    else:
        st.warning(
            "No hay tabla de códigos. Las facturas de luz, agua y gas no dicen de qué piso "
            "son: solo traen el CUPS o el nº de contrato, así que sin esa tabla acaban todas "
            "en la lista de colocar a mano."
        )
        subida = st.file_uploader("Tabla de códigos (CSV)", type=["csv"])
        if subida is not None:
            CODIGOS_CSV.write_bytes(subida.getvalue())
            st.rerun()
        st.caption(
            "Si no la tienes, sube el ZIP igual: según vayas colocando facturas a mano, la app "
            "va guardando los códigos y el trimestre que viene se colocan solas."
        )

    if st.button(
        "Ordenar las facturas", type="primary",
        disabled=not (zip_file and properties), width="stretch",
    ):
        with st.status("Ordenando las facturas...", expanded=True) as estado:
            # Un lote nuevo reemplaza al anterior: si no, las facturas del trimestre pasado
            # seguirían en las carpetas y en el ZIP de descarga.
            shutil.rmtree(WORK_DIR, ignore_errors=True)
            WORK_DIR.mkdir(parents=True, exist_ok=True)
            fuente = WORK_DIR / "facturas.zip"
            fuente.write_bytes(zip_file.getvalue())
            output_dir = WORK_DIR / "salida"

            progreso = st.empty()
            contador = {"n": 0}

            def _avisar_progreso(nombre_archivo: str) -> None:
                contador["n"] += 1
                progreso.text(f"{contador['n']} — {nombre_archivo}")

            try:
                detalle, properties = core.process_invoices(
                    fuente, properties, output_dir,
                    groq_api_key=GROQ_API_KEY,
                    on_file_processed=_avisar_progreso,
                    identifier_csv=CODIGOS_CSV if CODIGOS_CSV.exists() else None,
                )
            except ValueError as exc:
                estado.update(label="No se pudo leer el lote", state="error")
                st.error(str(exc))
                st.stop()
            estado.update(label=f"{len(detalle)} facturas leídas", state="complete")

        compartido["resultado"] = {
            "detalle": detalle,
            "properties": properties,
            "work_dir": str(WORK_DIR),
            "output_dir": str(output_dir),
            "identificadores_nuevos": [],
            "elecciones": {},
        }
        _guardar_estado(compartido["resultado"])
        st.rerun()
    st.stop()

# ---------------------------------------------------------------- lote ya procesado
detalle = resultado["detalle"]
# Lista en vivo, no la foto fija del momento de procesar: un piso añadido a mitad de
# revisar un lote de varios días tiene que aparecer sin reprocesar nada. Si Lodgify falla
# en una recarga puntual, mejor la última lista buena que dejar el desplegable vacío.
props = properties or resultado["properties"]
output_dir = Path(resultado["output_dir"])
pendientes = [d for d in detalle if d["propiedad"] == "Sin identificar"]
colocadas = len(detalle) - len(pendientes)

st.title("Facturas por propiedad")
c1, c2, c3 = st.columns(3)
c1.metric("Facturas del lote", len(detalle))
c2.metric("Ya colocadas", colocadas)
c3.metric("Faltan por colocar", len(pendientes))
st.progress(colocadas / len(detalle) if detalle else 0.0)

etiqueta_prop = {p["nombre"]: f"{p['nombre']} — {p['direccion']}" for p in props}
carpeta_destino = {p["nombre"]: p["carpeta"] for p in props}
carpeta_destino.update({c["nombre"]: c["carpeta"] for c in core.category_destinations()})


def _etiqueta(nombre: str) -> str:
    return ETIQUETA_CATEGORIA.get(nombre) or etiqueta_prop.get(nombre, nombre)


def _colocar(filas_archivos: list[str], destinos: list[str], importes: list[float], codigo: str | None) -> None:
    """Mueve cada factura a la carpeta de su destino y actualiza el detalle. Una factura
    repartida entre varios pisos se archiva en la carpeta de cada uno: es el mismo documento,
    y quien abra la carpeta de una villa tiene que encontrarlo ahí."""
    for archivo in filas_archivos:
        fila = next(d for d in detalle if d["archivo"] == archivo and d["propiedad"] == "Sin identificar")
        ruta = next(output_dir.rglob(archivo), None)
        if ruta is None or not ruta.exists():
            continue
        reparto = importes if len(destinos) > 1 else [fila["importe"]]
        metodo = "manual" if len(destinos) == 1 else f"manual (repartida entre {len(destinos)})"
        filas = [fila] + [dict(fila) for _ in destinos[1:]]
        for i, (destino, importe, f) in enumerate(zip(destinos, reparto, filas)):
            nuevo_dir = output_dir / carpeta_destino[destino] / ruta.parent.name
            nuevo_dir.mkdir(parents=True, exist_ok=True)
            mover = shutil.move if i == len(destinos) - 1 else shutil.copy2
            mover(str(ruta), str(nuevo_dir / ruta.name))
            f["propiedad"] = destino
            f["metodo_match"] = metodo
            f["confianza"] = 100.0
            f["importe"] = importe
            if codigo:
                fila_codigo = {"identificador": codigo, "propiedad": destino}
                resultado["identificadores_nuevos"].append(fila_codigo)
                _anadir_codigos([fila_codigo])
        detalle.extend(filas[1:])
    _guardar_estado(resultado)


def _grupos_pendientes() -> list[tuple[str, list[dict]]]:
    """Las facturas de un mismo proveedor salen de la misma plantilla y llevan la misma
    cabecera, así que caen juntas. Ordenarlas por grupo ahorra saltar de contexto en cada
    factura, y las del mismo proveedor de empresa (Vodafone, Stripe...) se colocan de una vez."""
    grupos: dict[str, list[dict]] = {}
    for d in pendientes:
        grupos.setdefault(d.get("emisor") or "(cabecera ilegible)", []).append(d)
    return sorted(grupos.items(), key=lambda kv: (-len(kv[1]), kv[0]))


tab_revisar, tab_resumen, tab_descargar = st.tabs(
    [f"Colocar las que faltan ({len(pendientes)})", "Resumen por piso", "Descargar"],
    on_change="rerun", key="pestana",
)

# ---------------------------------------------------------------- paso 2: colocar a mano
with tab_revisar:
    if not pendientes:
        st.success("Todas las facturas están colocadas. Ve a **Descargar**.")
    else:
        grupos = _grupos_pendientes()
        pos = st.session_state.get("grupo_pos", 0) % len(grupos)
        emisor, grupo = grupos[pos]

        nav_izq, nav_der, nav_sel = st.columns([1, 1, 6])
        if nav_izq.button("Anterior", width="stretch"):
            st.session_state["grupo_pos"] = (pos - 1) % len(grupos)
            st.rerun()
        if nav_der.button("Siguiente", width="stretch"):
            st.session_state["grupo_pos"] = (pos + 1) % len(grupos)
            st.rerun()
        nav_sel.caption(f"Grupo {pos + 1} de {len(grupos)} · {len(pendientes)} facturas sueltas")

        if len(grupo) > 1:
            archivo_sel = st.selectbox(
                f"Factura de este grupo ({len(grupo)})",
                [d["archivo"] for d in grupo],
                format_func=lambda a: f"{a} — {next(d['importe'] for d in grupo if d['archivo'] == a) or 0:.2f} €",
                # La clave lleva el grupo dentro: al cambiar de grupo, la opcion recordada del
                # anterior no pinta nada aqui y Streamlit no intenta reusarla.
                key=f"factura_sel_{pos}",
            )
            actual = next(d for d in grupo if d["archivo"] == archivo_sel)
        else:
            actual = grupo[0]

        ruta = next(output_dir.rglob(actual["archivo"]), None)
        if ruta is None:
            st.error(f"No se encuentra {actual['archivo']}. Empieza un lote nuevo desde la barra lateral.")
            st.stop()

        col_doc, col_form = st.columns([3, 2], gap="large")
        with col_doc:
            with st.container(border=True):
                st.caption(actual["archivo"])
                try:
                    st.image(core.render_preview(ruta), width="stretch")
                except Exception as exc:
                    st.warning(f"Este archivo no se puede previsualizar: {exc}")

        with col_form:
            st.subheader(emisor.title() if emisor != "(cabecera ilegible)" else "Sin cabecera legible")
            total_grupo = sum(d["importe"] or 0.0 for d in grupo)
            st.caption(
                f"{len(grupo)} factura(s) de este proveedor sin colocar · {total_grupo:.2f} € en total"
            )
            texto = _texto_factura(str(ruta))
            sugeridas = [p["nombre"] for p in core.detect_properties(core.normalize_text(texto), props)]
            if actual.get("evidencia_ia"):
                st.info(f"La IA apunta a: {actual['evidencia_ia']}")

            # Nada viene premarcado a propósito: estas coincidencias salen de buscar el nombre
            # del piso dentro del texto, y una factura que solo dice "CASTELLON" engancha con
            # cualquier piso de Castellón. Marcarlas solas invitaría a colocar mal de un clic.
            elegidas = []
            if sugeridas:
                elegidas = st.pills(
                    "Nombres que aparecen en el texto (compruébalo antes de fiarte)",
                    sugeridas, selection_mode="multi", format_func=_etiqueta,
                )

            categoria = st.pills(
                "¿Es un gasto general?", list(core.CATEGORIAS), format_func=_etiqueta,
                help="No es de ningún piso: repostajes, la gestoría, Vodafone, Stripe...",
            )
            pisos = st.multiselect(
                "¿O de qué piso es?", [p["nombre"] for p in props],
                default=[n for n in elegidas if n != categoria],
                format_func=_etiqueta,
                placeholder="Escribe para buscar el piso",
                help="Puedes marcar varios: una factura de mantenimiento de piscinas cubre "
                "cuatro villas en la misma hoja.",
            )
            destinos = [categoria] if categoria else pisos
            if categoria and pisos:
                st.warning("Elige una cosa o la otra: o es gasto general, o es de un piso.")
                destinos = []

            importes = [actual["importe"]]
            if len(destinos) > 1:
                a_partes = (actual["importe"] or 0.0) / len(destinos)
                st.caption(f"Reparto de {actual['importe'] or 0:.2f} €. Corrígelo si la factura lo detalla.")
                importes = [
                    st.number_input(
                        _etiqueta(d), min_value=0.0, value=round(a_partes, 2), step=1.0,
                        key=f"importe_{actual['archivo']}_{d}",
                    )
                    for d in destinos
                ]

            codigo = None
            if len(destinos) == 1:
                codigos = core.candidate_identifiers(texto)
                if codigos:
                    elegido = st.selectbox(
                        "¿Hay un código que identifique siempre a este destino?",
                        ["No, es un gasto suelto"] + codigos,
                        help="El CUPS de la luz, el nº de contrato del agua, la tarjeta de "
                        "gasolina... Se guarda en la tabla de códigos y el trimestre que viene "
                        "estas facturas se colocan solas.",
                    )
                    codigo = None if elegido.startswith("No,") else elegido

            # Empresa y gasolina se deciden por proveedor, no por factura: todo lo que emite
            # Vodafone o la gasolinera va al mismo sitio. Un piso concreto solo se aplica en
            # bloque si todas comparten el código del suministro, que prueba que son del mismo.
            comun = (
                set.intersection(*[set(d.get("codigos") or []) for d in grupo])
                if len(grupo) > 1 else set()
            )
            en_bloque = (len(destinos) == 1 and destinos[0] in core.CATEGORIAS) or bool(comun)

            b1, b2 = st.columns(2)
            if b1.button("Colocar esta", type="primary", disabled=not destinos, width="stretch"):
                with compartido["lock"]:
                    _colocar([actual["archivo"]], destinos, importes, codigo)
                st.rerun()
            if len(grupo) > 1:
                if b2.button(
                    f"Colocar las {len(grupo)}", disabled=not (destinos and en_bloque),
                    width="stretch",
                    help=None if en_bloque else
                    "Solo se pueden colocar de golpe si son gasto de empresa o si comparten "
                    "el código del punto de suministro.",
                ):
                    with compartido["lock"]:
                        _colocar([d["archivo"] for d in grupo], destinos[:1], importes, codigo)
                    st.rerun()

            with st.expander("Texto leído de esta factura"):
                st.text(texto[:3000] or "(sin texto: ni capa de texto ni OCR legible)")

# ---------------------------------------------------------------- resumen
with tab_resumen:
    resumen_df = core.build_resumen(detalle)
    st.dataframe(resumen_df, width="stretch", hide_index=True)
    with st.expander("Ver factura por factura"):
        st.dataframe(
            pd.DataFrame(detalle)[["archivo", "propiedad", "importe", "metodo_match"]].rename(
                columns={"metodo_match": "cómo se colocó", "propiedad": "piso"}
            ),
            width="stretch", hide_index=True,
        )

# ---------------------------------------------------------------- descargas
with tab_descargar:
    if pendientes:
        st.warning(
            f"Faltan {len(pendientes)} facturas por colocar. Puedes descargar igual: quedan "
            "en la carpeta 'Sin_identificar'."
        )
    # Comprimir el lote entero cuesta segundos: solo se rehace al abrir esta pestaña, no en
    # cada factura que se coloca.
    if tab_descargar.open:
        with st.spinner("Preparando el archivo..."):
            resumen_df = core.build_resumen(detalle)
            core.build_excel_report(detalle, resumen_df, output_dir / "informe_gastos.xlsx")
            zip_path = Path(resultado["work_dir"]) / "facturas_organizadas.zip"
            core.zip_folder(output_dir, zip_path)
        st.download_button(
            "Descargar todo ordenado (ZIP + Excel)", data=zip_path.read_bytes(),
            file_name="facturas_organizadas.zip", mime="application/zip",
            type="primary", width="stretch",
        )
        st.caption("Una carpeta por piso, dentro una por mes, y el Excel de gastos en la raíz.")

        guardados = _codigos_guardados()
        if guardados is not None:
            st.divider()
            nuevos = len(resultado["identificadores_nuevos"])
            st.markdown(
                f"**Tabla de códigos: {len(guardados)} código(s)**"
                + (f", {nuevos} identificado(s) en este lote." if nuevos else ".")
                + " Ya está guardada; no hace falta volver a subirla el trimestre que viene."
            )
            st.download_button(
                "Descargar la tabla de códigos (copia de seguridad)",
                data=CODIGOS_CSV.read_bytes(),
                file_name="identificadores.csv", mime="text/csv", width="stretch",
            )
