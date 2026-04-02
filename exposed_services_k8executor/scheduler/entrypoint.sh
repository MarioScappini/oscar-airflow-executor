#!/bin/bash
set -e

echo "========================================="
echo "Airflow Scheduler Init (KubernetesExecutor)"
echo "========================================="



# Scheduler health check


# Wait for services function
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
echo "  Executor: KubernetesExecutor"
echo "  SQL Alchemy: $AIRFLOW__DATABASE__SQL_ALCHEMY_CONN"
echo "  K8s Namespace: $AIRFLOW__KUBERNETES_EXECUTOR__NAMESPACE"
echo "  Auth Manager: $AIRFLOW__CORE__AUTH_MANAGER"
echo ""

# Wait for PostgreSQL
wait_for_service "PostgreSQL" "airflow db check"

echo "Creating missing opt dirs if missing:"
mkdir -v -p /mnt/airflow/{logs,dags,plugins,config}
mkdir -p /opt/airflow
echo '{"airflow": "airflow"}' > /opt/airflow/simple_auth_manager_passwords.json.generated
echo ""


echo "Airflow version:"
airflow version
echo ""

# ✅ NEW: Generate config list AFTER env vars are set
echo "Running airflow config list to create default config file if missing."
airflow config list >/dev/null
echo ""

# ✅ NEW: Force namespace in generated config
echo "Ensuring namespace is set to oscar-svc in config..."
if [ -f "$AIRFLOW_HOME/airflow.cfg" ]; then
  sed -i 's/^namespace = .*/namespace = oscar-svc/' "$AIRFLOW_HOME/airflow.cfg"
elif [ -f "/root/airflow/airflow.cfg" ]; then
  sed -i 's/^namespace = .*/namespace = oscar-svc/' "/root/airflow/airflow.cfg"
elif [ -f "/opt/airflow/airflow.cfg" ]; then
  sed -i 's/^namespace = .*/namespace = oscar-svc/' "/opt/airflow/airflow.cfg"
fi

# ✅ NEW: Verify what Airflow actually sees
echo "Airflow config check:"
airflow config get-value kubernetes namespace || echo "Could not read namespace config"
echo ""

echo "Running database migrations..."
airflow db migrate
echo "✓ Migrations complete"
echo ""



echo ""
echo "========================================="
echo "Starting Airflow Scheduler"
echo "========================================="
echo ""
echo "The scheduler will:"
echo "  - Monitor DAG files for changes"
echo "  - Schedule task instances based on DAG definitions"
echo "  - Create Kubernetes pods for task execution in namespace: $AIRFLOW__KUBERNETES_EXECUTOR__NAMESPACE"
echo ""


exec airflow scheduler
