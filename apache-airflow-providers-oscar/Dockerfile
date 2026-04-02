ARG AIRFLOW_VERSION=2.9.2
FROM apache/airflow:${AIRFLOW_VERSION}

USER airflow

# Option 1: Install from local source (during development)
COPY --chown=airflow:root . /opt/airflow/providers/oscar
RUN pip install --no-cache-dir -e /opt/airflow/providers/oscar

# Option 2: Install from PyPI (once published)
# RUN pip install apache-airflow-providers-oscar

ENV AIRFLOW__CORE__EXECUTOR=OSCARExecutor