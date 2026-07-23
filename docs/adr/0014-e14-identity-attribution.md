# ADR 0014: E14 — Identity attribution: who told the agent to do this

Fecha: 2026-07-23
Estado: aceptado

## Contexto

E14 implementa `docs/plan-v2.md`, sección "E14 -- Identity attribution: who
told the agent to do this". El ledger ya registra `approved_by` (spec §7.2,
E5) -- quién autorizó un paso que quedó en pausa -- pero nada registraba
*quién lanzó la sesión del agente en primer lugar*, o *en nombre de quién*.
En un despliegue con muchos empleados y muchos agentes compartiendo un solo
Belay, hoy se puede responder "qué pasó y quién aprobó el paso arriesgado"
pero no "qué humano produjo esta sesión" -- la cadena de responsabilidad que
una empresa exige antes de dar agentes a toda una plantilla.

## Decisiones

- **Belay no autentica a nadie; registra una identidad ya afirmada por el
  perímetro del despliegue.** El spec (§1) deja fuera de alcance
  "authentication/authorization of agents (gateways do this)" y E14 respeta
  esa frontera al pie de la letra: `initiated_by` es una cadena que el
  *llamador* (`belay wrap`/`belay run --initiated-by`) declara, no algo que
  Belay verifica contra ningún directorio, IdP o base de credenciales. Esto
  es una frontera de alcance deliberada, no un hueco que dé vergüenza
  admitir: exactamente el mismo patrón que §7.2 ya aplica a `approved_by`
  ("Belay does not define auth; it defines that anonymous approval is
  non-conforming"). Un operador real coloca Belay detrás de su propio
  gateway/IdP (API key con alcance por empleado, claim SSO, cuenta de
  servicio) y ese perímetro es quien realmente autentica; Belay se limita a
  **no dejar pasar una sesión sin que alguien, quien sea, haya declarado una
  identidad** -- lo mismo que ya hacía con `approved_by`, ahora aplicado al
  lado del *iniciador* en vez de solo al del *aprobador*.
- **`initiated_by` es obligatorio y ruidoso; nunca un blank silencioso.**
  `Lifecycle.start_session(initiated_by: str, on_behalf_of: str | None =
  None)` no tiene valor por defecto para `initiated_by` -- omitirlo es un
  `TypeError` en tiempo de llamada, no una sesión anónima que se cuela sin
  que nadie lo note. El operador que de verdad quiere una sesión sin
  atribución tiene que escribir el string `"unknown"` explícitamente (así lo
  hace `belay wrap`/`belay run` cuando ninguna de las dos `--initiated-by`
  se pasa: el fallback es el string literal `"unknown"`, nunca `""` ni
  `None`). Test: `test_start_session_without_initiated_by_is_a_type_error`
  en `tests/proxy/test_identity_attribution.py`.
- **`initiated_by` vs `on_behalf_of`.** `initiated_by` es la identidad que
  *técnicamente* arrancó la sesión (una API key, una cuenta de servicio, un
  scheduler). `on_behalf_of`, opcional, es el humano responsable cuando
  quien arrancó la sesión fue un proceso automatizado actuando en su nombre
  (p. ej. `initiated_by="scheduler-bot"`, `on_behalf_of="bob@corp"`). Ambos
  fluyen por el mismo mecanismo de estampado; ninguno reemplaza al otro --
  cuando un humano arranca la sesión directamente, `on_behalf_of` se deja
  en blanco (`None`), no se duplica el mismo valor en los dos campos.
- **Estampado una sola vez en `session_started`, no repetido por evento.**
  El plan permitía dos caminos: repetir la identidad en cada evento de la
  sesión, o fijarla una sola vez en `session_started` y dejar que
  `belay/ledger/replay.py`'s `SessionState` la exponga para toda la sesión.
  Se eligió la segunda: `Event.initiated_by`/`on_behalf_of` son campos
  nombrados y tipados en el modelo (promovidos desde `extra="allow"`
  incidental, como pedía el plan), pero `LedgerStore.append` solo los recibe
  como argumento cuando `Lifecycle.start_session` los pasa explícitamente
  para el evento `session_started` -- todo evento posterior de la misma
  sesión simplemente no los estampa (quedan en `None`). `replay()` los lee
  del primer `session_started` que ve y los mantiene en el `SessionState`
  resultante durante todo el fold. Esto evita el coste de almacenamiento y
  el riesgo de inconsistencia de repetir el mismo string en N eventos
  cuando la sesión es de un solo escritor (spec §8.3) y la identidad no
  cambia a media sesión -- exactamente la misma lógica por la que §4.7 fija
  `set_hash` una vez al arrancar la sesión en vez de repetirlo con
  posibilidad de divergencia.
  Coste real de esta elección: `EventRow`/`db/models.py` gana dos columnas
  nullable (`initiated_by`, `on_behalf_of`) -- una migración de esquema
  pequeña pero real, sin la cual el campo tipado en `Event` no sobreviviría
  el roundtrip por SQLite. Se aceptó ese coste (dos columnas nullable, cero
  impacto en filas existentes) en vez de esconder la identidad dentro de
  `payload` (que habría evitado tocar el esquema pero habría dejado
  `initiated_by` como un campo "no tipado de verdad", justo lo que el plan
  pedía dejar de hacer).
- **`approved_by` (E5, §7.2) queda completamente intacto.** E14 es aditivo
  sobre el lado del *iniciador*; no se tocó `belay/approvals/queue.py`, ni
  su modelo, ni sus tests. Un mismo humano puede aparecer como
  `initiated_by` de su propia sesión y luego, en teoría, como `approved_by`
  de un paso propio -- eso ya lo prohíbe §7.2/§12 (no-self-approval) por
  otro mecanismo (el agente no tiene ninguna herramienta de aprobación
  expuesta); E14 no necesita ni debe re-implementar esa regla.
- **Integración con la firma de E13: identidad dentro del resumen firmado,
  no un mecanismo paralelo.** `sign_session`'s summary (session_id,
  set_hash, chain_head_hash, event_count, signed_at) gana `initiated_by`/
  `on_behalf_of`, derivados del evento `session_started` embebido
  (`_identity_from_events`), y ambos entran en los mismos bytes que
  `key.sign()` firma. `verify_evidence` los revisa en el mismo orden de
  fallos ya establecido por E13 (chain -> coherence -> signature ->
  summary_mismatch): editar el campo del *resumen* sin re-firmar falla en
  `signature` (los bytes firmados ya no coinciden con lo que el bundle dice
  ahora); editar el campo directamente sobre el evento `session_started`
  embebido falla en `chain`, más temprano todavía, precisamente porque
  ahora es un campo nombrado del envelope y por tanto está dentro de lo que
  el hash cubre -- una garantía más fuerte que si hubiera quedado como
  metadato incidental de `payload`. Ningún camino nuevo de verificación: se
  reusa `_signed_summary`/`canonical_bytes`/`verify_chain` de E13 tal cual.
  Tests: `tests/proxy/test_identity_attribution.py::test_tamper_*`.
  Efecto secundario descubierto por el test de propiedad de Hypothesis:
  `SignedEvidence` no declaraba `extra="forbid"`, así que voltear un
  carácter en el *nombre* de un campo opcional (`on_behalf_of` ->
  `oX_behalf_of`) se ignoraba silenciosamente y el campo volvía a su valor
  por defecto (`None`) sin que `model_validate` fallara -- si el valor real
  también era `None`, la verificación pasaba igual, un falso negativo real.
  Se corrigió añadiendo `model_config = ConfigDict(extra="forbid")` a
  `SignedEvidence` (a diferencia de `Event`, que sigue siendo tolerante por
  diseño, spec §14: "evidence is tolerant, authority is strict" -- el
  *envelope* firmado sí es una superficie de autoridad, no evidencia cruda).

## Brechas conocidas / seguimiento (no resueltas, documentadas honestamente)

- **Ninguna verificación de que `initiated_by` corresponda a una identidad
  real.** Es el punto central de esta ADR, no un descuido: Belay confía en
  lo que el llamador de `belay run --initiated-by` le pasa, igual que confía
  en lo que el llamador de `belay approvals approve --by` le pasa desde E5.
  Un operador que expone `belay run` sin autenticación propia delante puede
  recibir cualquier string. La responsabilidad de que ese string sea
  verdad es, explícitamente, del despliegue, no de Belay.
- **Sin revocación/rotación de identidad dentro de una sesión.** Igual que
  `set_hash` (§4.7), `initiated_by`/`on_behalf_of` quedan fijados al
  arrancar la sesión y no pueden cambiar a media sesión -- si la identidad
  real detrás de una API key cambia de dueño mientras la sesión sigue
  abierta, Belay no lo detecta (ni lo intenta: está fuera de su modelo,
  sesiones son de un solo escritor, spec §8.3).
- **E15 (quota por identidad) depende de este campo** tal como anota
  `docs/plan-v2.md`'s sección "Sequencing" -- no se implementa aquí.

## Referencias

- `docs/spec.md` §1 (alcance: "authentication/authorization of agents...
  out of scope"), §7.2 (`approved_by`, el precedente directo), §4.7
  (`set_hash` fijado una vez al arrancar sesión, el precedente del
  "bind-once" storage choice), §14 (evidencia tolerante vs autoridad
  estricta).
- `docs/plan-v2.md`, sección "E14".
- `docs/adr/0013-e13-signed-evidence.md` (mecanismo de firma reusado sin
  duplicar).
- Código: `belay/ledger/model.py` (`Event.initiated_by`/`on_behalf_of`),
  `belay/db/models.py` (`EventRow`), `belay/ledger/store.py`
  (`LedgerStore.append`), `belay/proxy/lifecycle.py`
  (`Lifecycle.start_session`), `belay/ledger/replay.py` (`SessionState`),
  `belay/ledger/signing.py` (`_signed_summary`, `_identity_from_events`,
  `SignedEvidence.model_config`), `belay/proxy/config.py` (`WrapConfig`),
  `belay/cli/main.py` (`wrap`/`run --initiated-by`/`--on-behalf-of`,
  `verify`/`verify-evidence` output).
- Tests: `tests/proxy/test_identity_attribution.py`.
- Demo: `examples/demo_attribution.py`.
