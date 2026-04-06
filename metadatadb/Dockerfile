FROM python:3.11-slim

# Install Postgres
RUN apt-get update && apt-get install -y \
    postgresql postgresql-contrib \
    && rm -rf /var/lib/apt/lists/*


# Environment vars
ENV FLASK_APP=app.py
ENV FLASK_RUN_HOST=0.0.0.0
ENV POSTGRES_DATA_DIR=/var/lib/postgresql/data

WORKDIR /app
COPY app.py /app/

# Install Python dependencies
RUN pip install flask psycopg2-binary

# Create PostgreSQL data folder and set correct permissions
RUN mkdir -p ${POSTGRES_DATA_DIR} && \
    chown -R postgres:postgres /var/lib/postgresql

# Initialize PostgreSQL data directory
RUN su - postgres -c "/usr/lib/postgresql/17/bin/initdb -D ${POSTGRES_DATA_DIR}"

# Expose Flask and Postgres ports
EXPOSE 5000 5432

# Add entrypoint script
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]