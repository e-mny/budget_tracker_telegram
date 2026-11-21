FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DATA_DIR=/data MPLCONFIGDIR=/tmp/matplotlib
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && useradd --uid 10001 --create-home penny \
    && mkdir /data && chown penny:penny /data
COPY --chown=penny:penny app ./app
USER penny
EXPOSE 8000
CMD ["uvicorn", "app.web:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
