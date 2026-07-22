# ADR 0008: E8 — Suite de conformidad pública + packs de ejemplo

Fecha: 2026-07-22
Estado: aceptado

## Contexto

E8 implementa `docs/plan.md` sección "E8 — Suite de conformidad pública +
packs de ejemplo" y `docs/spec.md` §13 (Conformance). Extrae las
comprobaciones normativas de L1/L2/L3 a un paquete instalable,
`conformance/`, ejecutable contra cualquier implementación de Belay vía un
adaptador fino `ConformanceTarget`, y añade `examples/contracts/email.yaml`
como pack de ejemplo con un efecto irreversible.

## Estado real de los marcadores `@conformance` previos

El prompt pedía "normalizar" los marcadores `@conformance(level=…)`
supuestamente usados por E1-E7. En la práctica, `git grep -n "@conformance"
tests/` solo encuentra dos docstrings (`tests/ledger/test_replay.py`,
`tests/ledger/test_verify.py`) citando `@conformance §9.x` como comentario
de trazabilidad hacia la spec — nunca un marcador `pytest.mark` real, y
nunca junto a un nivel L1/L2/L3. No hay una convención de marcadores que
extraer mecánicamente. Decisión: en vez de inventar una migración de algo
que no existe, `conformance/` es una suite nueva y autocontenida que
reimplementa los escenarios destacados por spec §13 sobre el adaptador
`ConformanceTarget`, usando marcadores pytest reales (`l1`/`l2`/`l3`,
registrados en `conformance/tests/conftest.py`) desde el principio. Los
docstrings `@conformance §9.x` existentes en `tests/` se dejan como están
— son trazabilidad hacia la spec, no marcadores de nivel, y no son del
paquete público.

## Decisiones

- **`ConformanceTarget` tiene 6 métodos, uno por operación que un escenario
  L1/L2/L3 realmente necesita invocar u observar** (`conformance/target.py`):
  `new_session` (arranca una sesión gobernada sobre un contract set y un
  executor de herramientas — necesario en todos los niveles), `call`
  (resolve→plan→policy→(approval)→execute, spec §3-§7; L1/L2), `approve`
  (resuelve un `pause` como operador, nunca desde el camino del agente,
  spec §7.2; L2), `ledger` (lee el stream de eventos para verificación de
  cadena/coherencia, spec §9.1/§9.2; todos los niveles), `run_saga` (saga
  multi-paso con auto-compensación, spec §8; L3), `rewind` (spec §10; L3).
  No se añadió un séptimo método para leer contratos ni uno de
  arranque/parada por separado: `new_session` hace ambas cosas (recibe las
  rutas de contratos y el executor, devuelve un `session_id` ya con
  `session_started`/`contract_set_pinned` apendados), evitando un ciclo
  `start()`/`stop()` que ningún escenario necesita (las implementaciones en
  memoria de este repo no requieren cierre explícito).
- **`steps` en `run_saga` son "implementation-native step specs"**, no un
  tipo definido por `ConformanceTarget`. La suite de referencia
  (`conformance/tests/test_l3_sagas_rewind.py`) importa
  `belay.executor.saga.SagaStep` directamente porque construye contra el
  adaptador `belay`. Un adaptador de un tercero necesitaría su propio tipo
  de paso y, estrictamente, sus propios tests L3 (o una capa de
  construcción de pasos agnóstica) — se documenta aquí como límite conocido
  en vez de forzar un DSL de pasos genérico dentro del límite de "~6
  métodos" que pide plan.md; añadir esa capa es la extensión natural si un
  segundo target real (no `belay`) llega a implementar L3.
- **El adaptador de referencia (`conformance/targets/belay_target.py`)
  reutiliza el cableado real de producción** (`belay.proxy.lifecycle.Lifecycle`,
  `belay.executor.saga.SagaExecutor`, `belay.rewind.service.RewindService`)
  en vez de reimplementar la lógica de gobernanza. La suite debe ejercitar
  el código real, no un modelo paralelo de él — un adaptador que reimplemente
  su propia versión de "aprobación" o "rewind" estaría probando el
  adaptador, no Belay.
- **Los escenarios usan executores de herramientas en memoria
  (`conformance/tests/fakes.py`), no los servidores MCP reales de
  `examples/`.** `examples/fs-server` y `examples/crm-mock` ya se ejercitan
  contra stdio real en E3/E6 (`tests/proxy/test_stdio_integration.py`,
  `tests/executor/test_crm_mock_acceptance.py`); la suite de conformidad
  prueba la lógica de gobernanza de Belay, no el transporte MCP, así que un
  executor en memoria basta y mantiene el bucle rápido (`@slow` sigue
  reservado para lo que realmente arranca un subproceso).
- **`belay-conformance run` ejecuta pytest programáticamente** sobre
  `conformance/tests/` filtrando por marcador (`-m "l1 or l2 or l3"` según
  el nivel, acumulativo per spec §13: "Three levels, cumulative"), en vez de
  escribir un motor de test propio — pytest ya es una dependencia y ya sabe
  descubrir/ejecutar/reportar. `--target` acepta el alias `belay` o una ruta
  `modulo:Clase` para que "cualquier implementación" sea literal sin un
  sistema de plugins/entry-points.
- **`email.yaml` es `reversibility: irreversible` sin bloque `undo`**
  (`examples/contracts/email.yaml`, `email.send`): un envío de correo no
  tiene deshacer posible, y el schema del Apéndice A rechaza `undo` en un
  contrato `irreversible` — el pack existe precisamente para ejercitar la
  rama "no declares un undo que no existe" y el default `defaults.irreversible:
  pause` (spec §6.4) sin caps ni política explícita.
  Test: `conformance/tests/test_l2_plans_policy.py::test_irreversible_call_pauses_by_default`,
  `tests/contracts/test_loader.py::test_examples_email_yaml_loads_and_is_irreversible_with_no_undo`.

## Mapeo marcador → spec §13

| Marcador | Nivel | Añade (spec §13) | Ficheros |
|---|---|---|---|
| `l1` | L1 — Contracts | §4 (incl. default rule §4.6), eventos §9.1 | `conformance/tests/test_l1_contracts.py` |
| `l2` | L2 — Plans & policy | §5, §6, §7 (además de L1) | `conformance/tests/test_l2_plans_policy.py` |
| `l3` | L3 — Sagas & rewind | §8, §10, verificación completa §9.2 (además de L1+L2) | `conformance/tests/test_l3_sagas_rewind.py` |

`belay-conformance run --level N` selecciona `-m "l1 or ... or lN"` — cada
nivel es literalmente acumulativo, no solo en teoría (spec §13: "Three
levels, cumulative").

## Criterio de salida verificado

```
$ pip install -e ".[dev]"
$ belay-conformance run --target belay --level 3
...
11 passed in 2.63s
belay-conformance: target=belay -> L3 PASSED
```

Ejecutado también como subproceso real (no en proceso) en
`tests/cli/test_conformance.py::test_belay_conformance_console_script_runs_as_a_real_subprocess`
(`@slow`), invocando el binario `belay-conformance` instalado por
`pyproject.toml`'s `[project.scripts]`, no un import directo.
