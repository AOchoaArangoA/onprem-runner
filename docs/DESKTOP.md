# OPR Studio: aplicación de escritorio

Estado: **implementada** (Hitos 1 y 2 de las decisiones de abajo). Fecha: 2026-10-20.
La CLI `opr` y el paquete `onprem-runner` se mantienen; OPR Studio es una interfaz y un motor
de servicio construidos sobre el mismo núcleo.

## 1. Qué hay

- **Lienzo interactivo** (React Flow): pasos como nodos, dependencias como conexiones con condición
  (`si tiene éxito` / `si falla` / `siempre`), arrastrar para crear y conectar, panel de propiedades,
  deshacer/rehacer, ordenar el diseño, estado en vivo de cada nodo y ejecución de un solo paso o
  "desde aquí".
- **Ejecución paralela** de ramas independientes (`max_parallel`, 4 por defecto) y cancelación.
- **Logs línea a línea** en tiempo real y guardados en **DuckDB**, con filtros por paso, nivel, flujo
  y texto, y exportación a TXT/CSV/JSONL. Una línea JSON con `level` y `msg` se guarda con nivel y
  campos propios.
- **Secretos** cifrados con DPAPI de Windows (`${secret:NOMBRE}`), con enmascarado en logs.
- **Programación propia** (`schedule:`) que se ejecuta mientras la app está abierta o en la bandeja,
  con pausa y con "una ejecución al volver" si se perdió alguna (`catch_up`).
- **Bandeja del sistema**: cerrar la ventana no detiene nada; avisos de Windows al terminar una
  ejecución con la ventana oculta; opción de arrancar con Windows.
- **Interfaz en español e inglés**, tema claro/oscuro/sistema.
- **Un único `.exe` portable** que guarda sus datos junto a sí mismo.

## 2. Decisiones

| Tema | Decisión |
|---|---|
| Envoltura | **Electron** (un solo `.exe` portable con `electron-builder`). La UI es web: migrar a Tauri después solo cambia la envoltura. |
| UI | React + TypeScript + Vite; lienzo con `@xyflow/react`; i18n con i18next (es / en). |
| Motor | Python, servido como proceso local (`opr serve`) y empaquetado con PyInstaller. |
| Base de datos | **DuckDB**, un archivo. **El motor es el único dueño**; la CLI y el MCP no la abren. |
| Definiciones | **Archivos YAML** (fuente de verdad). El lienzo los lee y escribe conservando comentarios (`ruamel.yaml`). |
| Estado final de una ejecución | **Fallida si cualquier paso falló** (salvo `ignore_failure: true`). Un paso omitido no cuenta como fallo. |
| Ejecuciones perdidas | Al volver, **una sola** (la más reciente) si se perdió alguna; se desactiva con `catch_up: false`. |
| Paralelismo | 4 pasos a la vez por defecto. |
| Intérprete de scripts | **Externo por defecto** (`python:` por paso o ajuste global, con autodetección) y opción `embedded`. |
| Triggers v1 | **Manual + programación.** Archivo, webhook y encadenado quedan para la versión 2, como plugins. |
| Datos | Carpeta `data/` **junto al .exe**; si no es escribible, `%APPDATA%\OPR Studio`. |
| Distribución | Uso interno, **sin firma digital**. Actualizar = reemplazar el `.exe`. |

## 3. Arquitectura

```
┌────────────── OPR Studio (Electron, portable) ──────────────┐
│ main: ventana, bandeja, avisos, ciclo de vida del motor     │
│ renderer (React) ──HTTP + SSE, token de sesión──┐           │
└─────────────────────────────────────────────────┼───────────┘
                                                   ▼
                         opr-engine (Python)  127.0.0.1:<puerto>
                         ├─ API local (token, Host y Origin comprobados)
                         ├─ Planificador DAG: paralelo, condiciones, cancelación
                         ├─ Planificador de horarios (schedule)
                         ├─ Escritor único de DuckDB (cola + inserción por lotes)
                         └─ Secretos (DPAPI), enmascarado en logs
```

- Electron arranca el motor como proceso hijo en un puerto libre de loopback, con un token aleatorio
  de sesión que pasa por entorno; la API rechaza peticiones sin token, con `Host` ajeno o con
  `Origin` de otro sitio. Si el motor muere, Electron lo reinicia (hasta 3 veces).
- El motor escribe `data/engine.lock` (puerto y token) y rechaza un segundo motor en la misma carpeta.
- Los logs se insertan por lotes como literales SQL escapados: el enlace de parámetros de DuckDB
  cuesta ~1,5 ms por parámetro y hacía 200 veces más lento el volcado.
- Las ejecuciones de `opr run` hechas con la app cerrada se guardan en `.opr/runs/*.json` y el
  motor las **ingesta** en DuckDB al arrancar.

## 4. Formato YAML

```yaml
name: reporte
max_parallel: 4
steps:
  - {id: extraer, type: python, script: extraer.py}
  - {id: validar, type: python, script: validar.py, needs: [extraer]}
  - {id: alerta, type: python, script: alerta.py,
     needs: [{step: validar, when: failure}]}          # solo si validar falla
  - {id: publicar, type: shell, command: echo ok,
     needs: [{step: validar, when: success}]}
schedule: {frequency: daily, at: "07:30"}               # catch_up: false para no recuperar
ui:
  positions: {extraer: [0, 0], validar: [300, 0]}       # posiciones de los nodos
```

Un paso se ejecuta cuando **todas** sus dependencias terminaron y **cada** condición se cumple; si no,
queda `skipped`. Con `fail_fast`, tras un fallo solo corren los pasos de recuperación o limpieza
(con una arista `failure` o `always`). Los YAML anteriores siguen funcionando.

Alineación de horarios (determinista, un reinicio no cambia el ritmo): `minutes`/`hourly` cuentan
desde la medianoche local; `daily` y `weekly` usan días y semanas (empiezan en lunes) múltiplos de
`every`; `startup` se ejecuta una vez cada vez que arranca el motor.

## 5. Base de datos

```
runs(run_id PK, pipeline_file, pipeline_name, status, trigger, started_at, finished_at, duration_s)
step_runs(run_id, step_id, type, status, exit_code, attempts, ignored, started_at, finished_at, duration_s)
log_lines(run_id, step_id, seq, ts, stream, level, message, fields JSON)
```

Migraciones numeradas (`schema_version`), retención en días (90 por defecto) y tope de líneas por
ejecución (200.000), ambos configurables en Ajustes.

## 6. Construir y probar

```powershell
pip install -e ".[dev]"            # motor y pruebas
cd desktop; npm ci                 # interfaz y Electron
npm run build                      # interfaz + proceso principal
npm start                          # arrancar en desarrollo (usa .venv\Scripts\opr.exe)

npm test                           # pruebas unitarias de la interfaz
npx playwright test -c e2e/playwright.config.ts     # end-to-end (Chrome instalado)

python engine/build_engine.py      # motor -> engine-dist/opr-engine/opr-engine.exe
npm run dist                       # -> release/OPR-Studio-<versión>-portable.exe
```

El `.exe` portable pesa ~116 MB y se **autoextrae en una carpeta temporal en cada arranque** (unos
10-15 s hasta tener el motor listo). Sin firma digital, SmartScreen o algún antivirus pueden avisar
la primera vez.

## 7. Limitaciones conocidas

- Los **secretos no viajan** a otro equipo ni a otro usuario de Windows: DPAPI los ata a ambos.
- El intérprete `embedded` solo conoce la biblioteca estándar; para scripts con librerías propias hay
  que apuntar a un Python o `venv` externo.
- Las programaciones propias solo se ejecutan con la app abierta o en la bandeja; para ejecutar sin
  la app existe `opr schedule` (Windows Task Scheduler).
- Las notificaciones por correo y archivo se editan en el YAML (el lienzo las muestra pero no las edita).
- Triggers de archivo/carpeta, webhook y encadenado: versión 2.
