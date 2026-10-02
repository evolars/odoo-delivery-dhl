from odoo import fields, models
from odoo.exceptions import UserError


class StockPicking(models.Model):
    _inherit = "stock.picking"

    dhl_dispatch_confirmation = fields.Char(
        string="Coleta DHL", copy=False,
        help="Número de confirmação da coleta agendada junto com o envio.",
    )
    dhl_tracking_status = fields.Char(string="Status DHL", copy=False)

    def action_dhl_refresh_tracking(self):
        """Consulta o último evento do rastreio na DHL e registra na entrega."""
        for picking in self:
            if picking.delivery_type != "dhl_express" or not picking.carrier_tracking_ref:
                raise UserError(self.env._("Esta entrega não tem envio DHL."))
            resposta = picking.carrier_id._dhl_get_client().track(picking.carrier_tracking_ref)
            envio = (resposta.get("shipments") or [{}])[0]
            eventos = envio.get("events") or []
            # a ordem dos eventos não é garantida: o mais recente pela data e hora
            ultimo = max(eventos, key=lambda e: (e.get("date") or "", e.get("time") or ""),
                         default={})
            status = ultimo.get("description") or envio.get("status") or ""
            if not status or status == picking.dhl_tracking_status:
                continue
            picking.dhl_tracking_status = status
            quando = " ".join(parte for parte in (ultimo.get("date"), ultimo.get("time")) if parte)
            picking.message_post(body=self.env._(
                "DHL: %(status)s%(quando)s", status=status,
                quando=(" (%s)" % quando) if quando else "",
            ))
        return True
