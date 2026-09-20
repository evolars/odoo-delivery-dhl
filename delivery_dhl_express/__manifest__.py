{
    "name": "DHL Express - Frete Internacional",
    "version": "17.0.1.0.0",
    "category": "Inventory/Delivery",
    "summary": "Cotação, envio e rastreamento pela MyDHL API",
    "description": """
Conector da DHL Express para o Odoo 17, sobre a MyDHL API.

A Odoo mantém um conector oficial de DHL, mas ele vive no Enterprise: no
repositório `odoo/odoo`, branch 17.0, o único conector de transportadora que
existe em Community é o `delivery_mondialrelay`. Por isso este módulo **não se
chama `delivery_dhl`** — numa base com Enterprise o nome colidiria.

Cobre cotação, criação de envio com etiqueta e declaração aduaneira, e
rastreamento pelo portal do cliente.

**Incoterm.** O padrão é `DAP`: o destinatário paga imposto de importação e
desembaraço na entrega. Quando é esse o caso, a cotação devolve um aviso para
o checkout mostrar — sem isso o comprador é surpreendido na entrega e recusa o
pacote, que volta por conta do remetente.

Traz um **modo simulação** que cota por um valor fixo local, para o checkout
poder ser exercitado antes de a conta DHL existir. Despachar fica bloqueado.
    """,
    "author": "Evolars LTDA",
    "website": "https://github.com/evolars/odoo-delivery-dhl",
    "license": "AGPL-3",
    "depends": ["stock_delivery"],
    "external_dependencies": {"python": ["requests"]},
    "data": ["views/delivery_dhl_views.xml"],
    "installable": True,
    "application": False,
}
