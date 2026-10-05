import logging
from datetime import datetime, timedelta

import pytz

from odoo import fields, models
from odoo.exceptions import UserError

from .dhl_client import (
    INCOTERM_DAP,
    INCOTERM_DDP,
    DhlClient,
    DhlError,
    extract_products,
    tax_id,
)

_logger = logging.getLogger(__name__)

INCOTERM_CHOICE = [
    (INCOTERM_DAP, "DAP — destinatário paga impostos na entrega"),
    (INCOTERM_DDP, "DDP — remetente paga impostos"),
]

LABEL_TEMPLATES = [
    ("ECOM26_84_001", "8 × 4 pol (impressora térmica)"),
    ("ECOM26_A6_002", "A6"),
    ("ECOM26_84_A4_001", "A4"),
]

# Limites de tamanho dos campos de texto da API.
MAX_ADDRESS_LINE = 45
MAX_CITY = 45
MAX_CONTENT_DESCRIPTION = 70
MAX_REFERENCE = 35

# A DHL só aceita data planejada até 10 dias à frente.
MAX_LEAD_DAYS = 9


class DeliveryCarrier(models.Model):
    _inherit = "delivery.carrier"

    delivery_type = fields.Selection(
        selection_add=[("dhl_express", "DHL Express")],
        ondelete={"dhl_express": "set default"},
    )
    dhl_api_key = fields.Char(string="Chave da API", groups="base.group_system")
    dhl_api_secret = fields.Char(string="Segredo da API", groups="base.group_system")
    dhl_account_number = fields.Char(
        string="Número da Conta DHL",
        help="A conta de exportação (shipper) que paga o frete.",
    )
    dhl_product_code = fields.Char(
        string="Código do Produto",
        help="Deixe vazio para cotar a opção mais barata entre as disponíveis. "
             "Preencha para fixar um serviço (ex.: P de Express Worldwide).",
    )
    dhl_incoterm = fields.Selection(
        INCOTERM_CHOICE, string="Incoterm", default=INCOTERM_DAP,
        help="DAP é o padrão: o destinatário paga imposto de importação e "
             "desembaraço na entrega. Avise isso no checkout, ou o pacote é recusado.",
    )
    dhl_international_only = fields.Boolean(
        string="Só internacional", default=True,
        help="Esconde o método quando o destino é o mesmo país de onde o pacote sai.",
    )
    dhl_default_package_type_id = fields.Many2one(
        "stock.package.type", string="Embalagem padrão",
        help="Caixa usada quando nenhuma das caixas disponíveis comporta o pedido: "
             "o pedido é dividido em volumes dela, pelo peso máximo.",
    )
    dhl_package_type_ids = fields.Many2many(
        "stock.package.type", "delivery_carrier_dhl_package_type_rel",
        string="Caixas disponíveis",
        help="Na cotação, entra a menor caixa cujo peso máximo comporta o pedido.",
    )
    dhl_default_hs_code = fields.Char(
        string="Código HS padrão",
        help="Usado na declaração aduaneira quando o produto não tem código HS "
             "próprio (ex.: 490199 para livro impresso).",
    )
    dhl_lead_days = fields.Integer(
        string="Dias até o despacho", default=1,
        help="Quantos dias úteis o pedido leva para sair daqui. A DHL cota a "
             "partir dessa data (no máximo 9 dias).",
    )
    dhl_label_template = fields.Selection(
        LABEL_TEMPLATES, string="Formato da etiqueta", default="ECOM26_84_001",
    )
    dhl_commercial_invoice = fields.Boolean(
        string="Fatura comercial pela DHL", default=True,
        help="Pede à DHL a fatura comercial (commercial invoice) em PDF junto com a "
             "etiqueta, montada a partir da declaração aduaneira.",
    )
    dhl_request_pickup = fields.Boolean(
        string="Pedir coleta",
        help="Agenda a coleta da DHL ao criar o envio. Sem isso, o pacote é levado a "
             "um ponto da DHL ou entra na coleta já contratada.",
    )
    dhl_pickup_close_time = fields.Char(
        string="Horário limite da coleta", default="18:00",
        help="Até que horas o local recebe o motorista (HH:MM).",
    )
    dhl_pickup_location = fields.Char(
        string="Local da coleta", help="Ex.: Recepção, Expedição.",
    )
    dhl_simulation = fields.Boolean(
        string="Modo simulação",
        help="Cota por uma estimativa local, sem chamar a DHL. Serve para "
             "exercitar o checkout antes de a conta existir. Nunca cria envio.",
    )
    dhl_simulation_price = fields.Float(string="Preço simulado", default=180.0)
    dhl_require_nfe = fields.Boolean(
        string="Exigir NF-e autorizada", default=True,
        help="Remetente brasileiro com Inscrição Estadual só envia mercadoria com NF-e, "
             "no Brasil ou para fora: a DHL Brasil exige a nota no despacho (na exportação, "
             "a Receita cruza a nota com a fatura comercial). Ligado, a entrega não é "
             "despachada sem NF-e autorizada na venda.",
    )

    # ------------------------------------------------------------------ #
    # Infraestrutura                                                      #
    # ------------------------------------------------------------------ #

    def _dhl_get_client(self):
        """Cliente com as credenciais deste método.

        O ambiente segue o campo `prod_environment` do método: fora de
        produção as chamadas vão para o ambiente de teste, que tem 500
        chamadas por dia.
        """
        self.ensure_one()
        carrier = self.sudo()
        return DhlClient(
            api_key=carrier.dhl_api_key,
            api_secret=carrier.dhl_api_secret,
            account_number=carrier.dhl_account_number,
            test_mode=self.prod_environment is not True,
            env=self.env,
        )

    def action_dhl_test_connection(self):
        """Valida a credencial e, de quebra, se a DHL coleta no endereço de saída."""
        self.ensure_one()
        client = self._dhl_get_client()
        remetente = self._dhl_shipper_partner()
        endereco = self._dhl_address(remetente)
        client.validate_address(
            "pickup", endereco["countryCode"],
            postal_code=endereco.get("postalCode"), city_name=endereco["cityName"],
        )
        ambiente = self.env._("teste") if client.test_mode else self.env._("produção")
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "type": "success",
                "message": self.env._(
                    "Conexão com a DHL Express (%(ambiente)s) confirmada, e a DHL "
                    "atende a coleta em %(cidade)s.",
                    ambiente=ambiente, cidade=endereco["cityName"],
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

    def _match(self, partner, order):
        """Método só internacional some do checkout para destino nacional."""
        if self.delivery_type == "dhl_express" and self.dhl_international_only:
            origem = self._dhl_shipper_partner(order=order).country_id
            if origem and partner.country_id == origem:
                return False
        return super()._match(partner, order)

    def _dhl_planned_date(self, shipper=None):
        """Data do despacho na hora local do remetente, com o fuso explícito
        ("2026-10-05T10:00:00GMT-03:00"): é o formato da especificação, e a
        DHL recusa data passada ou mais de 10 dias à frente."""
        self.ensure_one()
        nome_fuso = (shipper and shipper.tz) or self.env.user.tz or "UTC"
        try:
            fuso = pytz.timezone(nome_fuso)
        except pytz.UnknownTimeZoneError:
            fuso = pytz.utc
        agora = datetime.now(fuso)
        dias = min(max(self.dhl_lead_days or 0, 0), MAX_LEAD_DAYS)
        if dias:
            quando = (agora + timedelta(days=dias)).replace(
                hour=10, minute=0, second=0, microsecond=0)
            while quando.weekday() >= 5:
                quando += timedelta(days=1)
            quando = fuso.normalize(quando)
        else:
            quando = (agora + timedelta(minutes=30)).replace(microsecond=0)
        offset = quando.utcoffset() or timedelta(0)
        minutos = int(offset.total_seconds() // 60)
        sinal = "+" if minutos >= 0 else "-"
        minutos = abs(minutos)
        return "%sGMT%s%02d:%02d" % (
            quando.strftime("%Y-%m-%dT%H:%M:%S"), sinal, minutos // 60, minutos % 60,
        )

    # ------------------------------------------------------------------ #
    # Unidades                                                            #
    # ------------------------------------------------------------------ #

    def _dhl_length_cm(self, value):
        """Medida da embalagem, na unidade do Odoo (mm por padrão), em cm."""
        uom = self.env["product.template"]._get_length_uom_id_from_ir_config_parameter()
        return uom._compute_quantity(value or 0.0, self.env.ref("uom.product_uom_cm"),
                                     round=False)

    def _dhl_weight_kg(self, value):
        uom = self.env["product.template"]._get_weight_uom_id_from_ir_config_parameter()
        return uom._compute_quantity(value or 0.0, self.env.ref("uom.product_uom_kgm"),
                                     round=False)

    def _dhl_package_payload(self, package):
        """Um volume. A DHL aceita decimal, mas recusa zero."""
        dimensao = package.dimension or {}
        medidas = {
            lado: max(round(self._dhl_length_cm(dimensao.get(chave)), 1), 1.0)
            for lado, chave in (("length", "length"), ("width", "width"),
                                ("height", "height"))
        }
        return {
            "weight": max(round(self._dhl_weight_kg(package.weight), 3), 0.001),
            "dimensions": medidas,
        }

    # ------------------------------------------------------------------ #
    # Endereço e partes                                                   #
    # ------------------------------------------------------------------ #

    def _dhl_address(self, partner, full=False):
        """Endereço da DHL. Cidade, país e CEP bastam para cotar; o endereço
        completo só é exigido na criação do envio."""
        _ = self.env._
        if not partner.country_id:
            raise DhlError(_(
                "Informe o país de %s: a DHL cota por país de destino.",
                partner.display_name,
            ))
        cidade = partner.city
        if "city_id" in partner._fields and partner.city_id:
            cidade = partner.city_id.name
        if not cidade:
            raise DhlError(_("Informe a cidade de %s.", partner.display_name))
        endereco = {
            "postalCode": tax_id(partner.zip)[:12],
            "cityName": cidade[:MAX_CITY],
            "countryCode": partner.country_id.code,
        }
        if partner.state_id.code and len(partner.state_id.code) >= 2:
            endereco["provinceCode"] = partner.state_id.code[:35]
        if full:
            bairro = partner.district if "district" in partner._fields else False
            linhas = [linha.strip()[:MAX_ADDRESS_LINE]
                      for linha in (partner.street, partner.street2, bairro) if linha]
            if not linhas:
                raise DhlError(_("Informe o endereço (rua) de %s.", partner.display_name))
            for numero, linha in enumerate(linhas[:3], start=1):
                endereco["addressLine%d" % numero] = linha
        return endereco

    def _dhl_registration_numbers(self, partner):
        """CNPJ/CPF de parte brasileira (código CNP da DHL); VAT de empresa
        estrangeira. A IE fica de fora: a especificação a chama de IE e o guia de
        referência da própria DHL, de STA."""
        comercial = partner.commercial_partner_id
        documento = tax_id(partner.vat) or tax_id(comercial.vat)
        pais = (partner.country_id or comercial.country_id).code
        registros = []
        if pais == "BR":
            if len(documento) in (11, 14):
                registros.append({"typeCode": "CNP", "number": documento,
                                  "issuerCountryCode": "BR"})
        elif documento and comercial.is_company:
            registros.append({"typeCode": "VAT", "number": documento[:35],
                              "issuerCountryCode": pais})
        return registros

    def _dhl_party(self, partner):
        _ = self.env._
        telefone = partner.phone or getattr(partner, "mobile", False) or (
            partner.commercial_partner_id.phone)
        if not telefone:
            raise DhlError(_(
                "Informe o telefone de %s: a DHL exige telefone do remetente e do "
                "destinatário.", partner.display_name,
            ))
        comercial = partner.commercial_partner_id
        empresa = partner.commercial_company_name or comercial.name or partner.name
        contato = {
            "fullName": (partner.name or comercial.name)[:255],
            "companyName": empresa[:100],
            "phone": telefone[:70],
        }
        email = partner.email or comercial.email
        if email:
            contato["email"] = email[:70]
        party = {
            "postalAddress": self._dhl_address(partner, full=True),
            "contactInformation": contato,
            "typeCode": "business" if comercial.is_company else "private",
        }
        registros = self._dhl_registration_numbers(partner)
        if registros:
            party["registrationNumbers"] = registros
        return party

    # ------------------------------------------------------------------ #
    # Cotação                                                             #
    # ------------------------------------------------------------------ #

    def _dhl_order_package_type(self, order):
        """A menor caixa que comporta o pedido; sem nenhuma, a embalagem padrão."""
        peso = order._get_estimated_weight()
        caixas = self.dhl_package_type_ids.filtered(
            lambda caixa: caixa.max_weight and caixa.max_weight >= peso + caixa.base_weight
        )
        if caixas:
            return min(caixas, key=lambda caixa: (
                caixa.packaging_length * caixa.width * caixa.height, caixa.max_weight,
            ))
        return self.dhl_default_package_type_id

    def _dhl_rate_payload(self, shipper, receiver, packages, declared_value, currency):
        internacional = shipper.country_id != receiver.country_id
        payload = {
            "customerDetails": {
                "shipperDetails": self._dhl_address(shipper),
                "receiverDetails": self._dhl_address(receiver),
            },
            "plannedShippingDateAndTime": self._dhl_planned_date(shipper),
            "unitOfMeasurement": "metric",
            "isCustomsDeclarable": internacional,
            "packages": packages,
        }
        if self.dhl_account_number:
            payload["accounts"] = [{"typeCode": "shipper", "number": self.dhl_account_number}]
        if declared_value:
            payload["monetaryAmount"] = [{
                "typeCode": "declaredValue",
                "value": round(declared_value, 2),
                "currency": (currency or "BRL").upper(),
            }]
        return payload

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
            _logger.info("DHL: produto %s indisponível; usando a opção mais barata.",
                         self.dhl_product_code)
        return min(produtos, key=lambda p: p["price"])

    def _dhl_price_in_company_currency(self, produto, company):
        """A DHL cota na moeda da conta (BILLC); o Odoo espera a da empresa."""
        moeda = self.env["res.currency"].with_context(active_test=False).search(
            [("name", "=", produto.get("currency") or "")], limit=1
        )
        if not moeda or moeda == company.currency_id:
            return produto["price"]
        return moeda._convert(produto["price"], company.currency_id, company,
                              fields.Date.context_today(self))

    def dhl_express_rate_shipment(self, order):
        self.ensure_one()
        _ = self.env._
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
        try:
            caixa = self._dhl_order_package_type(order)
            if not caixa:
                raise DhlError(_("Configure a embalagem padrão do método %s.", self.name))
            pacotes = self._get_packages_from_order(order, caixa)
            linhas = order.order_line.filtered(
                lambda line: not line.is_delivery and not line.display_type
            )
            # valor declarado: o que o cliente paga pelos produtos, não o custo
            valor = sum(line.price_reduce_taxinc * line.product_uom_qty for line in linhas)
            response = self._dhl_get_client().rates(self._dhl_rate_payload(
                remetente, destinatario,
                [self._dhl_package_payload(pacote) for pacote in pacotes],
                valor, order.currency_id.name,
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
        if remetente.country_id != destinatario.country_id and self.dhl_incoterm == INCOTERM_DAP:
            aviso = _(
                "Imposto de importação e desembaraço são pagos pelo destinatário "
                "na entrega, e não estão incluídos neste frete."
            )
        return {
            "success": True,
            "price": self._dhl_price_in_company_currency(produto, order.company_id),
            "error_message": False,
            "warning_message": aviso,
        }

    # ------------------------------------------------------------------ #
    # Envio                                                               #
    # ------------------------------------------------------------------ #

    def dhl_express_send_shipping(self, pickings):
        self.ensure_one()
        _ = self.env._
        if self.dhl_simulation:
            raise UserError(_(
                "O método DHL está em modo simulação: dá para cotar, mas não "
                "para despachar. Desligue a simulação e informe as credenciais."
            ))
        client = self._dhl_get_client()
        resultado = []
        for picking in pickings:
            pacotes = self._get_packages_from_picking(picking, self.dhl_default_package_type_id)
            produto = self._dhl_rate_picking(client, picking, pacotes)
            response = client.create_shipment(
                self._dhl_shipment_payload(picking, pacotes, produto)
            )
            tracking = response.get("shipmentTrackingNumber")
            if not tracking:
                raise DhlError(_(
                    "A DHL aceitou o envio mas não devolveu número de rastreio. "
                    "Confira no MyDHL antes de repetir, para não duplicar o pacote."
                ))
            picking.dhl_dispatch_confirmation = response.get("dispatchConfirmationNumber")
            self._dhl_attach_documents(picking, response, tracking)
            picking.dhl_tracking_status = False
            resultado.append({
                "exact_price": self._dhl_price_in_company_currency(produto, picking.company_id),
                "tracking_number": tracking,
            })
        return resultado

    def _dhl_rate_picking(self, client, picking, pacotes):
        """Cota de novo na hora do envio: dá o produto (obrigatório no envio) e
        o custo real destes volumes."""
        remetente = self._dhl_shipper_partner(picking=picking)
        response = client.rates(self._dhl_rate_payload(
            remetente, picking.partner_id,
            [self._dhl_package_payload(pacote) for pacote in pacotes],
            self._dhl_commodities_value(pacotes),
            picking.company_id.currency_id.name,
        ))
        produto = self._dhl_pick_product(extract_products(response))
        if not produto:
            raise DhlError(self.env._(
                "A DHL não atende o endereço de %s.", picking.partner_id.display_name
            ))
        return produto

    @staticmethod
    def _dhl_commodities(pacotes):
        return [commodity for pacote in pacotes for commodity in pacote.commodities]

    def _dhl_commodities_value(self, pacotes):
        return sum(c.monetary_value * c.qty for c in self._dhl_commodities(pacotes))

    def _dhl_pickup(self):
        if not self.dhl_request_pickup:
            return {"isRequested": False}
        pickup = {"isRequested": True}
        if self.dhl_pickup_close_time:
            pickup["closeTime"] = self.dhl_pickup_close_time.strip()[:5]
        if self.dhl_pickup_location:
            pickup["location"] = self.dhl_pickup_location[:80]
        return pickup

    def _dhl_shipment_payload(self, picking, pacotes, produto):
        _ = self.env._
        remetente = self._dhl_shipper_partner(picking=picking)
        destinatario = picking.partner_id
        internacional = remetente.country_id != destinatario.country_id
        moeda = picking.company_id.currency_id.name or "BRL"
        referencia = (picking.sale_id.name or picking.name)[:MAX_REFERENCE]

        shipper = self._dhl_party(remetente)
        if remetente.country_id.code == "BR" and not shipper.get("registrationNumbers"):
            # a empresa costuma estar no parceiro da empresa, não no do depósito
            shipper["registrationNumbers"] = self._dhl_registration_numbers(
                picking.company_id.partner_id)
        if remetente.country_id.code == "BR" and not shipper.get("registrationNumbers"):
            raise DhlError(_("Preencha o CNPJ da empresa: a DHL exige o do exportador."))

        volumes = []
        for pacote in pacotes:
            volume = self._dhl_package_payload(pacote)
            volume["customerReferences"] = [{"value": referencia, "typeCode": "CU"}]
            volumes.append(volume)

        # Mercadoria de quem tem IE só circula com NF-e, dentro ou fora do país:
        # no envio nacional a DHL emite o CT-e a partir da nota.
        nfe = self._dhl_nfe(picking)
        if not nfe and self._dhl_requires_nfe(remetente, picking.company_id):
            if internacional:
                raise DhlError(_(
                    "Emita a NF-e de exportação da venda %s antes de despachar: a DHL Brasil "
                    "exige a nota de quem tem Inscrição Estadual, e a Receita cruza a nota com "
                    "a fatura comercial.", picking.sale_id.name or picking.name,
                ))
            raise DhlError(_(
                "Emita a NF-e da venda %s antes de despachar: quem tem Inscrição Estadual "
                "só envia mercadoria com nota, e a DHL emite o CT-e a partir dela.",
                picking.sale_id.name or picking.name,
            ))

        etiqueta = {"typeCode": "label",
                    "templateName": self.dhl_label_template or "ECOM26_84_001"}
        if nfe:
            # impressa na etiqueta: quem confere o pacote acha a nota sem abrir nada
            etiqueta["labelCustomerDataText"] = "NF-e %s" % nfe["key"]
        imagens = [etiqueta]
        if internacional and self.dhl_commercial_invoice:
            imagens.append({"typeCode": "invoice", "isRequested": True,
                            "invoiceType": "commercial"})

        content = {
            "packages": volumes,
            "isCustomsDeclarable": internacional,
            "description": self._dhl_content_description(pacotes),
            "incoterm": self.dhl_incoterm or INCOTERM_DAP,
            "unitOfMeasurement": "metric",
        }
        if internacional:
            content["declaredValue"] = round(self._dhl_commodities_value(pacotes), 2)
            content["declaredValueCurrency"] = moeda
            content["exportDeclaration"] = self._dhl_export_declaration(
                picking, pacotes, destinatario, referencia, nfe)

        payload = {
            "plannedShippingDateAndTime": self._dhl_planned_date(remetente),
            "pickup": self._dhl_pickup(),
            "productCode": produto["code"],
            "accounts": [{"typeCode": "shipper", "number": self.dhl_account_number}],
            "customerDetails": {
                "shipperDetails": shipper,
                "receiverDetails": self._dhl_party(destinatario),
            },
            "content": content,
            "outputImageProperties": {"encodingFormat": "pdf", "imageOptions": imagens},
            "customerReferences": [{"value": referencia, "typeCode": "CU"}],
        }
        if produto.get("local_code"):
            payload["localProductCode"] = produto["local_code"]
        return payload

    def _dhl_content_description(self, pacotes):
        nomes = []
        for commodity in self._dhl_commodities(pacotes):
            nome = commodity.product_id.name
            if nome and nome not in nomes:
                nomes.append(nome)
        descricao = ", ".join(nomes).strip() or self.env._("Mercadorias")
        return descricao[:MAX_CONTENT_DESCRIPTION]

    def _dhl_export_declaration(self, picking, pacotes, destinatario, referencia, nfe=False):
        """Declaração aduaneira. Cada item precisa de código HS, valor unitário e
        o peso total da linha (a DHL não multiplica pela quantidade)."""
        _ = self.env._
        pais_empresa = picking.company_id.country_id.code or "BR"
        linhas = []
        for numero, commodity in enumerate(self._dhl_commodities(pacotes), start=1):
            produto = commodity.product_id
            codigo_hs = tax_id(produto.hs_code or self.dhl_default_hs_code)
            if not codigo_hs:
                raise DhlError(_(
                    "Informe o código HS de %s (ou o código padrão no método de "
                    "entrega): a declaração aduaneira exige.", produto.display_name,
                ))
            quantidade = int(commodity.qty) or 1
            peso = round(self._dhl_weight_kg(produto.weight) * quantidade, 3)
            descricao = produto.display_name or ""
            if len(descricao) < 3:
                # a DHL recusa descrição com menos de 3 caracteres
                descricao = "%s %s" % (_("Item"), descricao)
            linhas.append({
                "number": numero,
                "description": descricao[:512],
                "price": round(commodity.monetary_value, 2),
                "quantity": {"value": quantidade, "unitOfMeasurement": "PCS"},
                "commodityCodes": [{"typeCode": "outbound", "value": codigo_hs[:18]}],
                "exportReasonType": "permanent",
                "manufacturerCountry": produto.country_of_origin.code or pais_empresa,
                "weight": {"netValue": peso, "grossValue": peso},
            })
        if not linhas:
            raise DhlError(_("A entrega %s não tem produto para declarar.", picking.name))
        declaracao = {
            "lineItems": linhas,
            "invoice": {
                "number": referencia,
                "date": fields.Date.context_today(self).isoformat(),
            },
            "exportReason": "sale",
            "exportReasonType": "permanent",
            "shipmentType": "commercial",
        }
        if nfe:
            # A fatura comercial tem de corresponder à NF-e de exportação: mesmo
            # número e data. A API não tem campo para a chave; ela vai nas
            # observações da declaração e impressa na etiqueta.
            declaracao["invoice"]["number"] = nfe["number"]
            declaracao["invoice"]["date"] = nfe["date"]
            declaracao["remarks"] = [{"value": "NF-e %s" % nfe["key"]}]
        # DAP/DDP: o lugar do incoterm é o destino
        if destinatario.city:
            declaracao["placeOfIncoterm"] = destinatario.city[:256]
        return declaracao

    def _dhl_requires_nfe(self, remetente, company):
        """Remetente brasileiro com IE precisa de NF-e, no Brasil ou para fora. Sem a localização fiscal
        (OCA) não há como emitir nem checar a nota pelo Odoo."""
        if not self.dhl_require_nfe or remetente.country_id.code != "BR":
            return False
        if "document_key" not in self.env["account.move"]._fields:
            return False
        parceiro = company.partner_id
        ie = tax_id(getattr(parceiro, "l10n_br_ie_code", False))
        return bool(ie) and ie != "ISENTO"

    def _dhl_nfe(self, picking):
        """A NF-e autorizada da venda, se a localização fiscal (OCA) estiver
        instalada. Sem ela, os campos não existem."""
        faturas = picking.sale_id.invoice_ids.filtered(lambda move: move.state == "posted")
        if not faturas or "document_key" not in faturas._fields:
            return False
        for fatura in faturas.sorted("id", reverse=True):
            chave = "".join(c for c in (fatura.document_key or "") if c.isdigit())
            if len(chave) != 44:
                continue
            if "state_edoc" in fatura._fields and fatura.state_edoc != "autorizada":
                continue
            numero = "".join(c for c in (fatura.document_number or "") if c.isdigit())
            emissao = getattr(fatura, "document_date", False) or fatura.invoice_date
            return {
                "key": chave,
                "number": (numero or chave[25:34])[:MAX_REFERENCE],
                "date": fields.Date.to_date(emissao).isoformat() if emissao
                else fields.Date.context_today(self).isoformat(),
                "move": fatura,
            }
        return False

    def _dhl_attach_nfe(self, picking, nfe):
        """DANFE e XML autorizados vão junto da etiqueta: o DANFE acompanha o
        pacote, e o XML é o que a DHL Brasil importa quando pede a nota."""
        fatura = nfe["move"]
        anexos = self.env["ir.attachment"]
        for campo in ("file_report_id", "authorization_file_id"):
            arquivo = getattr(fatura, campo, False) if campo in fatura._fields else False
            if arquivo:
                anexos |= arquivo.sudo().copy({
                    "res_model": "stock.picking", "res_id": picking.id,
                })
        return anexos

    def _dhl_attach_documents(self, picking, response, tracking):
        """Guarda etiqueta e fatura comercial, que vêm em base64, junto à entrega,
        com o DANFE e o XML da NF-e quando houver."""
        anexos = self.env["ir.attachment"]
        for documento in response.get("documents") or []:
            conteudo = documento.get("content")
            if not conteudo:
                continue
            tipo = documento.get("typeCode") or "label"
            prefixo = (self._get_delivery_label_prefix() if tipo == "label"
                       else self._get_delivery_doc_prefix())
            formato = (documento.get("imageFormat") or "PDF").lower()
            anexos |= anexos.create({
                "name": "%s-%s-%s.%s" % (prefixo, tracking, tipo, formato),
                "type": "binary",
                "datas": conteudo,
                "mimetype": "application/pdf" if formato == "pdf" else False,
                "res_model": "stock.picking",
                "res_id": picking.id,
            })
        nfe = self._dhl_nfe(picking)
        if nfe:
            anexos |= self._dhl_attach_nfe(picking, nfe)
        if anexos:
            picking.message_post(
                body=self.env._(
                    "Documentos do envio DHL %s: imprima a etiqueta e a fatura comercial "
                    "e mande o DANFE junto do pacote.", tracking,
                ) if nfe else self.env._("Documentos do envio DHL %s.", tracking),
                attachment_ids=anexos.ids,
            )

    # ------------------------------------------------------------------ #
    # Rastreio e cancelamento                                             #
    # ------------------------------------------------------------------ #

    def dhl_express_get_tracking_link(self, picking):
        if not picking.carrier_tracking_ref:
            return False
        return (
            "https://www.dhl.com/br-pt/home/rastreamento.html"
            "?tracking-id=%s&submit=1" % picking.carrier_tracking_ref
        )

    def dhl_express_cancel_shipment(self, pickings):
        """Cancela o envio pelo que a DHL permite.

        A MyDHL API não anula conhecimento, e nem precisa: pelos termos da API,
        emitir o conhecimento não é contrato de transporte, que só nasce quando o
        pacote é entregue ou coletado. O que os termos permitem cobrar é a coleta
        agendada sem pacote para entregar, e essa a API cancela. Depois disso, as etiquetas são marcadas como canceladas para
        ninguém imprimir, e a entrega pode ser despachada de novo.
        """
        self.ensure_one()
        for picking in pickings:
            numero = picking.carrier_tracking_ref
            coleta = picking.dhl_dispatch_confirmation
            if coleta:
                self._dhl_get_client().cancel_pickup(
                    coleta, requestor=self.env.user.name or "Odoo", reason="Envio cancelado",
                )
                picking.dhl_dispatch_confirmation = False
            etiquetas = self.env["ir.attachment"].search([
                ("res_model", "=", "stock.picking"), ("res_id", "=", picking.id),
                ("name", "like", "%s-%s-" % (self._get_delivery_label_prefix(), numero)),
            ]) if numero else self.env["ir.attachment"]
            for etiqueta in etiquetas:
                etiqueta.name = "CANCELADA-%s" % etiqueta.name
            picking.dhl_tracking_status = self.env._("Cancelado")
            partes = [self.env._("Envio DHL %s cancelado.", numero)]
            if coleta:
                partes.append(self.env._("Coleta %s cancelada na DHL.", coleta))
            if etiquetas:
                partes.append(self.env._("Etiqueta marcada como CANCELADA: não imprima."))
            partes.append(self.env._(
                "Sem o pacote entregue à DHL o conhecimento não vira contrato de "
                "transporte. Para despachar de novo, use Enviar para a transportadora."
            ))
            picking.message_post(body=" ".join(partes))
        return True

    def _dhl_express_get_default_custom_package_code(self):
        return False
