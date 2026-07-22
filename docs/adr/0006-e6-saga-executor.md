# ADR 0006: E6 — Ejecutor de sagas (spec §8)

Fecha: 2026-07-22
Estado: aceptado

## Contexto

E6 implementa `docs/spec.md` §8 (Execution semantics / staged commits),
según `docs/plan.md` sección "E6 — Ejecutor de sagas (spec §8) — la entrega
más delicada". Reemplaza el passthrough plano tool_called/result_recorded
que E3-E5 usaban en `belay/proxy/lifecycle.py` por el ciclo de paso real de
seis etapas de §8.1, con materialización de compensación, idempotencia y
recuperación tras caída.

## Decisiones

- **Las seis etapas normativas de §8.1, en este orden exacto y sin
  excepciones.** `belay/executor/saga.py::SagaExecutor.run_step` ejecuta
  `journaled -> capturing -> calling -> result_recorded ->
  compensation_registered -> committed`, apendando su evento de ledger
  correspondiente en cada una (`STAGES` fija el orden como constante
  pública, no como comentario). "capturing" y "compensation_registered"
  emiten su evento incluso cuando no hay nada que hacer (sin `capture`
  declarado, o contrato irreversible) — la coherencia de §9.2 no distingue
  entre "no aplica" y "se omitió por accidente"; solo entre "el evento
  está" o "no está". Cualquier excepción en cualquier etapa se captura,
  apenda `step_failed` con lo que se sepa hasta ese punto, y se re-lanza —
  las etapas ya escritas antes del fallo permanecen intactas en el ledger
  (spec: "any -> failed(reason)").
  Test: `tests/executor/test_stage_order.py` — el más importante del repo
  según el plan — parametriza una inyección de fallo justo después de cada
  una de las seis etapas y verifica la secuencia exacta de eventos emitida
  en cada caso, más un caso feliz que corre las seis sin inyección.
- **Materialización, nunca re-evaluación (§8.1.5).** En `compensation_registered`,
  `SagaExecutor._materialize_compensation` evalúa `undo.args` contra
  `{$args, $result, $state}` en ese instante y persiste el resultado
  literal (un dict de valores concretos, no expresiones) en el payload del
  evento. Rewind (E7, aún no construido) recibirá ese payload ya
  materializado y lo reproducirá tal cual — no tiene forma de volver a
  evaluar la expresión aunque quisiera, porque la expresión ya no viaja
  con el evento después de este punto. Esto es intencional: el estado vivo
  en el momento del rewind puede ya no reflejar el estado en el momento de
  la ejecución original, y la spec exige que el undo revierta *lo que
  pasó*, no *lo que el estado actual sugeriría*.
- **`capture` corre antes de la llamada real y su contrato propio debe ser
  read-only.** `SagaExecutor._check_capture_is_read_only` resuelve el
  contrato del *tool de captura* (no el tool del paso) contra el
  `ContractSet` inyectado y rechaza con `contract_invalid` si alguno de sus
  `effects` no es `type: read`. Sin un `ContractSet` inyectado la
  comprobación se omite (best-effort) — es responsabilidad del llamador
  (el proxy real siempre inyecta uno) proveerlo para que la garantía
  aplique. Test: `tests/executor/test_capture.py`.
- **Idempotencia como tabla propia (`idempotency_keys`), no como
  replay del ledger.** `belay/executor/idempotency.py::IdempotencyStore`
  guarda una fila por `idempotency_key` con `status: "calling" | "done"`.
  `run_step` la consulta antes de llamar al upstream: si ya existe y está
  `"done"`, devuelve el resultado grabado sin tocar el upstream; si no
  existe, la crea en `"calling"`, llama, y la completa a `"done"` con el
  resultado. Se eligió una tabla dedicada en vez de derivar el estado
  releyendo el ledger en cada llamada porque la reconciliación de arranque
  (`recovery.py`) necesita exactamente esta misma tabla para distinguir
  "ya se resolvió" de "se está resolviendo ahora mismo" sin recorrer todo
  el ledger de la sesión en el camino caliente. Test:
  `tests/executor/test_idempotency.py` (upstream espía, llamado una sola
  vez).
- **Recuperación: reconciliar con clave, `step_indeterminate` sin ella.**
  `belay/executor/recovery.py::recover_session` escanea el ledger de una
  sesión buscando pasos con `tool_called` pero sin `result_recorded` (la
  ventana de caída literal del §8.1: "a crash between 3 and 4"). Si el paso
  declaró `idempotency_key`, reconcilia reinvocando el upstream vía un
  `Reconciler` inyectado (se espera que el upstream real deduplique por esa
  clave y devuelva el resultado original, no que repita el efecto) y
  completa el ciclo con un `result_recorded` marcado `recovered: true`. Si
  no hay clave, no hay forma segura de saber si la llamada tuvo efecto —
  Belay no adivina: apenda `step_indeterminate` como evento de primera
  clase (no una excepción, no un salto silencioso) y lo deja ahí.
  **Resolución operativa:** un `step_indeterminate` se resuelve hoy
  inspeccionando el upstream directamente (¿existe el efecto o no?) y
  corrigiendo el ledger fuera de banda si hace falta, o tratando el paso
  como irreversible a mano al hacer rewind; E7 documentará cómo el rewind
  reporta honestamente estos pasos en su informe (§10.3) en vez de
  fingir que fueron compensados. No hay todavía una herramienta de CLI
  dedicada a resolverlos — es deuda explícita, no un descuido.
  Test: `tests/executor/test_recovery.py` (ambos casos).
- **`conditional` con condiciones no cumplidas en ejecución =
  irreversible, no un error.** `_materialize_compensation` re-evalúa
  `contract.conditions` contra el `$state` real (post-captura,
  post-resultado) en el momento de `compensation_registered` — nunca
  contra lo que el plan asumió (§12 TOCTOU). Si no se cumplen, el payload
  es `{"reversible": false, "reason": "conditional_unmet"}` en vez de
  materializar un `undo` que ya no aplica; el paso igual se compromete
  (`step_committed`) porque la llamada sí tuvo efecto, solo que no hay
  forma de deshacerla. Test: `tests/executor/test_conditional.py`.
- **Property test: cualquier secuencia aleatoria de pasos produce un
  ledger coherente.** `tests/executor/test_property_coherence.py` genera
  secuencias de éxito/fallo con Hypothesis, corre `SagaExecutor.run_saga`
  con `auto_compensate=True`, y verifica `verify_coherence` (E2,
  `belay/ledger/verify.py`) sobre el resultado. Esta es la garantía de
  corrección más fuerte de la entrega: no depende de que el autor haya
  pensado en el caso concreto, sino de que la propiedad se sostenga para
  cualquier secuencia.
- **`run_saga`/`auto_compensate` como mecanismo mínimo de E6, no el rewind
  completo de E7.** Spec §8.2 permite auto-deshacer N-1…1 cuando la sesión
  se abrió con `auto_compensate: true`; `SagaExecutor.run_saga` implementa
  exactamente eso sobre los pasos que ya comprometió *en esa corrida*,
  llamando `compensate()` en orden estrictamente inverso de `step_seq`. No
  intenta fencing de sesión, reporte honesto de irreversibles/indeterminados,
  ni `--skip-and-continue` — eso es §10 completo y llega con E7. Este
  mecanismo es deliberadamente más estrecho: solo cubre el camino feliz de
  "una saga que falla a mitad de camino se deshace sola", que es
  exactamente el criterio de salida de esta entrega.
- **`ExecuteStage` reemplaza el passthrough, sin cambiar las etapas
  anteriores.** `belay/proxy/lifecycle.py::Lifecycle.govern_and_execute`
  ya no apenda `tool_called`/`result_recorded` directamente; delega en
  `ExecuteStage.execute`, que envuelve un `SagaExecutor` construido con el
  mismo `ContractSet` fijado por la sesión. `resolve -> plan -> policy ->
  approval` (E3-E5) no cambian de forma.
- **`examples/crm-mock`: un CRM de juguete real, no una interfaz de
  ejemplo.** `examples/crm-mock/server.py` es un servidor MCP real
  (FastMCP) con una tabla en memoria y `get/create/update/delete/
  import_records/export_records`, análogo a `examples/fs-server`. El test
  de aceptación (`tests/executor/test_crm_mock_acceptance.py`) lo levanta
  como subproceso stdio real vía `belay.proxy.upstream.connect_stdio` y
  corre una saga de 5 pasos con fallo inyectado en el paso 4 y
  `auto_compensate=True`; solo los tests de idempotencia (E6 (c)) usan un
  upstream espía/mock — el criterio de salida se prueba contra un
  servidor real, en la misma frontera que el test de integración stdio de
  E3.

## Referencias

- `docs/spec.md` §8 (Execution semantics), §4.2 (conditional/irreversible),
  §9.2 (verify_coherence), §12 (TOCTOU).
- `docs/plan.md` sección "E6 — Ejecutor de sagas (spec §8)".
- Código: `belay/executor/saga.py`, `belay/executor/idempotency.py`,
  `belay/executor/recovery.py`, `belay/db/models.py` (`IdempotencyRow`),
  `belay/proxy/lifecycle.py` (`ExecuteStage`), `examples/crm-mock/server.py`,
  `examples/contracts/crm.yaml`.
- Tests: `tests/executor/test_stage_order.py`,
  `tests/executor/test_capture.py`, `tests/executor/test_idempotency.py`,
  `tests/executor/test_recovery.py`, `tests/executor/test_conditional.py`,
  `tests/executor/test_property_coherence.py`,
  `tests/executor/test_crm_mock_acceptance.py`.
