import base64
import json
import re
from unittest.mock import patch

import requests

from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged

from odoo.addons.delivery_dhl_express.models.dhl_client import (
    API_VERSION,
    PRODUCTION_URL,
    TEST_URL,
    DhlClient,
    DhlError,
    extract_products,
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


def product(code, price, name=None, days=None, currency="BRL"):
    return {
        "productCode": code,
        "productName": name or code,
        "totalPrice": [
            {"currencyType": "BILLC", "priceCurrency": currency, "price": price},
            {"currencyType": "PULCL", "priceCurrency": "USD", "price": price / 5},
        ],
        "deliveryCapabilities": {"totalTransitDays": days},
    }


SHIPMENT = {
    "shipmentTrackingNumber": "1234567890",
    "packages": [{"referenceNumber": 1, "trackingNumber": "JD0001"}],
    "documents": [
        {"imageFormat": "PDF", "typeCode": "label",
         "content": base64.b64encode(b"%PDF etiqueta").decode()},
        {"imageFormat": "PDF", "typeCode": "invoice",
         "content": base64.b64encode(b"%PDF fatura").decode()},
    ],
}


@tagged("post_install", "-at_install", "delivery_dhl_express")
class TestDhlClient(TransactionCase):

    def test_environment_follows_test_flag(self):
        self.assertEqual(DhlClient("k", "s", "1", test_mode=True).base_url, TEST_URL)
        self.assertEqual(DhlClient("k", "s", "1", test_mode=False).base_url, PRODUCTION_URL)

    def test_every_call_carries_basic_auth_and_the_api_version(self):
        with patch.object(requests, "request", return_value=FakeResponse(payload={})) as call:
            DhlClient("chave", "segredo", "123").rates({})
        cabecalhos = call.call_args[1]["headers"]
        self.assertEqual(
            base64.b64decode(cabecalhos["Authorization"].split()[1]).decode(), "chave:segredo"
        )
        self.assertEqual(cabecalhos["x-version"], API_VERSION, "header obrigatório")
        self.assertLessEqual(len(cabecalhos["Message-Reference"]), 36)

    def test_missing_credentials_fails_before_the_network(self):
        with patch.object(requests, "request") as call:
            with self.assertRaises(DhlError):
                DhlClient("", "", "1").rates({})
        call.assert_not_called()

    def test_error_uses_additional_details_when_present(self):
        payload = {"title": "Bad request", "detail": "Validation failed", "status": "400",
                   "additionalDetails": ["receiverDetails.contactInformation.phone is missing"]}
        with patch.object(requests, "request", return_value=FakeResponse(400, payload)):
            with self.assertRaises(DhlError) as caught:
                DhlClient("k", "s", "1").rates({})
        self.assertEqual(caught.exception.status_code, 400)
        self.assertIn("phone is missing", str(caught.exception))
        self.assertIn("Validation failed", str(caught.exception))

    def test_connection_failure_becomes_dhl_error(self):
        with patch.object(requests, "request", side_effect=requests.ConnectionError("x")):
            with self.assertRaises(DhlError):
                DhlClient("k", "s", "1").rates({})

    def test_shipment_without_account_fails_before_the_network(self):
        with patch.object(requests, "request") as call:
            with self.assertRaises(DhlError):
                DhlClient("k", "s", "").create_shipment({})
        call.assert_not_called()

    def test_cancel_pickup_sends_requestor_and_reason(self):
        with patch.object(requests, "request", return_value=FakeResponse(payload={})) as call:
            DhlClient("k", "s", "1").cancel_pickup("PRG123", "Fulana", "Envio cancelado")
        args, kwargs = call.call_args
        self.assertEqual(args[0], "DELETE")
        self.assertTrue(args[1].endswith("/pickups/PRG123"))
        self.assertEqual(kwargs["params"], {"requestorName": "Fulana", "reason": "Envio cancelado"})

    def test_products_use_the_billed_currency(self):
        produtos = extract_products({"products": [product("P", 250.0, days=4)]})
        self.assertEqual(produtos[0]["price"], 250.0)
        self.assertEqual(produtos[0]["currency"], "BRL")
        self.assertEqual(produtos[0]["transit_days"], 4)

    def test_product_without_price_is_dropped(self):
        self.assertEqual(extract_products({"products": [{"productCode": "P"}]}), [])


class DhlCarrierCase(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        br = cls.env.ref("base.br")
        cls.env.company.write({
            "street": "Rua Idalino Rosso, 478", "city": "Içara", "zip": "88829-124",
            "state_id": cls.env.ref("base.state_br_sc").id, "country_id": br.id,
            "vat": "66.903.932/0001-52", "phone": "(48) 9830-5099",
        })
        cls.env.company.partner_id.tz = "America/Sao_Paulo"
        brl = cls.env.ref("base.BRL")
        brl.active = True
        cls.env.company.currency_id = brl
        cls.box_small = cls.env["stock.package.type"].create({
            "name": "Caixa P", "packaging_length": 240, "width": 170, "height": 50,
            "base_weight": 0.08, "max_weight": 1,
        })
        cls.box_large = cls.env["stock.package.type"].create({
            "name": "Caixa G", "packaging_length": 400, "width": 300, "height": 250,
            "base_weight": 0.35, "max_weight": 15,
        })
        cls.book = cls.env["product.product"].create({
            "name": "Livro", "type": "consu", "list_price": 50.0, "weight": 0.3,
        })
        cls.buyer = cls.env["res.partner"].create({
            "name": "Ana Lisboa", "street": "Rua Augusta, 100", "city": "Lisboa",
            "zip": "1100-053", "country_id": cls.env.ref("base.pt").id,
            "phone": "+351 912 345 678", "email": "ana@example.com",
        })
        cls.national_buyer = cls.env["res.partner"].create({
            "name": "Leitora", "street": "Av. Paulista, 1000", "city": "São Paulo",
            "zip": "01310-100", "state_id": cls.env.ref("base.state_br_sp").id,
            "country_id": br.id, "phone": "(11) 98888-7777",
        })
        cls.carrier = cls.env["delivery.carrier"].create({
            "name": "DHL", "delivery_type": "dhl_express",
            "product_id": cls.env["product.product"].create({
                "name": "Frete DHL", "type": "service",
            }).id,
            "dhl_api_key": "k", "dhl_api_secret": "s", "dhl_account_number": "960000000",
            "dhl_package_type_ids": [(6, 0, [cls.box_small.id, cls.box_large.id])],
            "dhl_default_package_type_id": cls.box_large.id,
            "dhl_default_hs_code": "4901.99.00",
        })

    def _order(self, qty=2, partner=None):
        return self.env["sale.order"].create({
            "partner_id": (partner or self.buyer).id,
            "order_line": [(0, 0, {"product_id": self.book.id, "product_uom_qty": qty,
                                   "tax_id": [(5, 0, 0)]})],
        })

    def _rate(self, order, *products):
        resposta = FakeResponse(payload={"products": list(products)})
        with patch.object(requests, "request", return_value=resposta) as call:
            return self.carrier.rate_shipment(order), call


@tagged("post_install", "-at_install", "delivery_dhl_express")
class TestDhlRating(DhlCarrierCase):

    def test_cheapest_product_wins_by_default(self):
        result, _call = self._rate(self._order(), product("P", 320.0), product("U", 280.0))
        self.assertTrue(result["success"])
        self.assertAlmostEqual(result["price"], 280.0, 2)

    def test_fixed_product_is_honoured(self):
        self.carrier.dhl_product_code = "P"
        result, _call = self._rate(self._order(), product("P", 320.0), product("U", 280.0))
        self.assertAlmostEqual(result["price"], 320.0, 2)

    def test_fixed_product_unavailable_falls_back(self):
        self.carrier.dhl_product_code = "X"
        result, _call = self._rate(self._order(), product("P", 320.0))
        self.assertTrue(result["success"])
        self.assertAlmostEqual(result["price"], 320.0, 2)

    def test_rate_payload_follows_the_api(self):
        _result, call = self._rate(self._order(qty=2), product("P", 300.0))
        corpo = call.call_args[1]["json"]
        remetente = corpo["customerDetails"]["shipperDetails"]
        self.assertEqual(remetente, {"postalCode": "88829124", "cityName": "Içara",
                                     "countryCode": "BR", "provinceCode": "SC"})
        self.assertEqual(corpo["customerDetails"]["receiverDetails"]["countryCode"], "PT")
        self.assertTrue(corpo["isCustomsDeclarable"])
        self.assertEqual(corpo["accounts"], [{"typeCode": "shipper", "number": "960000000"}])
        self.assertEqual(corpo["monetaryAmount"][0]["value"], 100.0)
        pacote = corpo["packages"][0]
        # 2 livros de 0,3 kg + caixa P de 0,08 kg; caixa de 240 mm vai como 24 cm
        self.assertAlmostEqual(pacote["weight"], 0.68, 3)
        self.assertEqual(pacote["dimensions"], {"length": 24.0, "width": 17.0, "height": 5.0})

    def test_planned_date_is_shipper_local_time_with_offset(self):
        _result, call = self._rate(self._order(), product("P", 300.0))
        data = call.call_args[1]["json"]["plannedShippingDateAndTime"]
        self.assertRegex(data, r"^\d{4}-\d{2}-\d{2}T10:00:00GMT-03:00$")
        self.assertLessEqual(len(data), 29)

    def test_price_in_another_billing_currency_is_converted(self):
        usd = self.env.ref("base.USD")
        usd.active = True
        self.env["res.currency.rate"].create({
            "currency_id": usd.id, "rate": 0.2, "company_id": self.env.company.id,
        })
        result, _call = self._rate(self._order(), product("P", 50.0, currency="USD"))
        self.assertAlmostEqual(result["price"], 250.0, 1)

    def test_international_dap_warns_about_import_taxes(self):
        result, _call = self._rate(self._order(), product("P", 300.0))
        self.assertIn("destinatário", result["warning_message"])

    def test_ddp_does_not_warn(self):
        self.carrier.dhl_incoterm = "DDP"
        result, _call = self._rate(self._order(), product("P", 300.0))
        self.assertFalse(result["warning_message"])

    def test_international_only_hides_the_method_for_brazil(self):
        self.assertFalse(self.carrier._is_available_for_order(
            self._order(partner=self.national_buyer)))
        self.assertTrue(self.carrier._is_available_for_order(self._order()))
        self.carrier.dhl_international_only = False
        self.assertTrue(self.carrier._is_available_for_order(
            self._order(partner=self.national_buyer)))

    def test_no_coverage_does_not_raise(self):
        result, _call = self._rate(self._order())
        self.assertFalse(result["success"])
        self.assertIn("não atende", result["error_message"])

    def test_api_failure_does_not_raise_during_checkout(self):
        with patch.object(requests, "request", side_effect=requests.ConnectionError("x")):
            result = self.carrier.rate_shipment(self._order())
        self.assertFalse(result["success"])
        self.assertTrue(result["error_message"])

    def test_simulation_quotes_without_touching_the_network(self):
        self.carrier.write({"dhl_simulation": True, "dhl_simulation_price": 199.0})
        with patch.object(requests, "request") as call:
            result = self.carrier.rate_shipment(self._order())
        call.assert_not_called()
        self.assertTrue(result["success"])
        self.assertAlmostEqual(result["price"], 199.0, 2)

    def test_simulation_refuses_to_ship(self):
        self.carrier.dhl_simulation = True
        with self.assertRaises(UserError):
            self.carrier.dhl_express_send_shipping(self.env["stock.picking"])

    def test_test_connection_validates_the_pickup_address(self):
        with patch.object(requests, "request", return_value=FakeResponse(payload={
            "address": [{"countryCode": "BR", "cityName": "ICARA"}],
        })) as call:
            action = self.carrier.action_dhl_test_connection()
        args, kwargs = call.call_args
        self.assertTrue(args[1].endswith("/address-validate"))
        self.assertEqual(kwargs["params"]["type"], "pickup")
        self.assertEqual(kwargs["params"]["postalCode"], "88829124")
        self.assertEqual(action["params"]["type"], "success")


@tagged("post_install", "-at_install", "delivery_dhl_express")
class TestDhlShipping(DhlCarrierCase):

    def _picking(self, qty=2, partner=None):
        order = self._order(qty, partner)
        order.carrier_id = self.carrier
        order.action_confirm()
        picking = order.picking_ids
        picking.move_ids.quantity = qty
        return picking

    def _ship(self, picking, shipment=None):
        respostas = [
            FakeResponse(payload={"products": [product("P", 320.0), product("U", 280.0)]}),
            FakeResponse(201, shipment or SHIPMENT),
        ]
        with patch.object(requests, "request", side_effect=respostas) as call:
            result = self.carrier.send_shipping(picking)
        return result, call

    def test_shipment_payload_follows_the_api(self):
        picking = self._picking(qty=2)
        result, call = self._ship(picking)

        self.assertEqual(result, [{"exact_price": 280.0, "tracking_number": "1234567890"}])
        url, corpo = call.call_args_list[1][0][1], call.call_args_list[1][1]["json"]
        self.assertTrue(url.endswith("/shipments"))
        self.assertEqual(corpo["productCode"], "U", "o produto que a nova cotação escolheu")
        self.assertEqual(corpo["pickup"], {"isRequested": False})
        self.assertRegex(corpo["plannedShippingDateAndTime"], r"GMT-03:00$")

        remetente = corpo["customerDetails"]["shipperDetails"]
        self.assertEqual(remetente["registrationNumbers"], [
            {"typeCode": "CNP", "number": "66903932000152", "issuerCountryCode": "BR"}])
        self.assertEqual(remetente["postalAddress"]["addressLine1"], "Rua Idalino Rosso, 478")
        self.assertEqual(remetente["typeCode"], "business")
        destinatario = corpo["customerDetails"]["receiverDetails"]
        self.assertEqual(destinatario["typeCode"], "private")
        self.assertEqual(destinatario["contactInformation"]["companyName"], "Ana Lisboa")
        self.assertEqual(destinatario["contactInformation"]["phone"], "+351 912 345 678")

        conteudo = corpo["content"]
        self.assertTrue(conteudo["isCustomsDeclarable"])
        self.assertEqual(conteudo["incoterm"], "DAP")
        self.assertEqual(conteudo["declaredValue"], 100.0)
        self.assertEqual(conteudo["declaredValueCurrency"], "BRL")
        self.assertEqual(conteudo["description"], "Livro")
        declaracao = conteudo["exportDeclaration"]
        linha = declaracao["lineItems"][0]
        self.assertEqual(linha["commodityCodes"], [{"typeCode": "outbound", "value": "49019900"}])
        self.assertEqual(linha["quantity"], {"value": 2, "unitOfMeasurement": "PCS"})
        self.assertEqual(linha["price"], 50.0, "preço unitário")
        self.assertEqual(linha["weight"]["netValue"], 0.6, "peso total da linha")
        self.assertEqual(linha["manufacturerCountry"], "BR")
        self.assertEqual(declaracao["placeOfIncoterm"], "Lisboa")
        imagens = corpo["outputImageProperties"]
        self.assertEqual(imagens["encodingFormat"], "pdf")
        self.assertEqual({i["typeCode"] for i in imagens["imageOptions"]}, {"label", "invoice"})

    def test_label_and_invoice_are_attached(self):
        picking = self._picking()
        self._ship(picking)
        anexos = self.env["ir.attachment"].search(
            [("res_model", "=", "stock.picking"), ("res_id", "=", picking.id)]
        )
        self.assertEqual(len(anexos), 2)
        self.assertTrue(all(a.mimetype == "application/pdf" for a in anexos))
        self.assertTrue(any(re.search(r"LabelShipping.*label", a.name) for a in anexos))

    def test_missing_phone_is_refused_with_a_useful_message(self):
        self.buyer.phone = False
        picking = self._picking()
        with self.assertRaises(DhlError) as caught:
            self._ship(picking)
        self.assertIn("telefone", str(caught.exception))

    def test_missing_hs_code_is_refused(self):
        self.carrier.dhl_default_hs_code = False
        picking = self._picking()
        with self.assertRaises(DhlError) as caught:
            self._ship(picking)
        self.assertIn("HS", str(caught.exception))

    def test_product_hs_code_wins_over_the_default(self):
        self.book.hs_code = "4901.10"
        picking = self._picking()
        _result, call = self._ship(picking)
        linha = call.call_args_list[1][1]["json"]["content"]["exportDeclaration"]["lineItems"][0]
        self.assertEqual(linha["commodityCodes"][0]["value"], "490110")

    def test_pickup_can_be_requested(self):
        self.carrier.write({"dhl_request_pickup": True, "dhl_pickup_close_time": "17:30",
                            "dhl_pickup_location": "Recepção"})
        picking = self._picking()
        envio = dict(SHIPMENT, dispatchConfirmationNumber="PRG261002000001")
        _result, call = self._ship(picking, envio)
        self.assertEqual(call.call_args_list[1][1]["json"]["pickup"], {
            "isRequested": True, "closeTime": "17:30", "location": "Recepção"})
        self.assertEqual(picking.dhl_dispatch_confirmation, "PRG261002000001")

    def test_cancel_cancels_the_pickup_and_voids_the_label(self):
        self.carrier.dhl_request_pickup = True
        picking = self._picking()
        self._ship(picking, dict(SHIPMENT, dispatchConfirmationNumber="PRG1"))
        picking.carrier_tracking_ref = "1234567890"
        with patch.object(requests, "request", return_value=FakeResponse(payload={})) as call:
            picking.cancel_shipment()
        self.assertTrue(call.call_args[0][1].endswith("/pickups/PRG1"))
        self.assertFalse(picking.dhl_dispatch_confirmation)
        self.assertFalse(picking.carrier_tracking_ref, "pode ser despachada de novo")
        self.assertEqual(picking.dhl_tracking_status, "Cancelado")
        etiqueta = self.env["ir.attachment"].search([
            ("res_model", "=", "stock.picking"), ("res_id", "=", picking.id),
            ("name", "like", "-label."),
        ])
        self.assertTrue(etiqueta.name.startswith("CANCELADA-"), "ninguém imprime por engano")
        mensagens = " ".join(picking.message_ids.mapped("body"))
        self.assertIn("Coleta PRG1 cancelada", mensagens)

    def test_cancel_without_pickup_makes_no_call(self):
        picking = self._picking()
        self._ship(picking)
        picking.carrier_tracking_ref = "1234567890"
        with patch.object(requests, "request") as call:
            picking.cancel_shipment()
        call.assert_not_called()
        self.assertFalse(picking.carrier_tracking_ref)

    # --- NF-e de exportação ------------------------------------------------- #

    NFE = {"key": "42261066903932000152550010000001231000001234", "number": "123",
           "date": "2026-10-02"}

    def test_exporter_with_ie_cannot_ship_without_nfe(self):
        Carrier = self.env.registry["delivery.carrier"]
        picking = self._picking()
        with patch.object(Carrier, "_dhl_requires_nfe", return_value=True), \
                patch.object(Carrier, "_dhl_nfe", return_value=False):
            with patch.object(requests, "request", side_effect=[
                FakeResponse(payload={"products": [product("P", 320.0)]}),
            ]) as call:
                with self.assertRaises(DhlError) as caught:
                    self.carrier.send_shipping(picking)
        self.assertIn("NF-e", str(caught.exception))
        self.assertEqual(call.call_count, 1, "só a cotação: o envio nem é criado")

    def test_nfe_goes_on_label_invoice_and_remarks(self):
        Carrier = self.env.registry["delivery.carrier"]
        nfe = dict(self.NFE, move=self.env["account.move"])
        picking = self._picking()
        with patch.object(Carrier, "_dhl_nfe", return_value=nfe):
            _result, call = self._ship(picking)
        corpo = call.call_args_list[1][1]["json"]
        etiqueta = corpo["outputImageProperties"]["imageOptions"][0]
        self.assertEqual(etiqueta["labelCustomerDataText"], "NF-e %s" % self.NFE["key"])
        declaracao = corpo["content"]["exportDeclaration"]
        self.assertEqual(declaracao["invoice"], {"number": "123", "date": "2026-10-02"},
                         "a fatura comercial corresponde à NF-e")
        self.assertEqual(declaracao["remarks"], [{"value": "NF-e %s" % self.NFE["key"]}])
        self.assertIn("DANFE", picking.message_ids[0].body)

    def test_requirement_only_applies_to_brazilian_exporter_with_ie(self):
        remetente = self.env.company.partner_id
        self.carrier.dhl_require_nfe = False
        self.assertFalse(self.carrier._dhl_requires_nfe(remetente, self.env.company))

    def test_tracking_link_and_refresh(self):
        picking = self._picking()
        self._ship(picking)
        picking.carrier_tracking_ref = "1234567890"
        self.assertIn("tracking-id=1234567890",
                      self.carrier.dhl_express_get_tracking_link(picking))
        with patch.object(requests, "request", return_value=FakeResponse(payload={
            "shipments": [{"status": "Success", "events": [
                {"date": "2026-10-03", "time": "09:00:00", "description": "Coletado"},
                {"date": "2026-10-04", "time": "14:00:00", "description": "Em trânsito"},
            ]}],
        })):
            picking.action_dhl_refresh_tracking()
        self.assertEqual(picking.dhl_tracking_status, "Em trânsito")
