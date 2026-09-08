# Documentación técnica — Organizador de facturas

Documento de referencia interna del proyecto: qué hace, cómo está construido, por qué cada
decisión de diseño es como es, y qué limitaciones reales tiene medidas contra facturas de
producción. El [README.md](README.md) es la guía de uso; esto es el detalle de implementación.

---

## 1. Problema que resuelve

Rental Holidays gestiona ~63 propiedades de alquiler vacacional. Cada trimestre llega un ZIP con
todas las facturas mezcladas (luz, gas, agua, internet, limpieza, mantenimiento, plataformas como
Lodgify/Stripe/Meta/Google), organizadas como mucho por mes, con nombres de archivo arbitrarios:
`055038000035277 (1).pdf`, `escaner sharp_20260107_152643.pdf`, `2025_EL_06050_14MZ.pdf`.

El trabajo manual consiste en abrir cada PDF, ver a qué propiedad pertenece, moverlo a su carpeta y
apuntar el importe. La app automatiza ese triaje: reparte las facturas en carpetas por propiedad y
mes, extrae el importe de cada una y genera un Excel con el detalle y el gasto agregado por
propiedad.

**Objetivo explícito de diseño: nunca clasificar mal.** Una factura en `Sin_identificar` cuesta un
minuto de revisión manual. Una factura archivada bajo la propiedad equivocada contamina el gasto de
dos propiedades a la vez y no se detecta hasta el cierre contable. Todo el sistema está sesgado
hacia el falso negativo.

---

## 2. Arquitectura

Dos archivos, separación estricta entre lógica y UI:

| Archivo | Responsabilidad |
|---|---|
| [core.py](core.py) | Todo el pipeline: extracción de texto, matching, importes, Excel, ZIP. Cero imports de Streamlit. |
| [app.py](app.py) | Interfaz Streamlit: subida de archivos, slider de umbral, avisos, tablas y descarga. |
| [organizar_facturas.py](organizar_facturas.py) | CLI sin UI, para correr un lote desde terminal o desde un cron. |

La separación no es cosmética: `core.py` es ejecutable sin UI, y eso es lo que permite que
[tests_synthetic/run_test.py](tests_synthetic/run_test.py) verifique el pipeline entero de punta a
punta sin levantar un navegador, y que el mismo pipeline tenga dos frontales (web y CLI) sin
duplicar una línea de lógica.

### Dependencias

| Paquete | Para qué |
|---|---|
| `streamlit` | UI web local |
| `pandas` | CSV de propiedades, agregación del resumen |
| `pdfplumber` | Extracción de texto de PDFs con capa de texto |
| `pytesseract` + `Pillow` | OCR de facturas escaneadas (JPG/PNG) |
| `pypdfium2` | Rasteriza a imagen las páginas de PDFs escaneados, para poder pasarlos por OCR |
| `rapidfuzz` | Coincidencia difusa (`partial_ratio`) |
| `openpyxl` | Escritura del `.xlsx` |
| `groq` | Cliente del fallback por IA (opcional) |

Un solo binario de sistema hay que instalar aparte: `pytesseract` es un binding de **Tesseract OCR**,
que además necesita el paquete de idioma español (`spa`). `pypdfium2` no: trae PDFium compilado
dentro de la propia rueda de Python, así que rasteriza sin depender de nada instalado en el sistema.

Si Tesseract falta, la app no rompe: la extracción de esa factura lanza excepción,
`process_invoices` la captura y sigue con el texto vacío, así que la factura se empareja solo por
nombre de archivo y su importe queda sin leer. **Es una degradación silenciosa**: no hay aviso en
pantalla, solo más facturas en `Sin_identificar` de lo esperado.

Ojo con una variante traicionera de esa degradación: si Tesseract está instalado pero el proceso
arrancó con una copia vieja del entorno (una terminal abierta antes de tocar el `PATH` o
`TESSDATA_PREFIX`), el binario "no existe" solo para ese proceso. El síntoma es idéntico al de no
tenerlo instalado. Ante resultados que empeoran de golpe, comprobar `tesseract --list-langs`
**desde la misma terminal** que va a correr el script, no desde otra.

---

## 3. Flujo de procesamiento

`core.process_invoices()` ([core.py:494](core.py#L494)) es el punto de entrada. Recibe el ZIP, la
lista de propiedades ya cargada y el directorio de salida, y devuelve `(detalle, properties)`.

1. **Cargar propiedades** — el llamador la carga antes de llamar: la app la trae de la API de
   Lodgify (`load_properties_from_api()`), el CLI la lee de un CSV (`load_properties()`). Ambas
   construyen la misma lista de propiedades con sus campos normalizados.
2. **Construir el índice de tokens** — `build_token_index()` calcula qué palabras identifican una
   única propiedad sin ambigüedad.
3. **Extraer el ZIP** a `<salida>/_extraido`, y recorrerlo recursivamente buscando `.pdf`, `.jpg`,
   `.jpeg`, `.png`. Se ignoran archivos ocultos y basura de macOS (`__MACOSX`).
4. **Por cada factura**:
   - Extraer texto (sección 4.1). Si falla, el texto queda vacío y la factura sigue el pipeline con
     lo que haya en el nombre de archivo.
   - Normalizar nombre y contenido.
   - Emparejar con una propiedad (cascada de 4 pasos, sección 5).
   - Si no hay match y hay `GROQ_API_KEY`, consultar al LLM como último recurso (sección 7).
   - Determinar la carpeta de mes a partir de la ruta dentro del ZIP (sección 6).
   - Copiar (`shutil.copy2`, preserva timestamps) a `<salida>/<propiedad>/<mes>/`.
   - Extraer el importe del texto y registrar la fila del detalle.
5. **Borrar** `_extraido` y devolver el detalle.

Después, `app.py` llama a `build_resumen()`, `build_excel_report()` y `zip_folder()` para producir
el entregable.

El callback opcional `on_file_processed` permite a la UI ir mostrando el avance factura a factura
sin que `core.py` sepa nada de Streamlit.

---

## 4. Extracción de texto y normalización

### 4.1 De qué se lee el texto de cada factura

`extract_invoice_text()` decide por extensión, y para PDFs hay dos caminos:

| Tipo de archivo | Camino |
|---|---|
| `.jpg` / `.jpeg` / `.png` | Pillow abre la imagen → `pytesseract.image_to_string(lang=OCR_LANG)` |
| PDF con capa de texto | `pdfplumber`, página a página |
| PDF escaneado (sin capa de texto) | `pypdfium2` rasteriza a 300 dpi → Tesseract, igual que una foto |

`OCR_LANG` se resuelve una vez al importar el módulo: pide `spa+eng` si Tesseract tiene instalado
el español, y cae a `eng` si no. Pedir un idioma ausente es un error duro de Tesseract, y el
instalador de Windows solo trae inglés. Medido sobre el lote real, correr con `eng` en vez de
`spa+eng` cuesta 7 importes de 65: el OCR en inglés sobre una factura española degrada lo justo
para que los rótulos de total dejen de casar. Merece la pena instalar el español.

El tercer caso es el que resuelve `ocr_pdf_pages()` ([core.py:193](core.py#L193)). Muchas facturas
llegan como PDF pero son solo la imagen de un escáner metida en un contenedor PDF: `pdfplumber`
devuelve cadena vacía y, sin este camino, la factura entra al matcher con el contenido en blanco.

El disparador es `MIN_PDF_TEXT_CHARS = 40`: si el texto extraído por `pdfplumber` no llega a 40
caracteres útiles, se asume que no hay capa de texto real y se pasa al OCR. No se usa "cero
caracteres" como umbral porque algunos PDFs de imagen traen solo un pie de página del escáner —
suficiente para parecer texto, inútil para el matching.

`OCR_MAX_PAGES = 3` acota el coste: cada página cuesta ~1 s de rasterizado y ~5 s de OCR, así que
un PDF largo escaneado podría dominar el tiempo de todo el lote. Sobre el lote real, activar este
camino sube el tiempo total de 65 s a 121 s (12 de 76 PDFs son escaneos puros).

### 4.2 Normalización y carga del CSV

`normalize_text()` ([core.py:116](core.py#L116)) reduce cualquier cadena a minúsculas, sin tildes
(descomposición NFKD + descarte de diacríticos) y sin caracteres especiales, colapsando espacios.
Es la base de todas las comparaciones: `"Chalet Borriol Golf, REF 028"` y `"chalet borriol golf ref
028"` son la misma cadena tras normalizar.

`load_properties()` ([core.py:218](core.py#L218)) detecta las columnas por alias, sin exigir un
formato rígido:

- Referencia: `ref`, `referencia`, `codigo`, `id`
- Dirección: `direccion`, `address`, `domicilio`
- Nombre (opcional): `name`, `nombre`, `propiedad`, `titulo`

El delimitador se detecta solo (`sep=None, engine="python"`), así que sirven tanto CSVs con `,` como
los exportados con `;`. Si faltan referencia o dirección, se lanza `ValueError` con un mensaje
explicativo que `app.py` muestra como error en pantalla.

Cada propiedad queda representada así:

```python
{
    "ref": "...", "direccion": "...", "nombre": "...",
    "campos_norm":  [ref_norm, direccion_norm, nombre_norm],  # para búsqueda exacta y fuzzy
    "campos_token": [ref_norm, nombre_norm],                  # para búsqueda por token suelto
    "carpeta": "nombre saneado para el sistema de archivos",
}
```

**La dirección se excluye deliberadamente de `campos_token`** ([core.py:246](core.py#L246)). Los
nombres de calle y ciudad ("Castelló", "Virgen del Carmen", "Amplaries") aparecen en casi cualquier
factura — empezando por la dirección fiscal de la propia empresa emisora — y como token suelto
disparan falsos positivos masivos. Para búsqueda exacta de la dirección completa sí se usa, porque
ahí la cadena larga es una señal real.

---

## 5. El algoritmo de emparejamiento

`match_invoice()` ([core.py:406](core.py#L406)) prueba cuatro estrategias en orden de confianza
decreciente y devuelve al primer acierto:

| # | Estrategia | Fuente | Confianza reportada |
|---|---|---|---|
| 1 | Exacta o por token único | Nombre de archivo | 100 |
| 2 | Difusa (`partial_ratio` ≥ umbral) | Nombre de archivo | score real |
| 3 | Identificador unívoco (CUPS, contrato, contador) | Contenido (texto/OCR) | 100 |
| 4 | Exacta | Contenido | 100 |
| 5 | Difusa | Contenido | score real |
| — | Ninguna → `Sin_identificar` | | 0 |

El nombre de archivo va antes que el contenido porque, cuando alguien nombra un archivo
`VILLA GRAO CASTELLON 20_11_2025 - 25_12_2025.pdf`, esa es una decisión humana explícita sobre a qué
propiedad pertenece; el contenido, en cambio, puede mencionar varias direcciones (la de la
propiedad, la fiscal del emisor, la de entrega).

### Búsqueda exacta — `_exact_search()`

Comprueba si algún campo normalizado de la propiedad aparece como substring del texto. Requiere
longitud ≥ 3 y, ante varias coincidencias, **gana la más larga**: si el texto contiene tanto
`"nerea"` como `"nerea ii urbanization aprt rental holidays ref 065"`, la segunda es más específica y
es la que se elige.

### Índice de tokens — `build_token_index()` ([core.py:329](core.py#L329))

Resuelve el caso de la factura nombrada solo `borriol.pdf`, donde no aparece la referencia completa.
Recorre todas las propiedades, acumula qué propiedades reclama cada palabra (≥ 4 caracteres, no
puramente numérica) y **conserva solo las palabras reclamadas por exactamente una propiedad**.

Así, `"borriol"` identifica unívocamente al Chalet Borriol Golf, mientras que `"apartamento"`,
`"rental"`, `"holidays"` o `"oropesa"` —compartidas por decenas de propiedades— quedan fuera del
índice automáticamente. La clave de diseño es que **no hay lista de stopwords hardcodeada**: la
ambigüedad se deduce del propio CSV, así que el sistema se adapta solo cuando cambia la cartera de
propiedades.

### Identificador unívoco — `load_identifier_map()` y `_identifier_search()`

Es la única vía que clasifica las facturas de suministros, que son la mayor parte del volumen real:
un recibo de luz no nombra la villa por ningún lado, solo trae el **CUPS** del punto de suministro
(`ES0021000012088785MM1F`), el nº de contrato y el nº de contador.

El script **no deduce formatos de código**. Lee un CSV auxiliar
([identificadores.csv](identificadores.csv)) con dos columnas obligatorias, `identificador` y
`propiedad`, y busca en el texto únicamente los valores que están en esa tabla, con delimitadores a
ambos lados para que un nº de contador corto no aparezca por dentro de un número de factura largo.
Así un identificador mal asignado solo puede ser un error del CSV, nunca una invención del script.
Ante varios identificadores en el mismo documento gana el más largo: si la factura trae el nº de
cliente (de la empresa) y el CUPS (del punto de suministro), manda el del punto de suministro.

La tabla se rellena a mano una vez. La que viene en el repo ya trae los 34 identificadores que
aparecen en el lote real (7 CUPS, 20 contratos, 7 contadores) con su proveedor, la dirección de
suministro y las facturas donde salen, para poder completarla sin abrir los PDFs. Solo están
rellenadas las 3 filas cuya dirección de suministro coincide con la dirección de la propiedad en el
maestro; el resto quedan en blanco a propósito, porque asignarlas sería adivinar.

### Búsqueda difusa — `_fuzzy_search()`

`rapidfuzz.fuzz.partial_ratio` contra cada campo, quedándose con el mejor score si supera el umbral
(82 por defecto, ajustable en la UI entre 50 y 100).

El filtro `_is_fuzzy_eligible()` descarta campos de menos de 6 caracteres y **campos puramente
numéricos**. Motivo: dos secuencias de dígitos casi siempre comparten un parecido casual, así que un
ID interno como `263584` da coincidencias difusas falsas contra casi cualquier factura larga
(números de factura, CIFs, códigos de contador, IBANs).

Y una segunda barrera, `build_generic_words()`: casi todas las propiedades se llaman
`<algo> Rental Holidays REF NNN`, así que una factura titulada `28 rental holidays sl.pdf` supera el
82 % contra las 63 y la que gana es azar — en el lote real se llevaba 1.985,61 € de una carpintería
a la propiedad equivocada. Para aceptar un match difuso se exige ahora que el texto contenga además
**alguna palabra propia de esa propiedad**, entendiendo por propia la que no comparte con ninguna
otra; se deduce del CSV igual que el índice de tokens, sin listas hardcodeadas.

Lo que **no** funciona, y se probó: recortar los campos a sus palabras discriminantes antes de
puntuar. `partial_ratio` busca el mejor trozo del texto parecido al campo, así que cuanto más corto
es el campo más fácil es parecerse por casualidad; al acortar las agujas, una sola propiedad se
llevó 7 facturas ajenas. La parte genérica se filtra después de puntuar, no antes.

---

## 6. Detección del mes

`detect_month_folder()` ([core.py:73](core.py#L73)) **no adivina la fecha del contenido**. Recorre
las carpetas de la ruta original dentro del ZIP, de la más cercana al archivo hacia afuera, y busca
un nombre de mes en español (incluye la variante `setiembre`).

La razón: el usuario ya organiza las facturas por carpeta de mes antes de subirlas, y esa es la
señal más fiable que existe. Una factura de electricidad puede llevar dentro la fecha de emisión, la
del periodo facturado, la de vencimiento y la de cargo — cuatro fechas distintas, ninguna
obviamente "la correcta".

Regla de desempate: **una carpeta que menciona más de un mes no cuenta**. La carpeta real
`OCTUBRE-NOVIEMBRE-DICIEMBRE` es ambigua, así que se ignora y se sigue buscando hacia afuera; si no
aparece ninguna carpeta con un único mes, la factura cae en `00-Sin_mes`. Prefijo numérico
(`10-Octubre`) para que las carpetas ordenen cronológicamente en el explorador.

---

## 7. Extracción de importes

Es la parte que más se ha reescrito, porque medida contra los 79 documentos reales la versión
inicial acertaba el **49 %** y se equivocaba con seguridad en 23 facturas: reportaba 7.906,43 €
cuando el gasto real del trimestre era 13.924,95 €, un 43 % por debajo. La versión actual acierta el
**82 %** y solo se equivoca en 2 (ambas escaneos ilegibles).

`extract_amount()` prueba estos rótulos, del más específico al más genérico:

1. `total a pagar`
2. `total importe factura`
3. `importe total`
4. `total factura`
5. `total impuestos incluidos`
6. `total in eur` (facturas en inglés: Google, Stripe)
7. `total` a secas — con `(?<!sub)(?<!sub )` para no capturar el subtotal (el lookbehind original
   fallaba con `Sub Total`, escrito con espacio) y con `\b` detrás para que `totaling €384.00` no
   se lea como un total

Cuatro decisiones que salieron de fallos concretos del lote:

- **El número admite formato español e inglés.** El orden de las alternativas importa: `1.641` tiene
  que leerse como miles (1641) antes de que la alternativa de decimal con punto lo parta en 1,64.
  Sin esto, `parse_amount_es("1.000")` devolvía 1.0 y `193.6` se truncaba a 193.
- **El hueco entre rótulo e importe solo admite separadores y la moneda**, no texto libre. Con texto
  libre, la letra pequeña ("…el importe total de la factura.") enganchaba el primer número que
  viniera detrás, que era el `21` del porcentaje de IVA.
- **Entre varias apariciones del mismo rótulo gana la que lleva céntimos.** El importe cobrado
  siempre los tiene; los números redondos que acompañan a las otras apariciones son cantidades de
  línea o porcentajes. Sin esto, `coste total\n1 Puerta blindada…` daba un importe de 1,00 €.
- **Validación contra el desglose de IVA**, en `_corregir_si_es_base()`. Muchos proveedores solo
  rotulan el total de la base ("Total albarán: 44,64") y dejan el importe cobrado sin etiqueta. Si
  el valor capturado multiplicado por 21 %, 10 % o 4 % **aparece literalmente en el documento**,
  lo capturado era la base y el bueno es ese otro. No es una estimación: el número corregido está
  impreso en la factura. Recuperó 6 facturas del lote sin producir ni una corrección falsa.

Si ningún patrón coincide, el importe queda `None` y en el resumen suma 0. Esa distinción es la que
hay que mirar al revisar: el CLI lista al final las facturas sin importe legible, y en el Excel se
ven como celda vacía. Preferimos dejarlo vacío antes que imprimir una cifra plausible y equivocada,
que es justo lo que hacía la versión anterior.

---

## 8. Fallback por IA (Groq) — opt-in y no vinculante

`groq_match_invoice()` ([core.py:440](core.py#L440)) se invoca **solo** para facturas que ya quedaron
sin identificar tras las cuatro estrategias locales, y solo si existe `GROQ_API_KEY` en el entorno.
Se le manda el nombre del archivo y los primeros 1200 caracteres del texto, más la lista de nombres
de propiedades. El prompt es explícitamente restrictivo: pide `idx=null` ante la duda y advierte que
muchas facturas son boletas genéricas que solo mencionan la dirección fiscal de la empresa.

**Resultado medido contra facturas reales — y por qué el match no se aplica:**

- `llama-3.1-8b-instant` (el modelo rápido/gratuito) **alucina con confianza alta** en facturas
  genéricas de luz, gas o teléfono facturadas a la empresa, sin ninguna pista de qué unidad son.
- La misma factura ambigua recibió respuestas **distintas e igual de "seguras"** en corridas
  separadas, con `temperature=0`.
- Pedirle evidencia textual literal no lo filtra: a veces cita el propio nombre de la propiedad en
  lugar de texto real de la factura.

Por eso, cuando Groq propone una propiedad, `prop` **se deja en `None` a propósito**
([core.py:555](core.py#L555)): el archivo se copia igualmente en `Sin_identificar/<mes>/`, no entra
en la carpeta de la propiedad y no suma a su gasto total. La sugerencia solo se refleja en dos
columnas del Excel — `metodo_match` = `groq (IA) sugiere: <propiedad> - revisar` y `evidencia_ia` —
para que un humano decida y mueva el archivo a mano. La UI avisa aparte cuántas facturas están en ese
estado.

Si la llamada falla (cuota diaria agotada, red caída), se captura la excepción, se anota en
`metodo_match` y el procesamiento del lote continúa.

---

## 9. Salidas

### Estructura de carpetas

```
facturas_organizadas/
├── Chalet Borriol Golf RentalHolidays REF 028/
│   └── 12-Diciembre/BORRIOL 25_11_2025 - 05_12_2025.pdf
├── Nerea II Urbanization Aprt Rental Holidays REF 065/
│   └── 11-Noviembre/…
├── Sin_identificar/
│   ├── 00-Sin_mes/…
│   ├── 10-Octubre/…
│   ├── 11-Noviembre/…
│   └── 12-Diciembre/…
└── informe_gastos.xlsx
```

`sanitize_folder_name()` sustituye `\ / * ? : " < > |` por `_` y recorta espacios y puntos finales,
para que cualquier nombre de propiedad del CSV sea un nombre de carpeta válido en Windows.

### Excel — `informe_gastos.xlsx`

Dos hojas:

- **Detalle facturas**: una fila por factura con `archivo`, `propiedad`, `metodo_match`,
  `confianza`, `importe`, `evidencia_ia`. Las columnas `metodo_match` y `confianza` son el material
  de auditoría: permiten ordenar por confianza y revisar primero los matches difusos flojos.
- **Resumen por propiedad**: `num_facturas` y `gasto_total` por propiedad, más una fila final
  `TOTAL GENERAL`. Los importes no leídos cuentan como 0 (`fillna(0.0)`), así que el número de
  facturas y el gasto no siempre se corresponden.

---

## 10. Verificación

### Tests sintéticos

[tests_synthetic/generate_data.py](tests_synthetic/generate_data.py) genera con `reportlab` y
`Pillow` cuatro facturas de prueba, cada una cubriendo un camino distinto del matcher: referencia en
el nombre del archivo, dirección en el contenido de un PDF, referencia dentro de una **imagen
escaneada** (fuerza el camino OCR) y una factura sin ninguna coincidencia.

> `reportlab` solo lo necesita el generador de datos de prueba, no está en `requirements.txt` porque
> la app no lo usa.

[tests_synthetic/run_test.py](tests_synthetic/run_test.py) ejecuta el pipeline completo sobre ese ZIP
y verifica siete aserciones: los tres caminos de match, el caso `Sin_identificar` y tres importes
(incluyendo `1.234,56` para probar el formato español de miles). Sale con código 1 si algo falla.

Detecta si Tesseract está disponible con `pytesseract.get_tesseract_version()` y, si no lo está,
**salta explícitamente los dos checks de OCR imprimiendo un aviso** en vez de fingir que pasaron.
Con Tesseract instalado pasan los 7.

```bash
python tests_synthetic/generate_data.py   # regenerar el ZIP de prueba (requiere reportlab)
python tests_synthetic/run_test.py        # ejecutar la verificación
```

### Banco de pruebas sobre las facturas reales

Los 4 documentos sintéticos cubren los caminos del código, pero no dicen nada de la precisión real.
Para eso se leyó a mano el total de cada una de las 79 facturas del trimestre y se guardó como
referencia; con eso, cada cambio en la extracción de importes se puede puntuar en vez de discutir.
Es lo que convirtió el trabajo sobre `extract_amount()` en medición y no en intuición: varias
heurísticas que parecían razonables (coger la última aparición del rótulo, recortar los campos a sus
palabras discriminantes) resultaron ser peores y se descartaron con el número delante.

### Resultado sobre datos reales

Corrida sobre el ZIP `OCTUBRE-NOVIEMBRE-DICIEMBRE` (79 facturas, 63 propiedades):

| | Antes | Ahora |
|---|---|---|
| Importes correctos | 38/77 (49 %) | **63/77 (82 %)** |
| Importes mal extraídos (cifra equivocada) | 23 | **2** |
| Importes no leídos (celda vacía, a revisar) | 16 | 12 |
| Facturas clasificadas en una propiedad | 7 | **9** |
| De ellas, confirmadas contra el maestro | — | **7** (ver más abajo) |
| Asignaciones a la propiedad equivocada, por parecido de nombre | 1 (1.985,61 €) | **0** |

La fila de las confirmadas es la que importa: de las 9 asignaciones, 7 se sostienen al cruzar la
dirección del suministro con el maestro y 2 no. Ninguna de esas 2 es del tipo de error que se
corrigió (llevarse una factura a una propiedad sin relación); las dos apuntan al residencial o al
pueblo correcto pero no a la vivienda concreta.

Las dos facturas con importe equivocado son escaneos donde el OCR confunde dígitos (lee `25,00` como
`29.00`); ahí no hay arreglo por software, hay que mirar el papel.

### Confirmación de cada asignación contra `propiedades activas.csv`

Que el matcher asigne una propiedad no prueba que la asignación sea correcta: lo único que prueba es
que algo del documento se pareció a algo del maestro. La comprobación que sí vale es cruzar la
**dirección del punto de suministro que trae la factura** con la **dirección de esa propiedad en
`propiedades activas.csv`**. Hecho una por una sobre las 9 asignadas:

| Factura | Propiedad asignada | Dirección en la factura | Dirección en el maestro | |
|---|---|---|---|---|
| `FACTURA CORAL 24 NOV…` | Apartamento Coral lll Ref 035 | AV CENTRAL 28 202 8, ORPESA | Av Central 28, escalera 1 apartamento 202 piso 8 | ✅ |
| `BORRIOL 25_11_2025…` | Chalet Borriol Golf REF 028 | CL DELS CAMPS 1 BAJO, BORRIOL | Avinguda Dels Camps | ✅ |
| `VILLA GRAO CASTELLON…` | Villa Grao Benicasim REF 026 | AV FERRANDIS SALVADOR 63 | Av. Ferrandis Salvador | ✅ |
| `F-SPL25-320.pdf` | Bungalow Santa Pola II REF 060 | C/ Virgen del Carmen, 57 1era-24 | C. Virgen del Carmen, 57 | ✅ |
| `nerea II 26_10_2025…` | Nerea II REF 065 | AV ERMITA SANT AN, ESC. 3, 1º H | Avenida Ermita San Antonio | ✅ |
| `2025_EL_06102_51KM.pdf` | Nerea II REF 065 | AV ERMITA SANT AN, ESC. 3, 1º H | Avenida Ermita San Antonio | ✅ |
| `2025_EL_07344_51KM.pdf` | Nerea II REF 065 | AV ERMITA SANT AN, ESC. 3, 1º H | Avenida Ermita San Antonio | ✅ |
| `gas nerea II 13_09…` | Nerea II REF 065 | URB. RESIDENCIAL NEREA Nº1, **ESC 15**, 3, 1, H, PEÑISCOLA | Avenida Ermita San Antonio | ⚠️ |
| `movistar valdelinares .pdf` | Bungalow Esqui Valdelinares REF 066 | **Sol y Nieve**, CTA-10, Alcalá de la Selva | Urbanización **Vega de la Selva 1** | ⚠️ |

**7 confirmadas, 2 no.** Las dos dudosas comparten causa: entraron por `nombre_archivo (exacto)`,
es decir por una palabra del nombre del archivo, sin que nada del contenido respalde la asignación.

- **`gas nerea II`**: la factura es del portal Nerea nº1 **escalera 15**, y la propiedad del maestro
  está en la escalera 3 de Avenida Ermita San Antonio. Son dos viviendas distintas del mismo
  residencial de Peñíscola. El maestro solo tiene una "Nerea", así que o la factura es de una unidad
  que no está dada de alta, o la dirección del maestro está incompleta. Sus 29,49 € están hoy
  sumados a Nerea II REF 065 sin respaldo.
- **`movistar valdelinares`**: la dirección de la línea no coincide con la del maestro, y además hay
  **dos** propiedades en la misma dirección — `Copy of Copy of Bungalow Esqui Valdelinares REF 066` y
  `Bungalow Esqui II Ref 076` — así que ni siquiera con la dirección correcta se podría decidir cuál
  de las dos es. "Valdelinares" es la estación de esquí, no la vivienda.

Las 7 confirmadas se sostienen porque la dirección del suministro aparece en el documento y coincide
con la del maestro. Es la misma evidencia que se exigió para rellenar las 3 filas de
[identificadores.csv](identificadores.csv), y por eso esa tabla es el mecanismo fiable: un CUPS
identifica un punto de suministro concreto, mientras que una palabra en el nombre del archivo
identifica, como mucho, un pueblo.

**Regla operativa que se deriva de esto:** antes de dar por buena una asignación de la columna
`metodo_match`, mirar de qué método viene. `contenido (identificador)` y `contenido (exacto)` traen
evidencia dentro del documento. `nombre_archivo (exacto)` y `nombre_archivo (fuzzy)` dependen de cómo
alguien tituló el archivo y hay que confirmarlas a mano contra el maestro, que es justo lo que ha
fallado en 2 de 9.

Que 70 facturas queden en `Sin_identificar` es **el comportamiento correcto para estos datos**, no un
fallo. La mayoría del lote son documentos que objetivamente no contienen ninguna pista de propiedad:
boletas de Vodafone, Lodgify, Stripe, Meta, Google, la gestoría y tickets de supermercado, emitidos a
nombre de Rental Holidays Experience SL con la dirección fiscal de la empresa. Ni un humano leyendo
solo el PDF puede asignarlas. La vía para recuperarlas no es afinar el matcher: es rellenar
[identificadores.csv](identificadores.csv), y por eso la mejora de mayor impacto pendiente es esa
tabla, no una regla nueva.

---

## 11. Limitaciones conocidas

- **`identificadores.csv` está casi vacío**: 3 de 34 filas rellenadas. Hasta que se complete, las
  facturas de suministros seguirán cayendo en `Sin_identificar`. Es la mejora de mayor impacto
  pendiente y no requiere tocar código.
- **El match por nombre de archivo no se confirma solo**: el script no comprueba que la dirección del
  suministro impresa en la factura coincida con la del maestro, así que una factura titulada con el
  nombre de un pueblo o de un residencial se asigna a la propiedad que tenga esa palabra aunque sea
  otra vivienda. Es lo que pasa con `gas nerea II` y `movistar valdelinares` (sección 10). Mientras
  no exista esa comprobación, toda asignación cuyo `metodo_match` empiece por `nombre_archivo` hay
  que revisarla a mano.
- **Dos propiedades comparten dirección en el maestro**: `Copy of Copy of Bungalow Esqui Valdelinares
  REF 066` y `Bungalow Esqui II Ref 076` están las dos en `Urb. Vega de la Selva 1`. Ninguna regla
  basada en la dirección puede distinguirlas; solo un identificador de suministro puede.
- **Facturas multi-suministro**: 5 documentos del lote facturan varias propiedades a la vez (la
  cuenta de Vodafone con 11 líneas, el mantenimiento de piscinas de 4 villas). El modelo "una
  factura = una propiedad" no las representa: aunque se identificaran, su importe entero se
  imputaría a una sola. Hoy quedan en `Sin_identificar`, que para ellas es lo correcto.
- **Nombres sucios en el CSV**: entradas como `Copy of Copy of Bungalow Esqui Valdelinares REF 066`
  se propagan tal cual al nombre de carpeta. El sistema no limpia el maestro de propiedades.
- **OCR**: la calidad depende del escaneo. En el lote real, dos tickets escaneados dan importes
  equivocados porque el OCR confunde dígitos (`25,00` leído como `29.00`), y otros siete quedan sin
  importe legible. El OCR abre documentos que antes eran opacos, pero no los vuelve fiables.
- **Sin persistencia ni deduplicación**: cada corrida es independiente. Procesar dos veces el mismo
  lote duplica los archivos en la salida.
- **Importe único por factura**: se extrae un solo total. Facturas con varios conceptos, varios
  albaranes o notas de abono no se desglosan; si el escaneo mete dos tickets en un PDF, solo cuenta
  uno.
- **Sin memoria de correcciones**: mover a mano una factura de `Sin_identificar` a su propiedad no
  enseña nada al sistema. La forma de que la corrección persista es añadir su identificador a
  `identificadores.csv`.

---

## 12. Archivos del repositorio

| Ruta | Qué es |
|---|---|
| [core.py](core.py) | Pipeline completo, sin UI |
| [app.py](app.py) | Interfaz Streamlit |
| [organizar_facturas.py](organizar_facturas.py) | CLI: `python organizar_facturas.py <origen> <propiedades.csv> [-i identificadores.csv]` |
| [README.md](README.md) | Guía de instalación y uso |
| [requirements.txt](requirements.txt) | Dependencias Python |
| `propiedades activas.csv` | Maestro real de 63 propiedades (export de Lodgify) |
| [identificadores.csv](identificadores.csv) | Tabla identificador → propiedad. 34 identificadores del lote real, 3 rellenados |
| `OCTUBRE-NOVIEMBRE-DICIEMBRE -*.zip` | Lote real de facturas del trimestre (~17 MB) |
| `facturas_organizadas/` | Salida de la última corrida real |
| [tests_synthetic/](tests_synthetic/) | Generador de datos sintéticos y verificación end-to-end |
