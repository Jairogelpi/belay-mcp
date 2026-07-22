# ADR 0003: E3 — Proxy L1 + CLI

Fecha: 2026-07-22
Estado: aceptado

## Contexto

E3 implementa `docs/spec.md` §3 (Architecture overview / request lifecycle),
§4.6 (the default rule), y el Apéndice C (MCP mapping), según `docs/plan.md`
sección "E3 — Proxy L1 + CLI — primer hito publicable". Es la primera
entrega que expone un proxy MCP real: Belay como servidor MCP hacia el
agente y cliente MCP hacia el servidor de herramientas envuelto.

## Decisiones

- **La regla por defecto de §4.6, interpretada literalmente y sin
  invertirla.** Cita exacta de la spec: *"For a tool with no contract: If
  its MCP annotations declare `readOnlyHint: true` ⇒ treat as `effects:
  []`, allow. Otherwise ⇒ Belay MUST refuse to proxy the call with error
  `contract_missing`, unless the operator has explicitly configured
  `unsafe_passthrough: true` per tool."* Y el Apéndice C añade: *"
  `destructiveHint: true` with no Belay contract ⇒ `contract_missing`."*
  `belay/proxy/lifecycle.py::resolve()` implementa exactamente esto:
  `readOnlyHint` es el ÚNICO hint que autoriza sin contrato; `destructiveHint`
  (o cualquier otro hint) es irrelevante para `resolve()` — el llamador
  (`belay/proxy/server.py`) ni siquiera se lo pasa, solo pasa
  `read_only_hint`. Sin contrato y sin `readOnlyHint` ⇒ `contract_missing`
  siempre, salvo `unsafe_passthrough` explícito por tool. Test dedicado:
  `tests/proxy/test_lifecycle.py::test_destructive_hint_with_no_contract_is_still_contract_missing`.
- **`unsafe_passthrough` se registra dos veces, no una.** Cuando se usa el
  override: (1) se añade un evento `config_override` dedicado (tipo de
  evento §9.1) antes de `tool_called`, y (2) el campo `config_override:
  bool` viaja también en el payload de `tool_called`/`result_recorded`/
  `step_failed` de esa misma llamada. Es redundante a propósito: la spec dice
  "MUST be recorded in every affected ledger event", así que cualquier
  evento de la llamada, leído aisladamente, ya lleva la marca — no hace
  falta correlacionar con un evento `config_override` anterior para saber
  que hubo override.
- **Pipeline de spec §3 como clase `Lifecycle` con etapas intercambiables.**
  `resolve()` (función libre) + `PlanStage`/`PolicyStage`/`ApprovalStage`
  (clases con un método cada una) + `Lifecycle.govern_and_execute()` que las
  encadena. En L1, `PlanStage.plan()` devuelve un plan trivial de base
  `contract`, `PolicyStage.evaluate()` siempre `"allow"`,
  `ApprovalStage.maybe_park()` no hace nada. E4 sustituye el cuerpo de
  `PlanStage`/`PolicyStage` por el planner/policy engine reales; E5 el de
  `ApprovalStage`; E6 inserta el ciclo de paso de §8.1 entre `tool_called` y
  `result_recorded`. Ninguna de esas entregas necesita tocar la firma de
  `Lifecycle` ni los call sites en `belay/proxy/server.py`.
- **`set_hash` fijado por sesión, verificado con una prueba explícita de "no
  retroactividad".** `Lifecycle.contract_set` es una referencia inmutable
  capturada en el constructor; nunca se relee de disco ni se resuelve de
  nuevo. `start_session()` graba `session_started` +
  `contract_set_pinned` con ese `set_hash`. El test
  `test_session_fixes_set_hash_and_later_contract_changes_do_not_apply`
  construye un segundo `ContractSet` con distinto `set_hash` y comprueba que
  el `Lifecycle` ya construido sigue apuntando al primero y que todos sus
  eventos siguen llevando el `set_hash` original.
- **Transporte: stdio como mínimo exigido; HTTP streamable queda como
  seguimiento documentado, no implementado en E3.** `docs/plan.md` §1 pide
  "stdio y HTTP streamable" a nivel de producto, pero la entrega E3 solo
  exige "al menos stdio" y una abstracción limpia. `belay/proxy/upstream.py`
  expone `connect_stdio()` como única función de conexión; el resto del
  proxy (`Lifecycle`, `BelayProxyServer`) no conoce el transporte en
  absoluto — recibe un `UpstreamClient` ya conectado. Añadir HTTP streamable
  en una entrega futura es una función `connect_http()` nueva con la misma
  forma, sin tocar `lifecycle.py` ni `server.py`. Issue de seguimiento
  anotado aquí en vez de en `docs/plan.md §11` porque es específico de esta
  entrega, no del post-1.0.
- **`belay wrap` valida el contract set inmediatamente**, no en el primer
  `belay run`: falla rápido si el fichero de contratos no compila o si
  `<server-dir>/server.py` no existe, antes de escribir `belay.wrap.json`.
- **El entorno del subproceso upstream hereda `os.environ` completo, no solo
  el subconjunto "seguro" del SDK.** `mcp.client.stdio.get_default_environment()`
  solo copia un puñado de variables por razones de seguridad (pensado para
  que un agente no inyecte variables arbitrarias al lanzar un proceso ajeno).
  Pero aquí quien lanza el proceso upstream es el propio operador de Belay
  (vía `belay run`), no el agente — así que `belay/cli/main.py::run()` pasa
  `env=dict(os.environ)` explícitamente a `connect_stdio()`. Sin esto,
  `examples/fs-server`'s `BELAY_FS_ROOT` (y cualquier config equivalente de
  un servidor real) no llegaría al subproceso.
- **`examples/fs-server` es un servidor MCP de verdad, no un mock.**
  Construido con `FastMCP` (mismo SDK oficial), cuatro tools
  (`fs.list_files`, `fs.read_file`, `fs.write_file`, `fs.delete_file`)
  sandboxed a un directorio (`BELAY_FS_ROOT` o un temp dir). Los nombres de
  tool llevan el prefijo `fs.` para casar exactamente con
  `examples/contracts/fs.yaml` (que ya usaba esos nombres desde E1).
  `fs.list_files`/`fs.read_file` llevan `readOnlyHint=True`;
  `fs.delete_file` lleva `destructiveHint=True` pero deliberadamente
  ningún contrato propio salvo el declarado en `fs.yaml` — es el caso de
  prueba vivo de la regla por defecto.

## Tradeoffs de rendimiento de la suite de tests

- El test de integración real por stdio (`tests/proxy/test_stdio_integration.py`)
  lanza **dos** subprocesos Python (uno para `belay run`, otro para
  `examples/fs-server` que `belay run` lanza a su vez) por caso de test.
  Esto cuesta ~1-2 s por test en vez de milisegundos. Con la suite completa
  en ~35 s (bajo el presupuesto de 60 s de `docs/plan.md` §0), se decidió
  **no** marcarlo `slow`/skip por defecto: sigue siendo el único test que
  demuestra el camino de extremo a extremo pedido por E3 (c), y el margen
  actual (35 s de 60 s) permite tenerlo en la ejecución rápida. Si entregas
  futuras (E4+) empujan el total cerca del límite, la primera palanca a
  tirar es marcar este fichero `@pytest.mark.slow` y excluirlo de la
  ejecución rápida por defecto (incluyéndolo siempre en CI) antes de tocar
  timeouts o paralelismo.
- Para no depender de subprocesos en cada test del proxy, la mayoría de los
  tests de `belay/proxy/server.py` usan el transporte en memoria del propio
  SDK (`mcp.shared.memory.create_connected_server_and_client_session`), que
  sigue siendo un `ClientSession` real hablando el protocolo real, solo que
  sin arrancar un proceso — cubre el lado agente↔Belay del proxy con coste
  de milisegundos, dejando el coste de subproceso solo para el único test
  que necesita demostrar el camino stdio completo.

## Referencias

- `docs/spec.md` §3 (arquitectura y ciclo de vida), §4.6 (regla por
  defecto), Apéndice C (mapeo MCP).
- `docs/plan.md` sección "E3 — Proxy L1 + CLI".
- Código: `belay/proxy/{lifecycle,server,upstream,config}.py`,
  `belay/cli/main.py` (`wrap`, `run`), `examples/fs-server/server.py`.
- Tests: `tests/proxy/test_{lifecycle,server,stdio_integration}.py`,
  `tests/cli/test_wrap.py`.
