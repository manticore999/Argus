# Builds the React dashboard, then packages it with the Python backend.

FROM node:20-alpine AS frontend
WORKDIR /build
COPY frontend/package.json ./
RUN npm install --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY agent/ agent/
COPY analyzers/ analyzers/
COPY engine/ engine/
COPY k8s/ k8s/
COPY snapshot/ snapshot/
COPY triggers/ triggers/
COPY config.py dashboard.py dashboard_main.py main.py ./
COPY --from=frontend /build/dist frontend/dist

RUN useradd --uid 10001 --no-create-home argus
USER 10001
EXPOSE 8080
CMD ["python", "-u", "dashboard_main.py"]
