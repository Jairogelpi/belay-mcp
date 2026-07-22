# ADR 0009: E9 — Demo, docs y pulido de portfolio (v0.1.0 readiness)

Fecha: 2026-07-22
Estado: aceptado

## Contexto

E9 implementa `docs/plan.md` sección "E9 — Demo, docs y pulido de
portfolio" y cierra la Definición de Terminado global (§0). No añade
comportamiento nuevo a `belay/`; produce el guion de demo reproducible, la
arquitectura documentada, el README final, la plantilla de contribución +
issues, y el workflow de release. El motor (E0-E8) ya estaba completo y
verificado (225 tests, 93% cobertura, L3) antes de empezar esta entrega.

## Decisiones

- **`examples/demo.py` usa la vía de "narrow por re-plan" que E7
  realmente construyó, no un `--narrow` inventado.** ADR 0007 ya documentó
  que `belay approvals approve --narrow <filter>` no existe y que el flujo
  probado es que el agente reintente con `args` distintos (nuevo `plan_id`,
  spec §12) y el humano apruebe *ese* plan. Añadir un `--narrow` real habría
  significado que `belay approvals approve` reescribiera el plan aprobado
  para que difiera de lo que el agente pidió — un cambio de semántica de
  aprobación (spec §7: el humano aprueba *el plan*, no edita el plan del
  agente desde la consola) que no es una mejora de UX pequeña y limpia; es
  una decisión de spec. Se optó por mantener `demo.py` honesto sobre lo que
  existe: reject del plan amplio + retry del agente con `before_year`
  distinto + approve del plan estrecho, exactamente el camino que
  `tests/cli/test_rewind.py` ya prueba end-to-end.
- **`demo.py` es un script real, no una grabación con texto embebido.**
  Ejecuta `belay wrap`, spawnea `belay run` (vía el SDK MCP, equivalente a
  `belay run &` en una terminal real), simula al agente con llamadas MCP
  reales contra `examples/crm-mock`, y hace shell-out al CLI real de
  `belay` (`approvals list/approve/reject`, `rewind --dry-run`, `rewind
  --by`, `verify`) para cada paso que un humano teclearía. Se verificó
  corriéndolo como subproceso dos veces (con y sin `--oops`); ambas
  terminan con `chain: OK` / `coherence: OK` / "session fully compensated".
  Ningún output del guion está hardcodeado.
- **La siembra usa `crm.import_records` en un solo paso, y el rewind del
  guion usa `--to-step` para acotar el alcance a los dos `bulk_delete`.**
  El servidor de juguete solo crea registros de uno en uno (`crm.create`);
  sembrar ~500 con 500 llamadas gobernadas habría sido lento y, más
  importante, habría metido 500 pasos reversibles en el alcance del rewind
  final, diluyendo la demo. `crm.import_records` es un único paso
  irreversible (según su contrato) — con `--to-step <ese step_seq>` el
  rewind solo compensa lo que la demo realmente quiere mostrar deshaciendo
  (los dos `bulk_delete`), sin reclamar "fully rewound" sobre pasos que
  nunca estuvieron en su alcance (coherente con la honestidad de §10.3: el
  alcance se declara explícitamente, no se esconde).
- **Los "500" y "80" registros son reales, no una paráfrasis del guion.**
  `demo.py` siembra 80 registros con `last_seen=2022` (realmente
  obsoletos) y 420 con `last_seen=2024` (no lo que se quería borrar). Una
  primera petición amplia (`before_year=2030`) coincide con los 500 y se
  pausa (motivo real: `defaults.unknown_effects`, spec §6.4 — el contrato
  de `crm.bulk_delete` no declara un `count` de efecto, así que la
  incertidumbre paga el default conservador; no hizo falta ningún policy
  YAML de cap explícito). Una segunda petición acotada
  (`before_year=2023`) coincide exactamente con los 80 obsoletos.
- **No se generó grabación (asciinema/VHS).** Ni `asciinema` ni `vhs`
  estaban instalados ni instalables de forma fiable en el sandbox de esta
  entrega (sin acceso de red confirmado para instalar binarios adicionales
  con garantías). En vez de fingir un GIF, se dejó `examples/demo.tape`
  (guion VHS real, listo para `vhs examples/demo.tape`) documentado en el
  README como brecha honesta con su comando exacto de resolución.
- **`docs/architecture.md` deriva el diagrama de la estructura de módulos
  real** (`belay/{contracts,planner,policy,approvals,executor,rewind,
  ledger,proxy}`) y del orden normativo en `belay/proxy/lifecycle.py`
  (resolve -> plan -> policy -> approval -> execute), no de una
  paráfrasis de la spec. Incluye el hecho de que el fencing de rewind es
  un evento de ledger compartido entre procesos (ADR 0007), visible en el
  diagrama como una flecha `RewindService -> Ledger` separada de la del
  proxy en ejecución.
- **README:** badge de PyPI omitido explícitamente con una nota, en vez de
  un badge que apuntaría a un proyecto PyPI que no existe todavía (no hay
  paquete publicado; `pyproject.toml` sigue en `0.1.0.dev0`). Badge de CI
  apunta al workflow real (`ci.yaml`) en `github.com/Jairogelpi/belay-mcp`
  (el remoto real del repo, no `belay-mcp/belay` como decían las URLs de
  `pyproject.toml` — discrepancia preexistente, no corregida aquí porque
  cambiar `pyproject.toml`'s `[project.urls]` está fuera del alcance
  declarado de E9 y no bloquea nada del DoD). Badge de conformance L3
  reclamado porque `belay-conformance run --target belay --level 3` se
  re-verificó pasando en esta entrega.
- **Sección comparativa** cita categorías reales (gateways/routers MCP,
  observabilidad de agentes, motores de saga/workflow enterprise) con
  enlaces a proyectos conocidos existentes (Temporal, AWS Step Functions,
  LangSmith, Langfuse, mcp-gateway) — sin alegar sustitución 1:1 ni
  fabricar comparativas de features que no se han verificado.
- **`release.yaml`** sigue el patrón estándar de `pypa/gh-action-pypi-publish`
  con OIDC trusted publishing (`permissions: id-token: write`, sin
  secretos), gated a `push: tags: v*.*.*`, con un job `test` previo. No se
  puede verificar que el `publish` job realmente funcione sin que el
  mantenedor configure trusted publishing en PyPI primero — eso es un paso
  humano fuera del alcance de un agente (requiere acceso a la cuenta de
  PyPI).

## Brechas conocidas / seguimiento

Ver la sección "Known gaps" de `CHANGELOG.md` bajo `[0.1.0]`: falta el
flag `--narrow` real (deliberado, ver arriba), falta
`docs/traceability.md` (nunca se construyó en ninguna entrega anterior,
no es un gap de E9 pero se deja anotado porque el DoD lo asume implícito
vía "ningún MUST sin test" — la cobertura se verifica leyendo la suite,
no con una tabla generada), falta la grabación (ver arriba), y falta el
tag `v0.1.0` + la primera publicación real a PyPI (pasos manuales del
mantenedor, explícitamente fuera del alcance de esta entrega — no se creó
ni empujó ningún tag).

## Referencias

- `docs/plan.md` sección "E9 — Demo, docs y pulido de portfolio" y
  sección "0. Producto final y definición de terminado global".
- `docs/plan.md` sección "10. Guion exacto de la demo".
- `docs/adr/0007-e7-rewind.md` ("Brechas conocidas": el gap de `--narrow`
  que esta entrega hereda en vez de inventar una solución nueva).
- Código: `examples/demo.py`, `examples/demo.tape`,
  `docs/architecture.md`, `README.md`, `CONTRIBUTING.md`,
  `.github/ISSUE_TEMPLATE/`, `.github/workflows/release.yaml`,
  `CHANGELOG.md`.
