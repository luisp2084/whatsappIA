# Agente de WhatsApp con Gemini

Worker de referencia: **WhatsApp Cloud API → webhook → cola → worker → Gemini (function calling) → respuesta**,
más un **panel web** para atender conversaciones como agente humano y ver métricas.
Python 3.12+, FastAPI, Redis y `google-genai`. El panel es HTML/CSS/JS sin build ni dependencias.

```
app/
  demo.py        Modo demo: todo el sistema sin WhatsApp ni Redis (`python -m app.demo`)
  main.py        Endpoints: GET/POST /webhook, POST /tasks/pubsub, GET /healthz; monta el panel en /admin
  admin.py       API del panel (/api/admin/*): conversaciones, responder, tomar/devolver control, métricas
  web/           Panel web (index.html, styles.css, app.js)
  worker.py      Debounce de ráfagas, comandos (STOP, AGENTE, BORRAR MIS DATOS), handoff, métricas
  agent.py       Bucle de function calling, modelo de respaldo, historial (recorte + sin binarios)
  tools.py       Declaraciones de herramientas + implementaciones MOCK (sustitúyelas por tu OMS/CRM)
  prompts.py     System prompt versionado y textos fijos
  whatsapp.py    Cliente Graph API: texto, botones, leído + "escribiendo…", descarga de media, reintentos
  session.py     Redis: sesión con TTL, deduplicación, buffer, lock por usuario, opt-out
  queue.py       LocalQueue (asyncio) o PubSubQueue (ordering key = wa_id)
  security.py    Firma HMAC del webhook, seudonimización, redacción de PII
  parsing.py     Payload del webhook → IncomingMessage / StatusEvent
  formatting.py  Markdown → formato WhatsApp, troceo a 4096 caracteres
tests/           27 tests (fakes de Gemini, WhatsApp y Redis; no hace falta red)
```

## Qué hace un turno

1. `POST /webhook` valida `X-Hub-Signature-256`, publica cada mensaje en la cola y responde 200. Nunca llama al LLM.
2. El worker descarta duplicados (`SET NX` de `message.id`), mete el mensaje en el buffer del usuario y toma
   un lock por usuario. Así se garantiza el orden y solo hay un procesador por conversación.
3. Espera `DEBOUNCE_SECONDS` y agrupa la ráfaga ("hola" / "quiero" / "devolver algo") en una sola llamada.
4. Marca el mensaje como leído y muestra "escribiendo…".
5. Los comandos deterministas no pasan por el LLM: `STOP`/`BAJA`, `ALTA`, `AGENTE`, `BORRAR MIS DATOS`
   y el botón con id `human`.
6. Si la sesión está en `mode=human`, el mensaje se reenvía al inbox humano y el bot no responde.
7. En otro caso, Gemini recibe el texto, las fotos, las notas de voz y los PDF del usuario, y ejecuta herramientas hasta en 5 pasos:
   - `offer_options` → botones de respuesta rápida
   - `escalate_to_human` → `mode=human`, aviso al usuario y webhook a tu inbox con resumen e historial
   - el resto → herramientas de negocio con timeout de 5 s; los errores vuelven al modelo como `{"error": ...}`
8. El primer mensaje incluye el aviso de IA y el enlace a la política de privacidad.
9. Se guarda el historial sin binarios y recortado, conservando los `thought_signature` que Gemini necesita.
   Después se emite un evento JSON sin PII (`user` seudonimizado) con latencias, herramientas usadas y versión del prompt.

Si Gemini falla se reintenta con `GEMINI_FALLBACK_MODEL`; si también falla, el usuario recibe un mensaje
de disculpa con la opción de hablar con un agente.

## Probarlo en la nube con GitHub Codespaces (sin instalar nada)

1. En la página del repositorio pulsa **Code → Codespaces → Create codespace on main**.
2. Espera 2–3 minutos: se instalan las dependencias y arranca el sistema en **modo demo** (`python -m app.demo`).
3. Se abre una pestaña con el panel. Si no aparece, ve a la pestaña **Ports** de abajo y abre el puerto **8080**.
4. Entra con el token **`demo`** (ya viene escrito).
5. En la barra **Modo demo**, escribe como si fueras un cliente. Por ejemplo:
   - `hola`
   - `¿dónde está mi pedido TX-123456?`
   - `quiero hablar con una persona`
   Verás la respuesta del bot y el paso a humano en tiempo real.

En el modo demo, WhatsApp y Redis se simulan en memoria: no se envía nada a nadie y los datos se pierden al
parar. La IA usa reglas simples. **Para probar Gemini de verdad**, crea una clave en
[Google AI Studio](https://aistudio.google.com/apikey) y guárdala en GitHub → Settings → Codespaces →
Secrets como `GEMINI_API_KEY` (con acceso a este repositorio). Después reinicia el codespace.

Si el bot contesta *"Ahora mismo tengo problemas para responder…"*, la llamada a Gemini está fallando.
El motivo exacto aparece en el panel, dentro de la conversación, como un aviso gris *"La IA no pudo responder: …"*.
Al arrancar, la demo también comprueba la clave y el modelo, y avisa en la barra "Modo demo" y en la terminal. Causas habituales:

| Lo que verás | Causa | Solución |
| --- | --- | --- |
| `400 INVALID_ARGUMENT API key not valid` | Clave mal copiada o borrada | Crea otra en AI Studio y actualiza el secreto |
| `404 NOT_FOUND … models/… is not found` | Modelo retirado o no disponible para tu cuenta | Define el secreto `GEMINI_MODEL` con un modelo vigente ([lista](https://ai.google.dev/gemini-api/docs/models)) |
| `429 RESOURCE_EXHAUSTED` | Límite del plan gratuito alcanzado | Espera unos minutos o activa la facturación en AI Studio |
| `403 PERMISSION_DENIED` | La API no está habilitada para el proyecto de la clave | Crea la clave desde AI Studio, que la habilita sola |

Si cierras la terminal, vuelve a arrancarlo con:

```bash
python -m app.demo
```

Codespaces es gratuito hasta un límite de horas al mes. Detén el codespace cuando no lo uses.

## Panel web (`/admin`)

Entra en `https://<tu-servicio>/admin/` con `ADMIN_TOKEN`.

- **Conversaciones:** lista en vivo (se actualiza cada 4 s) con búsqueda y filtros. Un punto rojo marca
  las conversaciones con humano cuyo último mensaje es del cliente, es decir, las que esperan respuesta.
- **Chat:** historial completo con burbujas para cliente, bot, agente y eventos del sistema, y la cuenta atrás de la ventana de 24 h.
  - *Tomar control* pausa el bot. *Devolver al bot* lo reactiva.
  - Escribir como agente toma el control automáticamente. El mensaje se envía por WhatsApp y
    se añade al historial del LLM para que el bot tenga contexto al recuperar la conversación.
  - Con la ventana cerrada, el envío se bloquea (HTTP 409), porque WhatsApp exige una plantilla.
- **Métricas:** turnos de IA, escaladas, latencia media, errores, mensajes de agentes, gráfico diario,
  uso de herramientas y tabla por día (7, 14 o 30 días). Son contadores agregados en Redis sin PII y se conservan 90 días.
- Funciona en móvil (la lista y el chat se alternan) y en modo oscuro.

La transcripción del panel (`transcript:<wa_id>`) caduca con la sesión (`SESSION_TTL_SECONDS`) y se borra
con `BORRAR MIS DATOS`. En producción, protege `/admin` y `/api/admin` además con SSO
(Cloud IAP, Cloudflare Access) y usa un `ADMIN_TOKEN` largo y aleatorio.

## Ejecutar en local

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
```

```bash
.venv/bin/python -m pytest -q
```

```bash
docker run -d -p 6379:6379 redis:7
```

```bash
cp .env.example .env   # rellena las variables
```

```bash
set -a; . ./.env; set +a; .venv/bin/uvicorn app.main:build --factory --port 8080
```

Expón el puerto con un túnel HTTPS (por ejemplo `cloudflared tunnel --url http://localhost:8080`).
En Meta Developers → WhatsApp → Configuration → Webhook, pega `https://<túnel>/webhook` y tu
`WA_VERIFY_TOKEN`, y suscríbete al campo `messages`. Escribe al número de prueba desde un teléfono verificado.

## Desplegar en Google Cloud Run con Pub/Sub

```bash
gcloud pubsub topics create wa-inbound
```

```bash
gcloud run deploy wa-agent --source . --region europe-west1 --min-instances 1 \
  --set-env-vars QUEUE_MODE=pubsub,PUBSUB_TOPIC=projects/$PROJECT/topics/wa-inbound,REDIS_URL=redis://<ip-memorystore>:6379/0 \
  --set-secrets WA_TOKEN=wa-token:latest,WA_APP_SECRET=wa-app-secret:latest,WA_VERIFY_TOKEN=wa-verify:latest,GEMINI_API_KEY=gemini-key:latest,PSEUDONYM_PEPPER=pepper:latest,PUBSUB_PUSH_TOKEN=push-token:latest,ADMIN_TOKEN=admin-token:latest \
  --network default --subnet default \
  --allow-unauthenticated
```

```bash
gcloud pubsub subscriptions create wa-inbound-push --topic wa-inbound \
  --enable-message-ordering --ack-deadline 120 \
  --push-endpoint "https://<url-cloud-run>/tasks/pubsub?token=<PUSH_TOKEN>" \
  --dead-letter-topic wa-inbound-dlq --max-delivery-attempts 5
```

Notas:
- `--allow-unauthenticated` es necesario porque Meta llama a `/webhook`, que queda protegido por la firma HMAC.
  Para endurecer `/tasks/pubsub`, despliega el worker como un segundo servicio privado y usa push con token OIDC.
- `--ack-deadline` debe cubrir el debounce más la llamada al LLM y las herramientas.
- Para Memorystore hace falta salida a la VPC (Direct VPC egress, como arriba, o un conector).
- En AWS, el equivalente es API Gateway + Lambda/ECS, SQS FIFO (`MessageGroupId = wa_id`) y ElastiCache.

## Antes de producción

- [ ] Sustituir los mocks de `tools.py` por tu OMS/CRM y **comprobar que cada pedido pertenece al `wa_id`**.
- [ ] Ajustar `prompts.py` (marca, horarios, enlace de privacidad) y pasar la suite de evaluación.
- [ ] Atender desde el panel `/admin`, o conectar `HANDOFF_WEBHOOK_URL` a otro inbox (Chatwoot, Zendesk) y llamar a `/handoff/release` al cerrar cada caso.
- [ ] Poner el panel detrás de SSO; el token compartido no identifica a cada agente.
- [ ] `BORRAR MIS DATOS`: verificar la identidad y propagar el borrado a CRM, analítica y backups.
- [ ] Mensajes fuera de la ventana de 24 h: solo con plantillas aprobadas (no incluido aquí).
- [ ] Usar Gemini de pago o Vertex AI para que los datos no se usen para entrenar.
