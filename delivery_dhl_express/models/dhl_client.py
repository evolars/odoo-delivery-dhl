"""Cliente HTTP da MyDHL API (DHL Express).

Sem acoplamento com registros do Odoo: recebe credencial explicitamente e
devolve dicionários, para poder ser testado sem banco. O `env` é opcional e só
serve para traduzir as mensagens fora de uma requisição HTTP.

Autenticação é Basic Auth com a chave e o segredo do portal do desenvolvedor
da DHL. Toda chamada leva o header `x-version` (obrigatório na especificação).
O ambiente de teste tem limite de 500 chamadas por dia.

Referência: especificação OpenAPI da MyDHL API 3.3.2 (06/09/2026), conferida
em 02/10/2026.
"""
import base64
import logging
import uuid

import requests

from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

PRODUCTION_URL = "https://express.api.dhl.com/mydhlapi"
TEST_URL = "https://express.api.dhl.com/mydhlapi/test"
API_VERSION = "3.3.2"
DEFAULT_TIMEOUT = 45
# A cotação roda no checkout, com o comprador esperando.
RATE_TIMEOUT = 15

# A DHL usa Incoterms na declaração aduaneira. DAP é o vigente para "o
# destinatário paga impostos na entrega" — DDU é o termo antigo.
INCOTERM_DAP = "DAP"
INCOTERM_DDP = "DDP"

# Na cotação a DHL devolve o preço em até três moedas; a faturada é a BILLC.
BILLING_CURRENCY = "BILLC"


class DhlError(UserError):
    """A DHL recusou a operação, ou não deu para falar com ela."""

    def __init__(self, message, status_code=None, payload=None):
        super().__init__(message)
        self.status_code = status_code
        self.payload = payload or {}


def _untranslated(source, *args, **kwargs):
    if args or kwargs:
        return source % (args or kwargs)
    return source


def tax_id(value):
    """CPF/CNPJ/VAT sem máscara. Mantém letras: o CNPJ alfanumérico vale desde
    julho de 2026, e VAT europeu começa pelo país."""
    return "".join(char for char in (value or "") if char.isalnum()).upper()


class DhlClient:
    def __init__(self, api_key, api_secret, account_number, test_mode=True,
                 timeout=DEFAULT_TIMEOUT, env=None):
        self.api_key = api_key
        self.api_secret = api_secret
        self.account_number = account_number
        self.test_mode = test_mode
        self.timeout = timeout
        self.base_url = TEST_URL if test_mode else PRODUCTION_URL
        self._ = env._ if env is not None else _untranslated

    # ------------------------------------------------------------------ #
    # Transporte                                                          #
    # ------------------------------------------------------------------ #

    def _headers(self):
        if not (self.api_key and self.api_secret):
            raise DhlError(self._("Configure a chave e o segredo da API da DHL Express."))
        credencial = base64.b64encode(
            ("%s:%s" % (self.api_key, self.api_secret)).encode()
        ).decode()
        return {
            "Authorization": "Basic %s" % credencial,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "x-version": API_VERSION,
            "Message-Reference": str(uuid.uuid4()),
        }

    def request(self, method, path, payload=None, params=None, timeout=None):
        headers = self._headers()
        url = "%s/%s" % (self.base_url, path.lstrip("/"))
        try:
            response = requests.request(
                method, url, headers=headers, json=payload, params=params,
                timeout=timeout or self.timeout,
            )
        except requests.RequestException as error:
            _logger.warning("DHL: falha de conexão em %s %s: %s", method, path, error)
            raise DhlError(self._("Não foi possível conectar à DHL Express.")) from error

        try:
            data = response.json() if response.content else {}
        except ValueError:
            data = {}

        if not response.ok:
            mensagem = self._error_detail(data) or response.reason
            _logger.warning("DHL: recusa status=%s em %s %s: %s",
                            response.status_code, method, path, mensagem)
            raise DhlError(
                self._("A DHL recusou a operação: %s", mensagem),
                status_code=response.status_code, payload=data,
            )
        return data

    @staticmethod
    def _error_detail(data):
        """A DHL responde no formato da RFC 7807 e detalha o campo recusado em
        `additionalDetails`, que é o que realmente ajuda."""
        if not isinstance(data, dict):
            return ""
        principal = data.get("detail") or data.get("message") or data.get("title") or ""
        detalhes = [str(item) for item in data.get("additionalDetails") or [] if item]
        return " — ".join(parte for parte in [principal] + detalhes if parte)

    # ------------------------------------------------------------------ #
    # Operações                                                           #
    # ------------------------------------------------------------------ #

    def rates(self, payload):
        """Cotação multivolume (`POST /rates`)."""
        return self.request("POST", "/rates", payload=payload, timeout=RATE_TIMEOUT)

    def create_shipment(self, payload):
        if not self.account_number:
            raise DhlError(self._("Configure o número da conta DHL."))
        return self.request("POST", "/shipments", payload=payload)

    def track(self, tracking_number):
        return self.request(
            "GET", "/shipments/%s/tracking" % tracking_number,
            params={"trackingView": "all-checkpoints", "levelOfDetail": "shipment"},
        )

    def cancel_pickup(self, dispatch_confirmation_number, requestor, reason):
        """Cancela a coleta agendada. O conhecimento em si não tem cancelamento
        na API."""
        return self.request(
            "DELETE", "/pickups/%s" % dispatch_confirmation_number,
            params={"requestorName": requestor[:35], "reason": reason[:35]},
        )

    def validate_address(self, kind, country_code, postal_code=None, city_name=None):
        """`GET /address-validate`: a chamada mais barata que exige credencial
        válida. Serve de teste de conexão e confere se a DHL atende o endereço."""
        params = {"type": kind, "countryCode": country_code}
        if postal_code:
            params["postalCode"] = postal_code
        if city_name:
            params["cityName"] = city_name
        return self.request("GET", "/address-validate", params=params)


# --------------------------------------------------------------------------- #
# Leitura de resposta                                                          #
# --------------------------------------------------------------------------- #

def extract_products(response):
    """Normaliza as opções de serviço devolvidas pela cotação."""
    produtos = []
    for produto in response.get("products") or []:
        precos = produto.get("totalPrice") or []
        escolhido = next(
            (p for p in precos if (p.get("currencyType") or "").upper() == BILLING_CURRENCY),
            precos[0] if precos else None,
        )
        if not escolhido or escolhido.get("price") in (None, ""):
            continue
        entrega = produto.get("deliveryCapabilities") or {}
        produtos.append({
            "code": produto.get("productCode"),
            "local_code": produto.get("localProductCode"),
            "name": produto.get("productName") or produto.get("productCode"),
            "price": float(escolhido.get("price") or 0.0),
            "currency": escolhido.get("priceCurrency"),
            "delivery_date": entrega.get("estimatedDeliveryDateAndTime"),
            "transit_days": entrega.get("totalTransitDays"),
        })
    return produtos
