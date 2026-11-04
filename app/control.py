"""Centro de control: la configuración del negocio que el código hace cumplir.

Se guarda en la tabla `settings` (clave `control_center`) y se edita desde el panel.
El modelo no puede saltarse estas reglas: las aplican el orquestador y las herramientas.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, field_validator

DAYS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]


class AreaConfig(BaseModel):
    enabled: bool = True
    temperature: float = Field(default=0.3, ge=0.0, le=1.0)


class Hours(BaseModel):
    open: str = "08:00"
    close: str = "18:00"

    @field_validator("open", "close")
    @classmethod
    def _hhmm(cls, v: str) -> str:
        datetime.strptime(v, "%H:%M")
        return v


class BankAccount(BaseModel):
    bank: str
    number: str
    holder: str
    kind: str = "corriente"


class Template(BaseModel):
    """Plantilla aprobada en Meta. `body` es el texto con {{1}}, {{2}}... para mostrar en el panel."""

    name: str
    language: str = "es"
    category: str = "utility"
    body: str
    params: int = 0


class Plan(BaseModel):
    name: str
    mbps: int
    price_usd: float
    description: str = ""


class ControlCenter(BaseModel):
    company: str = "PAALNET"
    privacy_url: str = "https://example.com/privacidad"
    timezone: str = "America/Guayaquil"
    currency: str = "USD"

    bot_enabled: bool = True  # interruptor de emergencia global
    areas: dict[str, AreaConfig] = Field(default_factory=lambda: {
        "cobros": AreaConfig(temperature=0.1),
        "soporte": AreaConfig(temperature=0.2),
        "ventas": AreaConfig(temperature=0.6),
        "general": AreaConfig(temperature=0.3),
    })

    # Cobros
    months_collectable: int = Field(default=0, ge=0, le=24)  # 0 = toda la deuda; N = solo los N meses más recientes
    payment_mismatch: str = Field(default="asesor", pattern="^(asesor|registrar)$")  # monto ≠ deuda
    receipt_max_age_days: int = Field(default=30, ge=1, le=365)
    extension_max_days: int = Field(default=5, ge=0, le=30)  # prórroga que el bot puede aprobar sola
    accounts: list[BankAccount] = Field(default_factory=lambda: [
        BankAccount(bank="Banco Pichincha", number="2200123456", holder="PAALNET S.A."),
        BankAccount(bank="Cooperativa JEP", number="406012345678", holder="PAALNET S.A.", kind="ahorros"),
    ])

    # Ventas
    plans: list[Plan] = Field(default_factory=lambda: [
        Plan(name="Hogar 100 Mbps", mbps=100, price_usd=25.0, description="Fibra óptica, instalación gratis"),
        Plan(name="Hogar 200 Mbps", mbps=200, price_usd=35.0, description="Fibra óptica, router wifi 6"),
        Plan(name="Gamer 300 Mbps", mbps=300, price_usd=45.0, description="Baja latencia, IP pública"),
        Plan(name="Pyme 200 Mbps", mbps=200, price_usd=45.0, description="Factura con RUC, soporte prioritario"),
    ])

    # Soporte
    reboot_cooldown_minutes: int = Field(default=15, ge=1, le=240)
    low_power_dbm: float = -27.0

    # Atención humana
    hours: dict[str, Hours | None] = Field(default_factory=lambda: {
        "lunes": Hours(), "martes": Hours(), "miércoles": Hours(), "jueves": Hours(), "viernes": Hours(),
        "sábado": Hours(open="09:00", close="13:00"), "domingo": None,
    })
    # Feriados nacionales de Ecuador 2026. Verifícalos con el calendario oficial: el Gobierno
    # traslada algunos cada año.
    holidays: list[str] = Field(default_factory=lambda: [
        "2026-01-01", "2026-02-16", "2026-02-17", "2026-04-03", "2026-05-01", "2026-05-25",
        "2026-08-10", "2026-10-09", "2026-11-02", "2026-11-03", "2026-12-25",
    ])

    templates: list[Template] = Field(default_factory=lambda: [
        Template(name="recordatorio_pago", body="Hola {{1}}, te recordamos que tu factura de {{2}} por ${{3}} vence el {{4}}.", params=4),
        Template(name="confirmacion_pago", body="Hola {{1}}, registramos tu pago de ${{2}}. ¡Gracias!", params=2),
        Template(name="aviso_corte", body="Hola {{1}}, el {{2}} habrá un corte programado en tu zona de {{3}}.", params=3),
        Template(name="visita_tecnica", body="Hola {{1}}, tu visita técnica quedó agendada para el {{2}}.", params=2),
        Template(name="retomar_conversacion", body="Hola {{1}}, te escribimos sobre tu consulta. ¿Podemos ayudarte en algo más?", params=1),
    ])

    # --- utilidades -------------------------------------------------------------

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    def now(self) -> datetime:
        return datetime.now(self.tz)

    def area_enabled(self, area: str) -> bool:
        cfg = self.areas.get(area)
        return self.bot_enabled and bool(cfg and cfg.enabled)

    def is_open(self, when: datetime | None = None) -> bool:
        when = (when or self.now()).astimezone(self.tz)
        if when.date().isoformat() in self.holidays:
            return False
        h = self.hours.get(DAYS[when.weekday()])
        if h is None:
            return False
        t = when.time()
        return _t(h.open) <= t < _t(h.close)

    def next_opening(self, when: datetime | None = None) -> datetime | None:
        when = (when or self.now()).astimezone(self.tz)
        for i in range(0, 15):
            day = (when + timedelta(days=i)).date()
            h = self.hours.get(DAYS[day.weekday()])
            if h is None or day.isoformat() in self.holidays:
                continue
            opening = datetime.combine(day, _t(h.open), tzinfo=self.tz)
            closing = datetime.combine(day, _t(h.close), tzinfo=self.tz)
            if i == 0 and when >= closing:
                continue
            return max(opening, when) if i == 0 else opening
        return None

    def account_is_ours(self, number: str) -> bool:
        clean = "".join(ch for ch in number if ch.isdigit())
        return any(clean and clean == "".join(ch for ch in a.number if ch.isdigit()) for a in self.accounts)

    def template(self, name: str) -> Template | None:
        return next((t for t in self.templates if t.name == name), None)


def _t(hhmm: str) -> time:
    return datetime.strptime(hhmm, "%H:%M").time()


def describe_opening(cc: ControlCenter, when: datetime | None = None) -> str:
    """Frase para el cliente sobre cuándo lo atenderá una persona."""
    when = when or cc.now()
    if cc.is_open(when):
        return "Un asesor te responderá por aquí en breve."
    nxt = cc.next_opening(when)
    if nxt is None:
        return "Un asesor te responderá por aquí lo antes posible."
    day = "hoy" if nxt.date() == when.date() else (
        "mañana" if nxt.date() == when.date() + timedelta(days=1) else f"el {DAYS[nxt.weekday()]} {nxt.day}")
    return f"Ahora estamos fuera de horario. Un asesor te responderá {day} desde las {nxt:%H:%M}."


def months_ago(today: date, months: int) -> str:
    y, m = today.year, today.month - months
    while m <= 0:
        m += 12
        y -= 1
    return f"{y:04d}-{m:02d}"
