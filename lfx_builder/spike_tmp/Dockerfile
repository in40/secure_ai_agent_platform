FROM python:3.12-slim
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu
RUN pip install --no-cache-dir lfx==1.13.3 langchain-openai docling docling-core langchain-qdrant qdrant-client tiktoken pdfplumber openpyxl
COPY basicrag_lfx.json /flows/flow.json
EXPOSE 8000
CMD ["lfx", "serve", "/flows/flow.json", "--host", "0.0.0.0", "--port", "8000"]
