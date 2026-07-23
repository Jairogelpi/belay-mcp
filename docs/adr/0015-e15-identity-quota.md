# ADR 0015: E15 — Per-identity irreversible-action quota (not just per-call caps)

Fecha: 2026-07-23
Estado: aceptado

## Contexto

`docs/plan-v2.md`, sección "E15 -- Per-identity irreversible-action quota (not
just per-call caps)". E4's `Cap` limita el radio de explosión de una sola
llamada/plan ("max 100 filas esta acción"). Nada limitaba cuántas
aprobaciones irreversibles por separado puede acumular una misma identidad
(E14) a lo largo de una ventana de tiempo, aunque cada una individualmente
parezca pequeña y se apruebe. El pedido real de gobernanza empresarial es
literal: "aprobé un bulk-delete, no aprobé que el agente lo repita 200
veces". Depende de E14 (`initiated_by`): sin saber a qué identidad pertenece
una sesión, no hay cuota por identidad que tenga sentido.

## Decisiones

- **`belay/policy/quota.py`: `QuotaTracker`, mismo patrón que
  `belay.policy.baseline.BaselineStore` (E10) -- lee el ledger, no guarda un
  segundo store paralelo de verdad.** La diferencia real frente a E10:
  `BaselineStore` lee **una sesión** (`ledger.read(session_id)`);
  `QuotaTracker` lee **todas las sesiones** (`ledger.read_all()`) agrupadas
  por `session_id`, porque una identidad (E14) puede abrir muchas sesiones y
  la cuota es por identidad, no por sesión. Para cada sesión, la identidad
  se resuelve leyendo su evento `session_started.initiated_by` (E14: fijado
  una sola vez al arrancar la sesión) -- sesiones sin `initiated_by` (o de
  otra identidad) simplemente no contribuyen ningún conteo.
- **Solo cuentan acciones irreversibles ya aprobadas y ejecutadas, nunca
  denegadas ni todavía pendientes.** Por cada `step_seq` de una sesión,
  `QuotaTracker` reconstruye: `plan_created.reversibility`,
  `policy_evaluated.verdict`, si existe un `approval_resolved` con
  `state="approved"`, y si existe `step_committed` (prueba de ejecución
  real, spec §8.1). Reglas de conteo:
  - `verdict == "allow"` cuenta si y solo si hay `step_committed` (auto-allow
    ejecutado).
  - `verdict == "pause"` cuenta si y solo si hay `approval_resolved`
    aprobado **y** `step_committed` (aprobado y realmente ejecutado, no solo
    aprobado y todavía en cola de ejecución).
  - `verdict == "deny"` nunca cuenta (y en la práctica nunca tiene
    `step_committed`, porque `Lifecycle.govern_and_execute` lanza antes de
    llegar al `SagaExecutor`).
  Test explícito: `test_denied_and_pending_actions_never_count_toward_quota`.
- **Ventana rodante con el `Clock` inyectable (E4), nunca el reloj de pared
  directo.** `QuotaTracker.count(identity, now, window)` recibe `now`
  explícito (viene de `PolicyEngine.clock.now()`, ya presente desde E4/E10
  para `quiet_hours`/`anomaly`) y calcula `cutoff = now - window`. Regla de
  frontera, deliberadamente inclusiva en el extremo antiguo: una acción
  cuenta si `cutoff <= at <= now`; una acción justo un instante más vieja que
  `cutoff` no cuenta. Ambos extremos tienen test dedicado
  (`test_action_just_inside_window_counts`,
  `test_action_aged_out_of_window_does_not_count`). El timestamp usado por
  acción es el `at` de su propio evento `policy_evaluated` (el momento en
  que el motor decidió, no el de `plan_created` ni el de `step_committed`).
  **Advertencia real descubierta al escribir el demo end-to-end**:
  `LedgerStore.append` siempre estampa `datetime.now(UTC)` real (spec §9.1)
  -- no acepta un `Clock` inyectado para el timestamp del evento en sí. Un
  `FixedClock` fijado en el pasado para `PolicyEngine` produciría `now`
  desalineado con el `at` real de los eventos, sacando toda acción fuera de
  su propia ventana. `examples/demo_quota.py` y el test end-to-end
  (`test_end_to_end_nth_bulk_action_paused_purely_by_quota_no_cap`) usan
  deliberadamente el `SystemClock` real por esa razón; los tests de frontera
  con `FixedClock` retro-fechan directamente la columna `at` del evento vía
  SQLAlchemy (simulando que la acción de verdad ocurrió en ese instante, no
  reescribiendo un evento ya firmado/verificado en producción).
- **Nueva dimensión `quota` en `PolicyEngine.evaluate`, combinada por la
  misma regla de severidad máxima (`deny > pause > allow`)** que ya usan
  `tools`/`quiet_hours`/`anomaly`(E10)/el default irreversible -- no hay un
  segundo camino de resolución. `_evaluate_quota` solo se evalúa cuando
  `plan.reversibility == "irreversible"` y `defaults.quota.enabled`; si no
  hay identidad resuelta para la sesión, no contribuye nada (mismo espíritu
  "ausencia de datos, no bloquea" que el cold-start de E10).
- **`Defaults.quota` (`QuotaDefaults`): `enabled=False` por defecto, a
  diferencia de `AnomalyDefaults.enabled=True`.** Esta es la diferencia
  honesta que pide el plan: el baseline estadístico de E10 es un número que
  se deriva solo de los propios datos de la sesión y es seguro activarlo sin
  configuración manual. Un número de cuota (`max_irreversible_actions=20`,
  `window="1d"`) **no** se deriva de nada -- es un juicio de política que un
  operador real fijará según su propio apetito de riesgo. Publicar
  `enabled=True` con un número inventado sería fingir que "20 por día" es
  neutral cuando no lo es. Por eso `belay-conformance run --target belay
  --level 3` sigue en PASSED con cero cambio de comportamiento por defecto:
  un operador tiene que optar explícitamente por `enabled=True` y elegir su
  propio número, exactamente como ya elige sus propios `Cap`s (E4).
- **`window` es un string parseado por `belay.policy.quota.parse_window`**
  (`"1d"`, `"7d"`, `"12h"`, `"30m"`, `"45s"`) -- una regex simple, no una
  dependencia nueva (`dateutil`/`pytimeparse` habrían sido overkill para
  cinco sufijos).
- **Explicabilidad: la razón de `quota` en `PolicyResult.reasons` nombra la
  identidad, el conteo actual, la ventana configurada y el máximo
  configurado en texto legible**, ej. `"quota: identity 'agent-bot' has 3
  approved irreversible action(s) in the trailing 1d window, at/over the
  configured max of 3"` -- mismo estándar de explicabilidad que la razón de
  `anomaly` de E10.
- **Composición con E4's `Cap`: no lo reemplaza, coexiste.** `quota` es un
  presupuesto acumulado por identidad a través del tiempo y de múltiples
  llamadas; `Cap` sigue siendo el límite de radio de explosión de una sola
  llamada. Un operador puede (y probablemente debería) configurar ambos:
  un `Cap` que impida un `bulk_delete` de 10,000 filas de una sola vez, y
  una `quota` que impida que el agente haga 50 `bulk_delete`s de 10 filas
  cada uno en un día. `test_quota_composes_with_cap_via_max_severity` y
  `test_quota_composes_with_irreversible_default_max_severity` confirman
  que ambos coexisten bajo la misma regla de severidad máxima sin caminos
  paralelos.
- **Test de propiedad (Hypothesis):** para cualquier `M` (máximo
  configurado) y cualquier número de acciones previas `>= M` de una misma
  identidad dentro de la ventana, la siguiente acción irreversible siempre
  dispara el veredicto configurado, nunca `allow`
  (`test_property_mth_plus_one_action_always_triggers_never_allow`) -- la
  garantía central de esta entrega.

## Brechas conocidas / seguimiento (no resueltas, documentadas honestamente)

- **`LedgerStore.append` no acepta un `Clock` inyectado para el timestamp
  del evento** (siempre `datetime.now(UTC)` real, spec §9.1 no lo exige de
  otra forma). Esto es correcto para producción (el ledger es evidencia de
  lo que de verdad pasó) pero significa que pruebas que necesitan simular
  "hace 2 días" deben retro-fechar filas directamente en vez de usar un
  `FixedClock` de punta a punta -- documentado arriba, no escondido.
- **`QuotaTracker.count` recorre `read_all()` sin índice por identidad.**
  Correcto para el tamaño de datos de v0.1 (SQLite, un despliegue), pero es
  O(todas las sesiones de todos los tiempos) por llamada a `evaluate()`.
  # ponytail: escaneo lineal de read_all(), añadir un índice/columna
  # consultable por initiated_by si el volumen de sesiones lo justifica.
- **Ningún mecanismo de "reset" manual de cuota.** Si un operador necesita
  liberar a una identidad antes de que expire la ventana, no hay comando
  para eso hoy -- coherente con que el ledger es append-only (spec §9.2),
  pero es una limitación operativa real, no resuelta aquí.

## Referencias

- `docs/plan-v2.md`, sección "E15".
- `docs/spec.md` §6 (Policies), §9.1/§9.2 (ledger), §8.1 (saga step
  lifecycle, `step_committed` como prueba de ejecución real).
- `docs/adr/0010-e10-anomaly-baselines.md` (precedente arquitectónico:
  leer el ledger en vez de mantener estado paralelo).
- `docs/adr/0014-e14-identity-attribution.md` (de dónde viene
  `initiated_by`, del que depende esta entrega).
- Código: `belay/policy/quota.py`, `belay/policy/engine.py`
  (`_evaluate_quota`), `belay/policy/model.py` (`QuotaDefaults`).
- Tests: `tests/policy/test_quota.py`.
- Demo: `examples/demo_quota.py`.
