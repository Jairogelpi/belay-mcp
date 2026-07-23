# ADR 0010: E10 -- Baselines estadísticos de anomalía (sin thresholds manuales)

Fecha: 2026-07-22
Estado: aceptado

## Contexto

`docs/plan-v2.md` sección "E10 -- Statistical anomaly baselines (no manual
thresholds)": hoy `PolicyEngine` solo pausa/deniega cuando un humano ya
configuró un `Cap` (ej. "max 100 filas"). Un agente que hace 50x su propio
comportamiento normal, sin cap configurado para ese tool, pasa como `allow`.
El objetivo es que el motor lo detecte solo, sin configuración manual, a
partir del propio historial del ledger (spec §9) -- determinista, sin LLM,
sin llamada de red, sin modelo de ML opaco.

## Decisiones

- **`belay/policy/baseline.py`: `Welford` + `BaselineStore`, no una
  dependencia nueva.** `Welford` es un acumulador streaming de media/varianza
  (algoritmo de Welford, una pasada, memoria O(1)) -- no requiere `numpy` ni
  guardar el historial completo en memoria. `BaselineStore.stats(session_id,
  tool, effect_type, exclude_plan_id=...)` lee los eventos `plan_created` de
  **una sola sesión** vía `LedgerStore.read(session_id)` (nunca
  `read_all()`) y alimenta un `Welford` por cada efecto que matchea
  `(tool, effect_type)`. Esto es deliberado: el baseline es historial de
  sesión, no estado global en memoria -- dos sesiones nunca se contaminan
  entre sí (`test_baseline_store_never_crosses_sessions`,
  `test_baseline_is_per_session_no_cross_contamination`).
- **`exclude_plan_id` existe porque `Lifecycle.govern_and_execute()` ya
  escribió `plan_created` del plan actual antes de llamar a
  `PolicyEngine.evaluate()`** (spec §3: plan -> policy, en ese orden, y cada
  etapa se registra en el ledger). Sin excluirlo, un plan se auto-contaminaría
  como su propia muestra de baseline. `BaselineStore.stats()` filtra por
  `payload["plan_id"] != exclude_plan_id`.
- **Nueva dimensión `anomaly` en `PolicyEngine.evaluate()`, combinada por la
  misma regla de severidad máxima (`deny > pause > allow`) que ya usan
  `tools`/`quiet_hours`/el default irreversible.** No es un cap más: se
  evalúa efecto por efecto del plan actual, comparando
  `effect.upper_bound()` contra el baseline de `(tool, effect.type)` en la
  sesión. Con `stddev > 0`: `z = (valor - media) / stddev`, dispara si
  `z >= z_score_threshold`. Con `stddev == 0` (todo el historial es un único
  valor constante): cualquier valor por encima de esa constante dispara --
  guard explícito contra división por cero, no un caso "nunca dispara".
- **`Defaults.anomaly` (`belay/policy/model.py::AnomalyDefaults`) trae
  valores que funcionan con cero configuración manual**: `enabled=True`,
  `min_samples=10`, `z_score_threshold=3.0`, `verdict="pause"`,
  `exclude=[]` (globs de tool para desactivar por tool, el equivalente de
  E4 para relajar el default irreversible por tool -- mismo patrón, campo
  nuevo en vez de reutilizar `tools: list[ToolRule]` porque
  irreversible/tools comparten el mismo espacio de veredictos por rule id y
  `anomaly` es una dimensión distinta con su propia config). Es exactamente
  lo que hace posible el test de aceptación
  `test_zero_policy_config_still_catches_the_outlier`: `default_policy()` sin
  tocar nada detecta el outlier de 50x.
- **Cold start: por debajo de `min_samples`, `anomaly` nunca contribuye
  nada** (ni `allow` explícito en `fired`, simplemente no se agrega). Nunca
  bloquea por falta de datos, sin importar la magnitud del efecto
  (`test_cold_start_never_flags_anomaly_below_min_samples`).
- **Composición cap-vs-anomaly: si un `Cap` ya se disparó sobre el mismo
  efecto (mismo `(type, resource)`), `anomaly` no vuelve a disparar sobre
  ese efecto.** `PolicyEngine.evaluate()` acumula un set `covered` con los
  `(type, resource)` de los efectos que matchean algún cap que sí se
  disparó, y `_evaluate_anomaly()` salta esos efectos. Si el cap se disparó
  sobre *otro* efecto del mismo plan, o si no hay ningún cap configurado
  para ese tool en absoluto, `anomaly` se evalúa igual y puede disparar por
  su cuenta -- ese es el caso ganador que E10 existe para resolver
  (`test_anomaly_composes_with_cap_without_double_firing_same_effect`,
  `test_anomaly_fires_independently_when_no_cap_covers_the_effect`). No hay
  doble conteo de severidad: ambos casos, si disparan, contribuyen como
  máximo un `pause`/`deny` cada uno a la lista `fired`, y el veredicto final
  sigue siendo el máximo de severidad de spec §6.2.
- **Explicabilidad: la razón de `anomaly` en `PolicyResult.reasons` incluye
  el valor observado, la media del baseline, el ratio y el z-score en texto
  legible por humano**, ej. `"anomaly: crm.bulk_delete delete count 500 is
  45.5x the trailing baseline of 11.0 (z=598.90, n=12, stddev=0.82)"` --
  cumple literalmente el ejemplo de plan-v2.md ("delete count 512 is 47.3x
  the trailing baseline of 10.8"). No hay una razón separada "por qué
  pausó" en el CLI: el string de `reasons` ya lleva todos los números, y
  fluye tal cual hacia el evento `policy_evaluated` del ledger (spec §9) y
  hacia `Plan.policy_reasons` que imprime `belay plan`/`belay run` -- no se
  necesitó un campo o subcomando nuevo para "mostrar el contexto del
  baseline al aprobador humano".
- **`PolicyEngine.ledger: LedgerStore | None = None` es opcional, no un
  parámetro nuevo de `evaluate()`.** Mantiene la firma
  `evaluate(plan, policy) -> PolicyResult` sin cambios (spec §6.2), así que
  `belay/cli/main.py`'s standalone `belay plan` (sin ledger real) y
  `belay/rewind/service.py`'s `PolicyEngine` de compensaciones (que evalúa
  planes de compensación, no llamadas nuevas del agente) siguen funcionando
  exactamente igual: sin `ledger`, la dimensión `anomaly` simplemente no
  contribuye nada (mismo comportamiento que antes de E10). Solo
  `belay/proxy/lifecycle.py::Lifecycle.__post_init__` pasa
  `ledger=self.ledger` al construir el `PolicyEngine` real de la sesión --
  es el único call site que gobierna llamadas reales del agente en vivo.

## Referencias

- `docs/plan-v2.md` sección "E10 -- Statistical anomaly baselines (no manual
  thresholds)".
- `docs/spec.md` §6 (Policies), §9.1/§9.2 (ledger).
- Código: `belay/policy/baseline.py`, `belay/policy/engine.py`,
  `belay/policy/model.py` (`AnomalyDefaults`), `belay/proxy/lifecycle.py`.
- Tests: `tests/policy/test_baseline.py`, `tests/policy/test_anomaly.py`.
- Demo: `examples/demo_anomaly.py` -- 12 llamadas normales + 1 outlier de
  50x, cero `Cap` configurado, `belay-conformance run --target belay
  --level 3` sigue en PASSED tras el cambio.
