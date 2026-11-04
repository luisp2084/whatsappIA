# Runbooks

Qué hacer ante cada incidente. En todos, el primer paso para proteger a los clientes es el mismo:
**apagar el bot** con el interruptor de la barra superior del panel (admin u owner). Los mensajes nuevos
pasan a la pestaña *Pendientes* y los atienden asesores; nada se pierde.

| Incidente | Señal | Primeros pasos |
| --- | --- | --- |
| [La IA no responde](#la-ia-no-responde) | Avisos grises *"La IA no pudo responder"*, métrica Errores, evento `agent_error` | Leer el código del aviso; ver la tabla de errores del README |
| [WhatsApp no entrega](#whatsapp-no-entrega) | Avisos *"No se pudo entregar"*, eventos `send_error`, `wa_status_failed` | Revisar token, calidad del número y límites de Meta |
| [Pagos duplicados o dudosos](#pagos-duplicados-o-dudosos) | Reclamos, auditoría `payment.registered` repetida | Apagar el área de cobros; conciliar con Network Control |
| [La OLT o Network Control no responden](#la-olt-o-network-control-no-responden) | Errores de herramientas `OLT_NO_RESPONDE`, `UPSTREAM_TIMEOUT` | Revisar la VPN y la API intermedia |
| [Corte masivo](#corte-masivo) | Pico de «no tengo internet» en una zona | Registrar el incidente en Network Control para que el bot lo informe |

## La IA no responde

1. Abre una conversación afectada: el aviso gris trae el código del error de Gemini.
2. `404 NOT_FOUND`: el modelo se retiró. Cambia `GEMINI_MODEL` a uno vigente y vuelve a desplegar el worker.
   `429`: cuota agotada; sube el límite en Google Cloud o espera. `400 API key not valid`: rota la clave en Secret Manager.
3. Mientras tanto, el bot envía un mensaje de disculpa con la opción `AGENTE`. Si el problema dura más de 15 minutos, apaga el bot.

## WhatsApp no entrega

1. Los envíos fallidos se reintentan solos hasta 6 veces (15 s, 30 s, 1 min… hasta 15 min). Los que agotan los
   intentos quedan como aviso en la conversación.
2. Error `190` o `401`: el token de System User caducó o se revocó; genera otro y actualiza `wa-token`.
3. Error `131047` (fuera de la ventana de 24 h): usa una plantilla desde el panel.
4. Errores `131048`/`131056` o calidad del número en rojo en WhatsApp Manager: Meta limita el número por bloqueos o
   reportes de usuarios. Reduce los envíos proactivos y revisa las plantillas de marketing.

## Pagos duplicados o dudosos

1. Apaga el área **cobros** en el Centro de control: los pagos pasan a asesores.
2. En Auditoría, filtra las acciones `payment.registered` y `payment.apply_failed`.
3. La base de datos impide registrar dos veces el mismo comprobante (banco + número y hash de la imagen). Un
   `payment.apply_failed` significa que el pago quedó guardado pero Network Control no lo aplicó: aplícalo a mano.
4. Si el problema fue una respuesta del bot, márcala como mala y crea la regla en Revisión; añade el caso a `evals/`.

## La OLT o Network Control no responden

1. El bot nunca dice que el servicio está bien si la OLT falla: ofrece visita o asesor.
2. Comprueba el túnel o VPN hacia la red del ISP y la API intermedia.
3. Si la caída se prolonga, apaga el área **soporte** (o **cobros** si es Network Control).

## Corte masivo

1. Registra el incidente de la zona en Network Control con la hora estimada de solución: `estado_ont` lo informa
   y `crear_ticket_visita` deja de agendar visitas en esa zona.
2. Si llegan cientos de mensajes, el agrupamiento de ráfagas y el bloqueo por conversación evitan respuestas
   duplicadas; vigila la latencia y las cuotas de Gemini en Métricas.
3. Al resolverse, envía la plantilla `aviso_corte` o una de seguimiento a los afectados desde el panel.
