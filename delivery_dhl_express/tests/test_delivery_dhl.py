import base64
import json
from unittest.mock import patch

import requests

from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged

from odoo.addons.delivery_dhl_express.models.dhl_client import (
    PRODUCTION_URL,
    TEST_URL,
    DhlClient,
    DhlError,
    address_payload,
    extract_products,
    package_payload,
    rate_payload,
)


class FakeResponse:
    def __init__(self, status_code=200, payload=None, reason="OK"):
        self.status_code = status_code
        self.reason = reason
        self._payload = payload if payload is not None else {}
        self.content = json.dumps(self._payload).encode()

    @property
    def ok(self):
        return 200 <= self.status_code < 300

    def json(self):
        return self._payload


def product(code, price, name=None, days=None):
    return {
        "productCode": code,
        "productName": name or code,
        "totalPrice": [
            {"currencyType": "BILLC", "priceCurrency": "BRL", "price": price},
            {"currencyType": "PULCL", "priceCurrency": "USD", "price": price / 5},
        ],
        "deliveryCapabilities": {"totalTransitDays": days},
    }


@tagged("post_install", "-at_install", "delivery_dhl_express")
class TestDhlClient(TransactionCase):

    def test_environment_follows_test_flag(self):
        self.assertEqual(DhlClient("k", "s", "1", test_mode=True).base_url, TEST_URL)
        self.assertEqual(DhlClient("k", "s", "1", test_mode=False).base_url, PRODUCTION_URL)

    def test_basic_auth_header_is_built_from_key_and_secret(self):
        client = DhlClient("chave", "segredo", "123")
        with patch.object(requests, "request", return_value=FakeResponse(payload={})) as call:
            client.rates({})
        cabecalho = call.call_args[1]["headers"]["Authorization"]
        self.assertTrue(cabecalho.startswith("Basic "))
        self.assertEqual(
            base64.b64decode(cabecalho.split(" ", 1)[1]).decode(), "chave:segredo"
        )

    def test_missing_credentials_fails_before_the_network(self):
        with patch.object(requests, "request") as call:
            with self.assertRaises(DhlError):
                DhlClient("", "", "1").rates({})
        call.assert_not_called()

    def test_error_uses_additional_details_when_present(self):
        """additionalDetails é o que diz qual campo a DHL recusou."""
        payload = {
            "detail": "Invalid request",
            "additionalDetails": ["plannedShippingDateAndTime deve ter fuso horário"],
        }
        with patch.object(requests, "request", return_value=FakeResponse(400, payload)):
            with self.assertRaises(DhlError) as caught:
                DhlClient("k", "s", "1").rates({})
        self.assertIn("fuso horário", str(caught.exception))
        self.assertEqual(caught.exception.status_code, 400)

    def test_connection_failure_becomes_dhl_error(self):
        with patch.object(requests, "request", side_effect=requests.Timeout("lento")):
            with self.assertRaises(DhlError):
                DhlClient("k", "s", "1").rates({})

    # --- payload ---------------------------------------------------------- #

    def test_package_never_sends_zero(self):
        """A DHL recusa peso ou dimensão zerada; o mínimo evita erro inútil."""
        pacote = package_payload(0, 0, 0, 0)
        self.assertGreater(pacote["weight"], 0)
        self.assertGreaterEqual(pacote["dimensions"]["length"], 1)

    def test_package_rounds_dimensions_up(self):
        pacote = package_payload(0.546, 21.0, 14.0, 1.4)
        self.assertEqual(pacote["dimensions"]["height"], 2)
        self.assertAlmostEqual(pacote["weight"], 0.546, 3)

    def test_rate_payload_carries_account_and_declared_value(self):
        corpo = rate_payload(
            shipper={"countryCode": "BR"}, receiver={"countryCode": "PT"},
            packages=[package_payload(1, 10, 10, 10)],
            planned_date="2026-09-22T13:00:00GMT+00:00",
            account_number="9876", declared_value=178.9, currency="brl",
        )
        self.assertEqual(corpo["accounts"][0]["number"], "9876")
        self.assertEqual(corpo["monetaryAmount"][0]["currency"], "BRL")
        self.assertEqual(corpo["unitOfMeasurement"], "metric")
        self.assertTrue(corpo["isCustomsDeclarable"])

    def test_address_requires_a_country(self):
        partner = self.env["res.partner"].create({"name": "Sem país", "city": "Lisboa"})
        with self.assertRaises(DhlError):
            address_payload(partner)

    def test_address_strips_the_zip_mask(self):
        partner = self.env["res.partner"].create({
            "name": "Cliente PT", "city": "Lisboa", "zip": "1250-096",
            "country_id": self.env.ref("base.pt").id,
        })
        self.assertEqual(address_payload(partner)["postalCode"], "1250096")

    # --- resposta --------------------------------------------------------- #

    def test_products_use_the_billed_currency(self):
        produtos = extract_products({"products": [product("P", 210.5, "Express Worldwide", 4)]})
        self.assertEqual(len(produtos), 1)
        self.assertAlmostEqual(produtos[0]["price"], 210.5, 2)
        self.assertEqual(produtos[0]["currency"], "BRL")
        self.assertEqual(produtos[0]["transit_days"], 4)

    def test_product_without_price_is_dropped(self):
        self.assertEqual(extract_products({"products": [{"productCode": "X"}]}), [])


@tagged("post_install", "-at_install", "delivery_dhl_express")
class TestDhlCarrier(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.company.write({
            "street": "Rua Teste, 99", "city": "Florianópolis", "zip": "88036-530",
            "country_id": cls.env.ref("base.br").id,
        })
        cls.package_type = cls.env["stock.package.type"].create({
            "name": "Caixa", "packaging_length": 24, "width": 17, "height": 5,
            "max_weight": 10,
        })
        cls.product = cls.env["product.product"].create({
            "name": "Livro", "type": "product", "list_price": 50.0, "weight": 0.3,
        })
        cls.abroad = cls.env["res.partner"].create({
            "name": "Cliente Lisboa", "street": "Rua Augusta, 10", "city": "Lisboa",
            "zip": "1100-053", "country_id": cls.env.ref("base.pt").id,
        })
        cls.carrier = cls.env["delivery.carrier"].create({
            "name": "DHL Express", "delivery_type": "dhl_express",
            "product_id": cls.env["product.product"].create({
                "name": "Frete DHL", "type": "service",
            }).id,
            "dhl_api_key": "k", "dhl_api_secret": "s", "dhl_account_number": "9876",
            "dhl_default_package_type_id": cls.package_type.id,
        })

    def _order(self, partner=None, qty=2):
        return self.env["sale.order"].create({
            "partner_id": (partner or self.abroad).id,
            "order_line": [(0, 0, {"product_id": self.product.id, "product_uom_qty": qty})],
        })

    def test_cheapest_product_wins_by_default(self):
        order = self._order()
        with patch.object(requests, "request", return_value=FakeResponse(payload={
            "products": [product("P", 320.0), product("U", 245.5)],
        })):
            result = self.carrier.rate_shipment(order)
        self.assertTrue(result["success"])
        self.assertAlmostEqual(result["price"], 245.5, 2)

    def test_fixed_product_is_honoured(self):
        self.carrier.dhl_product_code = "P"
        order = self._order()
        with patch.object(requests, "request", return_value=FakeResponse(payload={
            "products": [product("P", 320.0), product("U", 245.5)],
        })):
            result = self.carrier.rate_shipment(order)
        self.assertAlmostEqual(result["price"], 320.0, 2)

    def test_fixed_product_unavailable_falls_back(self):
        """Serviço indisponível no destino: cotar o que há é melhor que não cotar."""
        self.carrier.dhl_product_code = "INEXISTENTE"
        order = self._order()
        with patch.object(requests, "request", return_value=FakeResponse(payload={
            "products": [product("U", 245.5)],
        })):
            result = self.carrier.rate_shipment(order)
        self.assertTrue(result["success"])
        self.assertAlmostEqual(result["price"], 245.5, 2)

    def test_international_dap_warns_about_import_taxes(self):
        """Sem esse aviso o comprador é surpreendido e recusa o pacote."""
        order = self._order()
        with patch.object(requests, "request", return_value=FakeResponse(payload={
            "products": [product("P", 300.0)],
        })):
            result = self.carrier.rate_shipment(order)
        self.assertIn("destinatário", result["warning_message"])

    def test_ddp_does_not_warn(self):
        self.carrier.dhl_incoterm = "DDP"
        order = self._order()
        with patch.object(requests, "request", return_value=FakeResponse(payload={
            "products": [product("P", 300.0)],
        })):
            result = self.carrier.rate_shipment(order)
        self.assertFalse(result["warning_message"])

    def test_customs_flag_follows_the_destination(self):
        nacional = self.env["res.partner"].create({
            "name": "Cliente BR", "city": "São Paulo", "zip": "01310-100",
            "country_id": self.env.ref("base.br").id,
        })
        with patch.object(requests, "request", return_value=FakeResponse(payload={
            "products": [product("N", 40.0)],
        })) as call:
            self.carrier.rate_shipment(self._order(partner=nacional))
        self.assertFalse(call.call_args[1]["json"]["isCustomsDeclarable"])

    def test_no_coverage_does_not_raise(self):
        order = self._order()
        with patch.object(requests, "request", return_value=FakeResponse(payload={"products": []})):
            result = self.carrier.rate_shipment(order)
        self.assertFalse(result["success"])
        self.assertIn("não atende", result["error_message"])

    def test_api_failure_does_not_raise_during_checkout(self):
        order = self._order()
        with patch.object(requests, "request", side_effect=requests.ConnectionError("boom")):
            result = self.carrier.rate_shipment(order)
        self.assertFalse(result["success"])

    def test_planned_date_carries_a_timezone(self):
        """A DHL recusa plannedShippingDateAndTime sem offset."""
        quando = self.carrier._dhl_planned_date()
        self.assertIn("GMT", quando)
        self.assertIn("T", quando)

    # --- simulação e despacho --------------------------------------------- #

    def test_simulation_quotes_without_touching_the_network(self):
        self.carrier.write({"dhl_simulation": True, "dhl_simulation_price": 199.0})
        with patch.object(requests, "request") as call:
            result = self.carrier.rate_shipment(self._order())
        call.assert_not_called()
        self.assertAlmostEqual(result["price"], 199.0, 2)

    def test_simulation_refuses_to_ship(self):
        self.carrier.dhl_simulation = True
        with self.assertRaises(UserError):
            self.carrier.dhl_express_send_shipping(self.env["stock.picking"])

    def test_cancel_explains_it_must_be_done_in_mydhl(self):
        """A API não cancela envio criado; melhor dizer isso que falhar em silêncio."""
        picking = self.env["stock.picking"].new({"carrier_tracking_ref": "1234567890"})
        with self.assertRaises(UserError) as caught:
            self.carrier.dhl_express_cancel_shipment(picking)
        self.assertIn("MyDHL", str(caught.exception))

    def test_tracking_link_needs_a_number(self):
        picking = self.env["stock.picking"].new({"carrier_tracking_ref": False})
        self.assertFalse(self.carrier.dhl_express_get_tracking_link(picking))
        picking.carrier_tracking_ref = "1234567890"
        self.assertIn("1234567890", self.carrier.dhl_express_get_tracking_link(picking))
