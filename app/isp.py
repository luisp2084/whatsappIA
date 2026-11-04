"""Integraciones del ISP: Network Control (facturación), OLT y lectura de comprobantes.

Cada integración es un `Protocol`. Las implementaciones `Fake*` usan datos de ejemplo de PAALNET
y sirven para la demo, los tests y las evaluaciones. Para conectar los sistemas reales, escribe
una clase con los mismos métodos (por ejemplo `HttpNetworkControl`) y selecciónala en
`build_isp()` con la variable ISP_BACKEND. No hay una implementación HTTP incluida porque depende
de la API concreta de Network Control y de la API intermedia hacia la OLT (ver la guía, 3.1).
"""

from __future__ import annotations

import copy
import json
import os
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from typing import Any, Protocol

from google.genai import types
from pydantic import BaseModel, Field


@dataclass
class Contract:
    id: str
    customer_id: str  # cédula o RUC
    customer_name: str
    plan: str
    monthly_cents: int
    branch: str
    zone: str
    address: str
    status: str  # activo | suspendido


@dataclass
class Installment:
    month: str  # AAAA-MM
    concept: str
    amount_cents: int
    due_date: str  # AAAA-MM-DD


@dataclass
class Incident:
    zone: str
    description: str
    eta: str


class NetworkControl(Protocol):
    async def customer(self, customer_id: str) -> dict[str, Any] | None: ...
    async def customers_by_phone(self, phone: str) -> list[dict[str, Any]]: ...
    async def contracts(self, customer_id: str) -> list[Contract]: ...
    async def contract(self, contract_id: str) -> Contract | None: ...
    async def installments(self, contract_id: str) -> list[Installment]: ...
    async def apply_payment(self, contract_id: str, amount_cents: int, reference: str) -> dict[str, Any]: ...
    async def grant_extension(self, contract_id: str, until: str) -> dict[str, Any]: ...
    async def create_ticket(self, contract_id: str, problem: str, address: str) -> str: ...
    async def active_incident(self, zone: str) -> Incident | None: ...


class OLT(Protocol):
    async def ont_status(self, contract_id: str) -> dict[str, Any]: ...
    async def reboot(self, contract_id: str) -> dict[str, Any]: ...


class ReceiptData(BaseModel):
    """Datos que se extraen de un comprobante de transferencia o depósito."""

    es_comprobante: bool = Field(description="True si la imagen es un comprobante de pago bancario")
    banco: str = ""
    numero: str = Field(default="", description="Número de comprobante o referencia")
    monto: float = 0.0
    fecha: str = Field(default="", description="Fecha de la transacción en formato AAAA-MM-DD")
    cuenta_destino: str = Field(default="", description="Número de cuenta que recibió el dinero")
    titular_destino: str = ""
    confianza: float = Field(default=0.0, ge=0.0, le=1.0, description="0 a 1: qué tan legible y fiable es")


class ReceiptReader(Protocol):
    async def read(self, data: bytes, mime_type: str) -> ReceiptData: ...


# --- Datos de ejemplo -------------------------------------------------------------------------


def _month(today: date, back: int) -> tuple[str, str]:
    y, m = today.year, today.month - back
    while m <= 0:
        m += 12
        y -= 1
    return f"{y:04d}-{m:02d}", f"{y:04d}-{m:02d}-05"


def _sample(today: date) -> dict[str, Any]:
    def debt(contract_cents: int, months: int, extra: list[Installment] | None = None) -> list[Installment]:
        items = []
        for back in range(months - 1, -1, -1):
            month, due = _month(today, back)
            items.append(Installment(month=month, concept="Servicio de internet", amount_cents=contract_cents, due_date=due))
        return items + (extra or [])

    contracts = {
        "C-1001": Contract("C-1001", "0102030405", "María Fernanda Cedeño", "Hogar 100 Mbps", 2500, "Cuenca",
                           "Cuenca Centro", "Av. Solano 3-45", "activo"),
        "C-1005": Contract("C-1005", "0102030405", "María Fernanda Cedeño", "Pyme 200 Mbps", 4500, "Cuenca",
                           "Cuenca Centro", "Calle Larga 7-12 (local)", "activo"),
        "C-1002": Contract("C-1002", "0912345678", "Jorge Andrade", "Hogar 200 Mbps", 3500, "Cuenca",
                           "El Batán", "Calle Los Cedros 214", "suspendido"),
        "C-1003": Contract("C-1003", "1104567890", "Luis Pacheco", "Gamer 300 Mbps", 4500, "Cuenca",
                           "Totoracocha", "Av. Hurtado de Mendoza 9-80", "activo"),
    }
    return {
        "customers": {
            "0102030405": {"id": "0102030405", "name": "María Fernanda Cedeño", "phones": ["593999000001"]},
            "0912345678": {"id": "0912345678", "name": "Jorge Andrade", "phones": ["593987654321"]},
            "1104567890": {"id": "1104567890", "name": "Luis Pacheco", "phones": ["593998887766"]},
        },
        "contracts": contracts,
        "installments": {
            "C-1001": debt(2500, 2),
            "C-1005": debt(4500, 1),
            "C-1002": debt(3500, 3, [Installment(_month(today, 0)[0], "Reconexión", 500, _month(today, 0)[1])]),
            "C-1003": [],
        },
        "ont": {
            "C-1001": {"en_linea": True, "potencia_dbm": -19.5, "ultima_caida": None},
            "C-1005": {"en_linea": True, "potencia_dbm": -21.2, "ultima_caida": None},
            "C-1002": {"en_linea": False, "potencia_dbm": None, "ultima_caida": (today - timedelta(days=12)).isoformat()},
            "C-1003": {"en_linea": True, "potencia_dbm": -28.4, "ultima_caida": (today - timedelta(days=1)).isoformat()},
        },
        "incidents": {"El Batán": Incident("El Batán", "Corte de fibra troncal en el sector", "hoy a las 18:00")},
    }


class FakeNetworkControl:
    def __init__(self, today: date | None = None):
        self.data = _sample(today or date.today())
        self.tickets: list[dict[str, Any]] = []
        self.extensions: list[dict[str, Any]] = []
        self.payments: list[dict[str, Any]] = []

    async def customer(self, customer_id: str) -> dict[str, Any] | None:
        return copy.deepcopy(self.data["customers"].get(customer_id.strip()))

    async def customers_by_phone(self, phone: str) -> list[dict[str, Any]]:
        digits = "".join(c for c in phone if c.isdigit())
        return [copy.deepcopy(c) for c in self.data["customers"].values() if digits in c["phones"]]

    async def contracts(self, customer_id: str) -> list[Contract]:
        return [copy.copy(c) for c in self.data["contracts"].values() if c.customer_id == customer_id]

    async def contract(self, contract_id: str) -> Contract | None:
        c = self.data["contracts"].get(contract_id.strip().upper())
        return copy.copy(c) if c else None

    async def installments(self, contract_id: str) -> list[Installment]:
        return [copy.copy(i) for i in self.data["installments"].get(contract_id, [])]

    async def apply_payment(self, contract_id: str, amount_cents: int, reference: str) -> dict[str, Any]:
        pending = self.data["installments"].setdefault(contract_id, [])
        remaining, applied = amount_cents, []
        while pending and remaining >= pending[0].amount_cents:
            inst = pending.pop(0)
            remaining -= inst.amount_cents
            applied.append(f"{inst.concept} {inst.month}")
        contract = self.data["contracts"][contract_id]
        balance = sum(i.amount_cents for i in pending)
        status = contract.status
        if contract.status == "suspendido" and balance == 0:
            contract.status = "activo"
            status = "reconexión en curso"
        self.payments.append({"contract": contract_id, "amount_cents": amount_cents, "reference": reference})
        return {"aplicado_a": applied, "saldo_cents": balance, "saldo_a_favor_cents": remaining,
                "estado_servicio": status}

    async def grant_extension(self, contract_id: str, until: str) -> dict[str, Any]:
        self.extensions.append({"contract": contract_id, "until": until})
        return {"aprobada": True, "hasta": until}

    async def create_ticket(self, contract_id: str, problem: str, address: str) -> str:
        ticket = f"T-{2400 + len(self.tickets) + 1}"
        self.tickets.append({"id": ticket, "contract": contract_id, "problem": problem, "address": address})
        return ticket

    async def active_incident(self, zone: str) -> Incident | None:
        return self.data["incidents"].get(zone)


class FakeOLT:
    def __init__(self, nc: FakeNetworkControl):
        self.nc = nc
        self.reboots: list[str] = []

    async def ont_status(self, contract_id: str) -> dict[str, Any]:
        status = self.nc.data["ont"].get(contract_id)
        if status is None:
            raise LookupError("ONT no encontrada")
        return dict(status)

    async def reboot(self, contract_id: str) -> dict[str, Any]:
        self.reboots.append(contract_id)
        return {"reiniciada": True, "espera_minutos": 3}


class GeminiReceiptReader:
    """Extrae los datos del comprobante con Gemini (visión) y salida JSON validada."""

    PROMPT = ("Extrae los datos de este comprobante de pago bancario de Ecuador. Si no es un comprobante, "
              "responde es_comprobante=false. No inventes datos que no se lean con claridad: deja el campo vacío "
              "y baja la confianza.")

    def __init__(self, client: Any, model: str):
        self.client = client
        self.model = model

    async def read(self, data: bytes, mime_type: str) -> ReceiptData:
        resp = await self.client.aio.models.generate_content(
            model=self.model,
            contents=[types.Content(role="user", parts=[types.Part.from_bytes(data=data, mime_type=mime_type),
                                                         types.Part.from_text(text=self.PROMPT)])],
            config=types.GenerateContentConfig(temperature=0.0, response_mime_type="application/json",
                                               response_schema=ReceiptData))
        if resp.parsed is not None:
            return resp.parsed if isinstance(resp.parsed, ReceiptData) else ReceiptData.model_validate(resp.parsed)
        return ReceiptData.model_validate_json(resp.text or "{}")


class FakeReceiptReader:
    """Para demo y tests: la "imagen" es un JSON con los campos de ReceiptData."""

    async def read(self, data: bytes, mime_type: str) -> ReceiptData:
        try:
            return ReceiptData.model_validate(json.loads(data.decode()))
        except (ValueError, UnicodeDecodeError):
            return ReceiptData(es_comprobante=False, confianza=0.0)


@dataclass
class ISP:
    nc: NetworkControl
    olt: OLT
    receipts: ReceiptReader
    extra: dict[str, Any] = field(default_factory=dict)


def build_isp(client: Any = None, model: str = "") -> ISP:
    backend = os.environ.get("ISP_BACKEND", "fake")
    if backend != "fake":
        raise RuntimeError(f"ISP_BACKEND={backend!r} no está implementado: añade tu adaptador en app/isp.py")
    nc = FakeNetworkControl()
    reader: ReceiptReader = GeminiReceiptReader(client, model) if client is not None and model else FakeReceiptReader()
    return ISP(nc=nc, olt=FakeOLT(nc), receipts=reader)


def contract_dict(c: Contract) -> dict[str, Any]:
    d = asdict(c)
    d["mensualidad_usd"] = round(d.pop("monthly_cents") / 100, 2)
    d.pop("customer_id")
    return d
