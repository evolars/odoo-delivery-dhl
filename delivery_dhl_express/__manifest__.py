{
    "name": "DHL Express - Frete Internacional",
    "version": "18.0.1.2.0",
    "category": "Inventory/Delivery",
    "summary": "Cotação, envio, etiqueta e rastreamento pela MyDHL API",
    "description": """
Conector da DHL Express para o Odoo 18, sobre a MyDHL API (3.3.2).

A Odoo mantém um conector oficial de DHL, mas ele vive no Enterprise: no
repositório `odoo/odoo` o único conector de transportadora que existe em
Community é o `delivery_mondialrelay`. Por isso este módulo **não se chama
`delivery_dhl`**: numa base com Enterprise o nome colidiria.

Cobre cotação, criação de envio com etiqueta, fatura comercial e declaração
aduaneira, coleta opcional e rastreamento. Os eventos do rastreio ficam na entrega
(`dhl_tracking_events`) e uma tarefa agendada os atualiza a cada 3 horas até
a entrega, para o portal do cliente mostrar o caminho do pacote.

**Incoterm.** O padrão é `DAP`: o destinatário paga imposto de importação e
desembaraço na entrega. O checkout do Odoo 18 não mostra o aviso da cotação:
ponha o aviso na descrição do método no site, ou o comprador é surpreendido
na entrega e recusa o pacote, que volta por conta do remetente.

Traz um **modo simulação** que cota por um valor fixo local, para o checkout
poder ser exercitado antes de a conta DHL existir. Despachar fica bloqueado.
    """,
    "author": "Evolars LTDA",
    "website": "https://github.com/evolars/odoo-delivery-dhl",
    "license": "AGPL-3",
    "depends": ["stock_delivery"],
    "external_dependencies": {"python": ["requests"]},
    "data": [
        "data/ir_cron.xml",
        "views/delivery_dhl_views.xml",
    ],
    "installable": True,
    "application": False,
}
