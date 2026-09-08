# Contenedor propio en vez de depender del build de Streamlit Community Cloud: así los
# repos de apt los controlamos nosotros, no su imagen compartida.
FROM python:3.14-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr \
    tesseract-ocr-spa \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Render asigna el puerto por variable de entorno; 8501 es el valor local de reserva.
ENV PORT=8501
EXPOSE 8501

CMD ["sh", "-c", "streamlit run app.py --server.port=$PORT --server.address=0.0.0.0 --server.headless=true"]
