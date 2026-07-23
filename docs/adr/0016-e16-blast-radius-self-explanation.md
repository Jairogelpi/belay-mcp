# ADR 0016: E16 — Blast-radius self-explanation returned to the agent

Fecha: 2026-07-23
Estado: aceptado

## Contexto

`docs/plan-v2.md`, sección "E16 -- Blast-radius self-explanation returned to
the agent (not just the human)". Cada señal de gobernanza que Belay ya tiene
(caps de E4, baseline estadístico de E10, cuota por identidad de E15,
veredictos de política en general) se explica hoy a un **humano** -- en
`belay approvals list`, en `belay plan`, en `PolicyResult.reasons` que un
operador de CLI lee. El **agente** que hizo la llamada solo recibe un
`pending_approval`/`policy_denied` desnudo, sin ninguna razón legible
adjunta a la respuesta MCP que de verdad recibe. Eso significa que un agente
nunca puede autocorregirse ("ah, esto es 40x mi radio de explosión normal,
déjame acotar el filtro") sin que un humano medie primero -- toda
autocorrección hoy exige una ronda completa de pausa-y-espera.

## Decisiones

- **`belay/policy/explain.py`: `explain(policy_result, plan, contract=None)
  -> Explanation` es una función PURA DE FORMATEO.** No reevalúa política, no
  vuelve a leer el ledger, no re-deriva ningún número: solo clasifica y
  plantilla texto que `PolicyEngine.evaluate` (E4/E10/E15) ya calculó en
  `PolicyResult.reasons`, y campos que `Plan` (E4/E11) ya trae. `contract` es
  un parámetro opcional adicional al firmado en el plan (`explain(policy_result,
  plan)`) -- necesario porque la única regla determinística de
  `suggested_action` (ver abajo) depende de la forma del contrato
  (`conditions`/`sql.params`), que `Plan` en sí no conserva; omitirlo
  simplemente significa que no se ofrece ningún `suggested_action`, nunca uno
  adivinado.
- **Clasificación de dimensión por prefijo del `rule_id` que
  `PolicyEngine.evaluate` ya produce** (`tools[i]`, `quiet_hours[i]`,
  `caps[i]`, `anomaly:`, `quota:`, `defaults.irreversible`,
  `defaults.unknown_effects`) -- ningún dato nuevo, solo un `match` sobre un
  string que ya existe en `reasons`.
- **Política de divulgación: transparencia total, aplicada de forma
  uniforme.** La razón real detrás de esta elección, no la única
  técnicamente posible: se estudió explícitamente la tensión que pide el
  plan (transparencia total para que el agente se autocorrija vs. no
  entregarle a un agente malicioso el umbral exacto para "pedir
  umbral-1"), y spec §12 (TOCTOU/seguridad) no dice nada sobre no
  sobre-divulgar internals de política al agente gobernado -- solo habla de
  expiración de planes, re-chequeo de `conditions` en ejecución, binding del
  aprobador, y que "nada que un agente diga puede autorizar, relajar
  política o editar contratos" (que sigue siendo cierto: `explain()` es
  puramente de lectura, no expone ninguna superficie de escritura).
  Concretamente:
  - `anomaly`'s reason nunca lleva el umbral **configurado**
    (`z_score_threshold`), solo números **observados** de esta llamada (z,
    media, desviación, n) -- no hay nada configurado que redactar ahí para
    empezar.
  - `quota`'s reason SÍ lleva el `max_irreversible_actions` configurado
    (literal en el texto que `_evaluate_quota` ya construye).
  - `caps`/`tools`/`quiet_hours`/`defaults.irreversible` solo llevan un
    `rule_id` (`"caps[0]"`), nunca un número de umbral en absoluto.
  - Se eligió **no redactar** el número configurado que sí aparece en
    `quota`'s reason, por dos motivos: (1) ese mismo número ya es legible
    por cualquier humano vía `belay approvals list`/el ledger -- ocultarlo
    del agente mientras sigue siendo legible por un humano co-ubicado con
    el agente es teatro de seguridad, no una barrera real; (2) el objetivo
    de diseño explícito es autocorrección del agente, que necesita números
    exactos, no lenguaje vago tipo "muy por encima de lo normal" sobre el
    que no puede actuar mecánicamente. Se acepta el riesgo documentado de
    que un agente pueda sondear hasta `max_irreversible_actions - 1`: se
    juzga aceptable porque la cuota es un conteo rodante con verdad de
    ledger (E15), no una suposición del lado del cliente -- llegar hasta el
    límite es uso legítimo, no evasión, el mismo razonamiento que un `Cap`
    de gasto configurado por un humano.
  - **Invariante de consistencia real, no solo prosa**: para toda dimensión
    que dispara, `dimension.rule in dimension.detail` siempre se cumple
    (`test_disclosure_policy_is_applied_consistently_across_dimensions`).
    `detail` es o bien el `reason` verbatim (`anomaly`/`quota`, que ya son
    oraciones completas), o una oración con plantilla que incluye el
    `rule_id` sin tocarlo. Nunca hay una ruta de código que trunque o
    enmascare un número que ya estaba en `reasons`.
- **`suggested_action`: regla mecánica, nunca una adivinanza.** Se ofrece
  solo cuando el `contract` opcional declara un argumento de acotamiento de
  forma determinística: una `conditions` (contrato `conditional`, spec §4.2)
  o un `sql.params` (E11) cuya expresión referencia `$args.<path>`. Se
  camina el árbol de `Expr` ya parseado por
  `belay.contracts.expressions.parse` (parseo sintáctico de datos ya
  declarados en el contrato, no una segunda evaluación de política) y se
  toma el primer `$args.<path>` encontrado. Sin contrato, o con un contrato
  sin ningún `$args.<path>` declarado en ninguno de los dos lugares,
  `suggested_action` es `None` -- nunca un placeholder inventado. Precedente
  real reutilizado: `examples/contracts/crm.yaml`'s `crm.bulk_delete` ya
  declara `sql.params.cutoff: "$args.before_year"` (E11); `explain()` lo lee
  tal cual para sugerir `"narrow \`args.before_year\` and re-plan"`.
- **`belay/proxy/lifecycle.py`: la `Explanation` se adjunta a TODA respuesta
  gobernada, sin tocar la forma existente de ninguna.** `pending_approval`
  gana una clave nueva `"explanation"` en el mismo dict que ya se devolvía;
  `policy_denied`/`approval_rejected`/`approval_expired` ganan
  `detail["explanation"]` (aditivo sobre `BelayError.to_dict()`, que ya era
  `{"code", "detail", "retryable"}` -- `detail` es un dict libre, agregarle
  una clave no rompe ningún test que solo lea `code`/`retryable` o claves
  específicas de `detail` que ya existían). `allow` es el caso interesante:
  `govern_and_execute` sigue devolviendo exactamente el resultado crudo del
  ejecutor (nunca se toca su forma -- eso rompería contratos de tests
  existentes que aserten sobre el shape exacto del resultado ejecutado), así
  que la `Explanation` del camino `allow` viaja por un canal lateral
  (`Lifecycle.last_explanation`) que `belay/proxy/server.py` lee después de
  `await`ear la llamada, únicamente para fusionarla en
  `CallToolResult.structuredContent` -- la única capa donde el diseño pide
  explícitamente que viva (§ "belay/proxy/server.py" del plan).
- **`belay/proxy/server.py`: fusión aditiva de `structuredContent`.** Para el
  camino `allow` (el resultado ya es un `CallToolResult` real del upstream),
  se copia su `structuredContent` existente (`dict(result.structuredContent
  or {})`), se le agrega `"explanation"` solo si la clave no existía ya
  (`setdefault`), y se reconstruye el `CallToolResult` con
  `model_copy(update=...)` -- el resto de campos (`content`, `isError`,
  etc.) exactamente iguales al del upstream. Para `pending_approval`, la
  clave ya viene en el dict desde `Lifecycle`, así que
  `structuredContent=result` (ya existente, sin cambios en `server.py`) la
  incluye automáticamente.

## Por qué esto es decoración aditiva, no un nuevo camino de aplicación

`explain()` nunca decide nada: recibe un `PolicyResult` ya calculado por
`PolicyEngine.evaluate` (E4/E6/E10/E15's máquina de veredictos, sin tocar),
y un `Plan` ya calculado por `Planner.plan` (E4/E11, sin tocar). Un `pause`
sigue pausando exactamente igual que antes de E16; un `deny` sigue negando
exactamente igual. No existe ningún camino de código donde
`Explanation`/`suggested_action` influya en `verdict`,
`requires_approval`, o en si un item de aprobación se crea o resuelve --
`belay/approvals/queue.py`/`ApprovalStage` no importan `belay.policy.explain`
en absoluto. Si `explain()` se borrara del repo por completo, el
comportamiento de gobernanza (qué se ejecuta, qué se pausa, qué se niega)
sería exactamente el mismo; lo único que desaparecería es la explicación que
el agente puede leer sobre una decisión ya tomada. Esa separación es
deliberada y es la que permite que E16 se aterrice sin tocar
`belay/policy/engine.py`, `belay/approvals/queue.py`, ni
`belay/executor/saga.py`.

## Garantía de trazabilidad

Todo número que aparece en `Explanation.headline`/`Dimension.detail` viene
literalmente de `PolicyResult.reasons` (para `anomaly`/`quota`, el texto
completo es el `reason` verbatim) o de campos de `Plan` sin dígitos
inventados (`plan.tool`, un string). El test de propiedad (Hypothesis)
`test_property_explain_never_raises_and_never_invents_a_number` genera
`PolicyResult`s reales desde el `PolicyEngine.evaluate()` real (histórico de
ledger variable, cap opcional, reversibilidad variable, magnitud de outlier
variable) y confirma para cada uno: `explain()` nunca lanza, y todo dígito en
`headline`/cada `dimension.detail` ya aparece en `reasons` (o en el propio
`rule_id` de esa dimensión, que a su vez ya es un elemento de `reasons`) --
esta es la garantía central que hace que E16 no pueda, por construcción,
decirle al agente algo que la evaluación de política real no calculó.

## Brechas conocidas / seguimiento

- **`suggested_action` solo reconoce dos formas de "argumento de
  acotamiento"** (`conditions` de un contrato `conditional`, `sql.params` de
  E11) porque son las dos únicas formas hoy en que un contrato declara una
  relación mecánica entre un `$args.<path>` y el radio de explosión de la
  llamada. Un contrato que acota el conteo de otra manera (p. ej. un
  `native_dry_run` que interpreta `args.filter` sin que el contrato lo
  declare en ningún campo estructurado) no produce ningún
  `suggested_action` -- correcto por diseño (nunca adivinar), pero es una
  limitación real de cobertura, no escondida.
  # ponytail: dos reglas de detección, ampliar si aparece una tercera forma
  # estructural de "argumento de acotamiento" en contratos reales.
- **`Lifecycle.last_explanation` es estado mutable de instancia**, no parte
  del valor de retorno de `govern_and_execute` -- funciona porque
  `Lifecycle` ya es de una sola sesión, una llamada concurrente a la misma
  instancia ya estaría mal soportada por otras razones (`_step_seq` también
  es estado mutable secuencial); no es una regresión nueva de E16.

## Referencias

- `docs/plan-v2.md`, sección "E16".
- `docs/spec.md` §12 (Security considerations -- TOCTOU, no-autorización por
  el agente, binding del aprobador).
- `docs/adr/0010-e10-anomaly-baselines.md`, `docs/adr/0015-e15-identity-quota.md`
  (de dónde vienen los `reasons` que este ADR solo formatea).
- Código: `belay/policy/explain.py`, `belay/proxy/lifecycle.py`,
  `belay/proxy/server.py`.
- Tests: `tests/policy/test_explain.py`, `tests/proxy/test_server.py`.
- Demo: `examples/demo_self_explain.py`.
