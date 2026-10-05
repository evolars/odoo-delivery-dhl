import logging
from datetime import timedelta

from odoo import fields, models
from odoo.exceptions import UserError

from .dhl_client import DhlError

_logger = logging.getLogger(__name__)

# Código de evento da DHL (`typeCode`) que encerra o rastreio.
DELIVERED = "OK"

# A DHL descreve os eventos em inglês. Os mais comuns ganham texto em português;
# o resto aparece como a DHL escreveu.
EVENT_LABELS = {
    "PU": "Coletado pela DHL",
    "PL": "Processado na unidade",
    "AF": "Chegou à unidade da DHL",
    "AR": "Chegou à unidade de triagem",
    "DF": "Saiu da unidade da DHL",
    "RR": "Atualização da alfândega",
    "CR": "Liberado pela alfândega",
    "HP": "Aguardando o pagamento dos impostos",
    "OH": "Retido",
    "WC": "Saiu para entrega",
    "NH": "Destinatário ausente",
    "BA": "Endereço incorreto",
    "RT": "Devolvido ao remetente",
    DELIVERED: "Entregue",
}

# Envios mais antigos que isto deixam de ser consultados pela tarefa agendada.
TRACKING_WINDOW_DAYS = 60


class StockPicking(models.Model):
    _inherit = "stock.picking"

    dhl_dispatch_confirmation = fields.Char(
        string="Coleta DHL", copy=False,
        help="Número de confirmação da coleta agendada junto com o envio.",
    )
    dhl_tracking_status = fields.Char(string="Status DHL", copy=False)
    dhl_tracking_events = fields.Json(
        string="Eventos do rastreio DHL", copy=False,
        help="Do mais recente para o mais antigo: date, time, code, description, location.",
    )
    dhl_delivered = fields.Boolean(string="Entregue pela DHL", copy=False)
    dhl_tracking_checked_at = fields.Datetime(string="Rastreio consultado em", copy=False)

    def action_dhl_refresh_tracking(self):
        """Consulta o rastreio na DHL e registra na entrega."""
        for picking in self:
            if picking.delivery_type != "dhl_express" or not picking.carrier_tracking_ref:
                raise UserError(self.env._("Esta entrega não tem envio DHL."))
            picking._dhl_refresh_tracking()
        return True

    def _dhl_refresh_tracking(self):
        self.ensure_one()
        resposta = self.carrier_id._dhl_get_client().track(self.carrier_tracking_ref)
        envio = (resposta.get("shipments") or [{}])[0]
        eventos = self._dhl_normalize_events(envio.get("events") or [])
        valores = {
            "dhl_tracking_events": eventos,
            "dhl_tracking_checked_at": fields.Datetime.now(),
        }
        if any(evento["code"] == DELIVERED for evento in eventos):
            valores["dhl_delivered"] = True
        ultimo = eventos[0] if eventos else {}
        status = ultimo.get("description") or envio.get("status") or ""
        novo_status = status and status != self.dhl_tracking_status
        if novo_status:
            valores["dhl_tracking_status"] = status
        self.write(valores)
        if novo_status:
            quando = " ".join(parte for parte in (ultimo.get("date"), ultimo.get("time")) if parte)
            self.message_post(body=self.env._(
                "DHL: %(status)s%(quando)s", status=status,
                quando=(" (%s)" % quando) if quando else "",
            ))

    @staticmethod
    def _dhl_normalize_events(eventos):
        """Eventos da DHL no formato que a tela e o portal leem, do mais recente ao
        mais antigo (a ordem da resposta não é garantida)."""
        normalizados = []
        for evento in eventos:
            codigo = evento.get("typeCode") or ""
            local = ", ".join(
                area.get("description") for area in evento.get("serviceArea") or []
                if area.get("description")
            )
            normalizados.append({
                "date": evento.get("date") or "",
                "time": (evento.get("time") or "")[:5],
                "code": codigo,
                "description": EVENT_LABELS.get(codigo) or evento.get("description") or codigo,
                "location": local,
            })
        normalizados.sort(key=lambda e: (e["date"], e["time"]), reverse=True)
        return normalizados

    def _cron_dhl_refresh_tracking(self):
        """Atualiza o rastreio dos envios DHL a caminho. Erro num envio não para os outros."""
        desde = fields.Datetime.now() - timedelta(days=TRACKING_WINDOW_DAYS)
        pickings = self.search([
            ("delivery_type", "=", "dhl_express"),
            ("carrier_tracking_ref", "!=", False),
            ("state", "=", "done"),
            ("dhl_delivered", "=", False),
            ("date_done", ">=", desde),
        ])
        for picking in pickings:
            try:
                with self.env.cr.savepoint():
                    picking._dhl_refresh_tracking()
            except DhlError as error:
                _logger.warning("DHL: rastreio de %s não atualizado: %s", picking.name, error)
