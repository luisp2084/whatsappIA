# Agente de WhatsApp con IA para un ISP

Agente de cobros, soporte y ventas para un proveedor de internet (caso PAALNET, Ecuador), sobre la
**WhatsApp Cloud API** y **Gemini**, con un **panel web** para asesores. Implementa la guía
*"Agente de IA para WhatsApp (whatsappIA): creación, prueba y despliegue"*.

Python 3.12, FastAPI, SQLAlchemy (PostgreSQL en producción, SQLite en desarrollo), Redis y `google-genai`.
El panel es HTML/CSS/JS sin build.

```
app/
  main.py        App FastAPI; ROLE elige qué monta cada servicio (webhook, worker, sender, console)
  worker.py      Orquestador: ráfagas, comandos, estados, área, agente, outbox, acciones del panel
  agent.py       Bucle de function calling (máx. 5 pasos), modelo de respaldo, historial
  areas.py       Áreas (cobros, soporte, ventas, general), sus herramientas y el clasificador
  prompts.py     Prompts por área versionados (cobros@v1…), hechos del negocio, reglas, contexto
  tools.py       Herramientas del ISP; validan todo en el servidor
  isp.py         Adaptadores de Network Control, OLT y lector de comprobantes (simulados + Gemini)
  control.py     Centro de control: interruptores, cobros, horario, feriados, cuentas, planes, plantillas
  outbox.py      Envío idempotente, reintentos y guardarraíl contra mensajes repetidos
  states.py      Máquina de estados: bot → pendiente_humano → asignada → cerrada
  db.py, repo.py Base de datos durable: conversaciones, mensajes, outbox, pagos, auditoría, usuarios, reglas
  session.py     Redis: deduplicación, ráfagas, locks, historial de la IA, límites, métricas
  auth.py        Usuarios, roles (owner, admin, asesor), contraseñas y sesiones
  admin.py       API del panel (/api/auth, /api/admin)
  revision.py    Propuesta de reglas a partir de respuestas marcadas como malas
  demo.py        Modo demo completo sin servicios externos (`python -m app.demo`)
  web/           Panel: bandeja, Revisión, Centro de control, Métricas, Usuarios, Auditoría
evals/           Conjunto dorado (casos YAML) y ejecutor de evaluaciones
e2e/             Pruebas del panel con Playwright
tests/           Pruebas unitarias y de integración
docs/runbooks/   Qué hacer ante incidentes
```

## Probarlo en la nube con GitHub Codespaces

1. En la página del repositorio pulsa **Code → Codespaces → Create codespace on main**.
2. Espera 2–3 minutos: se instalan las dependencias y arranca el **modo demo**.
3. Se abre el panel. Si no aparece, en la pestaña **Ports** abre el puerto **8080**.
4. En la pantalla de acceso pulsa un usuario de ejemplo (owner, admin o asesora). La contraseña es `demo123456`.
5. En la barra **Modo demo** elige un cliente de ejemplo y escribe como él:

| Cliente | Situación | Prueba |
| --- | --- | --- |
| María Fernanda Cedeño | 2 contratos; C-1001 debe 2 meses | «cuánto debo», luego «Contrato C-1001», luego **Simular comprobante** |
| Jorge Andrade | Servicio suspendido, corte de fibra en su zona | «no tengo internet» |
| Luis Pacheco | Al día, potencia óptica baja | «el internet está lento», luego «Sí, agendar» |
| Cliente nuevo | No es cliente | «qué planes tienen» |

En la demo, WhatsApp, Redis, la base de datos, Network Control y la OLT son simulados: no se envía nada
a nadie y todo se pierde al parar. Sin clave, la IA usa un **modelo de reglas** que imita los flujos.
Para probar **Gemini real**, guarda una clave de [Google AI Studio](https://aistudio.google.com/apikey) en
GitHub → Settings → Codespaces → Secrets como `GEMINI_API_KEY` y reinicia el codespace. Con Gemini real,
**Adjuntar comprobante** lee una foto de verdad.

Si el bot contesta *"Ahora mismo tengo problemas para responder…"*, la llamada a Gemini está fallando y el
motivo aparece en la conversación como aviso gris *"La IA no pudo responder: …"*:

| Lo que verás | Causa | Solución |
| --- | --- | --- |
| `400 INVALID_ARGUMENT API key not valid` | Clave mal copiada o borrada | Crea otra en AI Studio y actualiza el secreto |
| `404 NOT_FOUND … models/… is not found` | Modelo retirado o no disponible para tu cuenta | Define `GEMINI_MODEL` con un modelo vigente ([lista](https://ai.google.dev/gemini-api/docs/models)) |
| `429 RESOURCE_EXHAUSTED` | Límite del plan gratuito alcanzado | Espera unos minutos o activa la facturación |
| `403 PERMISSION_DENIED` | La API no está habilitada para el proyecto de la clave | Crea la clave desde AI Studio |

Para volver a arrancar la demo desde la terminal, instala primero las dependencias (tras un `git pull` puede
haber librerías nuevas; si no, verás `ModuleNotFoundError`):

```bash
pip install -r requirements-dev.txt
```

```bash
python -m app.demo
```

## Cómo atiende un mensaje

1. `POST /webhook` valida la firma `X-Hub-Signature-256`, publica en la cola y responde 200 sin llamar a la IA.
2. El worker descarta duplicados, toma el bloqueo de la conversación y agrupa la ráfaga (`DEBOUNCE_SECONDS`, 4 s).
3. Registra los mensajes en la base de datos. Si la conversación estaba `cerrada`, vuelve a `bot`.
4. Comandos sin IA: `STOP`/`BAJA`, `ALTA`, `AGENTE`, `BORRAR MIS DATOS` (pide un código de confirmación).
5. Si la conversación está con un humano (`pendiente_humano` o `asignada`), el bot no responde.
6. Si el **interruptor del bot** está apagado o el área está desactivada, deriva a un asesor.
7. El clasificador elige el área. Se arma el prompt del área con los hechos del Centro de control, las reglas
   activas de Revisión y la fecha de Ecuador, y el agente solo ve las herramientas de esa área.
8. Las herramientas validan en el servidor: el contrato debe pertenecer al número que escribe, los montos salen
   de Network Control y del comprobante validado, y se respetan los límites (meses cobrables, prórrogas, reinicios).
9. La respuesta va al **outbox** con una clave por turno: se envía una sola vez, se reintenta si WhatsApp falla
   y se bloquea si repite un texto enviado hace menos de 10 minutos.
10. Si el agente deriva o agota 5 pasos, la conversación pasa a `pendiente_humano` con un resumen para el asesor.

## Panel web (`/admin`)

| Vista | Quién | Qué hace |
| --- | --- | --- |
| Conversaciones | todos | Pestañas Pendientes, Mías, Todas y Cerradas. Tomar, responder, devolver al bot, cerrar. Plantillas con la ventana de 24 h cerrada. «Marcar como mala» en cada respuesta del bot |
| Revisión | admin, owner | Reportes de respuestas malas → regla (escrita o propuesta con IA) → activar. Las reglas activas entran en el prompt del área |
| Centro de control | todos (editar: admin, owner) | Áreas y temperatura, meses cobrables, monto distinto, prórroga, reinicios, potencia baja, horario, feriados, cuentas, planes, plantillas |
| Métricas | todos | Turnos, derivaciones, pagos, latencia, errores, reportes, duplicados bloqueados, pendientes > 30 min, uso y errores de herramientas, costo de Meta |
| Usuarios | owner | Altas, roles, desactivar, restablecer contraseña |
| Auditoría | admin, owner | Acciones de asesores, cambios de configuración, pagos, reinicios y tickets |

El **interruptor del bot** de la barra superior (admin y owner) manda todos los mensajes nuevos a asesores.

## Pruebas y evaluaciones

```bash
python -m pytest -q
```

```bash
python -m evals.run
```

```bash
python -m pytest e2e -q
```

- `tests/`: herramientas, outbox, estados, worker, API y permisos, demo. Sin red.
- `evals/`: el conjunto dorado. Con `GEMINI_API_KEY` evalúa el modelo real; sin clave, el modelo de reglas.
  Cada respuesta marcada como mala en Revisión debe añadirse como caso en `evals/casos/`. Formato y campos al
  inicio de `evals/casos/cobros.yaml`.
- `e2e/`: el panel en un navegador real. Requiere `pip install playwright` y `python -m playwright install chromium`.
- CI: `tests.yml` ejecuta las tres en cada push. `evals.yml` ejecuta las evaluaciones con Gemini real en los
  pull requests que tocan prompts, herramientas, áreas o configuración (necesita el secreto `GEMINI_API_KEY`).

## Ejecutar en local con WhatsApp real

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
```

```bash
docker run -d -p 6379:6379 redis:7
```

```bash
cp .env.example .env   # rellena las variables, incluidas SECRET_KEY, OWNER_EMAIL y OWNER_PASSWORD
```

```bash
set -a; . ./.env; set +a; .venv/bin/uvicorn app.main:build --factory --port 8080
```

Expón el puerto con un túnel HTTPS (por ejemplo `cloudflared tunnel --url http://localhost:8080`). En Meta
Developers → WhatsApp → Configuration → Webhook, pega `https://<túnel>/webhook` y tu `WA_VERIFY_TOKEN`, y
suscríbete al campo `messages`. Entra al panel con `OWNER_EMAIL`, crea los usuarios de los asesores y
configura el Centro de control: cuentas, horario, planes y plantillas aprobadas.

## Conectar Network Control y la OLT

`app/isp.py` define las interfaces `NetworkControl`, `OLT` y `ReceiptReader`. Las clases `Fake*` usan datos
de ejemplo. Para producción escribe un adaptador con los mismos métodos contra la API de Network Control y
contra una API intermedia hacia la OLT (nunca expongas la gestión de la OLT a internet), y selecciónalo en
`build_isp()` con `ISP_BACKEND`. El lector de comprobantes con Gemini (`GeminiReceiptReader`) ya es real.

## Desplegar en Google Cloud

La misma imagen sirve para todos los servicios; `ROLE` elige qué monta cada uno:

| Servicio | `ROLE` | Acceso | Notas |
| --- | --- | --- | --- |
| `wa-webhook` | `webhook` | público | Valida la firma y publica en Pub/Sub. `--min-instances 1` |
| `wa-worker` | `worker` | privado | Push de Pub/Sub con OIDC, `--ack-deadline 120` |
| `wa-sender` | `sender` | privado | Reintenta el outbox. Una instancia basta |
| `wa-console` | `console` | detrás de Cloud IAP | Panel y API |

Datos: Cloud SQL PostgreSQL (`DATABASE_URL=postgresql+asyncpg://…`), Memorystore Redis, Secret Manager para
`WA_TOKEN`, `WA_APP_SECRET`, `GEMINI_API_KEY`, `SECRET_KEY` y `PSEUDONYM_PEPPER`. Las tablas se crean al
arrancar. Cuando el esquema cambie en producción, pasa a migraciones con Alembic.

```bash
gcloud pubsub topics create wa-inbound
```

```bash
gcloud run deploy wa-webhook --source . --region us-east1 --min-instances 1 --allow-unauthenticated \
  --set-env-vars ROLE=webhook,QUEUE_MODE=pubsub,PUBSUB_TOPIC=projects/$PROJECT/topics/wa-inbound,REDIS_URL=redis://<ip-memorystore>:6379/0,DATABASE_URL=postgresql+asyncpg://<usuario>@<ip-cloudsql>/whatsappia \
  --set-secrets WA_TOKEN=wa-token:latest,WA_APP_SECRET=wa-app-secret:latest,WA_VERIFY_TOKEN=wa-verify:latest,SECRET_KEY=secret-key:latest,PSEUDONYM_PEPPER=pepper:latest \
  --network default --subnet default
```

Despliega `wa-worker`, `wa-sender` y `wa-console` igual, cambiando `ROLE`, sin `--allow-unauthenticated`
y añadiendo `GEMINI_API_KEY` a los secretos del worker. Después crea la suscripción push hacia el worker:

```bash
gcloud pubsub subscriptions create wa-inbound-push --topic wa-inbound \
  --enable-message-ordering --ack-deadline 120 \
  --push-endpoint "https://<url-wa-worker>/tasks/pubsub" --push-auth-service-account <cuenta>@$PROJECT.iam.gserviceaccount.com \
  --dead-letter-topic wa-inbound-dlq --max-delivery-attempts 5
```

## Antes de producción

- [ ] Adaptadores reales de Network Control y la OLT en `app/isp.py` (con VPN o túnel hacia la red del ISP).
- [ ] Centro de control: cuentas reales, horario, feriados del año, planes y enlace a la política de privacidad.
- [ ] Plantillas aprobadas en WhatsApp Manager con los mismos nombres que en el Centro de control.
- [ ] Conjunto dorado con 150–300 conversaciones reales anonimizadas; `evals.yml` en verde con Gemini real.
- [ ] Panel detrás de Cloud IAP; `SECRET_KEY` larga y aleatoria; contraseña del owner cambiada.
- [ ] Gemini de pago o Vertex AI, para que los datos no se usen para entrenar (LOPDP).
- [ ] Alertas de Cloud Monitoring sobre los eventos `agent_error`, `tool_error`, `send_error`,
      `duplicate_blocked` y la métrica de pendientes > 30 min (ver `docs/runbooks/`).
