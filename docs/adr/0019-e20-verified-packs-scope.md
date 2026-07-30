# ADR 0019: E20 — Verified action packs, alcance de esta entrega

Fecha: 2026-07-30
Estado: aceptado

## Contexto

`BELAY_V1_COMPLETE_SPEC.md`, sección 11 ("Verified action packs") y E20
("Belay provee transacciones reales y útiles, no solo un framework").
La especificación completa de un "pack" (§11.1) es sustancial: nombre,
semver, versión de esquema, licencia, publisher, repositorio fuente y
revisión inmutable, versiones de Belay/upstream soportadas, esquemas de
entrada exactos, clasificación lectura/mutación, efectos declarados,
procedimiento de captura de pre-estado, procedimiento de compensación,
verificación post-acción y post-compensación, semántica de idempotencia
y reintento, credenciales requeridas, reglas de redacción, timeouts,
fixtures/tests de contrato/integración/sandbox destructivo, matriz de
plataforma, y firma criptográfica con metadata de proveniencia. Además,
§11.4 exige un índice de registro FIRMADO y versionado, digests
inmutables, comprobación de revocación en instalación/actualización,
instantáneas offline firmadas con marca de frescura, y una CLI de
instalación de packs.

Antes de esta entrega no existía nada de esto en el repo — solo un
`.github/ISSUE_TEMPLATE/propose-contract-pack.yaml` describiendo un
concepto mucho más liviano ("contract pack" = un YAML de `Contract`s en
`examples/contracts/`, validado contra el JSON Schema de spec Appendix A,
sin ningún concepto de firma/registro/trust-state).

Antes de escribir código se preguntó al usuario por el alcance, dado que
el criterio de salida de E20 exige mutación real, fallo parcial
inyectado, compensación y verificación **contra un servicio/repositorio
real y desechable** — no mocks. Filesystem y Git se pueden verificar así
enteramente en local; GitHub necesita credenciales de API reales; Odoo
necesita una instancia real (o desechable). El usuario eligió: **solo
Filesystem + Git en esta pasada**.

## Decisiones

- **Se construyó el núcleo de valor real (contratos reales, probados de
  verdad contra servidores MCP reales, con captura+compensación
  funcionando de punta a punta), NO la infraestructura de packaging
  completa de §11.** Concretamente:
  - `packs/filesystem/contracts.yaml` y `packs/git/contracts.yaml`:
    conjuntos de `Contract` (formato `belay_contract: '0.1'` ya existente,
    cargado por el `belay/contracts/loader.py` YA probado — ningún loader
    nuevo) apuntando a servidores MCP oficiales reales:
    `@modelcontextprotocol/server-filesystem` (npm) y `mcp-server-git`
    (PyPI).
  - `packs/*/pack.yaml`: metadata descriptiva (nombre, versión, publisher,
    upstream verificado, `trust_state: unverified`, limitaciones
    conocidas) — es documentación, **no** validada ni cargada por ningún
    código todavía. Se dice explícitamente en el propio archivo, no se
    finge que hay un loader.
  - `tests/packs/test_filesystem_pack.py` / `test_git_pack.py`: el
    criterio de salida de E20 aplicado literalmente — conectan al
    servidor real vía `connect_stdio`, corren una saga real de varios
    pasos con `SagaExecutor` (el mismo mecanismo que
    `tests/executor/test_crm_mock_acceptance.py` ya probó para el mock de
    CRM), inyectan un fallo real en un paso intermedio, y verifican en
    disco (no solo en el reporte del saga) que `auto_compensate` dejó el
    filesystem/repo git en su estado exacto original. Un tercer test por
    pack verifica que la lista de tools que el servidor real anuncia
    (`list_tools()`) coincide exactamente con las tools que el pack
    declara — deriva silenciosa entre pack y upstream real, detectada,
    no asumida.
  - **NO se construyó:** índice de registro firmado, comprobación de
    revocación, `belay pack install`/CLI de instalación, SDK de
    autoría, ni los trust states `official-verified`/
    `community-verified`/`revoked` (§11.2) — esa es infraestructura de
    release/hosting real (¿dónde vive el registro? ¿qué raíz de
    confianza lo firma? ¿quién opera el servicio de revocación?) que no
    se puede decidir unilateralmente ni construir de forma verificable
    en una sola pasada. `trust_state: unverified` en ambos `pack.yaml`
    refleja esto con honestidad: la falta de esa infraestructura, no una
    duda sobre si el pack funciona (sí funciona, con test real).

- **PACK-001 Filesystem: la heurística de `belay draft-contracts` se
  corrigió a mano en varios puntos reales, no se aceptó tal cual.**
  `edit_file` y `move_file` fueron marcadas irreversibles por la
  heurística ("no read counterpart") pero SÍ tienen undo bien definido
  (`edit_file` vía captura `read_file` + undo `write_file`; `move_file`
  vía intercambio source/destination). `create_directory`'s draft
  proponía un undo sin sentido (`create_directory` con un argumento
  `content` que esa tool no acepta) — corregido a `irreversible`
  honestamente: el servidor real no expone NINGUNA tool de
  borrado/remove, así que crear un archivo nuevo (`write_file` sobre un
  path que no existía) o un directorio nuevo no tiene camino de undo
  posible via este servidor, punto. `write_file` se declaró
  `conditional` (reversible solo si el archivo ya existía antes) en vez
  de mentir con `reversible` incondicional o bloquear la creación de
  archivos nuevos con `irreversible` incondicional — verificado
  empíricamente que una captura fallida (`read_file` sobre un archivo
  inexistente, que responde `isError: true`) no aborta el paso en el
  camino de ejecución real (`belay/proxy/server.py`'s executor nunca
  lanza excepción sobre `isError`, solo la devuelve).

- **PACK-002 Git: alcance más pequeño que Filesystem, y es un hecho real
  sobre el servidor upstream, no una limitación de esta pasada de
  autoría.** Descubierto empíricamente, no asumido: `mcp-server-git`
  devuelve SOLO texto plano (`CallToolResult.structuredContent` siempre
  `None`) — la gramática de expresiones de contratos
  (`belay/contracts/expressions.py`) solo soporta acceso por path a
  datos estructurados, deliberadamente sin parsing de strings/regex
  (spec §4.3: "no código definido por el usuario en contratos"). Eso
  significa que ningún undo que necesite referenciar un valor capturado
  específico (el SHA exacto que era HEAD, el nombre exacto de la rama
  activa antes de un checkout) se puede expresar contra este servidor en
  particular — no hay nada estructurado a lo que apuntar. La única
  excepción real: `git_add`'s undo es `git_reset`, que no toma ningún
  argumento más allá de `repo_path` — no necesita referenciar estado
  capturado, así que la limitación de texto plano nunca aplica.
  `git_commit`, `git_create_branch`, `git_checkout`, y `git_reset` (a sí
  mismo) se declaran `irreversible` honestamente por esta razón exacta,
  documentada en el propio `pack.yaml`, no escondida.

## Consecuencias

- Dos packs reales, cargables, y probados de punta a punta contra
  servidores MCP oficiales reales — la promesa central de E20 ("Belay
  provee transacciones reales y útiles") cumplida de verdad, no
  simulada.
- GitHub (PACK-003) y Odoo (PACK-004) quedan sin construir — necesitan
  credenciales/instancia real que este entorno no tiene; retomar cuando
  estén disponibles, siguiendo el mismo patrón de verificación real de
  esta ADR, no mocks.
- La infraestructura de packaging de §11 completa (registro firmado,
  trust states, revocación, CLI de instalación, SDK de autoría) sigue
  sin construir — trabajo real y separado, con decisiones de
  infraestructura/hosting propias, no algo para asumir unilateralmente
  ni apurar junto a la autoría de contratos.
