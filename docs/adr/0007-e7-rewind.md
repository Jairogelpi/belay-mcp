# ADR 0007: E7 — Rewind (spec §10) — cierra L3

Fecha: 2026-07-22
Estado: aceptado

## Contexto

E7 implementa `docs/spec.md` §10 (Rewind), según `docs/plan.md` sección "E7 —
Rewind (spec §10) — cierra L3". Construye `belay/rewind/service.py`
(`RewindService`) sobre la evidencia del ledger que E6 produce (journal,
captura, resultado, compensación materializada), unifica el auto-unwind
mínimo de `SagaExecutor.compensate`/`run_saga` (deliberadamente estrecho en
E6) con el rewind real, y añade `belay rewind` a la CLI.

## Decisiones

- **Fencing es un hecho del ledger, no estado en memoria.**
  `RewindService.fence()` apenda un evento `session_fenced` (sin `step_seq`);
  `is_fenced(ledger, session_id)` lo busca por tipo. `Lifecycle.govern_and_execute`
  llama a `is_fenced()` como primera línea de cada intento de paso, antes de
  incrementar `step_seq` o resolver el contrato, y lanza `session_fenced` si
  la sesión ya está cercada. Se eligió un evento de ledger en vez de un flag
  en el objeto `Lifecycle` en memoria porque `belay run` (el proceso que vive
  la sesión) y `belay rewind` (el proceso que la cierra) son procesos
  separados que solo comparten el fichero SQLite — cualquier mecanismo en
  memoria sería invisible entre procesos, que es exactamente el escenario
  real de la demo (plan.md §10). El costo es una lectura completa del ledger
  de la sesión en cada llamada gobernada; aceptable para v0.1, documentado
  como el único punto de fencing.
  Test: `tests/rewind/test_service.py::test_new_step_after_fence_raises_session_fenced`
  y `test_fencing_race_fence_wins_over_a_step_racing_to_start` (una carrera
  explícita entre un paso que arranca y un fence concurrente, vía
  `anyio.create_task_group`), más `tests/cli/test_rewind.py::
  test_rewind_fencing_blocks_the_governed_session_from_new_steps` contra un
  proceso `belay run` real.
- **La honestidad (§10.3) vive en una sola propiedad, no en lógica dispersa.**
  `RewindReport.fully_rewound` es la única fuente de verdad: falso si
  `dry_run`, falso si *cualquier* paso en alcance no es `"reversible"`
  (irreversible, conditional-unmet, o indeterminate), y falso si algún paso
  reversible no terminó con outcome `"compensated"` (lo que excluye
  `verification_failed`, `compensation_failed`, `paused`, `denied` y
  `skipped` de contar como éxito). No hay ningún código en `rewind()` que
  pueda reportar `fully_rewound=True` mientras alguna de esas condiciones se
  sostenga — la propiedad se computa siempre desde `plan.steps` +
  `outcomes`, nunca desde un contador que alguien podría olvidar decrementar.
  Test: `test_honesty_mixed_reversible_and_irreversible_never_reports_fully_rewound`
  (mezcla reversible + irreversible, ambos reversibles compensados con
  éxito, honestidad exige `False` de todos modos) y
  `test_verification_passing_counts_as_compensated` /
  `test_verification_is_executed_and_a_failure_does_not_count_as_compensated`
  (la contabilidad, no solo el código de error).
- **Verificación falla `step_failed` con `error.code = "verification_failed"`,
  pero no relanza.** Una verificación fallida se registra como evento y como
  outcome `"verification_failed"` — el undo *sí ocurrió* (¡se llamó al
  tool!), pero el paso no cuenta como compensado. Se decidió no lanzar una
  excepción Python porque el resto del bucle de rewind sigue siendo
  data-driven (`halt_on_failure`/`skip_and_continue` deciden si continuar),
  igual que `compensation_failed`/`denied`/`paused` — un único mecanismo de
  control de flujo para las cuatro formas de "este paso no se resolvió
  limpio", no una mezcla de excepciones y valores de retorno.
- **`compensate_one` es el único lugar que apenda
  `compensation_executed`/`compensation_failed`.** Antes de E7,
  `SagaExecutor.compensate` (usado por `run_saga`'s auto-unwind, spec §8.2) y
  el rewind real habrían duplicado esa lógica. `belay/rewind/service.py::
  compensate_one(ledger, session_id, step_seq, comp, executor)` es ahora esa
  única función; `SagaExecutor.compensate` es una fachada de una línea que
  delega en ella (import local para evitar un ciclo top-level entre
  `belay.executor.saga` y `belay.rewind.service`). El auto-unwind de E6 sigue
  sin fencing, sin reporte honesto ni `--skip-and-continue` — eso es
  exclusivo de `RewindService.rewind()`, tal como E6 lo dejó documentado como
  deliberadamente mínimo.
- **Las compensaciones pasan por el mismo `PolicyEngine` (§12), con
  `plan_id` determinista para que una aprobación sobreviva a un segundo
  intento.** `RewindService._compensation_plan` construye un `Plan` sintético
  (mismos campos que el forward path) con `plan_id = f"rewind_{sha256(session_id,
  step_seq, tool, args)[:16]}"` — determinista, no aleatorio — precisamente
  porque una `pause` puede requerir una segunda invocación de `belay rewind`
  tras `belay approvals approve`; con un `plan_id` aleatorio la segunda
  invocación jamás encontraría el item ya aprobado (`ApprovalQueue.for_plan`
  no lo hallaría) y quedaría parada para siempre. Un `deny` detiene igual
  que en el forward path (spec §6.2: `deny > pause > allow`).
  Test: `test_compensation_over_a_cap_pauses_like_a_forward_action` — pausa,
  aprueba vía `ApprovalQueue.approve` directamente (como haría `belay
  approvals approve`), reintenta `rewind()` y verifica que ahora procede y
  reporta `fully_rewound=True`.
- **`halt_on_failure` es el default; `skip_and_continue` se registra siempre
  que se use, incluso si termina sin encontrar ningún fallo.** Se apenda un
  evento `config_override` con `reason: "skip_and_continue"` inmediatamente
  después de `rewind_requested`, antes de tocar ningún paso — es una
  decisión de operador, visible en el ledger igual que cualquier otra
  relajación de default (§6.4 ya sentó ese patrón en E4).
- **`_as_dict` (en `saga.py` y ahora también en `rewind/service.py`) desenvuelve
  `CallToolResult.structuredContent` antes de tratar el valor como
  `$result`/`$state`.** Bug real encontrado al correr la demo completa
  (plan.md §10) contra el proxy real: el executor que `BelayProxyServer` le
  pasa a `SagaExecutor` es la respuesta MCP cruda del upstream
  (`ClientSession.call_tool`), cuyo payload de negocio vive anidado en
  `.structuredContent` (con un posible nivel extra `{"result": ...}` según
  cómo FastMCP serialice el tipo de retorno) y no en el objeto de nivel
  superior. Antes de este fix, `$state.before.records` se materializaba como
  `None` para cualquier `capture` corriendo tras un `CallToolResult` real, y
  la compensación fallaba con un error de validación del lado del upstream.
  Se corrigió desenvolviendo `structuredContent` (con el mismo fallback
  defensivo `.get("result", content)` que `tests/executor/
  test_crm_mock_acceptance.py` ya usaba en su propio arnés de test) en el
  único lugar compartido (`_as_dict`), no en cada llamador.
  # ponytail: solo entiende `expect: "not_found"` y subconjunto de dict para
  `verification.expect`; ampliar cuando un contrato real necesite otra forma.
- **`crm.bulk_delete` + su contrato se añadieron a `examples/crm-mock` /
  `examples/contracts/crm.yaml`** porque el guion de demo exacto de plan.md
  §10 lo nombra (`crm.bulk_delete`, cap de blast-radius, aprobación,
  "--oops", rewind) y no existía tras E6. `capture` es
  `crm.export_records()` (todo el snapshot, no solo lo que se va a borrar);
  `undo` es `crm.import_records($state.before.records)` — restaurar el
  snapshot completo es una compensación correcta y más simple que intentar
  reconstruir una expresión de filtrado que el lenguaje de §4.3
  deliberadamente no soporta (sin funciones arbitrarias).

## Brechas conocidas / seguimiento

- `belay approvals approve --narrow <filter>` (la re-narrow explícita del
  guion de plan.md §10) no existe; el flujo equivalente probado aquí es que
  el agente reintente con un `args` distinto (nuevo `plan_id` por
  construcción, spec §12) y el operador apruebe *ese* item. Añadir el flag
  es una mejora de UX de E9, no una brecha de conformidad L3.
- `examples/demo.py` (el guion reproducible con "--oops", mencionado en
  plan.md E9) no se creó en esta entrega; la mecánica completa del guion de
  §10 se probó end-to-end vía `tests/cli/test_rewind.py` contra procesos
  `belay run`/`belay rewind` reales, pero no vía ese script concreto.
- El fencing añade una lectura completa del ledger de la sesión en cada
  `govern_and_execute`; aceptable al volumen de v0.1, con nota para
  optimizar (p.ej. una tabla `sessions.fenced_at`) si el ledger de una sesión
  crece mucho.

## Referencias

- `docs/spec.md` §10 (Rewind), §10.1-§10.3, §12 (compensation blast radius),
  §11 (`session_fenced`, `verification_failed`).
- `docs/plan.md` sección "E7 — Rewind (spec §10) — cierra L3".
- Código: `belay/rewind/service.py`, `belay/proxy/lifecycle.py` (chequeo de
  fencing), `belay/executor/saga.py` (`compensate` delegado), `belay/cli/main.py`
  (`rewind` command), `examples/crm-mock/server.py` (`crm.bulk_delete`),
  `examples/contracts/crm.yaml`.
- Tests: `tests/rewind/test_service.py` (10 casos: orden, dry-run, fencing +
  carrera, halt/skip, verificación x2, honestidad, cap+aprobación),
  `tests/cli/test_rewind.py` (guion completo de plan.md §10 + fencing contra
  procesos reales, marcados `@pytest.mark.slow`).
