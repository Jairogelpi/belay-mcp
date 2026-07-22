# Belay — Plan maestro de implementación (SDD + TDD)

**Objetivo:** que un agente de desarrollo (Codex u otro) pueda construir el producto completo **solo con este documento + `docs/spec.md` (Belay Specification 0.1)**, hasta publicar la release `v0.1.0` en GitHub lista para portfolio.

**Idioma:** este plan y los mensajes de trabajo, en español. Todo artefacto del repo visible al público (README, docs, código, mensajes de commit, CHANGELOG) **en inglés** — el público de un estándar open source es global.

---

## 0. Producto final y definición de terminado global

La release `v0.1.0` de `belay` está terminada cuando, partiendo de un clon limpio:

1. `pip install -e ".[dev]" && pytest` pasa en < 60 s con cobertura ≥ 90 % en `belay/` (ramas incluidas).
2. `belay wrap examples/fs-server --contracts examples/contracts/fs.yaml && belay run` levanta un proxy MCP funcional al que se conecta cualquier cliente MCP estándar.
3. La **demo de 3 minutos** (§10) funciona de principio a fin: un agente intenta borrar en masa → Belay pausa → aprobación humana → ejecución → `belay rewind` restaura, con informe honesto.
4. La suite de conformidad (`belay-conformance`) declara la implementación **L3** según §13 de la spec.
5. CI verde en GitHub Actions (lint + types + tests + build), release etiquetada con changelog, README con badges reales.

Regla suprema, heredada de la spec: **ningún MUST de `docs/spec.md` sin su test**. Si durante la implementación un MUST resulta ambiguo o inviable, se cambia primero la spec en un commit separado con nota de decisión, nunca se divierge en silencio.

## 1. Stack y decisiones técnicas fijadas

- **Python 3.12+**. Paquete `belay-mcp` en PyPI, módulo `belay`.
- **MCP:** SDK oficial de Python (`mcp`). Belay es servidor MCP hacia el agente y cliente MCP hacia los tools (spec, Apéndice C). Transportes: stdio y HTTP streamable.
- **Persistencia:** SQLite vía SQLAlchemy 2 + Alembic. Un fichero por despliegue (`belay.db`), tablas: `sessions`, `events`, `approvals`, `contract_sets`.
- **Modelos:** Pydantic v2 para todo objeto de la spec (contratos, planes, políticas, eventos, errores). Serialización canónica: JSON con claves ordenadas, sin espacios, UTF-8 — es la base del hash de evidencia, congélala en `belay/canonical.py` con tests propios.
- **CLI:** Typer. **Consola de aprobaciones v0.1:** solo CLI (`belay approvals list/approve/reject`); la web queda fuera de alcance.
- **Lenguaje de expresiones (§4.3):** implementación propia con `ast` de gramática cerrada — PROHIBIDO `eval`/`exec`. Alternativa aceptada: parser recursivo manual. Nada de dependencias de plantillas.
- **Sin LLM en el camino de seguridad.** Belay no llama a ningún modelo. Determinista de punta a punta.
- **Lint/format/types:** ruff + mypy estricto en `belay/` (los tests pueden relajar mypy).
- **Licencia:** MIT. `docs/spec.md` con nota CC-BY-4.0.

## 2. Estructura del repositorio (crear en E0 y no renombrar después)

```
belay/
├── belay/
│   ├── __init__.py
│   ├── canonical.py          # JSON canónico + SHA-256
│   ├── errors.py             # error model §11 completo
│   ├── contracts/
│   │   ├── model.py          # Pydantic: Contract, Effect, Undo, Capture…
│   │   ├── loader.py         # YAML/JSON → ContractSet, set_hash
│   │   └── expressions.py    # §4.3: parse(), evaluate(expr, scope)
│   ├── ledger/
│   │   ├── model.py          # Event envelope, tipos §9.1
│   │   ├── store.py          # append, read, hash chain
│   │   ├── verify.py         # §9.2: verificación de cadena y coherencia
│   │   └── redact.py         # §9.3
│   ├── planner/
│   │   ├── model.py          # Plan, EffectEstimate
│   │   └── planner.py        # plan(), bases §5.3, expiración §5.4
│   ├── policy/
│   │   ├── model.py          # PolicyDoc, Cap, Verdict
│   │   └── engine.py         # evaluate(plan, policy) → verdict + reasons
│   ├── approvals/
│   │   └── queue.py          # §7: estados, expiración, no-self-approval
│   ├── executor/
│   │   ├── saga.py           # §8: ciclo de paso, materialización de undo
│   │   ├── idempotency.py
│   │   └── recovery.py       # arranque: pasos journaled sin resolver → §8.1
│   ├── rewind/
│   │   └── service.py        # §10: plan de rewind, ejecución, informe
│   ├── proxy/
│   │   ├── server.py         # cara MCP hacia el agente
│   │   ├── upstream.py       # cliente MCP hacia tools
│   │   └── lifecycle.py      # §3: resolve→plan→policy→(approval)→execute
│   └── cli/
│       └── main.py           # wrap, run, plan, approvals, rewind, verify
├── conformance/              # paquete belay-conformance (E8)
├── examples/
│   ├── fs-server/            # servidor MCP de ficheros de juguete
│   ├── crm-mock/             # CRM en memoria con get/create/update/delete/import/export
│   ├── contracts/            # fs.yaml, crm.yaml, email.yaml
│   └── demo.py               # guion de la demo §10
├── docs/
│   ├── spec.md               # Belay Specification 0.1 (el documento ya redactado)
│   ├── architecture.md
│   └── adr/
├── tests/                    # espejo de belay/: tests/contracts/, tests/ledger/…
├── .github/workflows/ci.yaml + release.yaml
├── AGENTS.md                 # §11 de este plan
├── README.md                 # el README de marketing ya redactado, con quickstart real
├── CHANGELOG.md, CONTRIBUTING.md, LICENSE, pyproject.toml
```

## 3. Metodología (idéntica disciplina que Grandmaster Champions)

1. **SDD:** cada entrega comienza copiando en `docs/adr/` las decisiones nuevas y enlazando las secciones de `docs/spec.md` que implementa. La spec manda; el prompt no.
2. **TDD:** rojo → verde → refactor. Ningún código de producción sin test rojo previo. Nombres de test = comportamiento (`test_refuses_destructive_tool_without_contract`), en inglés.
3. **Pirámide por entrega:** unitarios puros → property-based (Hypothesis) → integración (SQLite temporal, proxy en memoria) → aceptación (escenarios de la entrega, extremo a extremo).
4. **Cada bug = test de regresión permanente antes del fix.**
5. **Suite rápida desde el día 0:** base plantilla migrada una vez por sesión de pytest y copiada por test; objetivo < 60 s total. Si una entrega la supera, arreglar la velocidad ES parte de la entrega.
6. **Un PR por entrega**, sin mezclar. Mensajes de commit convencionales (`feat:`, `test:`, `docs:`).

## 4. Entregas

Orden estricto; cada una lista **(a)** alcance, **(b)** contratos/firmas clave, **(c)** tests obligatorios (mínimo — ampliar es bienvenido), **(d)** criterio de salida.

---

### E0 — Andamiaje

**(a)** Estructura §2, pyproject con extras `[dev]`, ruff+mypy+pytest+coverage configurados, CI en GitHub Actions (matriz 3.12/3.13), pre-commit, README y spec copiados a su sitio, Alembic inicializado con tablas vacías versionadas.
**(c)** un test trivial de importación por paquete; test de que `belay --help` ejecuta.
**(d)** CI verde en repo público desde el primer push. El repo ya es presentable aunque no haga nada.

### E1 — Contratos y lenguaje de expresiones (spec §4)

**(b)**
```python
parse(text: str) -> Expr                      # rechaza todo fuera de la gramática
evaluate(expr: Expr, scope: Scope) -> Value   # Scope = {args, result, context, state}
load_contract_set(paths) -> ContractSet       # valida contra el JSON Schema del Apéndice A
ContractSet.set_hash -> str
ContractSet.resolve(tool: str) -> Contract | None
```
**(c)**
- Unitarios de validación: `reversible` sin `undo` → `contract_invalid`; `irreversible` con `undo` → inválido; `conditional` exige `undo`+`conditions` (los tres `allOf` del schema).
- Expresiones: cada operador de §4.3; acceso a rutas anidadas; property (Hypothesis): ninguna cadena generada con tokens fuera de la gramática parsea; **seguridad**: `__import__`, atributos dunder, llamadas a función → `expression_invalid`.
- `set_hash` estable: mismo contenido con claves desordenadas y YAML vs JSON → mismo hash (vía canónico); un byte cambiado → hash distinto.
- Campos desconocidos en contrato → rechazo (§14: la autoridad es estricta).
**(d)** los YAML de `examples/contracts/` cargan; 100 % de los MUST de §4.1–4.3 y Apéndice A con test.

### E2 — Ledger (spec §9)

**(b)**
```python
LedgerStore.append(session_id, type, payload, step_seq=None) -> Event   # calcula prev_hash/hash
LedgerStore.read(session_id) -> list[Event]
verify_chain(events) -> VerifyReport          # recomputa cadena
verify_coherence(events) -> VerifyReport      # §9.2: journal/capture/result/compensación por paso
replay(events) -> SessionState                # §9.4 sin acceso a tools
redact(payload, contract) -> payload          # hashes con sal, §9.3
```
**(c)**
- Cadena: append de N eventos → verify ok; corromper un byte del evento k → verify señala exactamente k (test obligatorio de la suite de conformidad).
- Property: para cualquier secuencia válida generada, `replay` es determinista y puro (dos ejecuciones → estados idénticos).
- Redacción: campo redactado no aparece en claro; igualdad comprobable entre dos eventos con el mismo secreto; evento ya escrito es inmutable (no existe API de update — verificar que el store no la expone).
- Apéndice: eventos con campos desconocidos se conservan al releer (§14: la evidencia es tolerante).
**(d)** `belay verify <db>` funciona por CLI contra una base real.

### E3 — Proxy L1 + CLI (spec §3, §4.6, Apéndice C) — **primer hito publicable**

**(a)** Proxy MCP completo en modo L1: lista tools del upstream, resuelve contrato por llamada, aplica la **regla por defecto** (§4.6), ejecuta passthrough con eventos de ledger, expone `belay wrap` / `belay run` / `belay verify`. `unsafe_passthrough` por tool en config, registrado como `config_override`.
**(c)**
- `readOnlyHint:true` sin contrato → permitido, `effects:[read]` implícito.
- Tool sin contrato ni hint → `contract_missing`; con `destructiveHint` → ídem (hints nunca autorizan).
- `unsafe_passthrough` → pasa y TODOS sus eventos llevan el override.
- Sesión fija `set_hash` en `session_started`; cambiar contratos a mitad → las llamadas siguen gobernadas por el set fijado.
- Integración: cliente MCP real (SDK) contra Belay contra `examples/fs-server`, extremo a extremo por stdio.
**(d)** conformidad **L1** pasando; tag `v0.0.1-alpha` y nota "L1 preview" en README. *A partir de aquí el repo ya vale para el portfolio y cada entrega lo mejora.*

### E4 — Planner y motor de políticas (spec §5, §6)

**(b)**
```python
Planner.plan(tool, args, session) -> Plan          # bases: native_dry_run > dry_run > contract
PolicyEngine.evaluate(plan, policy) -> PolicyResult # verdict + reasons (ids de regla)
```
Adaptadores de dry-run v0.1: `contract` (siempre) y `native_dry_run` si el tool expone `<tool>.dry_run`; el simulador SQL queda como issue futuro documentado.
**(c)**
- Verdict más restrictivo entre dimensiones (`deny > pause > allow`); primer match por dimensión; reasons exactos.
- Incertidumbre: `estimate:true` evaluado contra cota superior; sin cota → `defaults.unknown_effects` (property: nunca un plan con unknowns obtiene `allow` si el default es `pause`).
- Irreversible → default `pause` (§6.4); relajación por tool queda en config y en ledger.
- Expiración de plan (§5.4): plan caducado al ejecutar → `plan_expired`; args no idénticos byte a byte → `plan_mismatch`.
- Quiet hours con reloj inyectable.
**(d)** `belay plan <tool> --args '<json>'` por CLI devuelve el objeto Plan completo de §5.1.

### E5 — Aprobaciones (spec §7)

**(c)**
- Transiciones unidireccionales; item expirado jamás ejecutable (incluye carrera: aprobar en el mismo instante de expirar → gana la expiración, test con reloj inyectable).
- El agente recibe `pending_approval` estructurado, no error; tras rechazo → `approval_rejected` con razón.
- **No-self-approval:** el proxy no expone superficie de aprobación al agente; test: un tool call del agente a cualquier ruta de aprobación no existe/falla.
- La aprobación queda ligada a `plan_id`; re-planificar invalida el item (§12 approver binding).
**(d)** flujo completo por CLI: acción pausada → `belay approvals list` → `approve` → la ejecución continúa y el ledger enlaza todo.

### E6 — Ejecutor de sagas (spec §8) — **la entrega más delicada**

**(b)** ciclo de paso EXACTAMENTE en el orden normativo de §8.1. Materialización: `undo.args` se evalúa en `compensation_registered` y se persiste literal; rewind jamás re-evalúa.
**(c)**
- Orden: inyectar fallo tras cada etapa (1→6) y verificar el estado resultante y los eventos emitidos (test parametrizado por etapa — es el test más importante del repo).
- Capture: se ejecuta antes de la llamada, su contrato debe ser read-only (violación → `contract_invalid`), snapshot presente en el ledger.
- Idempotencia: repetir llamada con la misma clave → resultado grabado, upstream llamado una sola vez (upstream espía).
- Recuperación: matar el proceso (simulado) entre `calling` y `result_recorded` → al arrancar, con clave de idempotencia se reconcilia; sin ella → `step_indeterminate` como estado de primera clase.
- `conditional` con condiciones no cumplidas en ejecución → el paso se registra irreversible (§4.2).
- Property: para cualquier secuencia de pasos generada con éxitos/fallos aleatorios, el ledger resultante pasa `verify_coherence`.
**(d)** una saga de 5 pasos contra `crm-mock` con fallo en el paso 4 y `auto_compensate:true` deja el CRM en su estado inicial.

### E7 — Rewind (spec §10) — cierra **L3**

**(c)**
- Orden inverso estricto por `step_seq`; cada compensación es mini-paso en el mismo ledger.
- `dry_run:true` → plan de rewind con enumeración honesta (irreversibles, conditional-unmet, indeterminate) sin ejecutar nada.
- Fencing: sesión viva se cerca antes de rewind; paso nuevo tras fence → `session_fenced`.
- `halt_on_failure` por defecto; `--skip-and-continue` explícito y registrado.
- `verification` declarada → se ejecuta y registra; fallo → `verification_failed` y el paso NO cuenta como compensado.
- **Honestidad (§10.3):** nunca "fully rewound" con pasos en alcance sin compensar+verificar — test con mezcla de reversibles e irreversibles.
- Las compensaciones pasan por el policy engine (§12): un undo sobre-cap → se pausa (test).
**(d)** conformidad **L3**; la demo (§10) completa funciona.

### E8 — Suite de conformidad pública + packs de ejemplo

**(a)** extraer los tests marcados `@conformance(level=…)` al paquete `belay-conformance`, ejecutable contra CUALQUIER implementación vía un adaptador fino (`ConformanceTarget` con ~6 métodos). Es lo que convierte a Belay de producto en estándar. Packs de contratos de ejemplo: filesystem, crm-mock, email (irreversible), cada uno con su test de carga.
**(d)** `pip install belay-conformance && belay-conformance run --target belay --level 3` → informe L3.

### E9 — Demo, docs y pulido de portfolio

**(a)**
1. `examples/demo.py`: guion reproducible — agente simulado pide `crm.bulk_delete` de ~500 filas → pausa → consola muestra el plan → humano aprueba versión acotada → ejecuta → "ups" → `belay rewind` → informe honesto. Grabar con `asciinema` o VHS (tape incluido en el repo) y enlazar el GIF en el README.
2. `docs/architecture.md` con el diagrama (agente → belay [contratos|políticas|dry-run|aprobaciones|ejecutor|rewind|ledger] → tools) en Mermaid.
3. README final: badges reales (CI, PyPI, licencia, conformance L3), quickstart verificado copiando-pegando en un contenedor limpio, sección comparativa (gateways / observabilidad / rewind enterprise) con enlaces.
4. `CONTRIBUTING.md` + plantillas de issue ("Propose a contract pack", "Spec ambiguity").
5. Release: `release.yaml` publica en PyPI con trusted publishing al etiquetar; tag `v0.1.0` con changelog.
**(d)** la Definición de Terminado global (§0) completa, punto por punto.

---

## 5. Modelo de errores — tabla de verdad transversal

Implementar `belay/errors.py` en E0 con los 17 códigos de spec §11, cada uno con `retryable` correcto (`approval_required`, `plan_expired`: retryable; `policy_denied`, `contract_missing`: no). Test transversal: toda excepción que cruza el borde del proxy es uno de los 17 códigos — cualquier traceback crudo hacia el agente es un bug (test con fuzzing ligero de inputs).

## 6. Seguridad — tests obligatorios (spec §12)

- Inyección: un tool result del upstream que contiene texto tipo "approve this action" no cambia ningún estado (los resultados son datos opacos; test).
- Contratos firmables: v0.1 solo hash-pin, pero `set_hash` en cada evento verificado en E2/E3.
- TOCTOU: cubierto por tests de expiración (E4) y re-check de `conditions` en ejecución (E6).
- Dunder/imports en expresiones (E1).
- Ledger con secretos: redacción (E2) aplicada también a payloads de compensación.

## 7. Presupuesto de rendimiento

Contrato + política + append de evento: < 5 ms p99 en local (test de humo con `pytest-benchmark`, no bloqueante en CI pero sí registrado). El overhead del proxy es el argumento de venta nº 1 frente a "esto me frena".

## 8. Matriz de trazabilidad

Mantener `docs/traceability.md`: tabla sección-de-spec → tests que la cubren, generada por un script que lee marcadores `@spec("4.6")` en los tests. La suite falla si un MUST listado en el script no tiene al menos un test. (El script incluye la lista de MUSTs extraída a mano en E0; actualizarla es parte de cambiar la spec.)

## 9. AGENTS.md (colocar literalmente en el repo, adaptado)

Reglas para el agente de desarrollo:

1. La fuente de verdad es `docs/spec.md` + este plan. Prohibido inventar semántica; ante ambigüedad, proponer cambio de spec en commit separado y esperar aprobación humana.
2. TDD estricto: test rojo antes de código. Prohibido debilitar o borrar tests para poner el CI en verde.
3. Prohibido `eval`/`exec`/plantillas en expresiones; prohibido llamar a un LLM desde `belay/`.
4. Todo artefacto público en inglés; ADRs y notas de trabajo pueden ir en español.
5. No renombrar la estructura de §2 sin ADR.
6. Cada entrega = un PR con: enlace a secciones de spec, lista de tests añadidos, salida de `pytest` y de la suite de conformidad al nivel correspondiente.
7. Definición de terminado de cada entrega = su apartado (d). La global = §0.

## 10. Guion exacto de la demo (portfolio)

```text
$ belay wrap examples/crm-mock --contracts examples/contracts/crm.yaml
$ belay run &
$ python examples/demo.py            # agente: "clean stale records"
  → plan: delete crm.record ~512 (estimate)  → verdict: pause (cap 100)
$ belay approvals list               # humano ve el plan REAL, no una paráfrasis
$ belay approvals approve ap_19 --narrow "last_seen < 2023"   # ~80 filas
  → step 17 committed (capture: 80 records snapshotted)
$ python examples/demo.py --oops     # filtro estaba mal
$ belay rewind s_7f3a --dry-run      # 1 compensación, 0 irreversibles
$ belay rewind s_7f3a --by jairo
  → compensation executed · verification passed · chain verified ✓
  → session fully compensated
```

Treinta segundos de GIF con esto en el README venden el producto mejor que mil palabras.

## 11. Después de v0.1.0 (issues a abrir, no implementar)

Adaptador SQL de dry-run real; consola web de aprobaciones; firma de contract sets (sigstore); adaptadores LangGraph/Claude Agent SDK/OpenAI Agents; port TypeScript del proxy; policy packs comunitarios; RFC para proponer `undo` como anotación MCP upstream — el final del juego: que el estándar se disuelva en el protocolo.
