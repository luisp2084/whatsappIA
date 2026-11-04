"""Pruebas extremo a extremo del panel con Playwright, contra `python -m app.demo`.

    pip install playwright && python -m playwright install --with-deps chromium
    python -m pytest e2e -q

Si ya tienes Chromium instalado en otra ruta, indícala con E2E_CHROMIUM=/ruta/al/chrome.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest
from playwright.sync_api import Page, expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def base_url():
    port = _free_port()
    env = {**os.environ, "PORT": str(port), "DEBOUNCE_SECONDS": "0.3"}
    env.pop("GEMINI_API_KEY", None)  # E2E siempre con el modelo de reglas: resultados deterministas
    proc = subprocess.Popen([sys.executable, "-m", "app.demo"], cwd=ROOT, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    url = f"http://127.0.0.1:{port}"
    deadline = time.time() + 30
    while time.time() < deadline:
        try:
            if urllib.request.urlopen(f"{url}/demo-info", timeout=1).status == 200:
                break
        except OSError:
            time.sleep(0.3)
    else:
        proc.kill()
        pytest.fail("El servidor de demo no arrancó:\n" + (proc.stdout.read() if proc.stdout else ""))
    time.sleep(1.0)  # siembra de datos de ejemplo
    yield url
    proc.terminate()
    proc.wait(timeout=10)


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as p:
        kwargs = {"executable_path": os.environ["E2E_CHROMIUM"]} if os.environ.get("E2E_CHROMIUM") else {}
        b = p.chromium.launch(**kwargs)
        yield b
        b.close()


@pytest.fixture
def page(browser):
    ctx = browser.new_context(viewport={"width": 1280, "height": 860})
    pg = ctx.new_page()
    errors: list[str] = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    yield pg
    ctx.close()
    assert not errors, f"Errores de JavaScript: {errors}"


def login(page: Page, base_url: str, role_index: int) -> None:
    """role_index: 0 owner, 1 admin, 2 asesor (botones de la pista de demo)."""
    page.goto(base_url)
    page.locator("#login-hint button").nth(role_index).click()
    expect(page.locator("#app")).to_be_visible()


def say(page: Page, text: str) -> None:
    page.fill("#demo-text", text)
    page.press("#demo-text", "Enter")


def test_cobro_con_comprobante_y_revision(page: Page, base_url: str):
    login(page, base_url, 1)
    page.select_option("#demo-customer", "593999000001")
    say(page, "cuánto debo")
    expect(page.locator(".msg.bot").last).to_contain_text("varios contratos", timeout=10_000)
    say(page, "Contrato C-1001")
    expect(page.locator(".msg.bot").last).to_contain_text("Total", timeout=10_000)
    page.click("#demo-receipt")
    expect(page.locator(".msg.bot").last).to_contain_text("Registré tu pago", timeout=10_000)

    # Marcar como mala -> Revisión -> regla -> activar
    first_bot = page.locator(".msg.bot").nth(1)
    first_bot.hover()
    first_bot.locator(".linkbtn").click()
    page.fill(".flag-form textarea", "Debe pedir la cédula antes")
    page.click(".flag-form button[type=submit]")
    expect(page.locator(".msg.bot.flagged")).to_have_count(1)
    page.click(".tab[data-view=review]")
    card = page.locator(".review").first
    card.get_by_role("button", name="Proponer regla con IA").click()
    expect(card.locator("textarea")).not_to_have_value("")
    assert "cédula" in card.locator("textarea").input_value()
    card.get_by_role("button", name="Crear regla").click()
    row = page.locator("#view-review tbody tr").first
    expect(row).to_contain_text("debe pedir la cédula antes")
    row.locator("input[type=checkbox]").check()
    expect(row).to_contain_text("Activa")


def test_asesor_solo_ve_lo_permitido(page: Page, base_url: str):
    login(page, base_url, 2)
    tabs = page.locator("#nav .tab")
    expect(tabs).to_have_text(["Conversaciones", "Centro de control", "Métricas"])
    expect(page.locator("#bot-switch")).to_be_disabled()
    page.click(".tab[data-view=control]")
    expect(page.locator("#view-control")).to_contain_text("Solo lectura")
    expect(page.locator("#view-control input").first).to_be_disabled()


def test_tomar_responder_y_cerrar(page: Page, base_url: str):
    login(page, base_url, 2)
    page.click(".chip[data-tab=pendientes]")
    page.locator(".conv", has_text="Jorge").click()
    expect(page.locator("#chat-summary")).to_contain_text("Motivo de la derivación")
    page.click("#act-take")
    expect(page.locator("#chat-state")).to_have_text("Asignada a Marta (asesora)")
    page.fill("#composer-text", "Hola Jorge, soy Marta. Reviso tu caso.")
    page.click("#send")
    expect(page.locator(".msg.agent").last).to_contain_text("soy Marta")
    page.click("#act-close")
    expect(page.locator("#chat-state")).to_have_text("Cerrada")


def test_movil_sin_scroll_horizontal(browser, base_url: str):
    ctx = browser.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True)
    page = ctx.new_page()
    login(page, base_url, 2)
    page.click(".chip[data-tab=todas]")
    page.locator(".conv").first.click()
    expect(page.locator(".msg").first).to_be_visible()
    overflow = page.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth")
    assert overflow <= 1, f"La página se desplaza {overflow}px en horizontal"
    ctx.close()
