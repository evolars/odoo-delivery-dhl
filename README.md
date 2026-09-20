# DHL Express para Odoo 17

Conector da [DHL Express](https://www.dhl.com) sobre a MyDHL API: cotação no carrinho, criação
do envio com etiqueta e declaração aduaneira, e rastreamento pelo portal do cliente.

## Por que não se chama `delivery_dhl`

A Odoo mantém um conector oficial de DHL, mas ele vive no **Enterprise**. No repositório
`odoo/odoo`, branch `17.0`, o único conector de transportadora que existe em Community é o
`delivery_mondialrelay`. Numa base com Enterprise instalado, um módulo chamado `delivery_dhl`
colidiria com o oficial — daí o nome `delivery_dhl_express`.

---

## Instalação

Depende de `stock_delivery` e da biblioteca `requests`.

```yaml
# custom/src/repos.yaml
./odoo-delivery-dhl:
  defaults:
    depth: $DEPTH_DEFAULT
  remotes:
    evolars: https://github.com/evolars/odoo-delivery-dhl.git
  target: evolars $ODOO_VERSION
  merges:
    - evolars $ODOO_VERSION
```

## Credenciais

Exige **conta ativa na DHL Express**. O consultor fornece o acesso à MyDHL API e o cadastro no
portal do desenvolvedor devolve dois e-mails, normalmente no dia útil seguinte: um liberando o
**teste** e outro a **produção**. O ambiente de teste tem limite de **500 chamadas por dia**.

O ambiente segue o campo *Ambiente* do método de entrega — fora de produção, as chamadas vão
para `/mydhlapi/test`.

### Antes de a conta existir

Ligue o **modo simulação**: o método cota por um valor fixo local, sem chamar a DHL. Despachar
fica bloqueado.

---

## Incoterm: leia isto

O padrão é **DAP** — o destinatário paga imposto de importação e desembaraço na entrega.

Quando a cotação é internacional e o incoterm é DAP, o módulo devolve um `warning_message`
dizendo isso. **Mostre esse aviso no checkout.** Sem ele o comprador descobre a cobrança só na
porta de casa, recusa o pacote, e a devolução sai por conta do remetente — frete pago duas
vezes e venda perdida.

Para a União Europeia isso pesa mais desde 1º de julho de 2026, quando acabou a isenção de
direitos aduaneiros para remessas de até €150, substituída por uma taxa fixa de €3 por remessa,
somada ao IVA do país de destino.

Com **DDP** o remetente assume os impostos e o aviso não aparece.

---

## Como funciona

**Cotação** monta `customerDetails`, `packages` e `monetaryAmount` e chama `POST /rates`. Os
volumes vêm do empacotamento do próprio Odoo (`_get_packages_from_order`).

`isCustomsDeclarable` acompanha o destino: verdadeiro só quando o país do remetente difere do
país de destino.

A DHL devolve o preço em mais de uma moeda; o módulo usa a **faturada** (`currencyType: BILLC`),
que é a que aparece na sua conta.

Sem código de produto configurado, cota a opção mais barata. Com um código fixado e ele
indisponível para o destino, cota a que houver em vez de falhar.

**Falha na cotação não estoura.** Devolve `success: False` com a mensagem, e o checkout segue
com os outros métodos.

**Envio** usa `POST /shipments`, grava o número de rastreio no picking e anexa a etiqueta em PDF
que a DHL devolve em base64. Em envio internacional monta a declaração aduaneira a partir das
linhas do picking, usando o `hs_code` do produto quando existe — livro impresso é NCM/HS 4901,
que é o padrão quando o produto não tem código próprio.

**Cancelamento não existe na API.** A MyDHL API não cancela envio já criado; o módulo diz isso
explicitamente em vez de falhar em silêncio. O cancelamento é feito no painel do MyDHL.

### Detalhes que a documentação não enfatiza

`plannedShippingDateAndTime` é recusado sem fuso horário explícito. O módulo sempre envia o
offset.

Peso ou dimensão zerada é recusada. O módulo garante um mínimo, para não gastar a chamada.

---

## Testes

```bash
docker run --rm --network <rede> -v "$PWD":/mnt/extra-addons:ro \
  -e HOST=<db> -e USER=odoo -e PASSWORD=odoo odoo:17.0 \
  odoo -d dhl_test --addons-path=/mnt/extra-addons,/usr/lib/python3/dist-packages/odoo/addons \
  -i delivery_dhl_express --test-enable --test-tags /delivery_dhl_express \
  --stop-after-init --without-demo=all
```

25 testes, nenhum tocando a rede.

---

## Estado

Escrito contra a [documentação oficial](https://developer.dhl.com/api-reference/dhl-express-mydhl-api)
e testado com as chamadas interceptadas. **Ainda não validado contra a API real** — falta a
conta. Conte com um ajuste fino quando as credenciais chegarem: códigos de produto variam por
conta e por rota, e a validação de endereço da DHL é mais rígida do que a documentação sugere.
