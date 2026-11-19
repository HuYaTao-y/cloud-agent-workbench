FROM node:22-alpine
RUN apk add --no-cache python3 git ca-certificates \
    && npm install --global @openai/codex@0.169.1
WORKDIR /app
COPY app.py .
COPY web ./web
ENV PYTHONUNBUFFERED=1 WORKBENCH_DATA=/data BIND=0.0.0.0 AGENT_SANDBOX=danger-full-access
EXPOSE 8000
CMD ["python3", "app.py"]
