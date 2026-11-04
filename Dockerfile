# Standard-library only. Put HTTPS in front (platform TLS or a reverse proxy): phones need HTTPS for GPS.
FROM python:3.12-slim
WORKDIR /app
COPY --chown=nobody:nogroup . .
RUN mkdir -p /app/.cache && chown nobody:nogroup /app/.cache
ENV HOST=0.0.0.0 PORT=8080 PYTHONUNBUFFERED=1
EXPOSE 8080
USER nobody
CMD ["python", "server.py"]
