# ADR 0004: E4 — Planner y motor de políticas

Fecha: 2026-07-22
Estado: aceptado

## Contexto

E4 implementa `docs/spec.md` §5 (Effect plans) y §6 (Policies), según
`docs/plan.md` sección "E4 — Planner y motor de políticas". Reemplaza los
stubs `PlanStage`/`PolicyStage` de E3 (`belay/proxy/lifecycle.py`) por
`belay/planner/{model,planner}.py` y `belay/policy/{model,engine}.py` reales.
`ApprovalStage` sigue siendo el stub de E3 (E5 lo implementa).

## Decisiones

- **Precedencia de bases de plan (§5.3): `native_dry_run` primero, `contract`
  como fallback; `dry_run` no tiene adaptador en v0.1.** `Planner.plan()`
  primero construye un plan `basis="contract"` (o efectos implícitos de la
  regla por defecto §4.6 si no hay contrato), y solo lo sobreescribe si
  `PlanningSession.native_dry_run` está presente y devuelve un resultado
  no-`None` (el tool expone el sibling `<tool>.dry_run`). El adaptador
  intermedio `dry_run` (simulador tipo `EXPLAIN`/`SELECT COUNT(*)` para SQL)
  queda deliberadamente sin implementar — `docs/plan.md` §4 (E4-b) lo marca
  como "issue futuro documentado" y §11 lo lista para después de v0.1. El
  literal `"dry_run"` se mantiene en el tipo `Basis` por compatibilidad hacia
  adelante, pero ningún código de este entrega lo produce; ver el comentario
  `# ponytail:` en `belay/planner/planner.py`.
- **El planner no reimplementa la regla por defecto de §4.6.** En vez de que
  `Planner.plan()` vuelva a resolver el contrato desde un `ContractSet`,
  recibe el resultado ya calculado por `resolve()` (E3) a través de
  `PlanningSession.contract` / `PlanningSession.implicit_effects`. Una sola
  función implementa §4.6; el planner solo lo consume. Efectos implícitos
  (lectura por `readOnlyHint`, o ningún efecto declarado bajo
  `unsafe_passthrough`) se marcan `estimate: false` — no hay incertidumbre en
  "esta llamada fue de lectura".
- **Honestidad de incertidumbre (§5.3/§6.3): un efecto de contrato sin
  `count` va a `unknown[]`, nunca a un número inventado.** Toda cuenta de
  base `contract` se marca `estimate: true` (la spec prohíbe presentarla como
  exacta). `confidence` se deriva mecánicamente: `"low"` si `unknown` no está
  vacío, `"high"` si la base es `native_dry_run` sin unknowns, `"medium"` en
  el resto (base `contract` con cuentas declaradas).
- **`PolicyEngine.evaluate()` combina dimensiones independientes por
  severidad máxima (`deny > pause > allow`), no una única regla ganadora.**
  Interpretación de "rules are evaluated in order; first match per dimension
  wins" (§6.2): dentro de las listas `tools` y `quiet_hours`, la primera
  regla que hace match decide (comportamiento de firewall clásico); dentro de
  `caps`, cada cap es una restricción independiente y **todas** las que se
  disparan contribuyen (dos caps distintos pueden aplicar a efectos
  distintos del mismo plan). El veredicto final es el máximo de severidad
  entre todo lo disparado; `reasons` lleva **todos** los ids que se
  dispararon, no solo el que decidió el máximo — así un `deny` por un cap no
  oculta que también había un `pause` por defaults, información relevante
  para el operador. Tests:
  `tests/policy/test_engine.py::test_most_restrictive_verdict_wins_across_dimensions`,
  `test_first_match_wins_within_tools_dimension`.
- **La relajación por tool del default irreversible (§6.4) reutiliza el
  mecanismo `tools` de §6.1, no un campo nuevo.** Una regla en `policy.tools`
  que hace match sobre un tool con `reversibility: irreversible` **reemplaza**
  (no combina por máximo) el veredicto de `defaults.irreversible` para ese
  tool — es el canal explícito de "operators may relax per tool". Si el
  veredicto de la regla es menos severo que el default, `PolicyEngine` lo
  marca en `PolicyResult.relaxations` (lista de rule ids), y
  `Lifecycle.govern_and_execute()` emite un evento `config_override` dedicado
  cuando `relaxations` no está vacío — la relajación queda visible tanto en
  la config corriente (la propia policy ya la declara) como en el ledger
  (spec: "relaxations are configuration, visible in the ledger"). Tests:
  `tests/policy/test_engine.py::test_tool_rule_relaxes_irreversible_default_and_is_recorded`,
  `tests/proxy/test_lifecycle.py::test_irreversible_relaxation_is_recorded_as_config_override`.
- **Reloj inyectable (`belay/clock.py`) en vez de `datetime.now()` disperso.**
  `Clock` es un `Protocol` con un único método `now()`; `SystemClock` en
  producción, `FixedClock` en tests (mutable via `.set()`, para simular el
  paso del tiempo sin `sleep`). Tanto `Planner` (expiración de plan, §5.4)
  como `PolicyEngine` (quiet hours, §6.1) lo reciben por constructor. Ningún
  módulo de `belay/planner` o `belay/policy` importa `datetime.now`
  directamente — es la única forma en que
  `tests/planner/test_planner.py::test_plan_expiration_rejects_execution_after_ttl`
  y `tests/policy/test_engine.py::test_quiet_hours_pauses_matching_effect_in_window`
  son deterministas.
- **Expiración y mismatch de plan (§5.4) como función libre, no método de
  `Plan`.** `check_plan_binding(plan, tool, args, clock=...)` en
  `belay/planner/planner.py`: primero compara `args` contra `plan.args` vía
  serialización canónica (`belay.canonical.canonical_bytes`, la misma base
  que `set_hash`/hash de ledger) — "byte-identical" se interpreta
  literalmente como igualdad de bytes canónicos, no solo `==` de dicts de
  Python — y solo si coincide comprueba expiración. El orden importa: un
  mismatch nunca es reintentable con los mismos args, así que se reporta
  primero aunque el plan también haya expirado. E6 (saga executor) será el
  llamador real de esta función al `bind`ear ejecución a un `plan_id`; E4 la
  deja lista y probada de forma aislada porque el ejecutor de sagas aún no
  existe.
- **`per: session` en `Cap` es solo informativo en v0.1.** `PolicyEngine.evaluate()`
  es una función pura de `(plan, policy)`: no tiene acceso a un acumulador
  entre llamadas de la misma sesión. Cada cap se evalúa contra los efectos
  de un único plan (semántica `per: call` de facto), documentado con un
  comentario `# ponytail:` en `belay/policy/model.py::Cap`. Añadir un
  acumulador real requiere leer el ledger de la sesión (o mantener estado en
  `Lifecycle`) — se deja para cuando un caso de uso concreto (ej. el tope de
  gasto `spend`/`session` del ejemplo de la spec) lo necesite de verdad.
- **`deny` bloquea en E4; `pause` todavía no (ese es trabajo de E5).**
  `Lifecycle.govern_and_execute()` lanza `BelayError("policy_denied")`
  inmediatamente si `PolicyEngine` decide `deny`, incluyendo el evento
  `step_failed` correspondiente. `pause` se registra en `policy_evaluated`
  (`requires_approval: true`) pero `ApprovalStage.maybe_park()` sigue siendo
  el no-op de E3 — parquear de verdad en la cola de aprobaciones es el
  alcance de E5, que reemplazará `ApprovalStage` sin tocar `Lifecycle` ni
  `PlanStage`/`PolicyStage`.
- **`belay plan <tool> --args '<json>'` no necesita un upstream conectado.**
  El comando CLI construye un `Planner`/`PolicyEngine` de un solo uso,
  resuelve el contrato desde el `ContractSet` de `belay.wrap.json`, y no pasa
  `native_dry_run` (no hay conexión MCP viva desde el CLI) — imprime siempre
  un plan de base `contract` (o efectos implícitos si el tool no tiene
  contrato). Política opcional vía `--policy <path>`; sin ella usa
  `default_policy()` (§6.4 tal cual). Carga de política
  (`belay.policy.model.load_policy`) no envuelve errores de validación en
  `BelayError`: es una herramienta de operador, no una llamada que cruza el
  borde del proxy hacia el agente, así que los 17 códigos de §11 no aplican
  aquí.

## Referencias

- `docs/spec.md` §5 (Effect plans), §6 (Policies).
- `docs/plan.md` sección "E4 — Planner y motor de políticas".
- Código: `belay/clock.py`, `belay/planner/{model,planner}.py`,
  `belay/policy/{model,engine}.py`, `belay/proxy/lifecycle.py`,
  `belay/cli/main.py` (`plan`).
- Tests: `tests/planner/test_planner.py`, `tests/policy/test_engine.py`,
  `tests/proxy/test_lifecycle.py` (casos añadidos en E4),
  `tests/cli/test_plan.py`.
