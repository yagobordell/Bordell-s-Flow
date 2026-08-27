# Fase 7 — Guía de despliegue GPU

Esta guía lleva la infraestructura desde un checkout limpio hasta una prueba completa:

```text
cliente -> Salad Job Queue -> worker HTTP -> R2
                                  |
                                  +-> Supabase/Postgres
```

La Fase 7 implementa el transporte, la persistencia, la idempotencia, los leases y la
reconciliación. El task incluido, `infrastructure.copy`, es una prueba determinista del recorrido
completo. La inferencia LTX-2.5 se registra como un nuevo task en la Fase 8; no se finge una
selección de GPU antes de medir la matriz real.

## 1. Requisitos

- Python 3.12 y `uv` o `pip`.
- Docker con BuildKit.
- `curl`, `psql` y, opcionalmente, `jq`.
- Una cuenta de [Cloudflare R2](https://developers.cloudflare.com/r2/).
- Un proyecto de [Supabase](https://supabase.com/docs/guides/database/connecting-to-postgres).
- Una organización, proyecto y API key de
  [SaladCloud](https://docs.salad.com/reference/saladcloud-api/using-the-api).
- Un registro de contenedores accesible por Salad, por ejemplo Docker Hub o GHCR.

Instala el proyecto:

```bash
uv sync --extra dev --extra gpu
```

Alternativa con `pip`:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev,gpu]'
```

Comprueba la base antes de continuar:

```bash
python -m ruff check .
python -m pytest
```

## 2. Ejecutar primero la matriz de benchmark

Ejecuta los perfiles documentados en
[`phase7-benchmark.md`](phase7-benchmark.md) sobre hardware GPU real. Mantén idénticos el prompt,
keyframe, seed, resolución, frames y FPS. Cada caso debe producir su propio informe:

```text
data/output/phase7/
├── l40s-distilled-fp8-cpu.json
├── rtx4090-distilled-fp8-cpu.json
└── rtx5090-distilled-fp8-cpu.json
```

No pases `--gpu-class` al renderizador de Salad para la prueba de infraestructura CPU. Para el
worker LTX de Fase 8, usa únicamente la clase que cumpla VRAM, tiempo por shot y coste según esos
JSON. El benchmark y el worker de infraestructura son independientes: no es necesario pagar una
GPU para probar R2, Postgres y la cola.

## 3. Preparar Cloudflare R2

En Cloudflare:

1. Crea un bucket, por ejemplo `ai-video-factory`.
2. Crea un API token S3 limitado a ese bucket con lectura y escritura de objetos.
3. Copia el Access Key ID, Secret Access Key y Account ID.
4. Construye el endpoint S3 como indica la
   [documentación oficial de boto3 para R2](https://developers.cloudflare.com/r2/examples/aws/boto3/):
   `https://<ACCOUNT_ID>.r2.cloudflarestorage.com` y región `auto`.

Copia el ejemplo de entorno y rellena los valores reales solo en `.env`:

```bash
cp .env.example .env
```

```dotenv
R2_ENDPOINT_URL=https://ACCOUNT_ID.r2.cloudflarestorage.com
R2_BUCKET=ai-video-factory
R2_ACCESS_KEY_ID=...
R2_SECRET_ACCESS_KEY=...
```

Nunca subas `.env`. El worker intercambia solo claves y metadatos con la cola; los binarios se
descargan y suben directamente a R2. `boto3.upload_file` activa multipart automáticamente cuando
corresponde.

## 4. Preparar Supabase/Postgres

En el panel de Supabase, abre **Connect** y copia una URI Postgres:

- usa **Direct connection** para un contenedor persistente si el nodo dispone de IPv6;
- usa **Session pooler**, puerto 5432, si necesitas IPv4;
- Transaction pooler, puerto 6543, también funciona porque el adapter desactiva prepared
  statements, pero no es la primera opción para este worker persistente.

Codifica caracteres especiales de la contraseña en la URI y exige TLS:

```dotenv
POSTGRES_DSN=postgresql://.../postgres?sslmode=require
```

Aplica la migración una sola vez:

```bash
set -a
. ./.env
set +a
psql "$POSTGRES_DSN" -v ON_ERROR_STOP=1 -f infra/sql/001_gpu_jobs.sql
```

También puedes pegar `infra/sql/001_gpu_jobs.sql` en el SQL Editor de Supabase. La tabla vive en el
schema privado `gpu`, no en la API pública de datos.

Verificación:

```bash
psql "$POSTGRES_DSN" -c '\d+ gpu.jobs'
```

## 5. Probar la API local sin servicios externos

Esta prueba usa filesystem + estado en memoria y no inicia el binario de Salad:

```bash
mkdir -p data/phase7-local/inputs
printf 'phase-7-local-smoke\n' > data/phase7-local/inputs/smoke.txt
INPUT_SHA="$(sha256sum data/phase7-local/inputs/smoke.txt | cut -d' ' -f1)"

GPU_WORKER_MODE=local \
LOCAL_OBJECT_ROOT=data/phase7-local \
uvicorn ai_video_factory.gpu.runtime:app --host 127.0.0.1 --port 8080
```

En otra terminal:

```bash
curl --fail-with-body --silent --show-error \
  -X POST http://127.0.0.1:8080/jobs \
  -H 'Content-Type: application/json' \
  -d "{
    \"schema_version\": \"1\",
    \"job_id\": \"local-smoke-001\",
    \"task\": \"infrastructure.copy\",
    \"inputs\": [{
      \"name\": \"source\",
      \"key\": \"inputs/smoke.txt\",
      \"sha256\": \"$INPUT_SHA\"
    }],
    \"output\": {
      \"key\": \"jobs/local-smoke-001/output.txt\",
      \"content_type\": \"text/plain\"
    },
    \"parameters\": {}
  }"

cmp \
  data/phase7-local/inputs/smoke.txt \
  data/phase7-local/jobs/local-smoke-001/output.txt
```

Repite el mismo `curl`: debe devolver `"replayed": true` sin volver a ejecutar el task. Cambiar el
payload conservando `job_id=local-smoke-001` debe devolver HTTP 409.

## 6. Construir y probar Docker

El Dockerfile fija el Salad Job Queue Worker oficial `v0.7.0` y verifica su SHA-256 durante el
build. El binario solo puede conectarse a la cola dentro de un nodo Salad, por lo que localmente se
desactiva.

```bash
export IMAGE="docker.io/TU_USUARIO/ai-video-factory:phase7-$(git rev-parse --short HEAD)"

docker build \
  --file docker/phase7-worker/Dockerfile \
  --tag "$IMAGE" \
  .
```

Prueba el contenedor en modo local:

```bash
docker run --rm \
  --name ai-video-factory-phase7 \
  --publish 8080:8080 \
  --user "$(id -u):$(id -g)" \
  --env GPU_WORKER_MODE=local \
  --env SALAD_QUEUE_ENABLED=false \
  --env LOCAL_OBJECT_ROOT=/app/data/phase7-local \
  --volume "$PWD/data/phase7-local:/app/data/phase7-local" \
  "$IMAGE"
```

Comprueba `http://127.0.0.1:8080/health` y `/ready`, y repite el `curl` anterior. Después publica la
imagen:

```bash
docker login
docker push "$IMAGE"
```

Usa una etiqueta inmutable o digest. No despliegues `latest`.

## 7. Crear la cola y el Container Group de Salad

Completa en `.env`:

```dotenv
SALAD_API_KEY=...
SALAD_ORGANIZATION=mi-organizacion
SALAD_PROJECT=mi-proyecto
SALAD_QUEUE_NAME=ai-video-factory-jobs
SALAD_LOG_LEVEL=info
```

Carga el entorno:

```bash
set -a
. ./.env
set +a
```

Renderiza los JSON. Para validar únicamente la Fase 7, no asignes GPU:

```bash
python scripts/render_phase7_salad.py \
  --image "$IMAGE" \
  --queue-name "$SALAD_QUEUE_NAME" \
  --cpu 4 \
  --memory-mb 8192 \
  --max-replicas 1
```

Para el futuro worker LTX, añade la clase elegida por el benchmark y ajusta CPU/RAM:

```bash
python scripts/render_phase7_salad.py \
  --image "$IMAGE" \
  --queue-name "$SALAD_QUEUE_NAME" \
  --gpu-class "$SALAD_GPU_CLASS" \
  --cpu 8 \
  --memory-mb 32768 \
  --max-replicas 1
```

Los archivos generados son:

```text
data/output/phase7/salad/
├── queue.json
└── container-group.json
```

`container-group.json` contiene secretos, se crea con permisos 0600 y está bajo `data/output/`, que
Git ignora. Revísalo sin copiarlo a tickets o logs.

Crea la cola con la API oficial:

```bash
curl --fail-with-body --silent --show-error \
  -X POST \
  "https://api.salad.com/api/public/organizations/$SALAD_ORGANIZATION/projects/$SALAD_PROJECT/queues" \
  -H "Salad-Api-Key: $SALAD_API_KEY" \
  -H 'Content-Type: application/json' \
  --data-binary @data/output/phase7/salad/queue.json
```

Crea el Container Group:

```bash
curl --fail-with-body --silent --show-error \
  -X POST \
  "https://api.salad.com/api/public/organizations/$SALAD_ORGANIZATION/projects/$SALAD_PROJECT/containers" \
  -H "Salad-Api-Key: $SALAD_API_KEY" \
  -H 'Content-Type: application/json' \
  --data-binary @data/output/phase7/salad/container-group.json
```

Si ya existen, no repitas los POST: inspecciona los recursos y usa los endpoints PATCH oficiales.
La configuración conecta la cola a `POST /jobs` en el puerto 8080, usa `/ready`, reinicio `always`
y escala de 0 a 1. El Container Gateway no es necesario.

Referencias oficiales:

- [Crear una Queue](https://docs.salad.com/reference/saladcloud-api/queues/create-queue)
- [Crear un Container Group](https://docs.salad.com/reference/saladcloud-api/container-groups/create-container-group)
- [Autoscaling con Job Queues](https://docs.salad.com/container-engine/how-to-guides/autoscaling/enable-autoscaling)
- [Salad Job Queue Worker](https://github.com/SaladTechnologies/salad-cloud-job-queue-worker/releases/tag/v0.7.0)

## 8. Ejecutar la prueba completa Salad -> R2 -> Postgres

El script crea un input temporal, lo sube a R2, envía un job, espera el estado terminal, descarga el
output y compara los bytes:

```bash
python scripts/submit_phase7_smoke.py \
  --queue-name "$SALAD_QUEUE_NAME" \
  --timeout-seconds 1800
```

Éxito esperado:

```text
Phase 7 end-to-end smoke test: OK
```

También quedan dos JSON auditables:

```text
data/output/phase7/salad/job-request-<job_id>.json
data/output/phase7/salad/queue-response-<job_id>.json
```

Consulta el estado directamente:

```bash
curl --fail-with-body --silent --show-error \
  "https://api.salad.com/api/public/organizations/$SALAD_ORGANIZATION/projects/$SALAD_PROJECT/queues/$SALAD_QUEUE_NAME/jobs/$SALAD_JOB_ID" \
  -H "Salad-Api-Key: $SALAD_API_KEY"
```

## 9. Verificar idempotencia y recuperación

La garantía se apoya en tres elementos:

1. `job_id` queda unido de forma inmutable al SHA-256 canónico del request.
2. El output debe vivir bajo `jobs/<job_id>/` y lleva metadatos `job-id`, `request-sha256` y
   `artifact-sha256`.
3. Postgres asigna un lease temporal; otro worker solo puede reclamarlo cuando vence.

Si el upload a R2 termina pero el nodo cae antes del commit de Postgres, el siguiente intento
reconcilia el objeto y marca el job como `succeeded` sin regenerarlo. Si la clave ya existe con otros
metadatos, el worker no la sobrescribe.

Consulta operaciones recientes:

```sql
SELECT
    job_id,
    status,
    attempt_count,
    lease_owner,
    lease_expires_at,
    output_key,
    last_error,
    updated_at
FROM gpu.jobs
ORDER BY updated_at DESC
LIMIT 50;
```

Los errores HTTP 503/500 son reintentables. Los conflictos 409 y payloads inválidos 422 son
terminales: el worker oficial de Salad `v0.7.0` no los reintenta, por lo que debes inspeccionar el
JSON de output del job además de su estado de cola.

## 10. Criterio de cierre de Fase 7

La implementación queda lista cuando pasan Ruff y pytest. La validación operativa queda cerrada
cuando conservas evidencia de:

- los informes reales de la matriz de benchmark;
- migración `gpu.jobs` aplicada;
- imagen Docker publicada por digest;
- Queue y Container Group activos;
- smoke test completo en estado `succeeded`;
- fila Postgres y objeto R2 con SHA-256 coincidente;
- repetición/reconciliación sin duplicar trabajo.

No marques como terminada la selección de hardware hasta medirla. No añadas checkpoints LTX ni
credenciales a la imagen. Rota las credenciales usadas durante pruebas antes de producción y limita
`max_replicas` según presupuesto.
