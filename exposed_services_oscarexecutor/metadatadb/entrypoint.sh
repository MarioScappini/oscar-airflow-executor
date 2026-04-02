#!/bin/bash
set -e

# Ensure correct permissions
chown -R postgres:postgres /var/lib/postgresql

# Allow external connections
echo "listen_addresses='*'" >> /var/lib/postgresql/data/postgresql.conf

# Allow password authentication for all hosts
echo "host all all 0.0.0.0/0 md5" >> /var/lib/postgresql/data/pg_hba.conf


# Start PostgreSQL in background
echo "Starting PostgreSQL..."
su - postgres -c "/usr/lib/postgresql/17/bin/postgres -D /var/lib/postgresql/data" &

# Wait for Postgres to be ready
echo "Waiting for PostgreSQL to be ready..."
until su - postgres -c "pg_isready" > /dev/null 2>&1; do
  sleep 1
done

# Create user and database
echo "Setting up database..."
su - postgres -c "psql -tc \"SELECT 1 FROM pg_roles WHERE rolname='airflow'\" | grep -q 1 || psql -c \"CREATE USER airflow WITH PASSWORD 'airflow';\""
su - postgres -c "psql -tc \"SELECT 1 FROM pg_database WHERE datname='airflow'\" | grep -q 1 || psql -c \"CREATE DATABASE airflow OWNER airflow;\""



# Start Flask
echo "Starting Flask..."
exec flask run --host=0.0.0.0 --port=5000