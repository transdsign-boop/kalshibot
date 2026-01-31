# Stage 1: Build React frontend
FROM node:20-slim AS frontend-build
WORKDIR /app/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ .
RUN npm run build

# Stage 2: Python runtime
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN rm -rf frontend/node_modules frontend/src frontend/*.config.* frontend/package*.json
COPY --from=frontend-build /app/frontend/dist frontend/dist
EXPOSE 8000
CMD ["uvicorn", "web:app", "--host", "0.0.0.0", "--port", "8000"]
