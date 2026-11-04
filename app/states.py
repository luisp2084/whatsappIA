"""Máquina de estados de una conversación.

    bot ──deriva/AGENTE──▶ pendiente_humano ──tomar──▶ asignada ──cerrar──▶ cerrada
     ▲                                                                       │
     └──────────────────────── el cliente vuelve a escribir ─────────────────┘

Además, un asesor puede tomar o escribir en una conversación que está con el bot
(pasa a `asignada`) y cerrar una pendiente sin tomarla.
"""

from __future__ import annotations

BOT = "bot"
PENDING = "pendiente_humano"
ASSIGNED = "asignada"
CLOSED = "cerrada"
STATES = (BOT, PENDING, ASSIGNED, CLOSED)
HUMAN_STATES = (PENDING, ASSIGNED)

# evento -> {estado_origen: estado_destino}
TRANSITIONS: dict[str, dict[str, str]] = {
    "escalate": {BOT: PENDING, CLOSED: PENDING},
    "take": {BOT: ASSIGNED, PENDING: ASSIGNED, ASSIGNED: ASSIGNED, CLOSED: ASSIGNED},
    "agent_message": {BOT: ASSIGNED, PENDING: ASSIGNED, ASSIGNED: ASSIGNED, CLOSED: ASSIGNED},
    "close": {PENDING: CLOSED, ASSIGNED: CLOSED, BOT: CLOSED},
    "customer_message": {CLOSED: BOT},
    "release": {PENDING: BOT, ASSIGNED: BOT},  # devolver al bot sin cerrar el caso
}


class InvalidTransition(Exception):
    pass


def next_state(current: str, event: str) -> str:
    """Estado tras `event`. Si el evento no aplica al estado actual, lanza InvalidTransition,
    salvo `customer_message`, que no cambia nada fuera de `cerrada`."""
    table = TRANSITIONS.get(event)
    if table is None:
        raise InvalidTransition(f"evento desconocido: {event}")
    if current in table:
        return table[current]
    if event == "customer_message":
        return current
    raise InvalidTransition(f"no se puede '{event}' desde '{current}'")


# Pestañas del panel: qué estados muestra cada una.
TABS = {
    "pendientes": (PENDING,),
    "mias": (ASSIGNED,),  # además filtradas por assigned_to = usuario actual
    "todas": (BOT, PENDING, ASSIGNED),
    "cerradas": (CLOSED,),
}
