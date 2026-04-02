FROM apache/airflow:3.1.3

USER root   

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    python3-dev \
    && rm -rf /var/lib/apt/lists/*

RUN mkdir -p /opt/airflow
COPY --chown=airflow:root oscar_executor.py /opt/airflow/plugins/oscar_executor.py

USER airflow

RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir apache-airflow-providers-cncf-kubernetes

