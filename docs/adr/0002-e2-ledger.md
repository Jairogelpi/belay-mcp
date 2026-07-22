# ADR 0002: E2 — Ledger

Fecha: 2026-07-22
Estado: aceptado

## Contexto

E2 implementa `docs/spec.md` §9 (The ledger), según `docs/plan.md` sección
"E2 — Ledger (spec §9)".

## Decisiones

- **Almacenamiento: una única tabla `events` vía SQLAlchemy 2, sin
  `sessions`/`approvals`/`contract_sets` todavía.** `LedgerStore` solo
  necesita `events` para `append`/`read`/`verify`/`replay`; las demás tablas
  las introducirán las entregas que efectivamente las lean o escriban (E5+).
  Migración Alembic `0001_events` añadida sobre el andamiaje de E0.
- **Cadena de hashes partida por sesión, literal a §9.2.** `hash =
  SHA-256(canonical(event sin hash) || prev_hash)`: se serializa el envelope
  completo (incluyendo su propio `prev_hash`) en forma canónica
  (`belay/canonical.py`, ya congelada en E0/E1) y se concatena `prev_hash` en
  bruto antes de hashear, tal como dice la spec, aunque `prev_hash` ya forme
  parte del envelope serializado. El primer evento de cada `session_id`
  encadena desde un `GENESIS_HASH` centinela (`"0" * 64`), no desde cadena
  vacía, para que "sin evento previo" sea un valor explícito y comparable.
- **`verify_chain` recibe una lista de eventos, no abre la base.** Permite
  verificar subconjuntos (una sesión) o el histórico completo
  (`LedgerStore.read_all()`, añadido sobre la firma del plan porque `belay
  verify <db>` necesita "toda la base", no una sesión) con la misma función.
  Reporta el primer índice y `event_id` cuya cadena o hash no cuadre — el
  test de conformidad obligatorio (corromper el evento *k* → fallo en *k*)
  vive en `tests/ledger/test_verify.py`.
- **`verify_coherence` solo exige lo que el ledger puede saber por sí
  mismo.** §9.2 pide que un paso `committed` tenga journal, capture (si el
  contrato lo declaraba), result y compensación registrada. Como la
  verificación opera solo sobre eventos (sin acceso al contrato), no puede
  saber si un `capture` estaba *declarado*; por tanto exige
  `step_journaled`, `result_recorded`, `compensation_registered` para todo
  `step_committed`, pero no exige `state_captured` incondicionalmente. Añade
  además la regla explícita de §9.2 sobre `compensation_executed`: debe
  referenciar un paso que sí fue `committed`.
- **`redact()` usa una sal fija de módulo, no aleatoria por llamada.** §9.3
  exige que la igualdad de dos secretos iguales siga siendo comprobable tras
  redactar — una sal aleatoria por evento lo impediría. Se usa
  `sha256(sal_fija || canonical(valor))`, prefijado `sha256:`; el
  compromiso de seguridad (una sal fija es más débil que una sal por
  secreto/rotable) queda anotado en el propio código como límite conocido.
- **`replay()` es un fold puro sobre la lista de eventos**, sin I/O, sin
  reloj, sin acceso a tools — determinismo por construcción, verificado con
  una propiedad Hypothesis que compara dos ejecuciones sobre la misma
  secuencia generada.
- **Tolerancia a campos desconocidos (§14):** `Event` usa
  `model_config = ConfigDict(extra="allow")` (al contrario que los modelos
  de contrato de E1, que son `extra="forbid"`): la evidencia es tolerante,
  la autoridad es estricta. `payload` es un dict libre, así que campos
  nuevos ahí sobreviven a un ciclo escritura/lectura sin cambios.

## Referencias

- `docs/spec.md` §9 (ledger), §14 (versionado y tolerancia).
- `docs/plan.md` sección "E2 — Ledger (spec §9)".
- Código: `belay/ledger/{model,store,verify,replay,redact}.py`,
  `belay/db/models.py`, `belay/db/migrations/versions/0001_events.py`,
  `belay/cli/main.py` (`belay verify`).
- Tests: `tests/ledger/test_{store,verify,replay,redact,unknown_fields}.py`,
  `tests/cli/test_verify.py`.
