# ADR 0012: E12 -- Counterfactual replay ("what if I had decided differently")

Fecha: 2026-07-23
Estado: aceptado

## Contexto

`docs/plan-v2.md` sección "E12 -- Counterfactual replay": `belay rewind`
responde "deshaz lo que realmente pasó"; nada respondía "¿qué habría pasado
si un humano hubiera aprobado/denegado/acotado distinto en el momento de la
decisión?" -- sin tocar producción, sin re-llamar al upstream real, sin
necesidad de haber corrido la ruta alternativa de verdad. Belay ya tiene la
base determinista ledger+replay (spec §9.4, E2) que ninguna herramienta
competidora tiene; E12 construye la feature diferenciadora encima de eso.

## Decisiones

- **La regla de honestidad (el núcleo de este entrega) vive en tres, y solo
  tres, resultados por paso: `unchanged` / `diverged` / `unknown`.** Análoga
  a `RewindReport.fully_rewound` (ADR 0007, spec §10.3): nunca hay un camino
  de código que reporte un resultado concreto que no fue observado ni
  estimado de forma segura.
  - **`unchanged`**: el paso es idéntico a la sesión real -- porque ocurre
    *antes* del punto de bifurcación (`step_seq < at_step_seq`, nada del
    override pudo haberlo afectado todavía), o porque el `override` es un
    no-op exacto (mismo `verdict`, mismos `args` que lo que realmente
    ocurrió en el punto de bifurcación). En ambos casos se reusa el
    resultado *real* grabado en el ledger (`result_recorded`), nunca se
    vuelve a "correr" nada.
  - **`diverged`**: el paso está en o después del punto de bifurcación *y*
    el override realmente cambia el veredicto/args en ese punto, *y* existe
    una estimación segura y de solo-lectura de lo que habría pasado: el
    propio `plan_created` real de ese paso ya trae una base
    `native_dry_run`/`sql_simulator`/`dry_run` (E4/E11 -- una observación ya
    hecha una vez, nunca una llamada nueva), o el llamador pasó
    `upstream_replay` (un hook opcional que puede usar un adaptador
    read-only real como `native_dry_run`/`sql_simulator` para una estimación
    mejor), o -- en el peor caso seguro -- una estimación declarada por
    contrato sin necesidad de ninguna llamada, marcada `"simulated"` (nunca
    presentada como una observación real). El paso lleva la `basis` exacta
    consigo.
  - **`unknown`**: el paso diverge y no hay ninguna forma segura de saber
    qué habría pasado -- típicamente porque nunca se grabó un `plan_created`
    para ese `step_seq` (sin identidad de tool ni estimación de efectos que
    razonar) o porque los efectos reales tienen blast radius desconocido sin
    ningún basis seguro disponible (`unknown_effects`, spec §6.3). Nunca se
    adivina un resultado aquí -- ver `examples/demo_counterfactual.py`, cuyo
    `crm.bulk_delete` real reporta honestamente `unknown` para el propio
    paso bifurcado y todo lo posterior, porque el blast radius de ese
    contrato es desconocido y no hay `sql`/`native_dry_run` adaptador
    conectado en la demo.
  Test: `tests/ledger/test_counterfactual.py::
  test_deny_where_real_was_pause_marks_downstream_diverged_never_fabricated`,
  `test_honesty_step_with_no_recorded_plan_is_unknown_never_guessed`,
  `test_upstream_replay_hook_yields_better_than_simulated_basis`.
- **El no-op (mismo veredicto que la realidad) es el ancla de regresión, no
  un caso más.** `run_counterfactual` con un `override` idéntico al
  veredicto/args reales en el punto de bifurcación reporta el 100% de los
  pasos `unchanged` y `counterfactual_final_state` es exactamente
  `replay(events)` -- no una reconstrucción parecida, el mismo objeto
  `SessionState`, byte a byte. Un test de propiedad Hypothesis
  (`test_property_any_noop_override_is_always_unchanged_and_matches_replay`)
  generaliza esto sobre distintos veredictos reales y distintos puntos de
  bifurcación válidos: para *cualquier* historial real y *cualquier*
  no-op, la garantía se sostiene siempre. Este es el test más fuerte del
  entrega -- si algún cambio futuro rompe la regla de honestidad de forma
  sutil, este test lo detecta sin necesidad de enumerar casos concretos.
- **Reusa `belay.ledger.replay.replay()`, no reimplementa el fold.**
  `real_final_state = replay(events)` es literalmente la función de E2/§9.4
  aplicada a los eventos reales, sin ninguna copia paralela. Para el estado
  final del branch cuando el override realmente diverge, tampoco se
  fabrica un `SessionState` a mano: se construye un evento `policy_evaluated`
  sintético (mismo tipo de evento que ya entiende `replay()`) con el
  `verdict` del override, se concatena al prefijo real *antes* del punto de
  bifurcación, y se vuelve a llamar a `replay()` sobre esa lista -- un solo
  fold, una sola implementación, ahora corriendo sobre una lista de eventos
  ligeramente distinta en vez de una función de fold nueva. El resultado
  omite honestamente todo lo posterior al fork (nunca reclama saber el
  estado final "real" del branch más allá de ese punto).
- **Reusa `Basis` (E4/E11), extendiéndolo con un único literal nuevo
  (`"simulated"`), no un enum paralelo.** `CounterfactualBasis = Basis |
  Literal["simulated"]` en `belay/ledger/counterfactual.py` -- mismo patrón
  que E11 extendió `Basis` con `"sql_simulator"` (ADR 0011): un tipo
  existente, un caso adicional documentado donde ese tipo no alcanza (una
  llamada que *nunca ocurrió* no puede tener una base `contract` real, que
  implica que sí hubo una llamada gobernada -- necesita su propio marcador
  honesto de "esto es una conjetura de una llamada hipotética").
- **Re-invoca `PolicyEngine`/el motor de contratos indirectamente, vía los
  eventos ya grabados -- no reimplementa la lógica de política.** El
  veredicto real en el punto de bifurcación se lee del propio evento
  `policy_evaluated` (`PolicyEngine.evaluate()` ya lo decidió una vez, en
  el momento real); el `override` sustituye ese valor para la comparación
  de honestidad, no vuelve a ejecutar `PolicyEngine.evaluate()` con un
  `Plan` sintético. Esto es deliberado y más simple que reconstruir un
  `Plan`/`PolicyDoc` completo solo para volver a evaluarlo: el punto de la
  feature es "¿qué habría cambiado si el veredicto hubiera sido otro?", no
  "¿qué habría decidido el motor con una política distinta?" (eso ya lo
  cubre `belay plan --policy <otro>`, spec §6). Si un caso de uso futuro
  necesita bifurcar sobre una política *distinta* en vez de un veredicto
  distinto, ese es un modo nuevo de `run_counterfactual`, no una
  reimplementación de `PolicyEngine` aquí.
- **La garantía de inmutabilidad está impuesta arquitectónicamente, no solo
  probada.** `run_counterfactual(events: list[Event], ...)` recibe una
  lista ya leída (vía `LedgerStore.read()`, responsabilidad del llamador) --
  la función nunca abre ni recibe un `LedgerStore`. `CounterfactualBranch`
  (el dataclass congelado que representa el fork) solo tiene un campo
  `events: tuple[Event, ...]`; no existe ningún campo por el que un bug
  pudiera alcanzar `LedgerStore.append` de la sesión real, porque el objeto
  simplemente no tiene ninguna referencia a un `LedgerStore`. No es
  disciplina de "no llamar a `.append()`" -- es que no hay forma de
  llamarlo desde dentro de esta función, ni por accidente. Mismo principio
  para el upstream: no hay ningún parámetro de tipo "cliente MCP" en toda la
  firma de `run_counterfactual`; el único hook de extensión
  (`upstream_replay: Callable[[str, dict], tuple[Basis, dict] | None]`) es
  una función síncrona y pura desde el punto de vista de este módulo -- si
  un llamador la implementa con una llamada de red real, eso es una
  decisión suya fuera de este módulo, exactamente el mismo límite de
  confianza que `PlanningSession.native_dry_run`/`.sql_runner` ya tienen en
  el camino forward (E4/E11).
  Test (la mitad de prueba, complementaria a la arquitectónica de arriba):
  `test_immutability_row_count_unchanged_and_no_upstream_calls` (conteo de
  filas antes/después de una llamada real a SQLite, idéntico) y
  `tests/cli/test_counterfactual.py::
  test_counterfactual_against_a_real_belay_run_session_leaves_ledger_untouched`
  (fixture real de un proceso `belay run` vía stdio, conteo de filas
  idéntico, `belay verify` sigue OK después).
- **Punto de bifurcación inválido: error claro, nunca un reporte vacío
  silencioso.** `at_step_seq` debe corresponder a un evento
  `policy_evaluated` real de esa sesión; si no, `InvalidForkPoint` (subclase
  de `ValueError`) se lanza de inmediato. `belay counterfactual` la atrapa
  y sale con código 1 y un mensaje de error, igual que `belay approvals
  approve` ante un `BelayError`.
  Test: `test_invalid_fork_point_raises_clear_error_not_silent_empty_report`.
- **CLI: `belay counterfactual <session_id> --at-step <n> --override
  '<json>' [--json] [--db <path>]`, solo lectura.** Nunca requiere un
  `belay run` vivo (lee el ledger una sola vez, como `belay verify`), nunca
  abre una conexión al upstream. `--db` sigue el mismo patrón que
  `approvals list/approve/reject` en vez de `--config` (no necesita el
  `WrapConfig` del upstream para nada, solo el path del ledger).

## Referencias

- `docs/spec.md` §9.4 (replay), §5.3 (bases de plan), §6 (políticas), §10.3
  (precedente de honestidad de `fully_rewound`).
- `docs/plan-v2.md` "E12 -- Counterfactual replay".
- `docs/adr/0007-e7-rewind.md` (el precedente de honestidad que este entrega
  iguala en espíritu), `docs/adr/0004-e4-planner-policy.md` y
  `docs/adr/0011-e11-sql-dry-run.md` (el precedente de `Basis`).
- Código: `belay/ledger/counterfactual.py` (`CounterfactualBranch`,
  `run_counterfactual`, `CounterfactualReport`), `belay/cli/main.py`
  (`counterfactual`).
- Tests: `tests/ledger/test_counterfactual.py` (7 casos + la propiedad
  Hypothesis), `tests/cli/test_counterfactual.py` (fixture real, marcado
  `@pytest.mark.slow`).
- Demo: `examples/demo_counterfactual.py` (bulk-delete real + rewind
  seguido de "¿y si hubiera denegado?" vía `belay counterfactual`, corrido
  de punta a punta contra un `belay run` real).
