# App de escritorio: diseño (v0.2)

Estado: **diseño aprobado en sus decisiones principales**, sin código todavía.
Nombre de trabajo: **OPR Studio** (la CLI `opr` y el paquete `onprem-runner` se mantienen).

## 1. Decisiones tomadas

| Tema | Decisión |
|---|---|
| Envoltura de escritorio | **Electron** (Node 22 ya instalado; un solo `.exe` portable con `electron-builder`). Se puede migrar a Tauri después: la UI es web. |
| UI | React + TypeScript + Vite; lienzo con **React Flow** (`@xyflow/react`); i18n **es / en** (i18next). |
| Motor | El motor Python actual, ampliado y servido como proceso local (`opr serve`). |
| Base de datos | **DuckDB**, un único archivo. **El motor es el único dueño**; nadie más lo abre. |
| Definiciones de flujos | **Archivos YAML** (fuente de verdad). El lienzo los lee y los escribe. |
| Ejecución | **Ramas independientes en paralelo** + **conexiones condicionales** (éxito / fallo / siempre). |
| Logs | Línea a línea (stdout/stderr) + **logs estructurados**: si una línea es JSON con `level`/`msg`, se guardan sus campos. Retención configurable. |
| Secretos | **Ambos**: `${NOMBRE}` (entorno) y `${secret:NOMBRE}` (almacén cifrado con DPAPI de Windows). |
| Intérprete de scripts | **Ambos, externo por defecto**: `python:` por tarea o ajuste global (autodetección); opción `embedded`. |
| Triggers v1 | **Manual + programación** (motor propio). Archivo, webhook y encadenado en v2, como plugins. |
| Segundo plano | **Bandeja del sistema**: cerrar la ventana no detiene los triggers. |
| Datos | Carpeta `data/` **junto al .exe**; si no es escribible, `%APPDATA%\OPR Studio`. |
| Distribución | Uso interno, **sin firma digital**. Actualizar = reemplazar el .exe. |
| Hitos | **H1**: lienzo + ejecución + logs en BD. **H2**: triggers, bandeja, .exe portable final. |

## 2. Arquitectura

```
┌─────────────── OPR Studio.exe (Electron, portable) ───────────────┐
│  main process ── bandeja, ventana, ciclo de vida del motor        │
│  renderer (React) ──HTTP + SSE (token)──┐                         │
└─────────────────────────────────────────┼─────────────────────────┘
                                          ▼
                    opr-engine.exe  (Python, PyInstaller)  127.0.0.1:<puerto aleatorio>
                    ├─ API local + eventos en vivo (SSE)
                    ├─ Planificador DAG (paralelo, condiciones)
                    ├─ Triggers: manual, schedule  (v2: file, webhook, chain)
                    ├─ Escritor único de DuckDB (cola + inserción por lotes)
                    └─ Secretos (DPAPI), enmascarado en logs
```

- **Electron arranca el motor** como proceso hijo, en un puerto libre de loopback, con un token de
  sesión aleatorio. La API rechaza peticiones sin token y con `Host` ajeno (igual que el panel actual).
- **`opr run` y el MCP desde fuera**: si el motor está abierto, le piden la ejecución por la API local
  (descubierto mediante un archivo de bloqueo en `data/`). Si no está abierto, `opr run` funciona como
  hoy y deja el resultado en `.opr/runs/*.json`; el motor lo **ingesta en DuckDB** al arrancar.
  Así se respeta que DuckDB solo admite un proceso escritor.
- **Escrituras a DuckDB**: un único hilo escritor con cola; las líneas de log se insertan por lotes
  (cada ~250 ms o 500 filas), porque DuckDB rinde mal con inserciones fila a fila.

## 3. Base de datos (DuckDB)

```
schema_version(version)
runs(run_id PK, pipeline_file, pipeline_name, status, trigger, started_at, finished_at, duration_s)
step_runs(run_id, step_id, type, status, exit_code, attempts, started_at, finished_at, duration_s)
log_lines(run_id, step_id, seq, ts, stream, level, message, fields JSON)
```
- `trigger` = `manual | schedule | cli | mcp | …`.
- `log_lines.level` y `fields` salen de las líneas JSON estructuradas; en texto plano `level` es `NULL`.
- Retención: borrado por días (90 por defecto) y tope de líneas por ejecución; ambos ajustables.
- Migraciones numeradas desde el primer día (`schema_version`).

## 4. Cambios al formato YAML

```yaml
name: reporte
max_parallel: 4            # límite de concurrencia (por defecto 4)
steps:
  - {id: extraer, type: python, script: extraer.py}
  - {id: validar, type: python, script: validar.py, needs: [extraer]}
  - {id: alerta,  type: python, script: alerta.py,
     needs: [{step: validar, when: failure}]}      # solo si validar falla
  - {id: publicar, type: shell, command: echo ok,
     needs: [{step: validar, when: success}]}
schedule: {frequency: daily, at: "07:30"}
ui:
  positions: {extraer: [0, 0], validar: [260, 0]}  # posiciones de los nodos en el lienzo
```
- `needs` acepta el formato actual (`[a, b]`, equivale a `when: success`) y el objeto con `when`.
- Un paso se ejecuta cuando **todas** sus dependencias terminaron y **cada** condición se cumple;
  si alguna no se cumple, queda `skipped`.
- Los YAML actuales siguen funcionando sin cambios.

## 5. Lienzo (React Flow)

- Nodos = pasos; aristas = dependencias (color según éxito / fallo / siempre).
- Interacciones: arrastrar para crear y conectar, editar parámetros en un panel lateral,
  **ejecutar todo / ejecutar solo este nodo**, estado en vivo (pendiente, ejecutando, éxito, fallo,
  omitido) y deshacer/rehacer.
- Guardado del YAML: atómico (archivo temporal + reemplazo) y con `ruamel.yaml` para **conservar
  comentarios y orden** de lo que el usuario haya escrito a mano; PyYAML los perdería.
- Visor de logs: filtro por nivel / paso / texto, seguimiento en vivo, exportar a TXT/CSV/JSON.

## 6. Secretos

- Almacén `data/secrets.dat`: nombres en claro, valores cifrados con **DPAPI** (`CryptProtectData`, vía
  `ctypes`, sin dependencias). Solo el mismo usuario de Windows en el mismo equipo puede descifrarlos.
- La UI **nunca recibe** el valor, solo si existe; se guardan, se reemplazan y se eliminan.
- Se resuelven al iniciar cada paso, solo hacia su entorno de proceso.
- **Enmascarado**: cualquier aparición del valor en stdout/stderr se sustituye por `***` antes de
  guardarse en la BD o mostrarse.
- Limitación asumida: los secretos **no viajan** a otro PC (hay que volver a introducirlos).

## 7. Intérprete de scripts

- Orden de resolución: `python:` del paso → ajuste global del proyecto → autodetección
  (lanzador `py`, `PATH`, `.venv` del proyecto).
- `python: embedded` ejecuta el script con el intérprete incluido en `opr-engine.exe`
  (solo biblioteca estándar y lo que se empaquete).

## 8. Empaquetado

- `opr-engine.exe` con PyInstaller; app con `electron-builder` (target `portable`), incluyendo el
  motor como recurso. Tamaño esperado: 150–250 MB (Electron + Python + DuckDB).
- El `.exe` portable se **autoextrae en una carpeta temporal en cada arranque** (unos segundos).
- Sin firma: SmartScreen y algunos antivirus pueden avisar la primera vez.
- Requisitos: Windows 10/11 de 64 bits.

## 9. Hitos y tareas

**H1: Lienzo + ejecución + logs en BD** (primer .exe probable)
1. Motor: `needs` condicionales, ejecución paralela, `max_parallel`, `ignore_failure`.
2. Captura de logs línea a línea con tiempo real y parseo de JSON estructurado.
3. Capa DuckDB: esquema, migraciones, hilo escritor, ingesta de `.opr/runs`, retención.
4. Secretos con DPAPI y enmascarado.
5. `opr serve`: API local con token + eventos en vivo (SSE) + resolución de intérprete.
6. Electron + React: lienzo editable, panel de parámetros, ejecución en vivo, visor de logs, i18n es/en.
7. Empaquetado y prueba del `.exe`.

**H2: Triggers y segundo plano**
1. Planificador propio (misma sintaxis `schedule:`), política de ejecuciones perdidas.
2. Bandeja, cerrar a bandeja, arranque con Windows (opcional), notificaciones de Windows.
3. Interfaz de triggers y estado de próxima ejecución.
4. `.exe` portable final y documentación.

**H3 (versión 2)**: triggers de archivo/carpeta, webhook local y encadenado.

## 10. Riesgos

| Riesgo | Mitigación |
|---|---|
| Arranque lento del portable (autoextracción) | Medir pronto; si molesta, ofrecer también zip sin autoextraer |
| DuckDB + escritura de logs a alto volumen | Inserción por lotes, un solo escritor; pruebas de carga en H1 |
| Falsos positivos de antivirus con PyInstaller | Documentarlo; usar `onedir` y evitar UPX |
| Intérprete `embedded` limitado | Dejarlo como opción secundaria y avisarlo en la UI |
| Secretos atados al usuario y equipo | Avisar en la UI; no prometer portabilidad de secretos |
