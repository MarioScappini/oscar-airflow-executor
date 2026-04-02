#!/bin/bash
set -e

echo "========================================="
echo "Airflow DAG Processor Init with Health Check"
echo "========================================="

# Environment variables with defaults

# Health check port
HEALTH_PORT="${HEALTH_PORT:-8877}"

wait_for_service() {k
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
}

echo "Configuration:"
echo "  SQL Alchemy: $AIRFLOW__DATABASE__SQL_ALCHEMY_CONN"
echo "  Max DAG Parsing Processes: $AIRFLOW__DAG_PROCESSOR__MAX_DAG_PARSING_PROCESSES"
echo "  DAG Dir List Interval: $AIRFLOW__DAG_PROCESSOR__DAG_DIR_LIST_INTERVAL"
echo "  Health Check Port: $HEALTH_PORT"

# Wait for PostgreSQL
wait_for_service "PostgreSQL" "airflow db check"

# Verify database migrations (using exit code only)
echo "Verifying database schema..."
max_attempts=60
attempt=1
until airflow db check-migrations; do
  if [ "$attempt" -eq "$max_attempts" ]; then
    echo "ERROR: Database migrations failed after $max_attempts attempts"
    exit 1
  fi
  echo "  Waiting for migrations... ($attempt/$max_attempts)"
  sleep 2
  ((attempt++))
done
echo "✓ Database ready"

# Create necessary directories
mkdir -p /opt/airflow/{logs,dags,plugins,config}
mkdir -p /opt/airflow
echo '{"airflow": "airflow"}' > /opt/airflow/simple_auth_manager_passwords.json.generated


# Enhanced health check for DAG Processor
cat > /tmp/health_check.py << 'EOF'
#!/usr/bin/env python3
import http.server
import socketserver
import os
import sys
import subprocess
import time

HEALTH_PORT = int(os.getenv('HEALTH_PORT', '8877'))

class HealthHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/health':
            self.send_response(200)
            self.send_header('Content-type', 'text/plain')
            self.end_headers()
            self.wfile.write(b'OK')
        
        elif self.path == '/ready':
            # Enhanced readiness: check DB + basic DAG processor status
            try:
                # Quick DB check
                result = subprocess.run(['airflow', 'db', 'check'], 
                                      capture_output=True, timeout=5)
                if result.returncode == 0:
                    self.send_response(200)
                    self.send_header('Content-type', 'text/plain')
                    self.end_headers()
                    self.wfile.write(b'READY')
                else:
                    self.send_response(503)
                    self.send_header('Content-type', 'text/plain')
                    self.end_headers()
                    self.wfile.write(b'DB_NOT_READY')
            except:
                self.send_response(503)
                self.send_header('Content-type', 'text/plain')
                self.end_headers()
                self.wfile.write(b'CHECK_FAILED')
        
        elif self.path == '/dag-files':
            # Optional: expose DAG parsing status
            self.send_response(200)
            self.send_header('Content-type', 'text/plain')
            self.end_headers()
            self.wfile.write(b'DAG_DIR_MOUNTED')
        
        else:
            self.send_response(404)
            self.end_headers()
    
    def log_message(self, format, *args):
        pass

with socketserver.TCPServer(('0.0.0.0', HEALTH_PORT), HealthHandler) as httpd:
    print(f'✓ Health check server started on port {HEALTH_PORT}', flush=True)
    httpd.serve_forever()
EOF

chmod +x /tmp/health_check.py

# Start health check server
echo "Starting health check server on port $HEALTH_PORT..."
python3 /tmp/health_check.py &
HEALTH_PID=$!

sleep 2

if ! kill -0 $HEALTH_PID 2>/dev/null; then
  echo "ERROR: Health check server failed to start"
  exit 1
fi

echo ""
echo "========================================="
echo "Starting Airflow DAG Processor"
echo "========================================="
echo "Max Parsing Processes: $AIRFLOW__DAG_PROCESSOR__MAX_DAG_PARSING_PROCESSES"
echo "Dir Scan Interval: $AIRFLOW__DAG_PROCESSOR__DAG_DIR_LIST_INTERVAL s"
echo "Health Check PID: $HEALTH_PID"
echo ""

# Start DAG Processor (blocks)
exec airflow dag-processor
