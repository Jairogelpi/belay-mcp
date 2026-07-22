# ADR 0005: E5 — Aprobaciones

Fecha: 2026-07-22
Estado: aceptado

## Contexto

E5 implementa `docs/spec.md` §7 (Approvals) y la parte de §12 sobre
approver binding / no-self-approval, según `docs/plan.md` sección "E5 —
Aprobaciones (spec §7)". Reemplaza el stub `ApprovalStage` de E3/E4
(`belay/proxy/lifecycle.py`) por una cola de aprobaciones real
(`belay/approvals/queue.py`) y las órdenes CLI `belay approvals
list/approve/reject`.

## Decisiones

- **Máquina de estados (§7.1): `pending -> approved | rejected | expired`,
  transiciones unidireccionales.** `belay/approvals/queue.py` codifica el
  grafo legal explícitamente en `_LEGAL_TRANSITIONS`: desde `approved`,
  `rejected` o `expired` no hay ninguna transición permitida — ni siquiera
  de vuelta a `pending`. `ApprovalQueue._resolve()` consulta ese grafo y
  lanza `ValueError` ante cualquier transición fuera de él (ilegal), y
  `BelayError("approval_expired")` específicamente cuando el origen es
  `expired` o cuando "ahora" ya alcanzó `expires_at`. Tests:
  `tests/approvals/test_queue.py::test_transitions_are_unidirectional_*`.
- **Expiración perezosa, y gana el empate exacto (§7.1/§12 TOCTOU).** No hay
  un proceso en segundo plano que expire items; `ApprovalQueue` calcula
  `pending -> expired` bajo demanda (`get`, `for_plan`, `list`,
  `approve`/`reject`) comparando `clock.now()` contra `expires_at`. En
  `approve`/`reject`, la comprobación de expiración ocurre *antes* de
  aplicar la transición solicitada, con `>=` (no `>`): si el reloj marca
  exactamente `expires_at` en el instante de aprobar, la expiración gana.
  Esto reutiliza el `Clock` inyectable de E4 (`belay/clock.py`) — ningún
  código nuevo de reloj — y es la única forma de escribir
  `test_exact_tie_between_approval_and_expiration_expiration_wins` sin
  `sleep`/carreras reales.
- **`pending_approval` es un resultado estructurado, no una excepción
  (§7.3).** `Lifecycle.govern_and_execute()` devuelve
  `{"status": "pending_approval", "approval_id": ..., "poll_after_ms": ...}`
  como valor de retorno normal (no lanza `BelayError`) cuando el veredicto
  es `pause` y no hay un item ya `approved` para el `plan_id`.
  `belay/proxy/server.py` envuelve cualquier retorno que no sea ya un
  `CallToolResult` (el passthrough de E3) en un `CallToolResult` con
  `isError=False`, tanto en `content` (JSON de texto) como en
  `structuredContent` — necesario porque el SDK de MCP valida la respuesta
  contra el `outputSchema` declarado del tool upstream y algunos clientes
  exigen `structuredContent` presente. `rejected`/`expired`, en cambio, sí
  se propagan como `BelayError` (`approval_rejected`/`approval_expired`),
  igual que cualquier otro código de §11 — son errores según la tabla de
  §11, `pending_approval` no lo es.
- **Binding por `plan_id`, no invalidación explícita (§12 approver
  binding).** Un item de aprobación se crea con `ApprovalQueue.request(...,
  plan_id=plan.plan_id)`, y `ApprovalStage.check()` solo busca items vía
  `queue.for_plan(plan.plan_id)`. Para que una re-planificación de la misma
  llamada lógica invalide el item viejo sin código de invalidación
  dedicado, `Planner.plan()` (E4) pasó a derivar `plan_id` de forma
  determinista sobre `(session_id, tool, args)` en vez de un UUID aleatorio
  por llamada (`belay/planner/planner.py::_plan_id`): un reintento
  idéntico (mismos args) resuelve al mismo `plan_id` y puede recoger un
  item ya `approved`/`rejected`; un re-plan genuino con args distintos
  (bait-and-switch) obtiene un `plan_id` distinto, así que cualquier
  aprobación ligada al `plan_id` viejo nunca se encuentra para el nuevo.
  Esto es lo que hace posible, a la vez, el flujo de "poll" que sugiere
  `poll_after_ms` (§7.3) y la garantía de "approver binding" de §12 con el
  mismo mecanismo. Test:
  `tests/approvals/test_queue.py::test_approval_item_is_bound_to_its_plan_id_and_replanning_invalidates_it`.
- **Persistencia en SQLite (tabla `approvals`), no en memoria.** El plan
  original (`docs/plan.md` §1) ya prevé una tabla `approvals` junto a
  `events`. Es necesaria de verdad: `belay run` y `belay approvals
  list/approve/reject` son procesos de SO distintos (así lo pide el guion
  de la demo, `docs/plan.md` §10), así que una cola en memoria de
  `Lifecycle` sería invisible para la CLI. `ApprovalQueue` comparte el
  mismo motor SQLAlchemy que `LedgerStore` (nueva propiedad pública
  `LedgerStore.engine`) cuando vive dentro del proxy, y abre su propio
  engine sobre el mismo fichero `belay.db` cuando la CLI lo invoca por
  separado.
- **No-self-approval (§12) forzado arquitectónicamente, no solo por
  test.** `BelayProxyServer._register_handlers()` solo registra
  `list_tools`/`call_tool`, y `list_tools` únicamente reenvía los tools del
  *upstream* — nunca hay un tool `approvals.*` que un agente pueda
  descubrir o invocar. `ApprovalStage` (el único objeto de aprobación
  alcanzable desde el lado del agente) solo tiene métodos de lectura/creación
  (`check` -> `queue.for_plan`/`queue.request`); no existe ninguna llamada a
  `ApprovalQueue.approve`/`.reject` en todo `belay/proxy/`. Esos verbos
  viven exclusivamente en `belay/cli/main.py`. Tests:
  `tests/proxy/test_no_self_approval.py` (lista de tools nunca incluye nada
  con "approv"; llamar a un nombre de tool con forma de aprobación se
  rechaza como `contract_missing`, igual que cualquier tool no declarado;
  chequeo de código fuente de que `ApprovalStage` no contiene `.approve(`/
  `.reject(`).
- **`belay run` gana `--policy` (antes solo `belay plan` lo tenía).**
  Necesario para poder ejercer el flujo completo de §7 sobre un servidor
  real por stdio sin depender de que un contrato declarado sea
  `irreversible` (el único gatillo de `pause` que ya existía en
  `default_policy()`). `BelayProxyServer` acepta ahora un `PolicyDoc`
  opcional.
- **`examples/contracts/fs.yaml`: `fs.read_file` pasó de `irreversible` a
  `reversible` con un undo no-op (`fs.read_file` de vuelta).** Efecto
  colateral necesario de que `pause` ahora bloquee de verdad: con
  `defaults.irreversible: pause` (default de fábrica, §6.4) y el contrato
  anterior, *toda* lectura por `fs.read_file` habría quedado parada en cada
  sesión, rompiendo la integración E3 existente
  (`tests/proxy/test_stdio_integration.py`). Declarar una lectura pura como
  `irreversible` era, en retrospectiva, una elección de modelado confusa —
  nada cambia, así que es trivialmente reversible (el undo es la misma
  lectura, que no hace nada). El contrato de E1 con `reversibility:
  irreversible` para probar el caso base sigue existiendo, pero ahora vive
  como fixture local en `tests/proxy/test_lifecycle.py`, no en el ejemplo
  que corre por stdio en cada test de integración.

## Referencias

- `docs/spec.md` §7 (Approvals), §12 (Security considerations: approver
  binding, no-self-approval).
- `docs/plan.md` sección "E5 — Aprobaciones (spec §7)".
- Código: `belay/approvals/queue.py`, `belay/db/models.py` (`ApprovalRow`),
  `belay/proxy/lifecycle.py` (`ApprovalStage`, `PendingApproval`,
  `ApprovalCheck`), `belay/proxy/server.py`, `belay/planner/planner.py`
  (`_plan_id`), `belay/cli/main.py` (`approvals` subcommands, `run
  --policy`).
- Tests: `tests/approvals/test_queue.py`,
  `tests/proxy/test_lifecycle.py` (casos de E5),
  `tests/proxy/test_no_self_approval.py`, `tests/cli/test_approvals.py`.
