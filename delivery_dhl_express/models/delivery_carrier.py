import logging
from datetime import timedelta

from odoo import _, fields, models
from odoo.exceptions import UserError

from .dhl_client import (
    DEFAULT_HS_CODE,
    INCOTERM_DAP,
    INCOTERM_DDP,
    DhlClient,
    address_payload,
    extract_products,
    package_payload,
    rate_payload,
)

_logger = logging.getLogger(__name__)

INCOTERM_CHOICE = [
    (INCOTERM_DAP, "DAP — destinatário paga impostos na entrega"),
    (INCOTERM_DDP, "DDP — remetente paga impostos"),
]


class DeliveryCarrier(models.Model):
    _inherit = "delivery.carrier"

    delivery_type = fields.Selection(
        selection_add=[("dhl_express", "DHL Express")],
        ondelete={"dhl_express": "set default"},
    )
    dhl_api_key = fields.Char(string="Chave da API", groups="base.group_system")
    dhl_api_secret = fields.Char(string="Segredo da API", groups="base.group_system")
    dhl_account_number = fields.Char(string="Número da Conta DHL")
    dhl_product_code = fields.Char(
        string="Código do Produto",
        help="Deixe vazio para a DHL escolher a opção mais barata entre as "
             "disponíveis. Preencha para fixar um serviço (ex.: P de Express Worldwide).",
    )
    dhl_incoterm = fields.Selection(
        INCOTERM_CHOICE, string="Incoterm", default=INCOTERM_DAP, required=False,
        help="DAP é o padrão: o destinatário paga imposto de importação e "
             "desembaraço na entrega. Avise isso no checkout, ou o pacote é recusado.",
    )
    dhl_default_package_type_id = fields.Many2one(
        "stock.package.type", string="Embalagem padrão",
        help="Caixa usada para dividir o pedido em volumes.",
    )
    dhl_lead_days = fields.Integer(
        string="Dias até o despacho", default=1,
        help="Quantos dias úteis o pedido leva para sair daqui. A DHL cota a "
             "partir dessa data, não da data do pedido.",
    )
    dhl_simulation = fields.Boolean(
        string="Modo simulação",
        help="Cota por uma estimativa local, sem chamar a DHL. Serve para "
             "exercitar o checkout antes de a conta existir. Nunca cria envio.",
    )
    dhl_simulation_price = fields.Float(string="Preço simulado", default=180.0)

    # ------------------------------------------------------------------ #
    # Infraestrutura                                                      #
    # ------------------------------------------------------------------ #

    def _dhl_get_client(self):
        """Cliente com as credenciais deste método.

        O ambiente segue o campo `prod_environment` do método: fora de
        produção as chamadas vão para o sandbox, que tem 500 chamadas por dia.
        """
        self.ensure_one()
        return DhlClient(
            api_key=self.dhl_api_key,
            api_secret=self.dhl_api_secret,
            account_number=self.dhl_account_number,
            test_mode=self.prod_environment is not True,
        )

    def action_dhl_test_connection(self):
        self.ensure_one()
        client = self._dhl_get_client()
        client.ping()
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "type": "success",
                "message": _("Conexão com a DHL Express (%s) confirmada.") % (
                    "sandbox" if client.test_mode else "produção"
                ),
                "sticky": False,
            },
        }

    def _dhl_shipper_partner(self, order=None, picking=None):
        if picking and picking.picking_type_id.warehouse_id.partner_id:
            return picking.picking_type_id.warehouse_id.partner_id
        if order and order.warehouse_id.partner_id:
            return order.warehouse_id.partner_id
        company = (order or picking or self).company_id or self.env.company
        return company.partner_id

    def _dhl_planned_date(self):
        """Data do despacho com fuso explícito — a DHL recusa sem offset."""
        self.ensure_one()
        quando = fields.Datetime.now() + timedelta(days=max(self.dhl_lead_days, 0))
        return quando.strftime("%Y-%m-%dT%H:%M:%S GMT+00:00").replace(" ", "")

    def _dhl_packages(self, packages):
        return [
            package_payload(
                weight_kg=p.weight,
                length_cm=(p.dimension or {}).get("length") or 0,
                width_cm=(p.dimension or {}).get("width") or 0,
                height_cm=(p.dimension or {}).get("height") or 0,
            )
            for p in packages
        ]

    def _dhl_pick_product(self, produtos):
        """Escolhe o serviço: o fixado, se houver, senão o mais barato."""
        if not produtos:
            return None
        if self.dhl_product_code:
            fixos = [p for p in produtos if p["code"] == self.dhl_product_code]
            if fixos:
                return fixos[0]
            # serviço fixado indisponível para o destino: cotar o que há é
            # melhor que não cotar
            _logger.info(
                "DHL: produto %s indisponível; usando a opção mais barata.",
                self.dhl_product_code,
            )
        return min(produtos, key=lambda p: p["price"])

    # ------------------------------------------------------------------ #
    # Contrato do delivery.carrier                                        #
    # ------------------------------------------------------------------ #

    def dhl_express_rate_shipment(self, order):
        self.ensure_one()
        if self.dhl_simulation:
            return {
                "success": True,
                "price": self.dhl_simulation_price,
                "error_message": False,
                "warning_message": _(
                    "Frete simulado: o método DHL está em modo simulação e não "
                    "consultou a transportadora."
                ),
            }

        remetente = self._dhl_shipper_partner(order=order)
        destinatario = order.partner_shipping_id
        internacional = remetente.country_id != destinatario.country_id
        try:
            packages = self._get_packages_from_order(
                order, self.dhl_default_package_type_id
            )
            response = self._dhl_get_client().rates(rate_payload(
                shipper=address_payload(remetente),
                receiver=address_payload(destinatario),
                packages=self._dhl_packages(packages),
                planned_date=self._dhl_planned_date(),
                account_number=self.dhl_account_number,
                declared_value=order.amount_untaxed,
                currency=order.currency_id.name or "BRL",
                customs_declarable=internacional,
            ))
        except UserError as error:
            # Cotação não pode estourar no checkout: o comprador precisa seguir
            # com os outros métodos.
            return {"success": False, "price": 0.0,
                    "error_message": str(error), "warning_message": False}

        produto = self._dhl_pick_product(extract_products(response))
        if not produto:
            return {
                "success": False, "price": 0.0,
                "error_message": _("A DHL não atende este destino."),
                "warning_message": False,
            }

        aviso = False
        if internacional and self.dhl_incoterm == INCOTERM_DAP:
            aviso = _(
                "Imposto de importação e desembaraço são pagos pelo destinatário "
                "na entrega, e não estão incluídos neste frete."
            )
        return {
            "success": True,
            "price": produto["price"],
            "error_message": False,
            "warning_message": aviso,
        }

    def dhl_express_send_shipping(self, pickings):
        self.ensure_one()
        if self.dhl_simulation:
            raise UserError(_(
                "O método DHL está em modo simulação: dá para cotar, mas não "
                "para despachar. Desligue a simulação e informe as credenciais."
            ))
        resultado = []
        client = self._dhl_get_client()
        for picking in pickings:
            response = client.create_shipment(self._dhl_shipment_payload(picking))
            tracking = response.get("shipmentTrackingNumber")
            if not tracking:
                raise UserError(_(
                    "A DHL aceitou o envio mas não devolveu número de rastreio. "
                    "Confira no MyDHL antes de repetir, para não duplicar o pacote."
                ))
            picking.carrier_tracking_ref = tracking
            self._dhl_attach_documents(picking, response)
            resultado.append({"exact_price": picking.carrier_price or 0.0,
                              "tracking_number": tracking})
        return resultado

    def _dhl_shipment_payload(self, picking):
        remetente = self._dhl_shipper_partner(picking=picking)
        destinatario = picking.partner_id
        internacional = remetente.country_id != destinatario.country_id
        packages = self._get_packages_from_picking(
            picking, self.dhl_default_package_type_id
        )
        moeda = picking.company_id.currency_id.name or "BRL"
        payload = {
            "plannedShippingDateAndTime": self._dhl_planned_date(),
            "pickup": {"isRequested": False},
            "productCode": self.dhl_product_code or "P",
            "accounts": [{"typeCode": "shipper", "number": self.dhl_account_number}],
            "customerDetails": {
                "shipperDetails": self._dhl_full_address(remetente),
                "receiverDetails": self._dhl_full_address(destinatario),
            },
            "content": {
                "packages": self._dhl_packages(packages),
                "isCustomsDeclarable": internacional,
                "description": picking.name,
                "incoterm": self.dhl_incoterm or INCOTERM_DAP,
                "unitOfMeasurement": "metric",
            },
            "outputImageProperties": {
                "imageOptions": [{"typeCode": "label", "templateName": "ECOM26_84_001"}],
            },
            "customerReferences": [{"value": picking.name, "typeCode": "CU"}],
        }
        if internacional:
            payload["content"]["exportDeclaration"] = self._dhl_export_declaration(
                picking, moeda
            )
        return payload

    def _dhl_full_address(self, partner):
        """Endereço completo, exigido na criação do envio (não na cotação)."""
        endereco = address_payload(partner)
        endereco["addressLine1"] = (partner.street or "")[:45] or "-"
        if partner.street2:
            endereco["addressLine2"] = partner.street2[:45]
        return {
            "postalAddress": endereco,
            "contactInformation": {
                "fullName": partner.name or "-",
                "companyName": (partner.commercial_company_name or partner.name or "-")[:60],
                "phone": (partner.mobile or partner.phone or "")[:25],
                "email": partner.email or "",
            },
        }

    def _dhl_export_declaration(self, picking, moeda):
        """Declaração aduaneira. Cada item precisa de NCM/HS e valor."""
        linhas = []
        for indice, move in enumerate(picking.move_ids, start=1):
            produto = move.product_id
            hs_code = getattr(produto, "hs_code", False) or DEFAULT_HS_CODE
            linhas.append({
                "number": indice,
                "description": (produto.name or "")[:75],
                "price": round(produto.list_price or 0.0, 2),
                "quantity": {
                    "value": int(move.product_uom_qty) or 1,
                    "unitOfMeasurement": "PCS",
                },
                "commodityCodes": [{"typeCode": "outbound", "value": hs_code}],
                "manufacturerCountry": (
                    picking.company_id.country_id.code or "BR"
                ),
                "weight": {
                    "netValue": round(produto.weight or 0.01, 3),
                    "grossValue": round(produto.weight or 0.01, 3),
                },
            })
        return {
            "lineItems": linhas,
            "invoice": {
                "number": picking.name,
                "date": fields.Date.context_today(self).isoformat(),
            },
            "exportReason": "sale",
            "placeOfIncoterm": picking.company_id.city or "",
        }

    def _dhl_attach_documents(self, picking, response):
        """Guarda a etiqueta que veio em base64 junto ao picking."""
        for documento in response.get("documents") or []:
            conteudo = documento.get("content")
            if not conteudo:
                continue
            tipo = documento.get("typeCode") or "label"
            self.env["ir.attachment"].create({
                "name": "DHL-%s-%s.pdf" % (tipo, picking.carrier_tracking_ref),
                "type": "binary",
                "datas": conteudo,
                "res_model": "stock.picking",
                "res_id": picking.id,
            })

    def dhl_express_get_tracking_link(self, picking):
        if not picking.carrier_tracking_ref:
            return False
        return (
            "https://www.dhl.com/br-pt/home/rastreamento.html"
            "?tracking-id=%s" % picking.carrier_tracking_ref
        )

    def dhl_express_cancel_shipment(self, picking):
        """A MyDHL API não cancela envio criado; o cancelamento é no painel."""
        raise UserError(_(
            "A DHL não permite cancelar um envio pela API. Cancele pelo MyDHL e "
            "depois limpe o código de rastreio nesta entrega."
        ))

    def _dhl_express_get_default_custom_package_code(self):
        return "YP"
