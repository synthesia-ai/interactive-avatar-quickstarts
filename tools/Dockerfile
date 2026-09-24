# Agent worker image (server.py, the demo frontend, is not part of it).
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN useradd --create-home --uid 10001 app
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

USER app
# Pre-fetches model files for plugins that download any (none of these do; the turn detector does).
RUN python -m livekit.agents download-files
COPY --chown=app . .

CMD ["python", "agent.py", "start"]
