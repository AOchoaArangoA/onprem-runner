# Arquitectura (Fase 1)

## 1. Nombre

Disponibilidad en PyPI verificada el 2026-10-05 (404 = libre):

| Nombre | PyPI | Comentario |
|---|---|---|
| `onprem-runner` | libre | Descriptivo; CLI corto: `opr`. **Recomendado.** |
| `winflow` | libre | Corto, pero acota a Windows. |
| `onpremflow` | libre | Alternativa descriptiva. |
| `localflow` | **tomado** | Descartado. |

Pendiente: comprobar el nombre en GitHub antes de crear el repositorio.

## 2. Scope

**MVP (v0.1):** ejecución bajo demanda por CLI, pipeline de pasos, configuración
YAML validada, notificadores Email y File, logs estructurados.

**v0.2+:** `watch` (archivo/webhook),
servicio de Windows, step types `powershell`/`exe`/`node`, API Python fluida.

## 3. Modelo de flujo

Pipeline de pasos con dependencias opcionales (`needs:`), resuelto por orden
topológico. No es un motor de DAGs: sin paralelismo en el MVP, sin estado persistente.

- `Pipeline`: nombre, pasos, notificadores, política de fallo.
- `Step`: `id`, `type`, parámetros propios, `needs`, `timeout`, `retries`.
- `StepResult`: estado, código de salida, stdout/stderr, duración.
- `RunResult`: resultados agregados; es lo que reciben los notificadores.

## 4. Extensibilidad

Tres puntos de extensión, todos registrados por *entry points* (`importlib.metadata`):

- `onprem_runner.steps`: tipos de paso (`python`, `shell` en el MVP).
- `onprem_runner.notifiers`: salidas (`email`, `file` en el MVP).
- `onprem_runner.triggers`: disparadores (v0.2).

Así un paquete externo añade PowerShell, Node o un `.exe` sin tocar el core.

## 5. Configuración

YAML como formato primario, validado con Pydantic v2. Los secretos nunca van en el
YAML: se referencian como `${ENV_VAR}` y se leen del entorno o de Windows Credential
Manager (`keyring`).

```yaml
name: reporte-diario
steps:
  - id: extraer
    type: python
    script: scripts/extraer.py
    timeout: 600
  - id: consolidar
    type: python
    script: scripts/consolidar.py
    needs: [extraer]
notifiers:
  - type: file
    path: out/{run_id}.json
    format: json
  - type: email
    when: failure   # no "on": YAML 1.1 parses it as a boolean
    smtp_host: smtp.empresa.local
    username: ${SMTP_USER}
    password: ${SMTP_PASSWORD}
    to: [ops@empresa.com]
```

## 6. Ejecución en Windows

No se construye un demonio propio en el MVP. Se delega en el sistema operativo:

1. **Programado:** `opr schedule install` genera el XML y registra la tarea con
   `schtasks`. Task Scheduler ya cubre ejecución sin sesión iniciada, al arranque y
   reintentos. La tarea invoca `opr run pipeline.yaml`.
2. **Bajo demanda:** `opr run`.
3. **Eventos:** `opr watch` en primer plano; luego envuelto como servicio (NSSM o
   `pywin32`).

## 7. Decisiones

| Tema | Decisión |
|---|---|
| Python | ≥ 3.11 |
| Build | hatch (hatchling) |
| CLI | typer |
| Validación | pydantic v2 |
| Calidad | ruff (lint + format), mypy, pytest |
| Publicación | PyPI Trusted Publishing (OIDC) |
| Licencia | MIT |
