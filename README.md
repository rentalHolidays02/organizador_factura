# Organizador de facturas

App Streamlit para separar por propiedad las facturas (PDF/JPG/PNG) que llegan
mezcladas dentro de un ZIP mensual, y generar un informe de gastos en Excel.

Todo el procesamiento es local. Opcionalmente se puede activar un fallback por IA (Groq) para las
facturas que el matching local no logra clasificar — ver [Fallback por IA (Groq)](#fallback-por-ia-groq-opcional).

## Instalación

### 1. Tesseract OCR (única dependencia de sistema)

Tesseract lee el texto de las facturas escaneadas. Rasterizar las páginas de un PDF que es solo un
escaneo lo hace `pypdfium2`, que se instala con el resto de paquetes de Python y no necesita nada
más — Poppler ya no hace falta.

**Linux (Debian/Ubuntu):**
```bash
sudo apt update
sudo apt install tesseract-ocr tesseract-ocr-spa
```

**macOS (Homebrew):**
```bash
brew install tesseract tesseract-lang
```

**Windows (winget):**
```powershell
winget install tesseract-ocr.tesseract
```

El instalador de Windows solo trae inglés y no añade Tesseract al PATH. Descarga `spa.traineddata`
desde [tessdata_fast](https://github.com/tesseract-ocr/tessdata_fast) a una carpeta propia junto a
los `.traineddata` que ya instaló, deja apuntando ahí `TESSDATA_PREFIX`, y añade
`C:\Program Files\Tesseract-OCR` al PATH de usuario.

Verifica desde una terminal **nueva** — los cambios de PATH no llegan a las que ya estaban abiertas:
```bash
tesseract --list-langs   # debe listar "spa" y "eng"
```

Sin Tesseract la app sigue funcionando, pero las facturas escaneadas (imágenes y PDF sin capa de
texto) quedan sin texto: solo se pueden emparejar por el nombre del archivo y su importe no se lee.
Sin el idioma español el OCR funciona igual pero peor: medido sobre el lote real, 7 importes menos
de 65.

### 2. Dependencias de Python

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## Uso desde terminal

```bash
python organizar_facturas.py <origen> <propiedades.csv> [-s salida] [-i identificadores.csv]
```

`<origen>` puede ser el ZIP tal cual llega o la carpeta ya descomprimida. Ejemplo:

```bash
python organizar_facturas.py "OCTUBRE-NOVIEMBRE-DICIEMBRE.zip" "propiedades activas.csv" \
    -s facturas_organizadas -i identificadores.csv
```

Al terminar imprime el resumen por propiedad, cuántas facturas quedaron en `Sin_identificar` y la
lista de las que no tienen importe legible.

## Uso con interfaz web

```bash
streamlit run app.py
```

1. Sube el ZIP con las facturas del mes (PDF y/o imágenes, sin importar cómo estén nombradas).
2. Sube el CSV de propiedades.
3. Pulsa **Procesar facturas**.
4. Revisa el resumen por propiedad y el detalle de facturas. Si aparecen facturas
   en "Sin identificar", ábrelas y clasifícalas a mano.
5. Descarga el ZIP con las carpetas organizadas por propiedad + `informe_gastos.xlsx`.

El resultado se guarda en la carpeta `.sesion/` junto a `app.py`, así que recargar la página o
reiniciar Streamlit no obliga a volver a procesar el ZIP: la app retoma donde estabas, incluidas las
facturas que ya clasificaste a mano. Procesar un ZIP nuevo borra `.sesion/` y empieza de cero; para
descartar el trabajo sin procesar nada, borra esa carpeta.

Dentro de cada propiedad, las facturas quedan además en una subcarpeta por mes
(`APT-CENTRO/10-Octubre/factura.pdf`), tomando el mes de la carpeta en la que ya venía
cada factura dentro del ZIP que subiste (no se adivina ninguna fecha). Si una factura no
está dentro de ninguna carpeta con un mes reconocible, cae en `00-Sin_mes`.

## Formato del CSV de propiedades

Se admite cualquiera de estos nombres de columna (no distingue mayúsculas/tildes) y el delimitador (`,` o `;`) se detecta solo:

- Referencia: `ref`, `referencia`, `codigo` o `id`
- Dirección: `direccion` o `address`
- Nombre (opcional, recomendado si existe): `name`, `nombre` o `propiedad`. Si está, se usa también para el matching y como nombre de carpeta — suele ser el campo que más se parece a lo que aparece en el nombre de la factura.

Ejemplo:

```csv
ref,direccion
APT-CENTRO,Calle Mayor 12 3B Madrid
CASA-PLAYA,Avenida del Mar 45 Valencia
```

El nombre de carpeta de cada propiedad es la referencia (o la dirección si no hay referencia).

## Cómo empareja cada factura con una propiedad

Por orden de prioridad, hasta que alguno coincida:

1. La referencia o dirección aparece tal cual dentro del **nombre del archivo**.
2. Coincidencia difusa (fuzzy, umbral configurable en la UI, 82% por defecto) del **nombre del archivo**.
3. Un **identificador unívoco** de `identificadores.csv` (CUPS, nº de contrato, nº de contador) aparece dentro del **contenido**.
4. La referencia o dirección aparece tal cual dentro del **contenido** de la factura.
5. Coincidencia difusa del **contenido**.
6. Si nada coincide, la factura va a la carpeta `Sin_identificar` para revisión manual.

Un match difuso solo se acepta si el texto contiene además alguna palabra propia de esa propiedad.
Sin ese filtro, como casi todas se llaman `<algo> Rental Holidays REF NNN`, cualquier factura con la
razón social en el nombre supera el umbral contra las 63 y la ganadora es azar.

## Facturas de suministros: `identificadores.csv`

Un recibo de luz, gas o teléfono no nombra la villa por ningún lado: solo trae el código del punto de
suministro. Para clasificarlos hace falta esta tabla, con dos columnas obligatorias:

```csv
identificador;propiedad
ES0021000012088785MM1F;Chalet Borriol Golf RentalHolidays REF 028
```

`propiedad` tiene que coincidir con el nombre o la referencia de una fila del CSV de propiedades. El
archivo del repo ya trae los 34 identificadores que aparecen en el lote de OCT-NOV-DIC, con su
proveedor, la dirección de suministro y las facturas donde salen, para poder completarlo sin abrir
los PDFs. Solo se pueden dar por buenas las filas que rellenes tú: se dejan en blanco a propósito
las que no tienen evidencia.

El script busca únicamente los valores que estén en esa tabla, nunca deduce formatos de código, así
que una fila mal rellenada es el único modo de que una factura acabe en la propiedad equivocada.

El contenido se lee del texto del PDF cuando lo tiene. Si el PDF es un escaneo (sus páginas son
imágenes y no hay texto que extraer), se rasteriza a 300 dpi con `pypdfium2` y se pasa por OCR,
igual que una foto de factura. Solo se procesan las 3 primeras páginas de un PDF escaneado.

El importe de cada factura se extrae buscando rótulos como "total a pagar", "importe total" o "total
factura" seguidos de una cantidad, en formato español (`1.234,56`) o inglés (`70.06`). Si lo
capturado resulta ser la base imponible —porque el documento contiene literalmente ese valor más el
IVA— se corrige al importe con impuestos. Si no se puede leer con claridad, la celda queda **vacía**
en vez de rellenarse con una cifra plausible; el CLI las lista al terminar.

Medido contra las 79 facturas del trimestre real: 82 % de importes correctos, 2 equivocados (ambos
escaneos donde el OCR confunde dígitos) y 12 sin leer.

## Fallback por IA (Groq, opcional)

Las facturas que el matching local no logra clasificar (quedarían en `Sin_identificar`) se pueden
mandar, como último recurso, a un modelo de IA vía [Groq](https://console.groq.com) para que elija
la propiedad más probable. Es opt-in: sin `GROQ_API_KEY` seteada, la app funciona 100% local igual
que antes.

```bash
export GROQ_API_KEY="tu-api-key"     # obligatoria para activar el fallback
export GROQ_MODEL="llama-3.1-8b-instant"   # opcional, este es el default (rápido y con más cupo gratis)
streamlit run app.py
```

**Importante — probado contra facturas reales, no confíes ciegamente en estos matches:**
- Solo se manda a Groq el texto de las facturas que ya quedaron sin identificar (nombre de archivo +
  hasta 1200 caracteres del contenido) — es lo único que sale de tu máquina, y solo si seteás la key.
- El modelo rápido/gratuito (`llama-3.1-8b-instant`) **alucina con confianza alta** en facturas
  genéricas (recibos de luz/gas facturados a la empresa, sin ninguna pista real de qué propiedad
  es): en las pruebas, la misma factura ambigua recibió respuestas distintas e igual de "seguras"
  en corridas separadas. Pedirle evidencia textual tampoco lo filtra del todo — a veces cita el
  propio nombre de la propiedad en lugar de texto real de la factura.
- Por eso una sugerencia de Groq **nunca se archiva en la carpeta de una propiedad ni se suma a su
  gasto total**: la factura queda físicamente en `Sin_identificar/<mes>/` igual que las que no
  tuvieron ninguna pista, pero con `metodo_match` = `groq (IA) sugiere: <propiedad> - revisar` y la
  "evidencia" que citó el modelo, para que la revises y la muevas a mano si es correcta. La app
  también te avisa si hay facturas en ese estado.
- El plan gratuito de Groq tiene un límite diario de tokens; si se agota a mitad del lote, las
  facturas restantes simplemente quedan en `Sin_identificar` (no rompe el procesamiento).

## Estructura del proyecto

- `organizar_facturas.py` — CLI, para correr un lote sin interfaz.
- `app.py` — interfaz Streamlit.
- `core.py` — lógica de extracción, matching y generación del informe (sin dependencias de UI).
- `identificadores.csv` — tabla identificador → propiedad para las facturas de suministros.
- `requirements.txt` — dependencias de Python.
