"""Cliente HTTP da MyDHL API (DHL Express).

Sem acoplamento com o Odoo: recebe credencial explicitamente e devolve
dicionários, para poder ser testado sem banco.

Autenticação é BasicAuth com a chave e o segredo que o consultor da DHL
fornece. O ambiente de teste tem limite de 500 chamadas por dia.
"""
import base64
import logging
import math

import requests

from odoo import _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

PRODUCTION_URL = "https://express.api.dhl.com/mydhlapi"
TEST_URL = "https://express.api.dhl.com/mydhlapi/test"
DEFAULT_TIMEOUT = 45

# A DHL usa Incoterms na declaração aduaneira. DAP é o vigente para "o
# destinatário paga impostos na entrega" — DDU é o termo antigo, ainda aceito.
INCOTERM_DAP = "DAP"
INCOTERM_DDP = "DDP"

# Livro impresso. Serve de padrão quando o produto não tem código próprio.
DEFAULT_HS_CODE = "4901"


class DhlError(UserError):
    """A DHL recusou a operação, ou não deu para falar com ela."""

    def __init__(self, message, status_code=None, payload=None):
        super().__init__(message)
        self.status_code = status_code
        self.payload = payload or {}


class DhlClient:
    def __init__(self, api_key, api_secret, account_number, test_mode=True,
                 timeout=DEFAULT_TIMEOUT):
        self.api_key = api_key
        self.api_secret = api_secret
        self.account_number = account_number
        self.test_mode = test_mode
        self.timeout = timeout
        self.base_url = TEST_URL if test_mode else PRODUCTION_URL

    # ------------------------------------------------------------------ #
    # Transporte                                                          #
    # ------------------------------------------------------------------ #

    def _headers(self):
        if not (self.api_key and self.api_secret):
            raise DhlError(_("Configure a chave e o segredo da API da DHL Express."))
        credencial = base64.b64encode(
            ("%s:%s" % (self.api_key, self.api_secret)).encode()
        ).decode()
        return {
            "Authorization": "Basic %s" % credencial,
            "Content-Type": "application/json",
        }

    def request(self, method, path, payload=None, params=None):
        url = "%s/%s" % (self.base_url, path.lstrip("/"))
        try:
            response = requests.request(
                method, url, headers=self._headers(), json=payload, params=params,
                timeout=self.timeout,
            )
        except requests.RequestException as error:
            _logger.warning("DHL: falha de conexão em %s %s: %s", method, path, error)
            raise DhlError(_("Não foi possível conectar à DHL Express.")) from error

        try:
            data = response.json() if response.content else {}
        except ValueError:
            data = {}

        if not response.ok:
            # A DHL responde no formato RFC 7807, e detalha o campo recusado em
            # additionalDetails — que costuma ser o que realmente ajuda.
            detalhes = data.get("additionalDetails") or []
            mensagem = "; ".join(str(d) for d in detalhes) if detalhes else ""
            mensagem = mensagem or data.get("detail") or data.get("title") or response.reason
            _logger.warning("DHL: recusa status=%s em %s %s", response.status_code, method, path)
            raise DhlError(
                _("A DHL recusou a operação: %s") % mensagem,
                status_code=response.status_code, payload=data,
            )
        return data

    # ------------------------------------------------------------------ #
    # Operações                                                           #
    # ------------------------------------------------------------------ #

    def rates(self, payload):
        """Cotação multi-volume. `POST /rates` aceita o corpo completo."""
        return self.request("POST", "/rates", payload=payload)

    def create_shipment(self, payload):
        return self.request("POST", "/shipments", payload=payload)

    def track(self, tracking_number):
        return self.request(
            "GET", "/shipments/%s/tracking" % tracking_number,
            params={"trackingView": "all-checkpoints", "levelOfDetail": "all"},
        )

    def ping(self):
        """Valida a credencial com a chamada mais barata: lista de produtos."""
        return self.request("GET", "/products", params={
            "accountNumber": self.account_number or "",
        })


# --------------------------------------------------------------------------- #
# Montagem de payload                                                          #
# --------------------------------------------------------------------------- #

def address_payload(partner):
    """Endereço no formato que a cotação da DHL espera.

    Cidade, país e CEP bastam para cotar; o endereço completo só é exigido na
    criação do envio.
    """
    if not partner.country_id:
        raise DhlError(_(
            "Informe o país de %s: a DHL cota por país de destino.",
            partner.display_name,
        ))
    payload = {
        "countryCode": partner.country_id.code,
        "cityName": (partner.city or "")[:45],
    }
    if partner.zip:
        payload["postalCode"] = partner.zip.replace("-", "").replace(".", "").strip()
    if partner.state_id and partner.state_id.code:
        payload["provinceCode"] = partner.state_id.code
    return payload


def package_payload(weight_kg, length_cm, width_cm, height_cm):
    """Um volume. A DHL aceita decimal, mas recusa zero."""
    return {
        "weight": round(max(weight_kg or 0.0, 0.01), 3),
        "dimensions": {
            "length": max(int(math.ceil(length_cm or 0)), 1),
            "width": max(int(math.ceil(width_cm or 0)), 1),
            "height": max(int(math.ceil(height_cm or 0)), 1),
        },
    }


def rate_payload(shipper, receiver, packages, planned_date, account_number,
                 declared_value=0.0, currency="BRL", customs_declarable=True):
    """Corpo de `POST /rates`.

    `plannedShippingDateAndTime` precisa do offset explícito — a DHL recusa o
    formato sem fuso.
    """
    payload = {
        "customerDetails": {"shipperDetails": shipper, "receiverDetails": receiver},
        "plannedShippingDateAndTime": planned_date,
        "unitOfMeasurement": "metric",
        "isCustomsDeclarable": customs_declarable,
        "packages": packages,
    }
    if account_number:
        payload["accounts"] = [{"typeCode": "shipper", "number": account_number}]
    if declared_value:
        payload["monetaryAmount"] = [{
            "typeCode": "declaredValue",
            "value": round(declared_value, 2),
            "currency": currency.upper(),
        }]
    return payload


def extract_products(response):
    """Normaliza as opções de serviço devolvidas pela cotação."""
    produtos = []
    for produto in response.get("products") or []:
        precos = produto.get("totalPrice") or []
        # a DHL devolve o preço em mais de uma moeda; a faturada é BILLC
        escolhido = next(
            (p for p in precos if (p.get("currencyType") or "").upper() == "BILLC"),
            precos[0] if precos else None,
        )
        if not escolhido:
            continue
        entrega = (produto.get("deliveryCapabilities") or {})
        produtos.append({
            "code": produto.get("productCode"),
            "name": produto.get("productName") or produto.get("productCode"),
            "price": float(escolhido.get("price") or 0.0),
            "currency": escolhido.get("priceCurrency"),
            "delivery_date": entrega.get("estimatedDeliveryDateAndTime"),
            "transit_days": entrega.get("totalTransitDays"),
        })
    return produtos
