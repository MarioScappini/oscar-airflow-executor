# OSCARExecutor — Guía de Instalación

Esta guía describe paso a paso cómo desplegar el OSCARExecutor: un executor personalizado para Apache Airflow 3 que delega la ejecución de tareas a la plataforma serverless OSCAR mediante invocaciones síncronas HTTP respaldadas por Knative.

## Requisitos previos

- [Docker](https://docs.docker.com/get-docker/) instalado y en ejecución
- [kubectl](https://kubernetes.io/docs/tasks/tools/) configurado
- [Helm](https://helm.sh/docs/intro/install/) v3+
- [kind](https://kind.sigs.k8s.io/docs/user/quick-start/#installation) (Kubernetes in Docker)

## Arquitectura general

El sistema despliega los siguientes componentes dentro de un clúster Kubernetes gestionado por OSCAR:

|Componente|Tipo de servicio OSCAR|Descripción|
|---|---|---|
|PostgreSQL|Servicio expuesto|Base de datos de metadatos de Airflow|
|API Server|Servicio expuesto|Interfaz REST y Execution API|
|Scheduler|Servicio expuesto|Planificador de DAGs con OSCARExecutor|
|DAG Processor|Servicio expuesto|Parseo y serialización de DAGs|
|airflow-worker|Servicio síncrono (Knative)|Worker serverless con scale-to-zero|

## Variables compartidas entre componentes

Varios componentes de Airflow deben compartir las mismas credenciales para comunicarse correctamente. Antes de comenzar el despliegue, generar los siguientes valores y utilizarlos de forma consistente en todos los pasos:

|Variable|Descripción|Componentes que la usan|
|---|---|---|
|`AIRFLOW__CORE__FERNET_KEY`|Clave de cifrado para credenciales almacenadas|API Server, Scheduler, DAG Processor, Worker|
|`AIRFLOW__API_AUTH__JWT_SECRET`|Secreto para la autenticación JWT entre componentes|API Server, Scheduler, DAG Processor, Worker|
|`AIRFLOW__API__SECRET_KEY`|Clave secreta de la API REST|API Server, Scheduler, DAG Processor, Worker|
|`AIRFLOW__DATABASE__SQL_ALCHEMY_CONN`|Cadena de conexión a PostgreSQL|API Server, Scheduler, DAG Processor, Worker|
|`MINIO_SECRET_KEY`|Contraseña del servidor MinIO|Worker|

> **Importante:** Si estos valores no coinciden entre componentes, las tareas fallarán con errores de autenticación. Es recomendable definirlos una vez y reutilizarlos en cada FDL.

Además, los scripts de arranque (`entrypoint.sh`) de los componentes de Airflow generan un fichero de credenciales para el `SimpleAuthManager`. La línea relevante es:

```bash
echo '{"airflow": "airflow"}' > /opt/airflow/simple_auth_manager_passwords.json.generated
```

Modificar las credenciales `"airflow": "airflow"` en los _entrypoints_ de cada componente si se desea utilizar un usuario y contraseña distintos para la interfaz web de Airflow.

---

## Paso 1 — Instalar OSCAR con kind

Desplegar un clúster OSCAR local utilizando el script de instalación oficial:

```bash
curl -sSL http://go.oscar.grycap.net | bash
```

Este script crea un clúster kind con todos los componentes de OSCAR (OSCAR Manager, MinIO, Knative Serving) preconfigurados. Verificar que el clúster está operativo:

```bash
kubectl get nodes
kubectl get pods -n oscar
```

> **Nota:** El script nombra el clúster `oscar-test`. Para eliminarlo posteriormente: `kind delete cluster --name oscar-test`.

---

## Paso 2 — Construir las imágenes Docker

El sistema requiere dos imágenes personalizadas: la base de datos PostgreSQL con un sidecar Flask para las sondas de salud, y el airflow-worker que ejecuta las tareas. Ambas deben publicarse en el registro local del clúster kind (`localhost:5001`).

> **Nota:** El clúster kind desplegado por OSCAR incluye un registro local accesible en `localhost:5001`. Todas las imágenes deben publicarse en este registro para que los pods del clúster puedan acceder a ellas.

### 2.1. Imagen de PostgreSQL

Antes de construir la imagen, revisar el fichero `metadatadb/entrypoint.sh` y ajustar las credenciales de la base de datos si se desea utilizar valores distintos a los predeterminados. Las líneas relevantes son:

```bash
su - postgres -c "psql -tc \"SELECT 1 FROM pg_roles WHERE rolname='airflow'\" | grep -q 1 || psql -c \"CREATE USER airflow WITH PASSWORD 'airflow';\""
su - postgres -c "psql -tc \"SELECT 1 FROM pg_database WHERE datname='airflow'\" | grep -q 1 || psql -c \"CREATE DATABASE airflow OWNER airflow;\""
```

Reemplazar `airflow` (usuario y contraseña) por los valores deseados. Si se modifican estas credenciales, actualizar también la cadena de conexión en `metadatadb/secret.yaml` y la variable `AIRFLOW__DATABASE__SQL_ALCHEMY_CONN` en todos los componentes para que coincidan.

Construir la imagen desde la carpeta `metadatadb/`:

```bash
cd metadatadb/
docker build -t pg-flask-sidecar:latest .
docker tag pg-flask-sidecar:latest localhost:5001/pg-flask-sidecar:latest
docker push localhost:5001/pg-flask-sidecar:latest
```

### 2.2. Imagen del airflow-worker

Construir la imagen desde la carpeta `airflow-local/airflow-oscar/`:

```bash
cd airflow-local/airflow-oscar/
docker build -t airflow-oscar:latest .
docker tag airflow-oscar:latest localhost:5001/airflow-oscar:latest
docker push localhost:5001/airflow-oscar:latest
```

---

## Paso 3 — Desplegar PostgreSQL (base de datos de metadatos)

Este paso despliega la base de datos PostgreSQL que Airflow utiliza como almacenamiento de metadatos.

### 3.1. Crear el servicio OSCAR

Aplicar el FDL del servicio de PostgreSQL:

```bash
oscar-cli apply metadatadb/service.yaml
```

### 3.2. Crear el Secret y el Service de Kubernetes

El Secret contiene la cadena de conexión que los demás componentes utilizan para conectarse a PostgreSQL. El Service expone PostgreSQL dentro del clúster:

```bash
kubectl apply -f metadatadb/secret.yaml -n oscar-svc
kubectl apply -f metadatadb/service.yaml -n oscar-svc
```

Verificar que PostgreSQL está operativo:

```bash
kubectl get pods -n oscar-svc | grep postgres
```

---

## Paso 4 — Desplegar el API Server

El API Server expone la interfaz REST de Airflow y la Execution API que los workers utilizan para reportar el estado de las tareas.

### 4.1. Configurar las variables de entorno

Antes de aplicar el servicio, editar `apiserver/service.yaml` y asignar los valores de las variables compartidas:

- `AIRFLOW__CORE__FERNET_KEY`
- `AIRFLOW__API_AUTH__JWT_SECRET`
- `AIRFLOW__API__SECRET_KEY`

Estos valores serán la referencia para todos los demás componentes.

### 4.2. Crear el servicio OSCAR

```bash
oscar-cli apply apiserver/service.yaml
```

### 4.3. Modificar el Deployment

Una vez creado el servicio, es necesario ajustar las sondas de salud (_health probes_) del Deployment generado por OSCAR. El API Server de Airflow 3 tarda en inicializarse debido a la carga de módulos y la conexión a PostgreSQL:

```bash
kubectl edit deployment <nombre-deployment-apiserver> -n oscar-svc
```

Realizar los siguientes cambios en el contenedor principal:

```yaml
livenessProbe:
  httpGet:
    path: /api/v2/monitor/health
    port: <puerto>
  initialDelaySeconds: 90

readinessProbe:
  httpGet:
    path: /api/v2/monitor/health
    port: <puerto>
  initialDelaySeconds: 90
```

> **¿Por qué este cambio?** El endpoint de health por defecto de OSCAR (`/`) no corresponde al endpoint de monitorización de Airflow. Sin esta modificación, Kubernetes reinicia el pod antes de que Airflow haya completado su inicialización. El `initialDelaySeconds` de 90 segundos da margen suficiente para que el API Server arranque, ejecute las migraciones de base de datos y establezca la conexión con PostgreSQL.

---

## Paso 5 — Desplegar el airflow-worker (servicio síncrono)

El airflow-worker es el componente que ejecuta las tareas de Airflow. Se despliega como un servicio síncrono de OSCAR respaldado por Knative, con capacidad de scale-to-zero.

### 5.1. Configurar las variables de entorno

Editar `executor/service.yaml` y configurar:

- `MINIO_SECRET_KEY`: asignar la contraseña de MinIO del clúster.
- `AIRFLOW__CORE__FERNET_KEY`, `AIRFLOW__API_AUTH__JWT_SECRET`, `AIRFLOW__API__SECRET_KEY`: deben coincidir con los valores configurados en el API Server (Paso 4).

### 5.2. Crear el servicio OSCAR

```bash
oscar-cli apply executor/service.yaml
```

### 5.3. Obtener el token del servicio

Anotar el **token del servicio** generado, ya que será necesario para configurar el Scheduler en el paso siguiente:

```bash
oscar-cli service get airflow-worker
```

Verificar que el Knative Service se ha creado correctamente:

```bash
kubectl get ksvc -n oscar-svc | grep airflow-worker
```

> **Nota:** Con `min_scale: 0`, el servicio no tendrá pods activos hasta que reciba la primera invocación. Esto es el comportamiento esperado.

---

## Paso 6 — Desplegar el Scheduler

El Scheduler es el componente central de Airflow que monitoriza los DAGs, determina qué tareas están listas para ejecutarse y las despacha al OSCARExecutor.

### 6.1. Configurar las variables de entorno

Editar `scheduler/service.yaml` y configurar:

- **Token del airflow-worker** (obtenido en el Paso 5.3): el OSCARExecutor utiliza este token para autenticarse contra la API REST de OSCAR al despachar tareas.
- `AIRFLOW__CORE__FERNET_KEY`, `AIRFLOW__API_AUTH__JWT_SECRET`, `AIRFLOW__API__SECRET_KEY`: deben coincidir con los valores del API Server (Paso 4).

### 6.2. Crear el servicio OSCAR

```bash
oscar-cli apply scheduler/service.yaml
```

### 6.3. Modificar el Deployment

El Scheduler de Airflow tarda más en inicializarse que los demás componentes, ya que debe parsear los DAGs existentes y establecer la conexión con el API Server:

```bash
kubectl edit deployment <nombre-deployment-scheduler> -n oscar-svc
```

Realizar los siguientes cambios:

```yaml
livenessProbe:
  httpGet:
    path: /health
    port: <puerto>
  initialDelaySeconds: 120

readinessProbe:
  httpGet:
    path: /health
    port: <puerto>
  initialDelaySeconds: 120
```

> **¿Por qué 120 segundos?** El Scheduler depende de que el API Server y PostgreSQL estén operativos. Si estos componentes aún se están inicializando cuando el Scheduler arranca, el tiempo de inicialización total se acumula. Un `initialDelaySeconds` de 120 segundos evita reinicios prematuros en cascada.

---

## Paso 7 — Desplegar el DAG Processor

El DAG Processor parsea los ficheros Python que definen los DAGs y serializa su estructura en la base de datos de metadatos.

### 7.1. Configurar las variables de entorno

Editar `processor/service.yaml` y asegurar que las variables `AIRFLOW__CORE__FERNET_KEY`, `AIRFLOW__API_AUTH__JWT_SECRET` y `AIRFLOW__API__SECRET_KEY` coinciden con los valores del API Server (Paso 4).

### 7.2. Crear el servicio OSCAR

```bash
oscar-cli apply processor/service.yaml
```

### 7.3. Modificar el Deployment

```bash
kubectl edit deployment <nombre-deployment-processor> -n oscar-svc
```

Ajustar las sondas de salud:

```yaml
livenessProbe:
  httpGet:
    path: /health
    port: <puerto>
  initialDelaySeconds: 90

readinessProbe:
  httpGet:
    path: /health
    port: <puerto>
  initialDelaySeconds: 90
```

---

## Paso 8 — Subir DAGs a MinIO

Los DAGs se distribuyen a todos los componentes a través del bucket `airflow` de MinIO. Subir los ficheros DAG al prefijo `dags/`:

```bash
# Usando el cliente de MinIO (mc)
mc alias set minio http://localhost:30300 minio <MINIO_PASSWORD>
mc cp mi_dag.py minio/airflow/dags/
```

Los componentes estáticos (Scheduler, DAG Processor) acceden a los DAGs mediante un montaje rclone continuo. El airflow-worker los descarga bajo demanda desde MinIO vía boto3 en cada invocación.

---

## Verificación

Una vez desplegados todos los componentes, verificar que el sistema está operativo:

```bash
# Verificar que todos los pods están Running
kubectl get pods -n oscar-svc

# Verificar los servicios OSCAR
oscar-cli service list

# Verificar el Knative Service del worker
kubectl get ksvc -n oscar-svc
```

Acceder a la interfaz web de Airflow a través del endpoint del API Server para monitorizar los DAGs y lanzar ejecuciones.

---

## Estructura del repositorio

```
.
├── airflow-local/
│   └── airflow-oscar/          # Dockerfile e imagen del airflow-worker
│       ├── Dockerfile
│       └── oscar_executor.py   # Implementación del OSCARExecutor
├── metadatadb/                 # PostgreSQL (base de datos de metadatos)
│   ├── Dockerfile              # Imagen pg-flask-sidecar
│   ├── entrypoint.sh           # Inicialización de PostgreSQL y credenciales
│   ├── service.yaml
│   ├── app.py
│   ├── secret.yaml
│   └── service-k8s.yaml
├── api-server/                  # API Server de Airflow
│   ├── entrypoint.sh
│   └── service.yaml
├── executor/                   # airflow-worker (servicio síncrono Knative)
│   └── service.yaml
│   └── entrypoint.sh 
├── scheduler/                  # Scheduler con OSCARExecutor
│   ├── service.yaml
│   └── entrypoint.sh
├── processor/                  # DAG Processor
│   ├── service.yaml
│   └── entrypoint.sh
└── README.md
```

---

## Solución de problemas

**Los pods se reinician continuamente:** Verificar que las sondas de salud (`livenessProbe`, `readinessProbe`) están configuradas con los endpoints y tiempos de espera correctos según se describe en los pasos 4, 6 y 7.

**Errores de autenticación entre componentes:** Comprobar que los valores de `AIRFLOW__CORE__FERNET_KEY`, `AIRFLOW__API_AUTH__JWT_SECRET` y `AIRFLOW__API__SECRET_KEY` son idénticos en todos los servicios (API Server, Scheduler, DAG Processor y Worker).

**El airflow-worker no escala:** Comprobar que el Knative Service está correctamente creado (`kubectl get ksvc -n oscar-svc`) y que el token de autenticación en el Scheduler coincide con el token del servicio.

**Las tareas quedan en estado RUNNING indefinidamente:** Esto puede ocurrir si la invocación HTTP falla antes de que el Task SDK arranque dentro del contenedor. Verificar la conectividad entre el Scheduler y el endpoint `/run/airflow-worker` de OSCAR.

**Los DAGs no aparecen en la interfaz de Airflow:** Verificar que los ficheros DAG están correctamente subidos al bucket `airflow/dags/` en MinIO y que el montaje rclone del DAG Processor está activo.
