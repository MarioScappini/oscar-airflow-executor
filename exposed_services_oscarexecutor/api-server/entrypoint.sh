#!/bin/bash
set -e


echo "========================================="
echo "Airflow API Server Init"
echo "========================================="


# Valores por defecto (los mismos del docker-compose)


# Función para esperar servicios
wait_for_service() {
  local service_name="$1"
  local check_command="$2"
  local max_attempts=30
  local attempt=1
  
  echo "Waiting for $service_name to be ready..."
  until eval "$check_command" >/dev/null 2>&1; do
    if [ "$attempt" -eq "$max_attempts" ]; then
      echo "ERROR: $service_name not available after $max_attempts attempts"
      exit 1
    fi
    echo "  Attempt $attempt/$max_attempts: waiting..."
    sleep 2
    attempt=$((attempt + 1))
  done
  echo "✓ $service_name is ready"
  echo ""
}


echo ""
echo "Configuration:"
echo "  SQL Alchemy: $AIRFLOW__DATABASE__SQL_ALCHEMY_CONN"
echo "  Broker URL:  $AIRFLOW__CELERY__BROKER_URL"
echo "  Auth Manager: $AIRFLOW__CORE__AUTH_MANAGER"
echo ""


# Esperar PostgreSQL
wait_for_service "PostgreSQL" "airflow db check"



echo "Creating missing opt dirs if missing:"
mkdir -v -p /mnt/airflow/{logs,dags,plugins,config}
mkdir -p /opt/airflow
echo '{"airflow": "airflow"}' > /opt/airflow/simple_auth_manager_passwords.json.generated
echo ""


echo "Airflow version:"
airflow version
echo ""


echo "Running airflow config list to create default config file if missing."
airflow config list >/dev/null
echo ""


echo "Running database migrations..."
airflow db migrate
echo "✓ Migrations complete"
echo ""



echo ""
echo "========================================="
echo "Starting Airflow API Server"
echo "========================================="
echo ""


exec airflow api-server --proxy-headers --apps core,execution
