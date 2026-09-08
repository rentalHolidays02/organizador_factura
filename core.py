"""Lógica de organización de facturas por propiedad (sin UI)."""
from __future__ import annotations

import io
import json
import os
import re
import shutil
import unicodedata
import zipfile
from pathlib import Path
from typing import Callable, Optional

import pandas as pd
import pdfplumber
import pypdfium2
import pytesseract
from PIL import Image
from rapidfuzz import fuzz

IMAGE_EXTS = (".jpg", ".jpeg", ".png")
PDF_EXT = ".pdf"
INVOICE_EXTS = (PDF_EXT,) + IMAGE_EXTS

# Por debajo de esto se asume que el PDF no trae capa de texto real (un escaneo suele
# devolver 0 caracteres; algún PDF de imagen trae solo un pie de página del escáner).
MIN_PDF_TEXT_CHARS = 40
OCR_DPI = 300
OCR_MAX_PAGES = 3


def _ocr_lang() -> str:
    """El instalador de Windows solo trae inglés: el diccionario español se añade a mano y
    pide permisos de administrador. Pedirle a tesseract un idioma que no tiene es un error,
    así que se pide español solo si está. Con inglés el OCR de una factura española sale algo
    peor en tildes y eñes, pero normalize_text las quita igual antes de comparar."""
    try:
        return "spa+eng" if "spa" in pytesseract.get_languages() else "eng"
    except Exception:
        return "eng"


OCR_LANG = _ocr_lang()

# Tipos de IVA españoles, para reconocer cuándo el "total" capturado era la base imponible.
IVA_RATES = (0.21, 0.10, 0.04)

REF_ALIASES = {"ref", "referencia", "codigo", "id"}
DIR_ALIASES = {"direccion", "address", "domicilio"}
NAME_ALIASES = {"name", "nombre", "propiedad", "titulo"}

DEFAULT_FUZZY_THRESHOLD = 82
GROQ_DEFAULT_MODEL = os.environ.get("GROQ_MODEL", "llama-3.1-8b-instant")
GROQ_CONFIDENCE_BY_LEVEL = {"alta": 90.0, "media": 70.0, "baja": 50.0}

MESES_ES = {
    "enero": (1, "Enero"),
    "febrero": (2, "Febrero"),
    "marzo": (3, "Marzo"),
    "abril": (4, "Abril"),
    "mayo": (5, "Mayo"),
    "junio": (6, "Junio"),
    "julio": (7, "Julio"),
    "agosto": (8, "Agosto"),
    "septiembre": (9, "Septiembre"),
    "setiembre": (9, "Septiembre"),
    "octubre": (10, "Octubre"),
    "noviembre": (11, "Noviembre"),
    "diciembre": (12, "Diciembre"),
}
SIN_MES = "00-Sin_mes"


def detect_month_folder(relative_path: Path) -> str:
    """Usa la carpeta de mes que ya trae el ZIP de origen (p.ej. "OCTUBRE") en vez de
    adivinar una fecha del contenido: es la señal más confiable, porque así es como el
    usuario ya organiza sus facturas antes de subirlas.
    Revisa las carpetas de más cerca del archivo hacia afuera. Una carpeta que menciona
    más de un mes (p.ej. "OCTUBRE-NOVIEMBRE-DICIEMBRE") es ambigua y no cuenta como señal
    válida: mejor Sin_mes que adivinar mal cuál de los varios es."""
    for part in reversed(relative_path.parts[:-1]):
        palabras = normalize_text(part).split()
        meses_encontrados = {palabra for palabra in palabras if palabra in MESES_ES}
        if len(meses_encontrados) == 1:
            num, nombre = MESES_ES[meses_encontrados.pop()]
            return f"{num:02d}-{nombre}"
    return SIN_MES

# Un importe. Las alternativas van de más a menos específica y el orden importa: "1.641" tiene
# que leerse como miles (1641) antes de que la alternativa de decimal con punto lo parta en 1,64.
# El punto decimal aparece en facturas en inglés (Google, Stripe) y en salidas de OCR.
_NUM = r"(\d{1,3}(?:\.\d{3})+,\d{2}|\d+,\d{2}|\d{1,3}(?:\.\d{3})+|\d+\.\d{1,2}|\d+)"
# Solo importes con decimales, para la etiqueta genérica "total": también es cabecera de columna
# en las tablas de detalle ("coste total"), y ahí el número que sigue es la cantidad de la línea.
_NUM_DEC = r"(\d{1,3}(?:\.\d{3})+,\d{2}|\d+,\d{2}|\d+\.\d{1,2})"
# Entre la etiqueta y el número solo caben separadores y, como mucho, la moneda:
# "TOTAL EUROS.................... 60,00", "Total € 582,29", "Total factura\n17,64 €".
# Admitir texto libre en ese hueco hace que la letra pequeña ("...el importe total de la
# factura.") enganche el primer número que venga detrás, que suele ser el 21 del IVA.
_HUECO = r"[\s.:·=\-]*(?:€|eur(?:os)?\.?|\$)?[\s.:·=\-]*"

# patrones de importe, de más a menos específico. \b tras "total" evita que "totaling €384.00"
# de las facturas en inglés se lea como un total.
# \s* y no \s+ entre palabras: pdfplumber pierde el espaciado en algunos PDFs
# ("TOTALFACTURAEUROS........ 10,00€").
AMOUNT_PATTERNS = [
    re.compile(r"total\s*a\s*pagar" + _HUECO + _NUM, re.IGNORECASE),
    re.compile(r"total\s*importe\s*factura" + _HUECO + _NUM, re.IGNORECASE),
    re.compile(r"importe\s*total" + _HUECO + _NUM, re.IGNORECASE),
    re.compile(r"total\s*factura" + _HUECO + _NUM, re.IGNORECASE),
    re.compile(r"total\s*impuestos\s*incluidos" + _HUECO + _NUM, re.IGNORECASE),
    re.compile(r"total\s*in\s*eur" + _HUECO + _NUM, re.IGNORECASE),
    re.compile(r"(?<!sub)(?<!sub )total\b" + _HUECO + _NUM_DEC, re.IGNORECASE),
]


def normalize_text(text: str) -> str:
    """minúsculas, sin tildes, sin caracteres especiales."""
    text = (text or "").lower()
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def sanitize_folder_name(name: str) -> str:
    name = re.sub(r'[\\/*?:"<>|]', "_", name).strip(" .")
    return name or "Sin_nombre"


def _nombre_sin_colision(dest_dir: Path, name: str) -> str:
    """Escáneres numeran cada tanda desde 1: dos facturas de la misma propiedad y mes
    pueden llamarse igual ("10.pdf"). shutil.copy2 pisaría la primera sin avisar."""
    destino = dest_dir / name
    if not destino.exists():
        return name
    stem, suffix = Path(name).stem, Path(name).suffix
    i = 2
    while (dest_dir / f"{stem} ({i}){suffix}").exists():
        i += 1
    return f"{stem} ({i}){suffix}"


def parse_amount_es(raw: str) -> float:
    raw = raw.strip()
    if "," in raw:
        return float(raw.replace(".", "").replace(",", "."))
    # Sin coma, un punto que separa grupos de 3 digitos solo puede ser separador de miles:
    # "1.641" son mil seiscientos cuarenta y uno, no 1,641. float() lo leeria como decimal.
    if re.fullmatch(r"\d{1,3}(?:\.\d{3})+", raw):
        return float(raw.replace(".", ""))
    return float(raw)


def _tiene_decimales(raw: str) -> bool:
    return "," in raw or re.search(r"\.\d{1,2}$", raw) is not None


def _aparece_importe(raw_text: str, valor: float) -> bool:
    es = f"{valor:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    en = f"{valor:.2f}"
    return any(
        re.search(r"(?<![\d,.])" + re.escape(v) + r"(?![\d,])", raw_text) for v in (es, en)
    )


def _corregir_si_es_base(raw_text: str, valor: float) -> float:
    """Muchos proveedores no rotulan el total con IVA, y el único "total" impreso es el de la
    base ("Total albarán: 44,64"). Si el propio documento contiene literalmente valor+IVA, lo
    capturado era la base imponible y el importe cobrado está impreso más abajo sin etiqueta."""
    for tipo in IVA_RATES:
        con_iva = round(valor * (1 + tipo), 2)
        if _aparece_importe(raw_text, con_iva):
            return con_iva
    return valor


def extract_amount(raw_text: str) -> Optional[float]:
    for pattern in AMOUNT_PATTERNS:
        candidatos = [m.group(1) for m in pattern.finditer(raw_text)]
        # Una etiqueta de total puede aparecer varias veces (cabecera de tabla, desglose de IVA,
        # cierre). El importe cobrado lleva céntimos; los números redondos que acompañan a esas
        # otras apariciones suelen ser cantidades de línea o el "21" del porcentaje de IVA.
        for raw in [c for c in candidatos if _tiene_decimales(c)] + candidatos:
            try:
                return _corregir_si_es_base(raw_text, parse_amount_es(raw))
            except ValueError:
                continue
    return None


def extract_text_from_pdf(path: Path) -> str:
    text_parts = []
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            page_text = page.extract_text()
            if page_text:
                text_parts.append(page_text)
    text = "\n".join(text_parts)
    if len(text.strip()) >= MIN_PDF_TEXT_CHARS:
        return text
    # Factura escaneada guardada como PDF: la página es una imagen, no hay texto que
    # extraer. Se rasteriza la página y se pasa por el mismo OCR que las fotos.
    return ocr_pdf_pages(path)


def ocr_pdf_pages(path: Path, lang: str = OCR_LANG) -> str:
    # pypdfium2 y no pdf2image: rasteriza desde la propia rueda de Python, sin necesitar
    # los binarios de poppler instalados aparte en el sistema. scale va contra las 72 dpi
    # nativas del PDF.
    pdf = pypdfium2.PdfDocument(Path(path).read_bytes())
    paginas = [
        pdf[i].render(scale=OCR_DPI / 72).to_pil() for i in range(min(len(pdf), OCR_MAX_PAGES))
    ]
    return "\n".join(pytesseract.image_to_string(p, lang=lang) for p in paginas)


def extract_text_from_image(path: Path, lang: str = OCR_LANG) -> str:
    with Image.open(path) as img:
        return pytesseract.image_to_string(img, lang=lang)


def extract_invoice_text(path: Path) -> str:
    ext = path.suffix.lower()
    if ext == PDF_EXT:
        return extract_text_from_pdf(path)
    if ext in IMAGE_EXTS:
        return extract_text_from_image(path)
    return ""


def render_preview(path: Path, scale: float = 1.6) -> Image.Image:
    """Primera página como imagen, para poder mirar la factura sin salir de la app.

    Se abre desde bytes y no desde la ruta: en Windows el handle del archivo queda abierto
    mientras viva la imagen devuelta, y entonces mover la factura a su carpeta falla."""
    data = Path(path).read_bytes()
    if Path(path).suffix.lower() == PDF_EXT:
        return pypdfium2.PdfDocument(data)[0].render(scale=scale).to_pil()
    return Image.open(io.BytesIO(data))


# Un código de punto de suministro (CUPS, nº de contrato, de contador) es una tirada larga de
# dígitos, sola o mezclada con letras. Los separadores de importe y de fecha quedan fuera a
# propósito: "1.234,56" y "19/09/2025" no son identificadores.
CODIGO_RE = re.compile(r"(?<![A-Za-z0-9])([A-Za-z0-9-]{8,26})(?![A-Za-z0-9])")


def candidate_identifiers(raw_text: str, limit: int = 15) -> list[str]:
    """Códigos que PODRÍAN identificar el punto de suministro, para que un humano elija uno.

    Nunca se elige solo: igual que en load_identifier_map, el script solo reconoce lo que alguien
    puso en la tabla a mano. Esto solo ahorra tener que buscar el código a ojo dentro del PDF.
    Se ordenan de más largo a más corto porque el CUPS, que es el que identifica el suministro,
    es el código más largo que trae una factura de luz."""
    vistos = {}
    for m in CODIGO_RE.finditer(raw_text or ""):
        codigo = m.group(1).upper().strip("-")
        if any(c.isdigit() for c in codigo):
            vistos.setdefault(codigo, None)
    return sorted(vistos, key=len, reverse=True)[:limit]


def _build_property(ref: str, direccion: str, nombre_raw: str) -> dict:
    ref, direccion, nombre_raw = ref.strip(), direccion.strip(), nombre_raw.strip()
    nombre = nombre_raw or ref or direccion
    ref_norm, direccion_norm, nombre_raw_norm = (
        normalize_text(ref),
        normalize_text(direccion),
        normalize_text(nombre_raw),
    )
    return {
        "ref": ref,
        "direccion": direccion,
        "nombre": nombre,
        "campos_norm": [f for f in (ref_norm, direccion_norm, nombre_raw_norm) if f],
        # Para tokens sueltos NO se usa la dirección: nombres de calle/ciudad
        # ("Castelló", "Virgen del Carmen") son comunes a cualquier factura
        # (p.ej. la propia empresa factura desde esa ciudad) y disparan falsos positivos.
        "campos_token": [f for f in (ref_norm, nombre_raw_norm) if f],
        "carpeta": sanitize_folder_name(nombre),
    }


def load_properties(csv_source) -> list[dict]:
    """Lee el CSV de propiedades detectando columnas ref/direccion automáticamente."""
    df = pd.read_csv(csv_source, dtype=str, sep=None, engine="python").fillna("")
    ref_col = dir_col = name_col = None
    for col in df.columns:
        col_norm = normalize_text(col)
        if ref_col is None and col_norm in REF_ALIASES:
            ref_col = col
        if dir_col is None and col_norm in DIR_ALIASES:
            dir_col = col
        if name_col is None and col_norm in NAME_ALIASES:
            name_col = col
    if ref_col is None or dir_col is None:
        raise ValueError(
            "El CSV debe tener una columna de referencia (ref/referencia/codigo/id) "
            "y una de dirección (direccion/address/domicilio)."
        )

    properties = []
    for _, row in df.iterrows():
        ref, direccion = str(row[ref_col]), str(row[dir_col])
        nombre_raw = str(row[name_col]) if name_col else ""
        if not (ref.strip() or direccion.strip() or nombre_raw.strip()):
            continue
        properties.append(_build_property(ref, direccion, nombre_raw))
    return properties


LODGIFY_PROPERTIES_URL = "https://api.lodgify.com/v2/properties"


def load_properties_from_api(api_key: str) -> list[dict]:
    """Trae las propiedades directo de Lodgify (paginado) en vez de un CSV exportado a mano.
    Cambian una vez al año (cada octubre), pero así el maestro es siempre el real, sin
    depender de que alguien recuerde subir el CSV al día."""
    import urllib.error
    import urllib.request

    properties = []
    page = 1
    while True:
        req = urllib.request.Request(
            f"{LODGIFY_PROPERTIES_URL}?page={page}&size=50",
            headers={"X-ApiKey": api_key},
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = json.load(resp)
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                raise ValueError("Lodgify rechazó la API key (401).") from exc
            raise ValueError(f"Lodgify devolvió un error ({exc.code}).") from exc
        items = data.get("items", [])
        for item in items:
            ref = str(item.get("id", ""))
            direccion = ", ".join(f for f in (item.get("address"), item.get("city")) if f)
            properties.append(_build_property(ref, direccion, item.get("name") or ""))
        if len(items) < 50:
            break
        page += 1
    if not properties:
        raise ValueError("Lodgify no devolvió ninguna propiedad. Revisa la API key.")
    return properties


IDENT_ALIASES = {"identificador", "id", "codigo", "cups", "valor", "contrato"}
PROP_ALIASES = {"propiedad", "ref", "referencia", "nombre", "name"}


def load_identifier_map(csv_source, properties: list[dict]) -> dict[str, dict]:
    """Tabla auxiliar identificador -> propiedad (CUPS de luz/gas, nº de contrato, de contador,
    de línea telefónica...). Es la única forma de clasificar las facturas de suministros, que no
    nombran la propiedad por ningún lado: solo traen el código del punto de suministro.

    No se deduce ningún formato de código: solo se buscan los valores que están en esta tabla,
    así que un identificador mal mapeado es un error del CSV, nunca una invención del script.
    """
    df = pd.read_csv(csv_source, dtype=str, sep=None, engine="python").fillna("")
    ident_col = prop_col = None
    for col in df.columns:
        col_norm = normalize_text(col)
        if ident_col is None and col_norm in IDENT_ALIASES:
            ident_col = col
        elif prop_col is None and col_norm in PROP_ALIASES:
            prop_col = col
    if ident_col is None or prop_col is None:
        raise ValueError(
            "El CSV de identificadores debe tener una columna de identificador "
            "(identificador/cups/contrato/codigo) y una de propiedad (propiedad/ref/nombre)."
        )

    destinos = {}
    for prop in properties:
        for clave in (prop["nombre"], prop["ref"]):
            clave_norm = normalize_text(clave)
            if clave_norm:
                destinos.setdefault(clave_norm, prop)

    index = {}
    for _, row in df.iterrows():
        ident_norm = normalize_text(row[ident_col])
        destino = destinos.get(normalize_text(row[prop_col]))
        if ident_norm and destino:
            index[ident_norm] = destino
    return index


def _identifier_search(text_norm: str, identifier_index: dict[str, dict]) -> Optional[dict]:
    # Gana el identificador más largo: si una factura trae a la vez el nº de cliente (corto,
    # de la empresa) y el CUPS (largo, del punto de suministro), manda el del punto de suministro.
    best, best_len = None, 0
    for ident, prop in identifier_index.items():
        if len(ident) > best_len and re.search(
            r"(?<![a-z0-9])" + re.escape(ident) + r"(?![a-z0-9])", text_norm
        ):
            best, best_len = prop, len(ident)
    return best


def _exact_search(text_norm: str, properties: list[dict]) -> Optional[dict]:
    best = None
    best_len = 0
    for prop in properties:
        for field in prop["campos_norm"]:
            if len(field) >= 3 and field in text_norm and len(field) > best_len:
                best, best_len = prop, len(field)
    return best


def build_token_index(properties: list[dict], min_len: int = 4) -> dict[str, dict]:
    """Palabras que identifican una única propiedad sin ambigüedad (p.ej. "borriol"),
    a diferencia de palabras genéricas compartidas por varias ("apartamento", "oropesa").
    Se calculan a partir del propio CSV, sin listas de stopwords hardcodeadas."""
    owners: dict[str, list[dict]] = {}
    for prop in properties:
        seen_words = set()
        for field in prop["campos_token"]:
            for word in field.split():
                if len(word) >= min_len and not word.isdigit():
                    seen_words.add(word)
        for word in seen_words:
            owners.setdefault(word, []).append(prop)
    return {word: props[0] for word, props in owners.items() if len(props) == 1}


def _token_search(text_norm: str, token_index: dict[str, dict]) -> Optional[dict]:
    best, best_len = None, 0
    for word in set(text_norm.split()):
        if word in token_index and len(word) > best_len:
            best, best_len = token_index[word], len(word)
    return best


def detect_properties(content_norm: str, properties: list[dict]) -> list[dict]:
    """TODAS las propiedades que el texto nombra, no solo la mejor.

    match_invoice devuelve una sola propiedad porque el caso normal es una factura de una
    unidad, pero el mantenimiento de piscinas o de jardines llega en una sola hoja que cubre
    varias villas. Se usa el mismo índice de palabras que identifican a una única propiedad
    ("aeroclub", "borriol", "torreta"): lo que nombre la factura con una palabra compartida
    por varias propiedades no se puede resolver solo, y lo elige el humano al revisar."""
    token_index = build_token_index(properties)
    vistas = {}
    for word in set(content_norm.split()):
        prop = token_index.get(word)
        if prop:
            vistas[prop["nombre"]] = prop
    return list(vistas.values())


def build_generic_words(properties: list[dict]) -> set[str]:
    """Palabras que comparten dos o más propiedades: "rental", "holidays", "apartamento",
    "oropesa", "castellon". Igual que en build_token_index, se deducen del propio CSV.

    Importan para el match difuso porque casi todas las propiedades se llaman "<algo> Rental
    Holidays REF NNN": comparada contra el nombre completo, una factura titulada "28 rental
    holidays sl" supera el umbral contra cualquiera de las 63, y la que gana es azar."""
    owners: dict[str, set[str]] = {}
    for prop in properties:
        for field in prop["campos_norm"]:
            for word in set(field.split()):
                owners.setdefault(word, set()).add(prop["nombre"])
    return {word for word, duenos in owners.items() if len(duenos) > 1}


def _discriminante(field: str, genericas: set[str]) -> str:
    return " ".join(word for word in field.split() if word not in genericas)


def _is_fuzzy_eligible(field: str) -> bool:
    # Los campos puramente numéricos (p.ej. un ID interno) dan coincidencias
    # difusas falsas casi con cualquier texto largo, porque dos secuencias de
    # dígitos casi siempre comparten algún parecido casual.
    return len(field) >= 6 and not field.replace(" ", "").isdigit()


def _fuzzy_search(
    text_norm: str, properties: list[dict], threshold: float, genericas: set[str]
) -> tuple[Optional[dict], float]:
    best, best_score = None, 0.0
    for prop in properties:
        # Se puntúa contra el campo COMPLETO: partial_ratio busca el mejor trozo del texto que
        # se parezca al campo, así que cuanto más corto es el campo más fácil es que algo se le
        # parezca por casualidad. Recortarlo a sus palabras discriminantes dispara los falsos
        # positivos en vez de reducirlos; la parte genérica se filtra después, no aquí.
        for field in filter(_is_fuzzy_eligible, prop["campos_norm"]):
            score = fuzz.partial_ratio(field, text_norm)
            if score > best_score:
                best, best_score = prop, score
    if best is None or best_score < threshold:
        return None, 0.0
    # Un parecido alto contra "<algo> Rental Holidays REF NNN" puede venir entero de la parte
    # que comparten las 63 propiedades. Para aceptarlo, el texto tiene que contener además
    # alguna palabra propia de ESA propiedad: si no, el ganador entre las 63 es el azar.
    if not any(
        palabra in text_norm.split()
        for field in best["campos_norm"]
        for palabra in _discriminante(field, genericas).split()
    ):
        return None, 0.0
    return best, best_score


def match_invoice(
    filename_norm: str,
    content_norm: str,
    properties: list[dict],
    token_index: dict[str, dict],
    threshold: float = DEFAULT_FUZZY_THRESHOLD,
    identifier_index: Optional[dict[str, dict]] = None,
    genericas: Optional[set[str]] = None,
) -> tuple[Optional[dict], str, float]:
    genericas = genericas if genericas is not None else build_generic_words(properties)
    prop = _exact_search(filename_norm, properties) or _token_search(filename_norm, token_index)
    if prop:
        return prop, "nombre_archivo (exacto)", 100.0

    prop, score = _fuzzy_search(filename_norm, properties, threshold, genericas)
    if prop:
        return prop, "nombre_archivo (fuzzy)", round(score, 1)

    if identifier_index:
        prop = _identifier_search(content_norm, identifier_index)
        if prop:
            return prop, "contenido (identificador)", 100.0

    prop = _exact_search(content_norm, properties)
    if prop:
        return prop, "contenido (exacto)", 100.0

    prop, score = _fuzzy_search(content_norm, properties, threshold, genericas)
    if prop:
        return prop, "contenido (fuzzy)", round(score, 1)

    return None, "sin_identificar", 0.0


def groq_match_invoice(
    filename: str,
    raw_text: str,
    properties: list[dict],
    api_key: str,
    model: Optional[str] = None,
) -> tuple[Optional[dict], float, str]:
    """Último recurso para facturas que el matching local no pudo resolver: le pasa a un LLM
    (via Groq) el texto de la factura y la lista de propiedades para que elija la que corresponde.
    Solo se llama para facturas ya en Sin_identificar, así se manda el mínimo de datos a la nube.

    Se le pide al modelo que cite la evidencia textual en la que se basó, pero esa cita NO se usa
    para aceptar/rechazar automáticamente: probado contra facturas reales, el modelo (llama-3.1-8b
    -instant, el rápido/gratuito de Groq) a veces "evidencia" el propio nombre de la propiedad en
    vez de texto real de la factura, y en facturas ambiguas (recibos de luz/gas facturados a la
    empresa, sin ninguna pista de que unidad es) alucina con confianza alta y de forma inconsistente
    entre llamadas. La evidencia se devuelve solo para que un humano la vea al revisar el resultado;
    todo match "groq (IA)" debe tratarse como sugerencia a confirmar, no como un hecho verificado."""
    from groq import Groq

    client = Groq(api_key=api_key)
    opciones = [{"idx": i, "nombre": p["nombre"]} for i, p in enumerate(properties)]
    texto_recortado = raw_text[:1200]
    prompt = (
        "Sos un clasificador ESTRICTO de facturas de alquiler vacacional. Solo elegis una propiedad si "
        "el nombre de archivo o el texto tienen una pista CLARA y ESPECIFICA de esa propiedad (su nombre, "
        "su referencia, o una direccion de la unidad en si). Muchas facturas son boletas genericas de "
        "proveedores (luz, gas, telefono, plataformas online) que solo mencionan la direccion FISCAL de la "
        "empresa Rental Holidays Experience SL, sin decir a que propiedad corresponde: en esos casos NO hay "
        "pista real, y debes responder idx=null. Ante la duda, preferi null: es peor asignar mal una factura "
        "que dejarla sin clasificar.\n\n"
        f"Nombre de archivo: {filename}\n"
        f"Texto extraido de la factura (puede tener errores de OCR):\n{texto_recortado}\n\n"
        f"Propiedades disponibles (JSON):\n{json.dumps(opciones, ensure_ascii=False)}\n\n"
        'Responde SOLO con JSON: {"idx": <indice de la propiedad correcta, o null si no hay pista clara>, '
        '"confianza": "alta"|"media"|"baja", "evidencia": "<fragmento EXACTO (copiado tal cual, sin '
        'parafrasear) del nombre de archivo o del texto que justifica tu eleccion>"}.'
    )
    response = client.chat.completions.create(
        model=model or GROQ_DEFAULT_MODEL,
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_object"},
        temperature=0,
    )
    data = json.loads(response.choices[0].message.content)
    idx = data.get("idx")
    if idx is None or not isinstance(idx, int) or not (0 <= idx < len(properties)):
        return None, 0.0, ""

    evidencia = str(data.get("evidencia", "")).strip()
    confianza = GROQ_CONFIDENCE_BY_LEVEL.get(str(data.get("confianza", "")).lower(), 60.0)
    return properties[idx], confianza, evidencia


def process_invoices(
    source_path,
    properties: list[dict],
    output_dir,
    threshold: float = DEFAULT_FUZZY_THRESHOLD,
    on_file_processed: Optional[Callable[[str], None]] = None,
    groq_api_key: Optional[str] = None,
    identifier_csv=None,
) -> tuple[list[dict], list[dict]]:
    """Empareja cada factura del origen con una propiedad y la copia a
    output_dir/<propiedad>/<mes>, usando la carpeta de mes que ya trae el origen.

    source_path puede ser un ZIP o una carpeta ya descomprimida. properties ya viene
    cargado (de CSV o de la API de Lodgify) con load_properties/load_properties_from_api.

    Devuelve (detalle, properties). detalle es una lista de dicts con
    archivo/propiedad/metodo_match/confianza/importe.
    """
    token_index = build_token_index(properties)
    genericas = build_generic_words(properties)
    identifier_index = load_identifier_map(identifier_csv, properties) if identifier_csv else {}

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    es_carpeta = isinstance(source_path, (str, Path)) and Path(source_path).is_dir()
    if es_carpeta:
        root = Path(source_path)
        extract_dir = None
    else:
        root = extract_dir = output_dir / "_extraido"
        with zipfile.ZipFile(source_path) as zf:
            zf.extractall(extract_dir)

    invoice_files = sorted(
        p
        for p in root.rglob("*")
        if p.is_file() and p.suffix.lower() in INVOICE_EXTS and not p.name.startswith((".", "__MACOSX"))
    )

    detalle = []
    for f in invoice_files:
        try:
            raw_text = extract_invoice_text(f)
        except Exception:
            raw_text = ""

        content_norm = normalize_text(raw_text)
        filename_norm = normalize_text(f.stem)
        prop, method, confidence = match_invoice(
            filename_norm, content_norm, properties, token_index, threshold, identifier_index, genericas
        )
        evidencia_groq = ""

        if prop is None and groq_api_key:
            try:
                groq_prop, groq_confidence, evidencia_groq = groq_match_invoice(f.name, raw_text, properties, groq_api_key)
            except Exception as exc:
                method = f"sin_identificar (groq fallo: {exc})"
            else:
                if groq_prop:
                    # OJO: prop queda en None a propósito. Un match de Groq es una sugerencia
                    # para revisar a mano, no una asignación confirmada — no se archiva dentro
                    # de la carpeta de la propiedad ni se suma a su gasto total, para no
                    # contaminar ninguna de las dos con una alucinación del modelo.
                    method = f"groq (IA) sugiere: {groq_prop['nombre']} - revisar"
                    confidence = groq_confidence

        carpeta = prop["carpeta"] if prop else "Sin_identificar"
        nombre_propiedad = prop["nombre"] if prop else "Sin identificar"
        mes_carpeta = detect_month_folder(f.relative_to(root))

        dest_dir = output_dir / carpeta / mes_carpeta
        dest_dir.mkdir(parents=True, exist_ok=True)
        nombre_destino = _nombre_sin_colision(dest_dir, f.name)
        shutil.copy2(f, dest_dir / nombre_destino)

        detalle.append(
            {
                "archivo": nombre_destino,
                "propiedad": nombre_propiedad,
                "metodo_match": method,
                "confianza": confidence,
                "importe": extract_amount(raw_text),
                "evidencia_ia": evidencia_groq,
            }
        )
        if on_file_processed:
            on_file_processed(f.name)

    if extract_dir is not None:
        shutil.rmtree(extract_dir, ignore_errors=True)
    return detalle, properties


def build_resumen(detalle: list[dict]) -> pd.DataFrame:
    columns = ["propiedad", "num_facturas", "gasto_total"]
    if not detalle:
        return pd.DataFrame(columns=columns)

    df = pd.DataFrame(detalle)
    df["importe_num"] = df["importe"].fillna(0.0)
    resumen = (
        df.groupby("propiedad", sort=False)
        .agg(num_facturas=("archivo", "count"), gasto_total=("importe_num", "sum"))
        .reset_index()
    )
    total_row = pd.DataFrame(
        [
            {
                "propiedad": "TOTAL GENERAL",
                "num_facturas": resumen["num_facturas"].sum(),
                "gasto_total": resumen["gasto_total"].sum(),
            }
        ]
    )
    return pd.concat([resumen, total_row], ignore_index=True)[columns]


def build_excel_report(detalle: list[dict], resumen_df: pd.DataFrame, output_path) -> None:
    detalle_df = pd.DataFrame(
        detalle, columns=["archivo", "propiedad", "metodo_match", "confianza", "importe", "evidencia_ia"]
    )
    with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
        detalle_df.to_excel(writer, sheet_name="Detalle facturas", index=False)
        resumen_df.to_excel(writer, sheet_name="Resumen por propiedad", index=False)


def zip_folder(folder_path, zip_output_path) -> None:
    folder_path = Path(folder_path)
    with zipfile.ZipFile(zip_output_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in folder_path.rglob("*"):
            if f.is_file():
                zf.write(f, f.relative_to(folder_path))
