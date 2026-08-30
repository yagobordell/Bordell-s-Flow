# Fase 7.2 — despliegue y validación en Salad

Estado: **Fase 7.2 completada y validada en cloud**. Esta guía
parte del checkout de AI Video Factory y usa PowerShell 7 en Windows. Los comandos deben ejecutarse
desde la raíz del repositorio, salvo que se indique otra cosa.

## Resultado ya validado

El 27 de agosto de 2026 se completó un smoke real de `infrastructure.copy`:

| Evidencia | Valor |
|---|---|
| Queue | `ai-video-factory-jobs` |
| Container Group | `ai-video-factory-worker` |
| Application job | `phase7-smoke-a1d3883364d8` |
| Salad job | `5e5e0230-bd62-4105-819e-df004a4d1dcf` |
| Estado | `succeeded` |
| Intentos | `1` |
| SHA-256 input/output | `0b7da0548cee3474b5ae86ed25eaaff071c7da52d3fc9d07977038a1b600d1a9` |
| Output R2 | `jobs/phase7-smoke-a1d3883364d8/output.txt` |

El 30 de agosto de 2026 se completó el cierre operativo:

| Evidencia de cierre | Valor |
|---|---|
| Salad replay job | `8e3a92fa-19fe-49f8-a443-04b5c1369a9b` |
| Queue/worker status | `succeeded` / `succeeded` |
| Replay | `true` |
| Attempt count | `1` |
| Container Group version | `4` |
| Imagen desplegada | `docker.io/yagobordell/ai-video-factory@sha256:82c93a035dbd25f1fc11e80df8b559f18f3f6cf7edf3b4b9d7332f440cb352cc` |
| Estado final | `replicas=0`, `pending_change=false` |

El replay devolvió el mismo application job, output key y SHA-256 que la primera ejecución. Esto
demuestra Queue → worker HTTP → Supabase/Postgres → R2 → Queue, despliegue inmutable e idempotencia
cloud sin repetir el trabajo.

## 1. Dónde hacer cada cosa

| Acción | Lugar |
|---|---|
| Docker, scripts Python, API de Salad y Git | PowerShell, raíz de `ai-video-factory` |
| Crear API key, consultar créditos, System Events y logs | Portal de Salad |
| Crear bucket y credenciales S3 | Panel de Cloudflare R2 |
| Obtener DSN y ejecutar migración | Panel de Supabase y PowerShell |
| Benchmark LTX real | Máquina/instancia GPU preparada; ver `phase7-benchmark.md` |

No pegues secretos en Git, documentación, capturas o JSON que vayas a compartir.

## 2. Preparar el checkout de Windows

```powershell
cd C:\Users\User\Downloads\ai-video-factory
git pull --ff-only
git status
python -m pip install -e ".[gpu,dev]"
```

`.gitattributes` fuerza LF para `*.sh` y evita `/usr/bin/env: 'bash\r'`. Si el repositorio existía
antes de esa regla, renormaliza una vez y revisa el diff antes de confirmar cambios:

```powershell
git add --renormalize .
git diff --cached --check
git diff --cached
```

## 3. Variables de entorno

Define las variables en la sesión actual. Usa valores reales y no los guardes en el historial que
vayas a publicar:

```powershell
$env:SALAD_API_KEY = "..."
$env:SALAD_ORGANIZATION = "yagobordellorg"
$env:SALAD_PROJECT = "aivideofactory"
$env:SALAD_QUEUE_NAME = "ai-video-factory-jobs"
$env:SALAD_PRIORITY = "high"

$env:POSTGRES_DSN = "postgresql://..."
$env:R2_ENDPOINT_URL = "https://<account-id>.r2.cloudflarestorage.com"
$env:R2_BUCKET = "ai-video-factory"
$env:R2_ACCESS_KEY_ID = "..."
$env:R2_SECRET_ACCESS_KEY = "..."
```

Para Supabase en una red IPv4, usa primero el **Session pooler** en puerto `5432`. La conexión
directa puede resolver solo a IPv6. Comprueba el DSN:

```powershell
python -c "import os, psycopg; c=psycopg.connect(os.environ['POSTGRES_DSN'], connect_timeout=10); print(c.execute('SELECT 1').fetchone()); c.close(); print('Supabase/Postgres: OK')"
```

Aplica la migración una sola vez contra esa base con `psql`, o pega el mismo archivo en el SQL
Editor de Supabase:

```powershell
psql $env:POSTGRES_DSN -f "infra/sql/001_gpu_jobs.sql"
```

Si la red local permite R2, compruébalo:

```powershell
python -c "import os; from ai_video_factory.gpu.storage import R2ObjectStorage; s=R2ObjectStorage.create(endpoint_url=os.environ['R2_ENDPOINT_URL'], bucket=os.environ['R2_BUCKET'], access_key_id=os.environ['R2_ACCESS_KEY_ID'], secret_access_key=os.environ['R2_SECRET_ACCESS_KEY']); s.ping(); print('Cloudflare R2: OK')"
```

Un timeout TCP a `*.r2.cloudflarestorage.com:443` es un bloqueo de red local, router, ISP, VPN o
firewall; no es un error de credenciales. El smoke ya demostró que el worker de Salad sí alcanza R2.

## 4. Construir, probar y publicar la imagen

```powershell
$ImageTag = "yagobordell/ai-video-factory:phase7"

docker build `
  --file docker/phase7-worker/Dockerfile `
  --tag $ImageTag `
  .

docker run --rm `
  --name ai-video-factory-phase7 `
  --publish 8080:8080 `
  --env GPU_WORKER_MODE=local `
  --env SALAD_QUEUE_ENABLED=false `
  --env LOCAL_OBJECT_ROOT=/app/data/phase7-local `
  --volume "${PWD}/data/phase7-local:/app/data/phase7-local" `
  $ImageTag
```

En otra consola, `Invoke-RestMethod http://localhost:8080/ready` debe devolver `ready = true`.
Después publica y resuelve el digest del manifiesto:

```powershell
docker login
docker push $ImageTag

$inspect = docker buildx imagetools inspect $ImageTag
$match = $inspect | Select-String -Pattern '^Digest:\s+(sha256:[0-9a-f]{64})$'
if (-not $match) { throw "No se pudo resolver el digest publicado" }
$Digest = $match.Matches[0].Groups[1].Value
$Image = "docker.io/yagobordell/ai-video-factory@$Digest"
Write-Host "Imagen inmutable: $Image"
```

No uses el hash interno que muestra Salad como sustituto del digest del registro. La referencia que
se despliega debe contener literalmente `@sha256:`.

## 5. Generar los JSON de Salad

El render falla por defecto si recibe una etiqueta mutable. También genera el campo requerido
`readiness_probe.http.headers = []`.

```powershell
python scripts/render_phase7_salad.py `
  --image $Image `
  --queue-name $env:SALAD_QUEUE_NAME `
  --cpu 4 `
  --memory-mb 8192 `
  --replicas 0 `
  --min-replicas 0 `
  --max-replicas 1
```

Se crean:

- `data/output/phase7/salad/queue.json`;
- `data/output/phase7/salad/container-group.json`.

El segundo contiene secretos, tiene permisos restrictivos donde el sistema lo permite y está bajo
una ruta ignorada por Git. No lo confirmes ni lo compartas.

## 6. Crear o actualizar recursos por API

```powershell
$base = "https://api.salad.com/api/public/organizations/$env:SALAD_ORGANIZATION/projects/$env:SALAD_PROJECT"
$headers = @{
  "Salad-Api-Key" = $env:SALAD_API_KEY
  "Accept" = "application/json"
}
```

Salad puede no mostrar Job Queues en el portal. La API es la fuente de verdad. Lista como máximo
25 elementos por página:

```powershell
$queues = Invoke-RestMethod `
  -Uri "$base/queues?page=1&page_size=25" `
  -Headers $headers
$queues.items | Select-Object name, display_name | Format-Table
```

Si `ai-video-factory-jobs` no existe, créala una vez:

```powershell
$queueBody = Get-Content "data/output/phase7/salad/queue.json" -Raw
Invoke-RestMethod `
  -Method Post `
  -Uri "$base/queues" `
  -Headers $headers `
  -ContentType "application/json" `
  -Body $queueBody
```

Para el Container Group, evita duplicados por `name`. Si no existe:

```powershell
$groups = Invoke-RestMethod -Uri "$base/containers" -Headers $headers
$existing = $groups.items | Where-Object name -eq "ai-video-factory-worker"

if (-not $existing) {
  $body = Get-Content "data/output/phase7/salad/container-group.json" -Raw
  Invoke-RestMethod `
    -Method Post `
    -Uri "$base/containers" `
    -Headers $headers `
    -ContentType "application/json" `
    -Body $body
}
```

Si ya existe, actualiza únicamente el contenedor y la readiness probe desde el JSON recién
generado. El objeto `container` contiene todas las variables, incluidas las secretas, para evitar
borrarlas accidentalmente al reemplazar ese objeto anidado:

```powershell
$desired = Get-Content "data/output/phase7/salad/container-group.json" -Raw |
  ConvertFrom-Json
$patchBody = @{
  container = $desired.container
  readiness_probe = $desired.readiness_probe
} | ConvertTo-Json -Depth 30

Invoke-RestMethod `
  -Method Patch `
  -Uri "$base/containers/ai-video-factory-worker" `
  -Headers $headers `
  -ContentType "application/merge-patch+json" `
  -Body $patchBody
```

Comprueba que `container.image` contiene el digest y que `pending_change` termina en `false`:

```powershell
$group = Invoke-RestMethod `
  -Uri "$base/containers/ai-video-factory-worker" `
  -Headers $headers
$group | Select-Object version, pending_change, replicas, priority
$group.container | Select-Object image, hash
```

## 7. Arrancar y observar el worker

Si el grupo fue detenido manualmente, arráncalo antes de enviar el replay:

```powershell
Invoke-RestMethod `
  -Method Post `
  -Uri "$base/containers/ai-video-factory-worker/start" `
  -Headers $headers
```

El autoscaler puede mantener `0/0` hasta que haya un job. Para inspeccionar instancias:

```powershell
$result = Invoke-RestMethod `
  -Uri "$base/containers/ai-video-factory-worker/instances" `
  -Headers $headers
$result.instances |
  Select-Object id, state, ready, started, pulling_progress, update_time |
  Format-Table
```

Una instancia válida termina en `state=running` y `ready=True`. `downloading` puede tardar por la
imagen; `running` con `ready=False` exige revisar logs y `/ready`.

## 8. Smoke inicial

Para una instalación nueva:

```powershell
python scripts/submit_phase7_smoke.py `
  --queue-name $env:SALAD_QUEUE_NAME `
  --timeout-seconds 1800
```

El script sube un input a R2, crea el job, espera el resultado, descarga el output y verifica su
SHA-256. El checkout ya validado puede reutilizar su request guardado; no necesita repetir este paso
para demostrar replay.

## 9. Replay e idempotencia

Resubmite exactamente el request original. Este script solo necesita la API de Salad localmente; el
timeout local hacia R2 no lo bloquea:

```powershell
python scripts/replay_phase7_smoke.py `
  "data/output/phase7/salad/job-request-phase7-smoke-a1d3883364d8.json" `
  --queue-name $env:SALAD_QUEUE_NAME `
  --expected-attempt-count 1 `
  --timeout-seconds 1800
```

El cierre exige:

- Queue job `succeeded`;
- `output.replayed = true`;
- `attempt_count = 1`;
- mismo `job_id`, output key y SHA-256;
- nuevo `queue-replay-result-*.json` guardado localmente.

Si devuelve `replayed=false` o `attempt_count=2`, no cierres la fase: el mismo trabajo se volvió a
ejecutar en lugar de reconciliarse.

## 10. Consultar jobs

```powershell
$jobs = Invoke-RestMethod `
  -Uri "$base/queues/$env:SALAD_QUEUE_NAME/jobs?page=1&page_size=25" `
  -Headers $headers
$jobs.items |
  Select-Object id, status, create_time, update_time |
  Format-Table
```

Un job concreto:

```powershell
$job = Invoke-RestMethod `
  -Uri "$base/queues/$env:SALAD_QUEUE_NAME/jobs/<SALAD_JOB_ID>" `
  -Headers $headers
$job | Select-Object id, status, create_time, update_time | Format-List
$job.output | ConvertTo-Json -Depth 30
```

## 11. Detener coste al terminar

Tras guardar la evidencia, deja el autoscaler en cero y detén el grupo:

```powershell
$scaleDownBody = @{
  replicas = 0
  queue_autoscaler = @{
    min_replicas = 0
    max_replicas = 1
    desired_queue_length = 1
    polling_period = 30
  }
} | ConvertTo-Json -Depth 10

Invoke-RestMethod `
  -Method Patch `
  -Uri "$base/containers/ai-video-factory-worker" `
  -Headers $headers `
  -ContentType "application/merge-patch+json" `
  -Body $scaleDownBody

Invoke-RestMethod `
  -Method Post `
  -Uri "$base/containers/ai-video-factory-worker/stop" `
  -Headers $headers
```

Verifica `replicas=0`, `pending_change=False` y ninguna instancia activa.

## 12. Diagnóstico rápido

| Síntoma | Causa/acción |
|---|---|
| `bash\r: No such file` | Checkout CRLF; conserva `.gitattributes` y reconstruye la imagen |
| HTTP 403 Cloudflare 1010 | Usa el `User-Agent` actual del script; no reintentes una versión antigua |
| HTTP 400 `no_credits_available` | Añade créditos/entitlement en Salad |
| HTTP 400 `ReadinessProbe.Http.Headers` | Regenera JSON; ahora incluye `headers: []` |
| `pageSize must be between 1 and 25` | Usa `page_size=25` como máximo |
| `deploying`, 0 instancias | Puede ser autoscaler a cero; envía job o sube temporalmente replicas |
| Trouble allocating workload | Prioridad alta, menos recursos o más clases GPU; revisa System Events |
| `downloading` prolongado | Espera y observa progreso; revisa tamaño/registro si no avanza |
| `running`, `ready=False` | Revisa logs, variables, Supabase/R2 y respuesta de `/ready` |
| R2 timeout solo en el PC | Prueba otra red/VPN/firewall; el replay no requiere acceso R2 local |

La referencia operativa de Salad confirma que la prioridad pertenece a `container.priority` y que
el handler HTTP de readiness requiere `headers`: [Deploy or Update a Container Group](https://docs.salad.com/agents/container-engine/deploy-or-update-container-group).

## Criterio de cierre de 7.2

- [x] migración de jobs aplicada y `SELECT 1` correcto;
- [x] imagen publicada y desplegada como `repository@sha256:...`;
- [x] Queue y Container Group verificados por API;
- [x] instancia `running/ready` con la versión actual durante la prueba;
- [x] smoke real `succeeded`, con fila Postgres y objeto R2 coherentes;
- [x] replay real `succeeded`, `replayed=true` y sin incrementar `attempt_count`;
- [x] recursos escalados de nuevo a cero y secretos fuera de Git.

**Resultado: Fase 7.2 cerrada el 30 de agosto de 2026.**
